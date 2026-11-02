"""
Experiments: a hypothesis, a steady state, some chaos, and a verdict.

An experiment (see ``ExperimentConfig`` in ``src.schemas``) is run in four steps:

1. **Baseline.** The steady-state checks must pass before anything is injected.
   If the target is not healthy to begin with, the run is *refused* and no chaos
   happens: a failure you cannot attribute to the experiment proves nothing.
2. **Chaos.** The experiment's faults are rolled on its schedule by a dedicated
   :class:`~src.engine.ChaosEngine`, so everything the agent already guarantees
   applies unchanged: the kill switch, the one-injection-per-type guard, the
   resource budget, cleanup on abort, and audit events (attributed to the person
   who started the run).
3. **Recovery.** When the schedule ends, running injections finish on their own
   and the steady state is checked again for up to ``recovery_timeout_seconds``.
4. **Verdict.** ``confirmed`` if the steady state came back, ``falsified`` if it
   did not. Anything that cut the run short (an operator abort, the kill switch,
   shutdown, a refused baseline, an error) is ``inconclusive``.

What this does not do yet: the steady state is checked before and after the chaos,
not while it runs. Watching it during the run, and stopping early when it is
breached, is the job of the SLO abort feature.

If the experiment has time windows (see ``src.windows``) it will not start
outside them, or when the window will close before its chaos could finish, and a
run whose window closes mid-way is aborted. Dry runs are exempt: they change
nothing.

Only one experiment runs at a time, and the API refuses to start one while the
agent loop is running (and the other way round), so two sources of chaos can never
overlap and make a result unreadable.
"""

import copy
import datetime as dt
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional

from .audit import audit
from .engine import ChaosEngine, Settings
from .kill_switch import KillSwitch
from .lifecycle import lifecycle
from .logging_config import get_logger
from .metrics import EXPERIMENT_ACTIVE, EXPERIMENTS_TOTAL
from . import windows as time_windows
from .steady_state import SteadyStateResult, wait_for_steady_state

logger = get_logger(__name__)

HISTORY_SIZE = 50
_POLL_SECONDS = 0.1
# Slack on top of the longest fault when waiting for injections to finish by
# themselves at the end of an experiment.
_FINISH_GRACE_SECONDS = 15.0
_ABORT_TIMEOUT_SECONDS = 10.0


class ExperimentBusy(RuntimeError):
    """An experiment is already running."""


class OutsideWindow(RuntimeError):
    """The experiment may not start now: its allowed time window is closed, or
    will close before the chaos could finish."""


def chaos_budget_seconds(experiment: Mapping[str, Any]) -> float:
    """
    The longest the chaos phase of ``experiment`` can last, worst case.

    That is the scheduled duration (or, with ``max_injections``, the time until
    the last allowed injection starts) plus the longest single fault, which may
    still be running when the schedule ends. The baseline and recovery checks
    are not chaos and are not counted.
    """
    schedule = experiment["schedule"]
    active = float(schedule["duration_seconds"])
    cap = schedule.get("max_injections")
    if cap is not None:
        active = min(active, (cap - 1) * float(schedule["interval_seconds"]))
    longest = max(
        (f.get("duration_seconds", 0) for f in experiment["faults"].values()),
        default=0,
    )
    return active + longest


class RunStatus(str, Enum):
    PENDING = "pending"
    CHECKING_BASELINE = "checking_baseline"
    RUNNING = "running"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    ABORTED = "aborted"
    REFUSED = "refused"
    ERROR = "error"


FINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.ABORTED, RunStatus.REFUSED, RunStatus.ERROR}
)


class Verdict(str, Enum):
    CONFIRMED = "confirmed"
    FALSIFIED = "falsified"
    INCONCLUSIVE = "inconclusive"


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    moment = dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class ExperimentRun:
    """One run of an experiment. Read it through the runner, which locks."""

    id: str
    experiment: str
    hypothesis: str
    dry_run: bool
    principal: str
    role: Optional[str]
    created_at: float
    status: RunStatus = RunStatus.PENDING
    verdict: Optional[Verdict] = None
    reason: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    injections_started: int = 0
    window_closes_at: Optional[dt.datetime] = None
    baseline: Optional[SteadyStateResult] = None
    recovery: Optional[SteadyStateResult] = None

    @property
    def finished(self) -> bool:
        return self.status in FINAL_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        end = self.finished_at if self.finished_at is not None else time.time()
        duration = None
        if self.started_at is not None:
            duration = round(max(end - self.started_at, 0.0), 3)
        return {
            "id": self.id,
            "experiment": self.experiment,
            "hypothesis": self.hypothesis,
            "status": self.status.value,
            "verdict": self.verdict.value if self.verdict else None,
            "reason": self.reason,
            "dry_run": self.dry_run,
            "principal": self.principal,
            "role": self.role,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "duration_seconds": duration,
            "injections_started": self.injections_started,
            "window_closes_at": time_windows.iso(self.window_closes_at),
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "recovery": self.recovery.to_dict() if self.recovery else None,
        }


class ExperimentRunner:
    """
    Runs one experiment at a time on a background thread.

    Args:
        kill_switch_provider: Returns the kill switch that gates chaos right now,
            or ``None``. Called while an experiment runs, so a swapped kill
            switch is honoured.
        injector: ``(name, cfg, dry_run) -> None``, passed to the engine.
            Defaults to the real injectors. Injectable for tests.
        history_size: How many runs (running and finished) to remember.
        clock: Returns the current time (timezone-aware) for the time windows.
            Defaults to the real clock. Injectable for tests.
    """

    def __init__(
        self,
        kill_switch_provider: Optional[Callable[[], Optional[KillSwitch]]] = None,
        injector: Optional[Callable[[str, Mapping[str, Any], bool], None]] = None,
        history_size: int = HISTORY_SIZE,
        clock: Optional[Callable[[], dt.datetime]] = None,
    ) -> None:
        self._ks_provider = kill_switch_provider or (lambda: None)
        self._injector = injector
        self._clock = clock
        self._lock = threading.RLock()
        self._history: Deque[ExperimentRun] = deque(maxlen=history_size)
        self._current: Optional[ExperimentRun] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._abort_reason = "aborted"
        self._shutting_down = False

    # -- state -------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._current is not None

    def current(self) -> Optional[Dict[str, Any]]:
        """The run in progress, or ``None``."""
        with self._lock:
            return self._current.to_dict() if self._current else None

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for run in self._history:
                if run.id == run_id:
                    return run.to_dict()
        return None

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Remembered runs, newest first."""
        with self._lock:
            runs = list(self._history)
        runs.reverse()
        return [r.to_dict() for r in runs[: max(limit, 0)]]

    # -- control -------------------------------------------------------------------

    def start(
        self,
        experiment: Mapping[str, Any],
        principal: str = "unknown",
        role: Optional[str] = None,
        dry_run: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Start ``experiment`` (a validated experiment dict) and return the new run.

        ``dry_run`` overrides the experiment's own setting when given. Raises
        :class:`ExperimentBusy` if another experiment is running, and
        :class:`OutsideWindow` if its time windows are closed or too short for
        it. A dry run changes nothing, so the windows do not apply to it.
        """
        # A private copy: a config reload while the experiment runs must not
        # change it halfway.
        exp = copy.deepcopy(dict(experiment))
        effective_dry_run = exp["dry_run"] if dry_run is None else dry_run
        with self._lock:
            if self._current is not None:
                raise ExperimentBusy(
                    f"Experiment '{self._current.experiment}' is already running"
                )
            if not effective_dry_run:
                self._check_window(exp)
            run = ExperimentRun(
                id=uuid.uuid4().hex[:12],
                experiment=exp["name"],
                hypothesis=exp["hypothesis"],
                dry_run=effective_dry_run,
                principal=principal,
                role=role,
                created_at=time.time(),
            )
            self._cancel.clear()
            self._abort_reason = "aborted"
            self._shutting_down = False
            self._current = run
            self._history.append(run)
            EXPERIMENT_ACTIVE.set(1)
            thread = threading.Thread(
                target=self._execute,
                args=(run, exp),
                daemon=True,
                name=f"experiment-{run.id}",
            )
            self._thread = thread
            snapshot = run.to_dict()
        thread.start()
        return snapshot

    def abort(self, reason: str = "aborted by an operator") -> bool:
        """
        Cut the running experiment short. Its injections are aborted and their
        effects removed. Returns False if nothing is running.
        """
        with self._lock:
            if self._current is None:
                return False
            self._abort_reason = reason
            self._cancel.set()
        return True

    def stop(self, timeout: float = 15.0, reason: str = "agent shutting down") -> bool:
        """Abort the running experiment (if any) and wait for it to wind down."""
        with self._lock:
            self._shutting_down = True
        self.abort(reason)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return not self.is_running

    def join(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # -- time windows ----------------------------------------------------------------

    def _now(self) -> dt.datetime:
        return self._clock() if self._clock else time_windows.utcnow()

    def _check_window(self, exp: Mapping[str, Any]) -> None:
        """Raise OutsideWindow unless the chaos can start now and finish in time."""
        spec = exp.get("windows")
        if not spec:
            return
        now = self._now()
        status = time_windows.evaluate(spec, now)
        if not status.open:
            raise OutsideWindow(
                "Outside the allowed time window; the next one opens "
                + time_windows.format_local(status.opens_at, spec)
            )
        assert status.closes_at is not None
        needed = chaos_budget_seconds(exp)
        remaining = (status.closes_at - now).total_seconds()
        if needed > remaining:
            raise OutsideWindow(
                f"Not enough time left in the allowed window: it closes "
                f"{time_windows.format_local(status.closes_at, spec)}, in "
                f"{int(remaining)}s, and this experiment needs up to "
                f"{int(needed)}s of chaos"
            )

    # -- the run -------------------------------------------------------------------

    def _update(self, run: ExperimentRun, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(run, key, value)

    def _execute(self, run: ExperimentRun, exp: Dict[str, Any]) -> None:
        who: Dict[str, Any] = {"principal": run.principal, "role": run.role}
        try:
            self._update(
                run, status=RunStatus.CHECKING_BASELINE, started_at=time.time()
            )
            logger.info(
                "Experiment started",
                extra={
                    "experiment": run.experiment,
                    "run_id": run.id,
                    "dry_run": run.dry_run,
                },
            )
            audit.record(
                "experiment.started",
                experiment=run.experiment,
                run_id=run.id,
                dry_run=run.dry_run,
                hypothesis=run.hypothesis,
                **who,
            )
            self._run_phases(run, exp)
        except Exception as e:
            logger.error(
                "Experiment failed",
                exc_info=True,
                extra={"experiment": run.experiment, "run_id": run.id},
            )
            self._finish(
                run,
                RunStatus.ERROR,
                Verdict.INCONCLUSIVE,
                f"internal error: {type(e).__name__}",
            )
        finally:
            with self._lock:
                self._current = None
            EXPERIMENT_ACTIVE.set(0)
            audit.record(
                "experiment.finished",
                experiment=run.experiment,
                run_id=run.id,
                status=run.status.value,
                verdict=run.verdict.value if run.verdict else None,
                reason=run.reason,
                injections_started=run.injections_started,
                **who,
            )
            logger.info(
                "Experiment finished",
                extra={
                    "experiment": run.experiment,
                    "run_id": run.id,
                    "status": run.status.value,
                    "verdict": run.verdict.value if run.verdict else None,
                },
            )

    def _finish(
        self,
        run: ExperimentRun,
        status: RunStatus,
        verdict: Verdict,
        reason: Optional[str] = None,
    ) -> None:
        self._update(
            run,
            status=status,
            verdict=verdict,
            reason=reason,
            finished_at=time.time(),
        )
        EXPERIMENTS_TOTAL.labels(status=status.value, verdict=verdict.value).inc()

    def _run_phases(self, run: ExperimentRun, exp: Dict[str, Any]) -> None:
        steady_state = exp["steady_state"]

        # 1. Baseline
        baseline = wait_for_steady_state(
            steady_state, steady_state["baseline_timeout_seconds"], self._cancel
        )
        self._update(run, baseline=baseline)
        if self._cancel.is_set():
            self._finish(
                run, RunStatus.ABORTED, Verdict.INCONCLUSIVE, self._abort_reason
            )
            return
        if not baseline.reached:
            self._finish(
                run,
                RunStatus.REFUSED,
                Verdict.INCONCLUSIVE,
                "the target was not in its steady state before the experiment, "
                "so no chaos was injected",
            )
            return

        # The baseline can take a while: make sure the window is still open now.
        window_closes_at = None
        spec = exp.get("windows")
        if spec and not run.dry_run:
            status = time_windows.evaluate(spec, self._now())
            if not status.open:
                self._finish(
                    run,
                    RunStatus.REFUSED,
                    Verdict.INCONCLUSIVE,
                    "the allowed time window closed before the chaos could start",
                )
                return
            window_closes_at = status.closes_at
            self._update(run, window_closes_at=window_closes_at)

        # 2. Chaos
        self._update(run, status=RunStatus.RUNNING)
        abort_reason = self._inject(run, exp, window_closes_at)
        if self._shutting_down or lifecycle.is_shutting_down():
            self._finish(
                run, RunStatus.ABORTED, Verdict.INCONCLUSIVE, abort_reason or "shutdown"
            )
            return

        # 3. Recovery. After an abort there is no point waiting for the target to
        # come back: look once and record what is there.
        self._update(run, status=RunStatus.RECOVERING)
        timeout = 0 if abort_reason else steady_state["recovery_timeout_seconds"]
        recovery = wait_for_steady_state(steady_state, timeout, self._cancel)
        self._update(run, recovery=recovery)
        if abort_reason is None and recovery.cancelled and not recovery.reached:
            abort_reason = self._abort_reason

        # 4. Verdict
        if abort_reason:
            self._finish(run, RunStatus.ABORTED, Verdict.INCONCLUSIVE, abort_reason)
        elif recovery.reached:
            self._finish(run, RunStatus.COMPLETED, Verdict.CONFIRMED)
        else:
            self._finish(
                run,
                RunStatus.COMPLETED,
                Verdict.FALSIFIED,
                "the steady state was not restored within "
                f"{steady_state['recovery_timeout_seconds']}s of the chaos ending",
            )

    def _inject(
        self,
        run: ExperimentRun,
        exp: Dict[str, Any],
        window_closes_at: Optional[dt.datetime] = None,
    ) -> Optional[str]:
        """
        Run the chaos phase. Returns why it was cut short, or ``None`` if it ran
        its schedule to the end. Always leaves the engine stopped.
        """
        schedule = exp["schedule"]
        faults = exp["faults"]
        settings = Settings(
            interval_seconds=schedule["interval_seconds"],
            dry_run=run.dry_run,
            failures=faults,
        )
        engine = ChaosEngine(
            settings_provider=lambda: settings,
            kill_switch_provider=self._ks_provider,
            injector=self._injector,
            max_injections=schedule["max_injections"],
            audit_context={
                "principal": run.principal,
                "role": run.role,
                "source": "experiment",
                "experiment": run.experiment,
                "run_id": run.id,
            },
            name=f"experiment-{run.id}",
        )
        engine.start()
        try:
            abort_reason = self._hold(
                engine, schedule["duration_seconds"], run.dry_run, window_closes_at
            )
            if abort_reason is None:
                longest = max(
                    (f.get("duration_seconds", 0) for f in faults.values()), default=0
                )
                clean = engine.stop(
                    timeout=longest + _FINISH_GRACE_SECONDS,
                    reason="experiment finished",
                    abort_running=False,
                )
                if not clean:
                    abort_reason = "injections did not finish in time and were aborted"
            if abort_reason is not None:
                engine.stop(timeout=_ABORT_TIMEOUT_SECONDS, reason=abort_reason)
        finally:
            # Whatever happened above, never leave an engine (and so chaos) behind.
            if engine.is_running or engine.in_flight():
                engine.stop(timeout=_ABORT_TIMEOUT_SECONDS, reason="experiment ended")
            self._update(run, injections_started=engine.injections_started)
        return abort_reason

    def _hold(
        self,
        engine: ChaosEngine,
        duration: float,
        dry_run: bool,
        window_closes_at: Optional[dt.datetime] = None,
    ) -> Optional[str]:
        """Wait until the chaos phase should end. Returns an abort reason or None."""
        deadline = time.monotonic() + duration
        while True:
            if self._cancel.is_set():
                return self._abort_reason
            if lifecycle.is_shutting_down():
                return "agent shutting down"
            if window_closes_at is not None and self._now() >= window_closes_at:
                return "the allowed time window closed"
            ks = self._ks_provider()
            if ks is not None and not dry_run:
                allowed, why = ks.allows_injection()
                if not allowed:
                    return f"stopped by the kill switch: {why}"
            if not engine.is_running or time.monotonic() >= deadline:
                return None
            self._cancel.wait(_POLL_SECONDS)
