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

def _run_cmd_safe(args: list) -> subprocess.CompletedProcess:
    """
    Execute command safely without shell interpretation.
    
    Args:
        args: Command and arguments as a list (NOT a string)
        
    Returns:
        CompletedProcess object with returncode, stdout, stderr
    """
    try:
        result = subprocess.run(
            args,
            shell=False,  #No shell interpretation
            capture_output=True,
            text=True,
            timeout=30  # Prevent hanging
        )
        return result
    except subprocess.TimeoutExpired:
        raise Exception(f"Command timed out: {' '.join(args)}")
    except Exception as e:
        raise Exception(f"Command execution failed: {e}")


def cleanup_network_rules(interface="eth0"):
    """
    Remove any existing tc qdisc rules on the interface.
    
    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    is_valid, error = validate_interface_name(interface)
    if not is_valid:
        return False, f"Invalid interface: {error}"
    
    exists, error = verify_interface_exists(interface)
    if not exists:
        return False, error
    
    # use list of args instead of shell string
    result = _run_cmd_safe(["tc", "qdisc", "del", "dev", interface, "root"])
    
    if result.returncode == 0:
        return True, None
    
    # Check for benign errors
    stderr_lower = result.stderr.lower()
    benign_errors = [
        "no such file or directory",
        "cannot delete qdisc with handle of zero",
    ]
    
    if any(err in stderr_lower for err in benign_errors):
        return True, None
    
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
    
    is_valid, error = validate_interface_name(interface)
    if not is_valid:
        print(f"[NETWORK] Validation failed: {error}")
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()
        return
    
    is_valid, error = validate_delay_ms(delay_ms)
    if not is_valid:
        print(f"[NETWORK] Validation failed: {error}")
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()
        return
    
    exists, error = verify_interface_exists(interface)
    if not exists:
        print(f"[NETWORK] {error}")
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()
        return

    if dry_run:
        print(f"[DRY RUN] Would add {delay_ms}ms latency on {interface}")
        INJECTIONS_TOTAL.labels(failure_type="network", status="skipped").inc()
        return

    print(f"[NETWORK] Adding {delay_ms}ms latency for {duration}s...")
    INJECTION_ACTIVE.labels(failure_type="network").set(1)

    try:
        # Clean any existing rules first
        success, error = cleanup_network_rules(interface)
        if not success:
            raise Exception(f"Pre-cleanup failed: {error}")

        #use safe command execution (no shell)
        result = _run_cmd_safe([
            "tc", "qdisc", "add", 
            "dev", interface,
            "root", "netem", "delay", f"{delay_ms}ms"
        ])
        
        if result.returncode != 0:
            raise Exception(f"Failed to add delay: {result.stderr}")

        INJECTIONS_TOTAL.labels(failure_type="network", status="success").inc()
        time.sleep(duration)

    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc()
        print(f"[NETWORK] Failed: {e}")
    finally:
        success, error = cleanup_network_rules(interface)
        if success:
            print(f"[NETWORK] Cleaned up latency on {interface}")
        else:
            print(f"[NETWORK] Warning: Cleanup failed - {error}")
        
        INJECTION_ACTIVE.labels(failure_type="network").set(0)
