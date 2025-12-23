import subprocess
import time
import atexit
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE

def _run_cmd(cmd):
    """Execute shell command and return result."""
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def cleanup_network_rules(interface='eth0'):
    """Remove any existing tc qdisc rules on the interface."""
    del_cmd = f"tc qdisc del dev {interface} root 2>/dev/null"
    result = _run_cmd(del_cmd)
    # Suppress error output - it's OK if no rules exist
    return result.returncode

def inject_network(config: dict, dry_run: bool = False):
    interface = config.get('interface', 'eth0')
    delay_ms = config.get('delay_ms', 100)
    duration = config['duration_seconds']

    add_cmd = f"tc qdisc add dev {interface} root netem delay {delay_ms}ms"

    if dry_run:
        print(f"[DRY RUN] Would add {delay_ms}ms latency on {interface}")
        INJECTIONS_TOTAL.labels(failure_type='network', status='skipped').inc()
        return

    print(f"[NETWORK] Adding {delay_ms}ms latency for {duration}s...")
    INJECTION_ACTIVE.labels(failure_type='network').set(1)

    try:
        # Clean any existing rules first (idempotent operation)
        cleanup_network_rules(interface)
        
        # Add the delay rule
        result = _run_cmd(add_cmd)
        if result.returncode != 0:
            raise Exception(f"Failed to add delay: {result.stderr}")
        
        INJECTIONS_TOTAL.labels(failure_type='network', status='success').inc()
        
        # Hold the delay for the specified duration
        time.sleep(duration)
        
    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type='network', status='failed').inc()
        print(f"[NETWORK] Failed: {e}")
    finally:
        # Always clean up, even if injection failed
        cleanup_network_rules(interface)
        INJECTION_ACTIVE.labels(failure_type='network').set(0)
        print(f"[NETWORK] Cleaned up latency on {interface}")