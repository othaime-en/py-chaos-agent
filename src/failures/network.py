import subprocess
import time
from typing import Tuple
import re
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE


def validate_interface_name(interface: str) -> Tuple[bool, str]:
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
        return False, f"Interface name too long (max 15 chars): {interface}"
    
    # Linux interface naming pattern
    pattern = r'^[a-zA-Z0-9._:-]+$'
    
    if not re.match(pattern, interface):
        return False, f"Invalid interface name: {interface}"
    
    # Explicitly block shell metacharacters
    dangerous_chars = [';', '&', '|', '$', '`', '(', ')', '<', '>', '\n', '\r', '\\', '"', "'", ' ']
    for char in dangerous_chars:
        if char in interface:
            return False, f"Interface name contains forbidden character: '{char}'"
    
    return True, None

def validate_delay_ms(delay_ms: int) -> Tuple[bool, str]:
    """
    Validate delay value is within reasonable bounds.
    
    Returns:
        tuple: (is_valid: bool, error_message: str or None)
    """
    if not isinstance(delay_ms, (int, float)):
        return False, f"Delay must be a number, got {type(delay_ms)}"
    
    if delay_ms < 0:
        return False, f"Delay cannot be negative: {delay_ms}"
    
    if delay_ms > 10000:  # 10 seconds max
        return False, f"Delay too high (max 10000ms): {delay_ms}"
    
    return True, None


def verify_interface_exists(interface: str) -> Tuple[bool, str]:
    """
    Verify that the network interface actually exists on the system.
    
    Returns:
        tuple: (exists: bool, error_message: str or None)
    """
    try:
        # Use ip link show with exact interface name (no shell injection possible)
        result = subprocess.run(
            ["ip", "link", "show", interface],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            return True, None
        else:
            return False, f"Interface '{interface}' does not exist"
            
    except subprocess.TimeoutExpired:
        return False, f"Timeout checking interface '{interface}'"
    except Exception as e:
        return False, f"Error checking interface: {e}"

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
        return False, "Permission denied - NET_ADMIN capability required"
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
        success, error = cleanup_network_rules(interface)
        if not success:
            raise Exception(f"Pre-cleanup failed: {error}")

        result = _run_cmd(add_cmd)
        if result.returncode != 0:
            raise Exception(f"Failed to add delay: {result.stderr}")

        INJECTIONS_TOTAL.labels(failure_type="network", status="success").inc()

        time.sleep(duration)

    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()
        print(f"[NETWORK] Failed: {e}")
    finally:
        # Always clean up, but report if cleanup fails
        success, error = cleanup_network_rules(interface)
        if success:
            print(f"[NETWORK] Cleaned up latency on {interface}")
        else:
            print(f"[NETWORK] Warning: Cleanup failed - {error}")

        INJECTION_ACTIVE.labels(failure_type="network").set(0)
