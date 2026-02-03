import subprocess
import time
from typing import Tuple, Optional
import re
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE
from ..logging_config import get_logger

logger = get_logger(__name__)


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
        time.sleep(duration)

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
        success, error = cleanup_network_rules(interface)

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
