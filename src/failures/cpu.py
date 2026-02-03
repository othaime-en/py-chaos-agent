import multiprocessing
import time
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE
from ..logging_config import get_logger

logger = get_logger(__name__)


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
    logger.debug(
        "Spawning CPU worker processes", extra={"cores": cores, "duration": duration}
    )

    procs = [
        multiprocessing.Process(target=_worker, args=(duration,)) for _ in range(cores)
    ]

    try:
        for i, p in enumerate(procs):
            p.start()
            logger.debug(
                "CPU worker process started", extra={"worker_id": i, "pid": p.pid}
            )

        for i, p in enumerate(procs):
            p.join()
            logger.debug(
                "CPU worker process completed", extra={"worker_id": i, "pid": p.pid}
            )

    except Exception as e:
        logger.error(
            "Error during CPU hogging, terminating workers",
            exc_info=True,
            extra={"cores": cores, "error": str(e)},
        )

        for i, p in enumerate(procs):
            if p.is_alive():
                logger.debug(f"Terminating worker {i} (PID: {p.pid})")
                p.terminate()
                p.join(timeout=2)
                if p.is_alive():
                    logger.warning(f"Force killing worker {i} (PID: {p.pid})")
                    p.kill()
        raise

    finally:
        # Final cleanup
        alive_count = 0
        for p in procs:
            if p.is_alive():
                alive_count += 1
                p.terminate()
                p.join(timeout=1)

        if alive_count > 0:
            logger.warning(
                "Some CPU workers still alive after completion",
                extra={"alive_count": alive_count},
            )


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
        logger.info(
            "CPU injection (DRY RUN)",
            extra={"cores": cores, "duration_seconds": duration, "dry_run": True},
        )
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="skipped").inc()
        return

    logger.info(
        "Starting CPU stress injection",
        extra={"cores": cores, "duration_seconds": duration, "operation": "cpu_stress"},
    )

    INJECTION_ACTIVE.labels(failure_type="cpu").set(1)
    start_time = time.time()

    try:
        _cpu_hog(cores, duration)
        elapsed = time.time() - start_time

        INJECTIONS_TOTAL.labels(failure_type="cpu", status="success").inc()

        logger.info(
            "CPU stress injection completed successfully",
            extra={
                "cores": cores,
                "duration_seconds": duration,
                "elapsed_seconds": round(elapsed, 2),
                "status": "success",
            },
        )

    except Exception as e:
        elapsed = time.time() - start_time

        INJECTIONS_TOTAL.labels(failure_type="cpu", status="failed").inc()

        logger.error(
            "CPU stress injection failed",
            exc_info=True,
            extra={
                "cores": cores,
                "duration_seconds": duration,
                "elapsed_seconds": round(elapsed, 2),
                "error": str(e),
                "error_type": type(e).__name__,
                "status": "failed",
            },
        )

    finally:
        INJECTION_ACTIVE.labels(failure_type="cpu").set(0)
        logger.debug("CPU injection active metric reset to 0")
