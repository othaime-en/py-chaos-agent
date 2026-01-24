import subprocess
import time
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE


def _run_cmd(cmd):
    """Execute shell command and return result."""
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def cleanup_network_rules(interface="eth0"):
    """
    Remove any existing tc qdisc rules on the interface.
    
    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    del_cmd = f"tc qdisc del dev {interface} root"
    result = _run_cmd(del_cmd)
    
    if result.returncode == 0:
        return True, None
    
    # Check if the error is benign (no qdisc exists)
    stderr_lower = result.stderr.lower()
    
    # These are expected errors when no rules exist - not a problem
    benign_errors = [
        "no such file or directory",
        "cannot delete qdisc with handle of zero",
        "rtnetlink answers: no such file or directory",
        "RTNETLINK answers: No such file or directory",
    ]
    
    if any(err in stderr_lower for err in benign_errors):
        return True, None
    
    error_msg = result.stderr.strip() or "Unknown error"
    
    # Check for specific critical errors
    if "cannot find device" in stderr_lower or "no such device" in stderr_lower:
        return False, f"Interface '{interface}' does not exist"
    elif "operation not permitted" in stderr_lower:
        return False, f"Permission denied - NET_ADMIN capability required"
    elif "command not found" in stderr_lower or "tc: not found" in stderr_lower:
        return False, "tc command not found - install iproute2 package"
    else:
        return False, f"Failed to cleanup: {error_msg}"


def inject_network(config: dict, dry_run: bool = False):
    interface = config.get("interface", "eth0")
    delay_ms = config.get("delay_ms", 100)
    duration = config["duration_seconds"]

    add_cmd = f"tc qdisc add dev {interface} root netem delay {delay_ms}ms"

    if dry_run:
        print(f"[DRY RUN] Would add {delay_ms}ms latency on {interface}")
        INJECTIONS_TOTAL.labels(failure_type="network", status="skipped").inc()
        return

    print(f"[NETWORK] Adding {delay_ms}ms latency for {duration}s...")
    INJECTION_ACTIVE.labels(failure_type="network").set(1)

    try:
        # Clean any existing rules first (idempotent operation)
        cleanup_network_rules(interface)

        # Add the delay rule
        result = _run_cmd(add_cmd)
        if result.returncode != 0:
            raise Exception(f"Failed to add delay: {result.stderr}")

        INJECTIONS_TOTAL.labels(failure_type="network", status="success").inc()

        # Hold the delay for the specified duration
        time.sleep(duration)

    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()
        print(f"[NETWORK] Failed: {e}")
    finally:
        # Always clean up, even if injection failed
        cleanup_network_rules(interface)
        INJECTION_ACTIVE.labels(failure_type="network").set(0)
        print(f"[NETWORK] Cleaned up latency on {interface}")
