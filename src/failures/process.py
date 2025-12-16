import psutil
from ..metrics import INJECTIONS_TOTAL

def inject_process(config: dict, dry_run: bool = False):
    target_name = config.get('target_name')
    if not target_name:
        return

    procs = [p for p in psutil.process_iter(['name']) if target_name.lower() in p.info['name'].lower()]
    if not procs:
        print(f"[PROCESS] No process named '{target_name}' found")
        return

    target = procs[0]  # kill first match
    pid = target.pid

    if dry_run:
        print(f"[DRY RUN] Would kill process {target_name} (PID: {pid})")
        INJECTIONS_TOTAL.labels(failure_type='process', status='skipped').inc()
        return

    print(f"[PROCESS] Killing {target_name} (PID: {pid})...")
    try:
        target.terminate()
        INJECTIONS_TOTAL.labels(failure_type='process', status='success').inc()
    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type='process', status='failed').inc()
        print(f"[PROCESS] Failed: {e}")