import multiprocessing
import os
import time
from ..lifecycle import lifecycle
from ..limits import MAX_CORES, MAX_DURATION_SECONDS, within_upper_bound
from ..metrics import INJECTIONS_CLAMPED, INJECTIONS_TOTAL, INJECTION_ACTIVE
from ..resources import governor
from ..logging_config import get_logger

logger = get_logger(__name__)


def _worker(duration: int, parent_pid: int = 0):
    """
    Worker process that consumes CPU for the specified duration.

    Exits early if its parent dies (SIGKILL, OOM kill), so a crashed agent
    cannot leave workers burning CPU until the duration runs out.
    """
    end = time.time() + duration
    next_parent_check = 0.0
    while True:
        now = time.time()
        if now >= end:
            return
        if parent_pid and now >= next_parent_check:
            if os.getppid() != parent_pid:
                return
            next_parent_check = now + 0.2


def _terminate(procs):
    """Stop any worker still running. Safe to call repeatedly."""
    for i, p in enumerate(procs):
        if p.is_alive():
            logger.debug(f"Terminating worker {i} (PID: {p.pid})")
            p.terminate()
    for i, p in enumerate(procs):
        p.join(timeout=2)
        if p.is_alive():
            logger.warning(f"Force killing worker {i} (PID: {p.pid})")
            p.kill()
            p.join(timeout=2)


def _cpu_hog(cores: int, duration: int):
    """
    Spawn multiple worker processes to consume CPU cores.

    The wait is interruptible: an abort or shutdown stops the workers
    immediately. Workers are always terminated on the way out, whether the
    exit is normal, an exception, or a signal-driven SystemExit.
    """
    logger.debug(
        "Spawning CPU worker processes", extra={"cores": cores, "duration": duration}
    )

    parent = os.getpid()
    procs = [
        multiprocessing.Process(target=_worker, args=(duration, parent), daemon=True)
        for _ in range(cores)
    ]

    try:
        for i, p in enumerate(procs):
            p.start()
            logger.debug(
                "CPU worker process started", extra={"worker_id": i, "pid": p.pid}
            )

        interrupted = lifecycle.sleep(duration)
        if interrupted:
            logger.warning(
                "CPU injection cut short", extra={"reason": "abort or shutdown"}
            )
        else:
            # Workers stop on their own at the deadline; give them a moment.
            for p in procs:
                p.join(timeout=5)

    except Exception as e:
        logger.error(
            "Error during CPU hogging, terminating workers",
            exc_info=True,
            extra={"cores": cores, "error": str(e)},
        )
        raise

    finally:
        _terminate(procs)


def inject_cpu(config, dry_run=False):
    """
    Inject CPU stress. Only one CPU injection runs at a time; a second request
    is skipped (logged and counted) rather than stacked on the first.
    """
    with lifecycle.injection("cpu", enabled=not dry_run) as ticket:
        if ticket is None and not dry_run:
            return
        _inject_cpu(config, dry_run)


def _inject_cpu(config: dict, dry_run: bool = False):
    """
    Inject CPU stress by spawning worker processes.

    Args:
        config: Configuration dictionary with 'cores' and 'duration_seconds'
        dry_run: If True, log actions without executing
    """
    cores = config.get("cores", 1)
    duration = config["duration_seconds"]

    # Last line of defense: config and API writes are validated upstream, but
    # a direct caller must not be able to spawn an unbounded number of workers.
    if not within_upper_bound(cores, MAX_CORES) or not within_upper_bound(
        duration, MAX_DURATION_SECONDS
    ):
        logger.error(
            "CPU injection rejected - parameters exceed hard limits",
            extra={
                "cores": cores,
                "duration_seconds": duration,
                "max_cores": MAX_CORES,
                "max_duration_seconds": MAX_DURATION_SECONDS,
                "status": "failed",
            },
        )
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="failed").inc()
        return

    # Fit the request to this container's real CPU budget. Fewer workers still
    # produce load, so an oversized request is clamped rather than refused.
    grant = (governor.preview_cores if dry_run else governor.acquire_cores)(cores)
    if not grant.ok:
        logger.error(
            "CPU injection refused - outside resource budget",
            extra={"cores": cores, "reason": grant.reason, "status": "failed"},
        )
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="failed").inc()
        return

    requested_cores = cores
    cores = grant.granted
    if grant.clamped:
        logger.warning(
            "CPU injection clamped to resource budget",
            extra={
                "requested_cores": requested_cores,
                "effective_cores": cores,
                "reason": grant.reason,
            },
        )
        INJECTIONS_CLAMPED.labels(failure_type="cpu").inc()

    if dry_run:
        logger.info(
            "CPU injection (DRY RUN)",
            extra={
                "cores": requested_cores,
                "effective_cores": cores,
                "duration_seconds": duration,
                "dry_run": True,
            },
        )
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="skipped").inc()
        return

    # From here the cores are reserved. The finally below releases them.
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
        governor.release_cores(cores)
        INJECTION_ACTIVE.labels(failure_type="cpu").set(0)
        logger.debug("CPU injection active metric reset to 0")
