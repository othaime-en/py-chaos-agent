import multiprocessing
import time
import psutil
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE

def _worker(duration: int):
    end = time.time() + duration
    while time.time() < end:
        pass  # spin


def _cpu_hog(cores: int, duration: int):
    procs = [multiprocessing.Process(target=_worker, args=(duration,)) for _ in range(cores)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

def inject_cpu(config: dict, dry_run: bool = False):
    cores = config.get('cores', 1)
    duration = config['duration_seconds']

    if dry_run:
        print(f"[DRY RUN] Would hog {cores} CPU core(s) for {duration}s")
        INJECTIONS_TOTAL.labels(failure_type='cpu', status='skipped').inc()
        return

    print(f"[CPU] Hogging {cores} core(s) for {duration}s...")
    INJECTION_ACTIVE.labels(failure_type='cpu').set(1)
    INJECTIONS_TOTAL.labels(failure_type='cpu', status='success').inc()

    try:
        _cpu_hog(cores, duration)
    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type='cpu', status='failed').inc()
        print(f"[CPU] Failed: {e}")
    finally:
        INJECTION_ACTIVE.labels(failure_type='cpu').set(0)