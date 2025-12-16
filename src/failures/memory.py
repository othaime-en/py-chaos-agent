import time
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE

def inject_memory(config: dict, dry_run: bool = False):
    mb = config.get('mb', 100)
    duration = config['duration_seconds']
    chunk = bytearray(1024 * 1024)  # 1 MB
    data = []

    if dry_run:
        print(f"[DRY RUN] Would allocate {mb} MB for {duration}s")
        INJECTIONS_TOTAL.labels(failure_type='memory', status='skipped').inc()
        return

    print(f"[MEMORY] Allocating {mb} MB for {duration}s...")
    INJECTION_ACTIVE.labels(failure_type='memory').set(1)
    INJECTIONS_TOTAL.labels(failure_type='memory', status='success').inc()

    try:
        start = time.time()
        allocated = 0
        while allocated < mb and time.time() - start < duration:
            data.append(chunk)
            allocated += 1
        remaining = duration - (time.time() - start)
        if remaining > 0:
            time.sleep(remaining)
    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type='memory', status='failed').inc()
        print(f"[MEMORY] Failed: {e}")
    finally:
        INJECTION_ACTIVE.labels(failure_type='memory').set(0)
        del data  # free