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

If the experiment has a target selector (see ``src.targets``) it only runs on an
agent whose labels match, is refused if they stop matching during the baseline,
and is aborted if they stop matching mid-run. Removing the label from the pod is
therefore a remote off switch.

If the experiment has time windows (see ``src.windows``) it will not start
outside them or inside a blackout, or when the window will close before its chaos
could finish, and a run whose window closes mid-way is aborted.

Whether this agent may run an experiment at all is decided by ``src.selection``:
the agent-wide opt-in, the selector, the windows and the sample, in that order.
A dry run is never refused for any of them. It runs, and its ``selection`` records
what a real run would have been told.

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
from . import selection as selecting
from . import targets
from . import windows as time_windows
from .selection import chaos_budget_seconds  # noqa: F401  (re-exported)
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


class TargetMismatch(RuntimeError):
    """The experiment's selector does not match this agent's labels."""


class OutsideWindow(RuntimeError):
    """The experiment may not start now: its allowed time window is closed, or
    will close before the chaos could finish."""


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
    target_labels: Optional[Dict[str, str]] = None
    selection: Optional[Dict[str, Any]] = None
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
            "selection": self.selection,
            "target_labels": (
                dict(self.target_labels) if self.target_labels is not None else None
            ),
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
        labels_provider: Returns this agent's labels for target selectors.
            Defaults to the labels from the environment (``src.targets``).
        eligibility_provider: Returns the agent-wide eligibility rule (a selector
            dict) or ``None``. Chaos only runs while the labels satisfy it.
        agent_id: This agent's identity for sampling. Defaults to
            ``CHAOS_TARGET_ID`` or the host name.
    """

    def __init__(
        self,
        kill_switch_provider: Optional[Callable[[], Optional[KillSwitch]]] = None,
        injector: Optional[Callable[[str, Mapping[str, Any], bool], None]] = None,
        history_size: int = HISTORY_SIZE,
        clock: Optional[Callable[[], dt.datetime]] = None,
        labels_provider: Optional[Callable[[], Mapping[str, str]]] = None,
        eligibility_provider: Optional[
            Callable[[], Optional[Mapping[str, Any]]]
        ] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        self._ks_provider = kill_switch_provider or (lambda: None)
        self._injector = injector
        self._clock = clock
        self._labels_provider = labels_provider
        self._eligibility_provider = eligibility_provider
        self._agent_id = agent_id
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
        sample_key: str = "",
    ) -> Dict[str, Any]:
        """
        Start ``experiment`` (a validated experiment dict) and return the new run.

        ``dry_run`` overrides the experiment's own setting when given;
        ``sample_key`` picks which agents fall in a ``sample_percent`` sample.

        Raises :class:`ExperimentBusy` if another experiment is running,
        :class:`TargetMismatch` if this agent is not eligible, not selected, or
        not in the sample, and :class:`OutsideWindow` if its time windows are
        closed or too short for it. A dry run changes nothing, so it is never
        refused for those reasons: it runs, and the run's ``selection`` says what
        a real run would have been told.
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
            selection = self.select(exp, sample_key)
            if not effective_dry_run:
                self._enforce(selection)
            run = ExperimentRun(
                id=uuid.uuid4().hex[:12],
                experiment=exp["name"],
                hypothesis=exp["hypothesis"],
                dry_run=effective_dry_run,
                principal=principal,
                role=role,
                created_at=time.time(),
                selection=selection.to_dict(),
                target_labels=(
                    dict(self.target_labels())
                    if exp.get("selector") and not effective_dry_run
                    else None
                ),
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

    # -- where and when ------------------------------------------------------------

    def target_labels(self) -> Dict[str, str]:
        """This agent's labels right now."""
        if self._labels_provider is not None:
            return dict(self._labels_provider())
        return targets.agent_labels.get()

    def _now(self) -> dt.datetime:
        return self._clock() if self._clock else time_windows.utcnow()

    def eligibility(self) -> Optional[Mapping[str, Any]]:
        """The agent-wide eligibility rule, or ``None``."""
        return self._eligibility_provider() if self._eligibility_provider else None

    def agent_id(self) -> str:
        return self._agent_id or targets.agent_id()

    def select(
        self,
        exp: Mapping[str, Any],
        sample_key: str = "",
        kill_switch_reason: Optional[str] = None,
        busy_reason: Optional[str] = None,
        report_state: bool = False,
    ) -> selecting.Selection:
        """
        Every selection gate for ``exp`` right now, with the reason for each (see
        :mod:`src.selection`). Changes nothing; this is what a dry run and the
        preview endpoint report.
        """
        return selecting.evaluate(
            exp,
            labels=self.target_labels(),
            now=self._now(),
            eligibility=self.eligibility(),
            agent_id=self.agent_id(),
            sample_key=sample_key,
            kill_switch_reason=kill_switch_reason,
            busy_reason=busy_reason,
            report_state=report_state,
        )

    @staticmethod
    def _enforce(selection: selecting.Selection) -> None:
        """Raise for the first gate that failed, in the order they are checked."""
        for gate in selection.failed:
            if gate.name == "eligibility":
                raise TargetMismatch(
                    "This agent is not eligible for chaos: " + gate.detail
                )
            if gate.name == "selector":
                raise TargetMismatch(
                    "This agent is not a target of the experiment: " + gate.detail
                )
            if gate.name == "window":
                raise OutsideWindow(gate.detail)
            if gate.name == "sample":
                raise TargetMismatch(
                    "This agent is not in the experiment's sample: " + gate.detail
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
                target_labels=run.target_labels,
                selection=run.selection,
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

        # The baseline can take a while: make sure we may still go ahead.
        window_closes_at = None
        selector = exp.get("selector")
        if not run.dry_run:
            problem = selecting.eligibility_problem(
                self.eligibility(), self.target_labels()
            )
            if problem:
                self._finish(
                    run,
                    RunStatus.REFUSED,
                    Verdict.INCONCLUSIVE,
                    "this agent is no longer eligible for chaos, so no chaos was "
                    "injected: " + problem,
                )
                return
            result = targets.match(selector, self.target_labels())
            if not result.matches:
                self._finish(
                    run,
                    RunStatus.REFUSED,
                    Verdict.INCONCLUSIVE,
                    "the target's labels no longer match the selector, so no "
                    "chaos was injected: " + result.summary,
                )
                return
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
        guard = self._guard(selector, window_closes_at, run.dry_run)
        abort_reason = self._inject(run, exp, guard)
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

    def _guard(
        self,
        selector: Optional[Mapping[str, Any]],
        window_closes_at: Optional[dt.datetime],
        dry_run: bool,
    ) -> Callable[[], Optional[str]]:
        """
        A check run while the chaos is going: returns why it must stop now, or
        ``None``. Covers the time window closing, the agent no longer being
        eligible, and the target's labels no longer matching the selector
        (someone removed a label: the remote off switch).
        """

        def check() -> Optional[str]:
            if dry_run:
                return None
            if window_closes_at is not None and self._now() >= window_closes_at:
                return "the allowed time window closed"
            labels = self.target_labels()
            problem = selecting.eligibility_problem(self.eligibility(), labels)
            if problem:
                return "this agent is no longer eligible for chaos: " + problem
            if selector:
                result = targets.match(selector, labels)
                if not result.matches:
                    return (
                        "the target's labels no longer match the selector: "
                        + result.summary
                    )
            return None

        return check

    def _inject(
        self,
        run: ExperimentRun,
        exp: Dict[str, Any],
        guard: Optional[Callable[[], Optional[str]]] = None,
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
            max_concurrent=schedule.get("max_concurrent_injections"),
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
            # Keep watching the whole time, including after the schedule ends while
            # the last injections finish: an abort, the kill switch, a closing
            # window or a removed label must stop the chaos then too.
            deadline = time.monotonic() + schedule["duration_seconds"]
            abort_reason = self._watch(
                lambda: not engine.is_running or time.monotonic() >= deadline,
                run.dry_run,
                guard,
            )
            if abort_reason is None:
                engine.halt()
                longest = max(
                    (f.get("duration_seconds", 0) for f in faults.values()), default=0
                )
                drain_deadline = time.monotonic() + longest + _FINISH_GRACE_SECONDS
                abort_reason = self._watch(
                    lambda: (not engine.is_running and engine.in_flight() == 0)
                    or time.monotonic() >= drain_deadline,
                    run.dry_run,
                    guard,
                )
                if abort_reason is None and (engine.is_running or engine.in_flight()):
                    abort_reason = "injections did not finish in time and were aborted"
            if abort_reason is not None:
                engine.stop(timeout=_ABORT_TIMEOUT_SECONDS, reason=abort_reason)
        finally:
            # Whatever happened above, never leave an engine (and so chaos) behind.
            if engine.is_running or engine.in_flight():
                engine.stop(timeout=_ABORT_TIMEOUT_SECONDS, reason="experiment ended")
            self._update(run, injections_started=engine.injections_started)
        return abort_reason

    def _watch(
        self,
        done: Callable[[], bool],
        dry_run: bool,
        guard: Optional[Callable[[], Optional[str]]] = None,
    ) -> Optional[str]:
        """
        Wait until ``done()``, watching for reasons to stop early. Returns that
        reason (an abort, shutdown, a guard such as the window or labels, or the
        kill switch), or ``None`` if ``done()`` came first.
        """
        while True:
            if self._cancel.is_set():
                return self._abort_reason
            if lifecycle.is_shutting_down():
                return "agent shutting down"
            if guard is not None:
                stop = guard()
                if stop:
                    return stop
            ks = self._ks_provider()
            if ks is not None and not dry_run:
                allowed, why = ks.allows_injection()
                if not allowed:
                    return f"stopped by the kill switch: {why}"
            if done():
                return None
            self._cancel.wait(_POLL_SECONDS)
