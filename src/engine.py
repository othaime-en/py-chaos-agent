"""
The chaos engine: the one injection loop behind both entry points.

``python -m src.agent`` (standalone) and ``python -m src.api_server`` (REST API)
used to carry separate copies of the same loop. They had already drifted apart
(different logging, different error handling) and both ran injections inline,
so a 60 second CPU burn froze the loop for 60 seconds and faults could never
overlap. This module replaces both loops.

How it works
------------
* Each tick the engine takes a **settings snapshot** (a deep copy, made by the
  caller's ``settings_provider``), so a ``PATCH /config`` that lands mid-tick can
  never be observed half applied, and a failure dict is never read while it is
  being replaced.
* For every enabled failure type that passes its probability roll, the engine
  starts the injection on its **own worker thread** and moves on. Injections of
  different types therefore overlap. Two of the same type cannot: a tick leaves
  a type alone while its previous injection is still running, and the lifecycle
  guard refuses any request that races past that check (logged, counted as
  ``skipped``).
* Overlapping injections are safe for the host because the resource governor
  gives them a shared budget through reservations.
* The kill switch is consulted twice: before a worker is started (so the tick
  can stop early and the engine can report ``paused``), and again by the worker
  itself through ``KillSwitch.injection`` (so a breaker that trips in between
  still wins, and the failure context is recorded for process kills).
* ``stop()`` ends the loop, aborts running injections (their ``finally`` blocks
  remove the effect within milliseconds) and waits for the workers.

The engine knows nothing about FastAPI or signal handlers. The entry points own
those and just call ``start()`` and ``stop()``.
"""

import copy
import importlib
import random
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, cast

from .chance import should_inject
from .kill_switch import KillSwitch, KillSwitchOpen
from .lifecycle import lifecycle
from .logging_config import get_logger, log_failure_injection, set_correlation_id

logger = get_logger(__name__)

# Failure type -> module (relative to this package) that defines ``inject_<type>``.
FAILURE_MODULES = {
    "cpu": ".failures.cpu",
    "memory": ".failures.memory",
    "process": ".failures.process",
    "network": ".failures.network",
}

# Granularity of the wait between ticks. Short enough that a shutdown signal is
# noticed quickly, long enough to cost nothing.
_WAIT_SLICE_SECONDS = 0.25


def resolve_injector(name: str) -> Callable[..., None]:
    """
    Return ``inject_<name>`` from its failure module.

    Looked up at call time (not import time), so there is exactly one table of
    injectors and tests can patch ``src.failures.<module>.inject_<name>``.
    """
    try:
        module_path = FAILURE_MODULES[name]
    except KeyError:
        raise ValueError(f"Unknown failure type '{name}'") from None
    module = importlib.import_module(module_path, package=__package__)
    return cast(Callable[..., None], getattr(module, f"inject_{name}"))


def run_injection(name: str, cfg: Mapping[str, Any], dry_run: bool) -> None:
    """Run one injection to completion on the calling thread."""
    resolve_injector(name)(cfg, dry_run=dry_run)


@dataclass(frozen=True)
class Settings:
    """What one tick needs from the config. Immutable once taken."""

    interval_seconds: float
    dry_run: bool
    failures: Mapping[str, Mapping[str, Any]]


def snapshot_settings(config: Any) -> Settings:
    """
    Copy the loop-relevant parts of ``config``.

    The caller holds whatever lock protects ``config`` while this runs; after it
    returns the snapshot is private to the engine.
    """
    return Settings(
        interval_seconds=config.agent.interval_seconds,
        dry_run=config.agent.dry_run,
        failures=copy.deepcopy(config.failures),
    )


@dataclass
class TickResult:
    """What one tick did. Returned by :meth:`ChaosEngine.run_tick`."""

    iteration: int
    attempted: int = 0
    started: int = 0
    busy: int = 0
    paused: bool = False


class ChaosEngine:
    """
    Runs injections on a schedule until stopped.

    Args:
        settings_provider: Returns a fresh :class:`Settings` snapshot (or raises
            or returns ``None`` if the config is unavailable). Called once per
            tick, so config changes apply on the next tick.
        kill_switch_provider: Returns the kill switch that should gate
            injections right now, or ``None`` for none. Called every tick and
            before every injection, so a swapped or bypassed kill switch is
            honored immediately.
        on_exit: Called from the loop thread when it ends for any reason.
        injector: ``(name, cfg, dry_run) -> None``. Defaults to
            :func:`run_injection`. Injectable for tests.
        error_backoff_seconds: How long to wait after a tick fails outright
            (for example because the config is unavailable).
        name: Used for the thread names.
    """

    def __init__(
        self,
        settings_provider: Callable[[], Optional[Settings]],
        kill_switch_provider: Optional[Callable[[], Optional[KillSwitch]]] = None,
        on_exit: Optional[Callable[[], None]] = None,
        injector: Optional[Callable[[str, Mapping[str, Any], bool], None]] = None,
        error_backoff_seconds: float = 10.0,
        name: str = "chaos-engine",
    ) -> None:
        self._settings_provider = settings_provider
        self._kill_switch_provider = kill_switch_provider or (lambda: None)
        self._on_exit = on_exit
        self._injector = injector or run_injection
        self._error_backoff = error_backoff_seconds
        self._name = name

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._workers: List[threading.Thread] = []
        self._start_time: Optional[float] = None
        self._iteration = 0
        self._paused = False

    # -- state ---------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True while the loop thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def thread(self) -> Optional[threading.Thread]:
        return self._thread

    @property
    def start_time(self) -> Optional[float]:
        return self._start_time

    @property
    def iteration(self) -> int:
        return self._iteration

    @property
    def paused(self) -> bool:
        """True while the engine is holding chaos back for the kill switch."""
        return self._paused

    def in_flight(self) -> int:
        """How many worker threads are still running an injection."""
        with self._lock:
            self._workers = [w for w in self._workers if w.is_alive()]
            return len(self._workers)

    # -- start and stop --------------------------------------------------------

    def start(self) -> None:
        """Start the loop on a background thread. Raises if already running."""
        with self._lock:
            if self.is_running:
                raise RuntimeError("The chaos engine is already running")
            self._stop.clear()
            self._start_time = time.time()
            self._iteration = 0
            self._paused = False
            thread = threading.Thread(target=self._run, daemon=True, name=self._name)
            self._thread = thread
        thread.start()

    def stop(self, timeout: float = 5.0, reason: str = "agent stopped") -> bool:
        """
        Stop the loop, abort running injections, and wait for them to finish.

        Returns True if everything has stopped within ``timeout`` seconds. Safe
        to call when not running and from any thread except the loop's own.
        """
        self._stop.set()
        lifecycle.abort_active(reason)

        deadline = time.monotonic() + max(timeout, 0)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(deadline - time.monotonic(), 0))
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(max(deadline - time.monotonic(), 0))

        stopped = not self.is_running and self.in_flight() == 0
        if not stopped:
            logger.warning(
                "Chaos engine did not stop in time",
                extra={"timeout": timeout, "in_flight": self.in_flight()},
            )
        return stopped

    def join(self, timeout: Optional[float] = None) -> None:
        """Block until the loop ends (or ``timeout``). Interruptible by signals."""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # -- the loop ----------------------------------------------------------------

    def _should_exit(self) -> bool:
        return self._stop.is_set() or lifecycle.is_shutting_down()

    def _wait(self, seconds: float) -> None:
        """Sleep up to ``seconds``, waking early on stop or process shutdown."""
        deadline = time.monotonic() + max(seconds, 0)
        while not self._should_exit():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(remaining, _WAIT_SLICE_SECONDS))

    def _run(self) -> None:
        logger.info("Chaos engine started")
        try:
            while not self._should_exit():
                wait_for = self._error_backoff
                try:
                    settings = self._settings_provider()
                    if settings is None:
                        raise RuntimeError("Configuration not loaded")
                    self._iteration += 1
                    self.run_tick(settings, self._iteration)
                    wait_for = settings.interval_seconds
                except Exception as e:
                    logger.error(
                        "Unexpected error in chaos loop",
                        exc_info=True,
                        extra={
                            "iteration": self._iteration,
                            "error": str(e),
                            "error_type": type(e).__name__,
                        },
                    )
                self._wait(wait_for)
        finally:
            logger.info("Chaos engine stopped")
            if self._on_exit is not None and self._thread is threading.current_thread():
                try:
                    self._on_exit()
                except Exception:
                    logger.error("on_exit callback failed", exc_info=True)

    def run_tick(self, settings: Settings, iteration: int) -> TickResult:
        """
        Roll for every enabled failure type and start the winners.

        Returns as soon as the injections are started; it does not wait for them.
        """
        correlation_id = f"iter-{iteration}-{uuid.uuid4().hex[:8]}"
        set_correlation_id(correlation_id)
        logger.debug(
            "Starting chaos iteration",
            extra={"iteration": iteration, "correlation_id": correlation_id},
        )

        result = TickResult(iteration=iteration)
        self._resume_if_recovered()

        for name, cfg in settings.failures.items():
            if self._should_exit():
                break
            if not cfg["enabled"]:
                logger.debug(
                    "Failure type disabled, skipping", extra={"failure_type": name}
                )
                continue

            probability = cfg["probability"]
            roll = random.random()
            if not should_inject(probability, roll):
                logger.debug(
                    "Probability check failed, skipping injection",
                    extra={
                        "failure_type": name,
                        "probability": probability,
                        "roll": round(roll, 3),
                    },
                )
                continue

            result.attempted += 1

            # The previous injection of this type is still running. That is
            # normal when its duration is longer than the interval, so the tick
            # just leaves it alone: no warning, and not counted as skipped.
            # (The lifecycle guard still refuses a racing request, and that
            # one is counted.) Dry runs hold nothing, so they never wait.
            if not settings.dry_run and lifecycle.is_active(name):
                logger.debug(
                    "Injection still running, not starting another",
                    extra={"failure_type": name},
                )
                result.busy += 1
                continue

            # Paused, not stopped: skip the rest of this tick and try again next
            # interval. Dry runs change nothing, so they are never gated.
            ks = self._kill_switch_provider()
            if ks is not None and not settings.dry_run:
                allowed, reason = ks.allows_injection()
                if not allowed:
                    self._pause(reason)
                    result.paused = True
                    break

            logger.info(
                "Probability check passed, injecting failure",
                extra={
                    "failure_type": name,
                    "probability": probability,
                    "roll": round(roll, 3),
                    "dry_run": settings.dry_run,
                    "iteration": iteration,
                },
            )
            self._spawn(name, cfg, settings.dry_run, ks, correlation_id)
            result.started += 1

        logger.debug(
            "Chaos iteration complete",
            extra={
                "iteration": iteration,
                "injections_attempted": result.attempted,
                "injections_started": result.started,
                "injections_busy": result.busy,
            },
        )
        return result

    # -- pause bookkeeping ---------------------------------------------------------

    def _pause(self, reason: str) -> None:
        if not self._paused:
            logger.warning("Chaos paused by kill switch", extra={"reason": reason})
            self._paused = True

    def _resume_if_recovered(self) -> None:
        if not self._paused:
            return
        ks = self._kill_switch_provider()
        if ks is None or ks.allows_injection()[0]:
            logger.info("Kill switch closed, chaos resuming")
            self._paused = False

    # -- workers ---------------------------------------------------------------------

    def _spawn(
        self,
        name: str,
        cfg: Mapping[str, Any],
        dry_run: bool,
        ks: Optional[KillSwitch],
        correlation_id: str,
    ) -> None:
        worker = threading.Thread(
            target=self._work,
            args=(name, cfg, dry_run, ks, correlation_id),
            daemon=True,
            name=f"{self._name}-{name}",
        )
        with self._lock:
            self._workers = [w for w in self._workers if w.is_alive()]
            self._workers.append(worker)
        worker.start()

    def _work(
        self,
        name: str,
        cfg: Mapping[str, Any],
        dry_run: bool,
        ks: Optional[KillSwitch],
        correlation_id: str,
    ) -> None:
        set_correlation_id(correlation_id)
        try:
            log_failure_injection(
                logger,
                failure_type=name,
                action="start",
                status="executing",
                config=dict(cfg),
            )
            # The kill switch is checked again here: it may have tripped between
            # the tick and this thread starting. It also records the injection
            # so a process kill earns its grace period.
            with ks.injection(name, dry_run) if ks is not None else nullcontext():
                self._injector(name, cfg, dry_run)
            log_failure_injection(
                logger, failure_type=name, action="complete", status="success"
            )
        except KillSwitchOpen as e:
            logger.warning(
                "Injection skipped: kill switch tripped before it started",
                extra={"failure_type": name, "reason": e.reason},
            )
            self._pause(e.reason)
        except Exception as e:
            logger.error(
                "Failure injection raised exception",
                exc_info=True,
                extra={
                    "failure_type": name,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            log_failure_injection(
                logger,
                failure_type=name,
                action="failed",
                status="error",
                error=str(e),
            )
