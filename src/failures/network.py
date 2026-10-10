import subprocess
import threading
import time
import uuid
from typing import Dict, Iterable, List, Set, Tuple, Optional
import re
from ..lifecycle import lifecycle
from ..limits import MAX_DURATION_SECONDS, within_upper_bound
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE
from ..logging_config import get_logger

logger = get_logger(__name__)

# Every tc command and every change to the ownership table below happens under
# this lock, so concurrent callers (the loop, a manual API call, shutdown, and
# startup repair) can never interleave. It is re-entrant because shutdown runs
# from signal handlers on the main thread, which may already hold it. It is
# NOT held while an injection waits out its duration.
_tc_lock = threading.RLock()

# interface -> id of the injection that applied the rule. A rule is removed only
# by its owner (or by shutdown), so one injection can never delete a rule that
# another one applied.
_applied: Dict[str, str] = {}


def validate_interface_name(interface: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that interface name is safe and follows Linux naming conventions.

    Linux interface names must:
    - Be 1-15 characters long
    - Contain only alphanumeric, dash, underscore, dot, colon
    - Not contain shell metacharacters

    Returns:
        tuple: (is_valid: bool, error_message: str or None)
    """
    if not interface:
        return False, "Interface name cannot be empty"

    if len(interface) > 15:
        logger.warning(
            "Interface name validation failed - too long",
            extra={"interface": interface, "length": len(interface)},
        )
        return False, f"Interface name too long (max 15 chars): {interface}"

    # Linux interface naming pattern
    pattern = r"^[a-zA-Z0-9._:-]+$"

    if not re.match(pattern, interface):
        logger.warning(
            "Interface name validation failed - invalid pattern",
            extra={"interface": interface},
        )
        return False, f"Invalid interface name: {interface}"

    # Explicitly block shell metacharacters
    dangerous_chars = [
        ";",
        "&",
        "|",
        "$",
        "`",
        "(",
        ")",
        "<",
        ">",
        "\n",
        "\r",
        "\\",
        '"',
        "'",
        " ",
    ]
    for char in dangerous_chars:
        if char in interface:
            logger.error(
                "Interface name contains forbidden character - possible injection attempt",
                extra={
                    "interface": interface,
                    "forbidden_char": char,
                    "security_event": True,
                },
            )
            return False, f"Interface name contains forbidden character: '{char}'"

    logger.debug("Interface name validation passed", extra={"interface": interface})
    return True, None


def validate_delay_ms(delay_ms: int) -> Tuple[bool, Optional[str]]:
    """
    Validate delay value is within reasonable bounds.

    Returns:
        tuple: (is_valid: bool, error_message: str or None)
    """
    if not isinstance(delay_ms, (int, float)):
        logger.warning(
            "Delay validation failed - invalid type",
            extra={"delay_ms": delay_ms, "type": type(delay_ms).__name__},
        )
        return False, f"Delay must be a number, got {type(delay_ms)}"

    if delay_ms < 0:
        logger.warning(
            "Delay validation failed - negative value", extra={"delay_ms": delay_ms}
        )
        return False, f"Delay cannot be negative: {delay_ms}"

    if delay_ms > 10000:  # 10 seconds max
        logger.warning(
            "Delay validation failed - too high", extra={"delay_ms": delay_ms}
        )
        return False, f"Delay too high (max 10000ms): {delay_ms}"

    logger.debug("Delay validation passed", extra={"delay_ms": delay_ms})
    return True, None


def verify_interface_exists(interface: str) -> Tuple[bool, Optional[str]]:
    """
    Verify that the network interface actually exists on the system.

    Returns:
        tuple: (exists: bool, error_message: str or None)
    """
    import sys

    if sys.platform == "win32":
        logger.debug("Skipping interface verification on Windows")
        return True, None

    try:
        logger.debug(f"Verifying interface exists: {interface}")

        # Use ip link show with exact interface name (no shell injection possible)
        result = subprocess.run(
            ["ip", "link", "show", interface], capture_output=True, text=True, timeout=5
        )

        if result.returncode == 0:
            logger.debug(
                "Interface verification successful", extra={"interface": interface}
            )
            return True, None
        else:
            logger.warning(
                "Interface does not exist",
                extra={"interface": interface, "returncode": result.returncode},
            )
            return False, f"Interface '{interface}' does not exist"

    except FileNotFoundError:
        logger.debug("ip command not found - skipping interface verification")
        return True, None  # ip command not found - probably not on linux

    except subprocess.TimeoutExpired:
        logger.error("Interface verification timed out", extra={"interface": interface})
        return False, f"Timeout checking interface '{interface}'"

    except Exception as e:
        logger.error(
            "Interface verification failed with unexpected error",
            exc_info=True,
            extra={"interface": interface, "error": str(e)},
        )
        return False, f"Error checking interface: {e}"


def _run_cmd(args: list) -> subprocess.CompletedProcess:
    """
    Execute command safely without shell interpretation.

    Args:
        args: Command and arguments as a list (NOT a string)

    Returns:
        CompletedProcess object with returncode, stdout, stderr
    """
    logger.debug("Executing command", extra={"command": " ".join(args)})

    try:
        result = subprocess.run(
            args,
            shell=False,  # No shell interpretation
            capture_output=True,
            text=True,
            timeout=30,  # Prevent hanging
        )

        logger.debug(
            "Command completed",
            extra={
                "command": " ".join(args),
                "returncode": result.returncode,
                "stdout_length": len(result.stdout),
                "stderr_length": len(result.stderr),
            },
        )

        return result

    except subprocess.TimeoutExpired:
        logger.error(
            "Command execution timed out",
            extra={"command": " ".join(args), "timeout_seconds": 30},
        )
        raise Exception(f"Command timed out: {' '.join(args)}")

    except Exception as e:
        logger.error(
            "Command execution failed",
            exc_info=True,
            extra={"command": " ".join(args), "error": str(e)},
        )
        raise Exception(f"Command execution failed: {e}")


def cleanup_network_rules(interface="eth0"):
    """
    Remove any existing tc qdisc rules on the interface.

    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    logger.debug("Attempting network rules cleanup", extra={"interface": interface})

    is_valid, error = validate_interface_name(interface)
    if not is_valid:
        logger.error(
            "Network cleanup failed - invalid interface",
            extra={"interface": interface, "error": error},
        )
        return False, f"Invalid interface: {error}"

    exists, error = verify_interface_exists(interface)
    if not exists:
        logger.warning(
            "Network cleanup skipped - interface does not exist",
            extra={"interface": interface, "error": error},
        )
        return False, error

    # use list of args instead of shell string
    with _tc_lock:
        result = _run_cmd(["tc", "qdisc", "del", "dev", interface, "root"])

    if result.returncode == 0:
        logger.info(
            "Network rules cleaned up successfully", extra={"interface": interface}
        )
        return True, None

    # Check for benign errors
    stderr_lower = result.stderr.lower()
    benign_errors = [
        "no such file or directory",
        "cannot delete qdisc with handle of zero",
    ]

    if any(err in stderr_lower for err in benign_errors):
        logger.debug(
            "Network cleanup - no rules to remove",
            extra={"interface": interface, "stderr": result.stderr},
        )
        return True, None

    logger.warning(
        "Network cleanup failed",
        extra={
            "interface": interface,
            "returncode": result.returncode,
            "stderr": result.stderr,
        },
    )
    return False, result.stderr.strip()


def _release_rule(interface: str, owner: str) -> Tuple[bool, Optional[str]]:
    """
    Remove the rule on ``interface`` if ``owner`` applied it. If the rule was
    already removed (by shutdown) or belongs to someone else, do nothing.
    """
    with _tc_lock:
        if _applied.get(interface) != owner:
            logger.debug(
                "Network rule not owned by this injection, leaving it",
                extra={"interface": interface},
            )
            return True, None
        success, error = cleanup_network_rules(interface)
        # Forget ownership even if deletion failed: the failure is logged, and
        # startup repair will retry. Keeping a stale claim would block nothing
        # useful and could make a later cleanup delete a rule we did not apply.
        _applied.pop(interface, None)
        return success, error


def cleanup_all_network_rules() -> None:
    """
    Remove every rule this process applied and still owns. Registered with the
    lifecycle so it runs on shutdown, from signal handlers, and at exit.
    Idempotent: a second call finds nothing to do.
    """
    with _tc_lock:
        interfaces = list(_applied)
        for interface in interfaces:
            success, error = cleanup_network_rules(interface)
            _applied.pop(interface, None)
            if success:
                logger.info(
                    "Network rule removed during shutdown",
                    extra={"interface": interface},
                )
            else:
                logger.error(
                    "Could not remove network rule during shutdown",
                    extra={"interface": interface, "error": error},
                )


def _root_qdisc_is_netem(interface: str) -> bool:
    result = _run_cmd(["tc", "qdisc", "show", "dev", interface, "root"])
    return result.returncode == 0 and "netem" in result.stdout.lower()


def reconcile_stale_rules(interfaces: Iterable[str]) -> List[str]:
    """
    Remove netem rules left behind by a previous run that could not clean up
    (SIGKILL, an OOM kill, a crash). Call at startup.

    The agent treats a netem root qdisc on an interface it is configured to
    manage as its own. Other kinds of root qdisc are never touched. Interfaces
    this process is currently injecting on are skipped.

    Returns the interfaces that were repaired.
    """
    repaired: List[str] = []
    seen: Set[str] = set()
    for interface in interfaces:
        if interface in seen:
            continue
        seen.add(interface)

        if not validate_interface_name(interface)[0]:
            continue
        try:
            with _tc_lock:
                if interface in _applied:
                    continue
                if not _root_qdisc_is_netem(interface):
                    continue
                success, error = cleanup_network_rules(interface)
        except Exception as e:  # tc missing, no permission, timeout
            logger.debug(
                "Could not check for stale network rules",
                extra={"interface": interface, "error": str(e)},
            )
            continue

        if success:
            repaired.append(interface)
            logger.warning(
                "Removed stale network rule left by a previous run",
                extra={"interface": interface},
            )
        else:
            logger.error(
                "Found a stale network rule but could not remove it",
                extra={"interface": interface, "error": error},
            )
    return repaired


# Run on shutdown, from signal handlers, and at interpreter exit.
lifecycle.register_cleanup("network-rules", cleanup_all_network_rules)


def inject_network(config: dict, dry_run: bool = False):
    """
    Inject network latency using Linux traffic control (tc).

    Args:
        config: Configuration with 'interface', 'delay_ms', 'duration_seconds'
        dry_run: If True, validate but don't execute
    """
    interface = config.get("interface", "eth0")
    delay_ms = config.get("delay_ms", 100)
    duration = config["duration_seconds"]

    # Validation
    is_valid, error = validate_interface_name(interface)
    if not is_valid:
        logger.error(
            "Network injection failed - interface validation",
            extra={"interface": interface, "error": error, "status": "failed"},
        )
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()
        return

    is_valid, error = validate_delay_ms(delay_ms)
    if not is_valid:
        logger.error(
            "Network injection failed - delay validation",
            extra={"delay_ms": delay_ms, "error": error, "status": "failed"},
        )
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()
        return

    if not within_upper_bound(duration, MAX_DURATION_SECONDS):
        logger.error(
            "Network injection failed - duration exceeds hard limit",
            extra={
                "duration_seconds": duration,
                "max_duration_seconds": MAX_DURATION_SECONDS,
                "status": "failed",
            },
        )
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()
        return

    exists, error = verify_interface_exists(interface)
    if not exists:
        logger.error(
            "Network injection failed - interface does not exist",
            extra={"interface": interface, "error": error, "status": "failed"},
        )
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()
        return

    if dry_run:
        logger.info(
            "Network latency injection (DRY RUN)",
            extra={
                "interface": interface,
                "delay_ms": delay_ms,
                "duration_seconds": duration,
                "dry_run": True,
            },
        )
        INJECTIONS_TOTAL.labels(failure_type="network", status="skipped").inc()
        return

    # Only one network injection at a time. A refused request is logged and
    # counted as skipped by the lifecycle.
    with lifecycle.injection("network") as ticket:
        if ticket is None:
            return
        _run_network_injection(interface, delay_ms, duration)


def _run_network_injection(interface: str, delay_ms: int, duration: int) -> None:
    """Apply the rule, hold it, and always remove it. Caller holds the guard."""
    owner = uuid.uuid4().hex

    logger.info(
        "Starting network latency injection",
        extra={
            "interface": interface,
            "delay_ms": delay_ms,
            "duration_seconds": duration,
            "operation": "network_latency",
        },
    )

    INJECTION_ACTIVE.labels(failure_type="network").set(1)
    start_time = time.time()

    try:
        with _tc_lock:
            # Claim the interface BEFORE touching it, so that if we are
            # interrupted halfway, shutdown still knows to clean it up.
            _applied[interface] = owner

            # Clean any existing rules first
            logger.debug("Performing pre-injection cleanup")
            success, error = cleanup_network_rules(interface)
            if not success:
                raise Exception(f"Pre-cleanup failed: {error}")

            # use safe command execution (no shell)
            logger.debug(
                "Adding network delay rule",
                extra={"interface": interface, "delay_ms": delay_ms},
            )

            result = _run_cmd(
                [
                    "tc",
                    "qdisc",
                    "add",
                    "dev",
                    interface,
                    "root",
                    "netem",
                    "delay",
                    f"{delay_ms}ms",
                ]
            )

            if result.returncode != 0:
                raise Exception(f"Failed to add delay: {result.stderr}")

        logger.info(
            "Network delay rule applied successfully",
            extra={"interface": interface, "delay_ms": delay_ms},
        )

        INJECTIONS_TOTAL.labels(failure_type="network", status="success").inc()

        logger.debug(f"Holding network delay for {duration} seconds")
        # Returns early on abort or shutdown; the finally below removes the rule.
        if lifecycle.sleep(duration):
            logger.warning(
                "Network injection cut short",
                extra={"interface": interface, "reason": "abort or shutdown"},
            )

    except Exception as e:
        elapsed = time.time() - start_time

        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()

        logger.error(
            "Network latency injection failed",
            exc_info=True,
            extra={
                "interface": interface,
                "delay_ms": delay_ms,
                "duration_seconds": duration,
                "elapsed_seconds": round(elapsed, 2),
                "error": str(e),
                "error_type": type(e).__name__,
                "status": "failed",
            },
        )

    finally:
        # Always cleanup
        logger.debug("Performing post-injection cleanup")
        success, error = _release_rule(interface, owner)

        if success:
            logger.info(
                "Network delay removed successfully", extra={"interface": interface}
            )
        else:
            logger.warning(
                "Post-injection cleanup failed",
                extra={"interface": interface, "error": error},
            )

        INJECTION_ACTIVE.labels(failure_type="network").set(0)

        elapsed = time.time() - start_time
        logger.info(
            "Network latency injection completed",
            extra={
                "interface": interface,
                "delay_ms": delay_ms,
                "duration_seconds": duration,
                "elapsed_seconds": round(elapsed, 2),
            },
        )
