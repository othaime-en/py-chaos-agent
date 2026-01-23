import multiprocessing
import time
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE


def _worker(duration: int):
    """Worker process that consumes CPU for the specified duration."""
    end = time.time() + duration
    while time.time() < end:
        pass  # spin


def _cpu_hog(cores: int, duration: int):
    """
    Spawn multiple worker processes to consume CPU cores.

    Ensures all processes are properly cleaned up even if exceptions occur.
    """
    procs = [
        multiprocessing.Process(target=_worker, args=(duration,)) for _ in range(cores)
    ]

    try:
        for p in procs:
            p.start()

        for p in procs:
            p.join()

    except Exception as e:
        print(f"[CPU] Error during CPU hogging: {e}")
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(timeout=2)
                if p.is_alive():
                    p.kill()
        raise

    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(timeout=1)


def inject_cpu(config: dict, dry_run: bool = False):
    """
    Inject CPU stress by spawning worker processes.

    Args:
        config: Configuration dictionary with 'cores' and 'duration_seconds'
        dry_run: If True, log actions without executing
    """
    cores = config.get("cores", 1)
    duration = config["duration_seconds"]

    if dry_run:
        print(f"[DRY RUN] Would hog {cores} CPU core(s) for {duration}s")
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="skipped").inc()
        return

    print(f"[CPU] Hogging {cores} core(s) for {duration}s...")
    INJECTION_ACTIVE.labels(failure_type="cpu").set(1)

    try:
        _cpu_hog(cores, duration)
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="success").inc()
        print(f"[CPU] Successfully completed {duration}s CPU stress")

    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="failed").inc()
        print(f"[CPU] Failed: {e}")

    finally:
        INJECTION_ACTIVE.labels(failure_type="cpu").set(0)
