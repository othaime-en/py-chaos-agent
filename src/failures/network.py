import subprocess
import time
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE

def _run_cmd(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def inject_network(config: dict, dry_run: bool = False):
    interface = config.get('interface', 'eth0')
    delay_ms = config.get('delay_ms', 100)
    duration = config['duration_seconds']

    add_cmd = f"tc qdisc add dev {interface} root netem delay {delay_ms}ms"
    del_cmd = f"tc qdisc del dev {interface} root"

    if dry_run:
        print(f"[DRY RUN] Would add {delay_ms}ms latency on {interface}")
        INJECTIONS_TOTAL.labels(failure_type='network', status='skipped').inc()
        return

    print(f"[NETWORK] Adding {delay_ms}ms latency for {duration}s...")
    INJECTION_ACTIVE.labels(failure_type='network').set(1)
    INJECTIONS_TOTAL.labels(failure_type='network', status='success').inc()

    try:
        _run_cmd(add_cmd)
        time.sleep(duration)
    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type='network', status='failed').inc()
    finally:
        _run_cmd(del_cmd)
        INJECTION_ACTIVE.labels(failure_type='network').set(0)