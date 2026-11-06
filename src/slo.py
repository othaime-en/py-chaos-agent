"""
SLO abort: stop an experiment when the system leaves its limits.

An experiment's ``slo`` block (``SloConfig`` in ``src.schemas``) lists checks, of the
same kinds the steady state uses (http and Prometheus), that must hold *while the
chaos runs*. A :class:`SloMonitor` evaluates them every ``check_interval_seconds``
on its own thread, so a slow metrics server can never stall the runner's watch loop.

Deciding that the limits are breached:

* A round passes only if every check passes.
* ``breach_after`` failing rounds *in a row* trip the monitor. One passing round
  resets the count, so a single noisy sample does not end an experiment.
* ``grace_seconds`` delays the first round, for metrics that need time to show an
  effect (a ``rate(...[1m])`` window, for example).
* What tripped it decides what the run means. If any check failed by evaluating and
  violating its limit, that is a **breach**: the hypothesis was falsified. If every
  failing check could not be evaluated at all (the metrics server is down, the query
  is rejected), that is **unavailable**: nothing can be concluded, but carrying on
  blind is not safe either, so the run still stops.

The monitor only reports. The runner polls :attr:`SloMonitor.breach` and does the
aborting, so every stop condition goes through the same path.
"""

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from .logging_config import get_logger
from .steady_state import CheckResult, run_round

logger = get_logger(__name__)

_STOP_JOIN_SECONDS = 2.0

BREACH = "breach"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Breach:
    """Why the SLO monitor tripped."""

    kind: str  # BREACH or UNAVAILABLE
    check: str
    detail: str
    round: int

    def reason(self) -> str:
        if self.kind == BREACH:
            return f"SLO breached: {self.check}: {self.detail}"
        return f"SLO could not be checked: {self.check}: {self.detail}"


class SloMonitor:
    """
    Evaluates an experiment's SLO checks in the background.

    Args:
        slo: A validated ``slo`` dict (``checks``, ``check_interval_seconds``,
            ``breach_after``, ``grace_seconds``).
        rounds: ``(checks) -> results``; defaults to running them for real.
            Injectable for tests.
    """

    def __init__(
        self,
        slo: Mapping[str, Any],
        rounds: Callable[[List[Mapping[str, Any]]], List[CheckResult]] = run_round,
    ) -> None:
        self._checks = list(slo["checks"])
        self._interval = slo["check_interval_seconds"]
        self._breach_after = slo["breach_after"]
        self._grace = slo["grace_seconds"]
        self._rounds = rounds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._count = 0
        self._streak = 0
        self._last: List[CheckResult] = []
        self._breach: Optional[Breach] = None

    # -- control -------------------------------------------------------------

    def start(self) -> None:
        thread = threading.Thread(target=self._run, daemon=True, name="slo-monitor")
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop evaluating. A round already under way is allowed to finish quietly."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_STOP_JOIN_SECONDS)

    # -- state ---------------------------------------------------------------

    @property
    def breach(self) -> Optional[Breach]:
        with self._lock:
            return self._breach

    def summary(self) -> Dict[str, Any]:
        """A snapshot for the run record: rounds so far, the last round, any breach."""
        with self._lock:
            breach = self._breach
            return {
                "rounds": self._count,
                "streak": self._streak,
                "breach_after": self._breach_after,
                "breached": breach is not None,
                "kind": breach.kind if breach else None,
                "check": breach.check if breach else None,
                "detail": breach.detail if breach else None,
                "last": [r.to_dict() for r in self._last],
            }

    # -- the loop ------------------------------------------------------------

    def _run(self) -> None:
        if self._grace and self._stop.wait(self._grace):
            return
        while not self._stop.is_set():
            results = self._rounds(self._checks)
            if self._stop.is_set():
                break  # the chaos ended while this round ran; it says nothing now
            if self._record(results):
                break
            if self._stop.wait(self._interval):
                break

    def _record(self, results: List[CheckResult]) -> bool:
        """Add one round's results. Returns True if the monitor has now tripped."""
        failed = [r for r in results if not r.ok]
        with self._lock:
            self._count += 1
            self._last = results
            self._streak = self._streak + 1 if failed else 0
            if self._streak < self._breach_after:
                return False
            violated = [r for r in failed if not r.unavailable]
            culprit = violated[0] if violated else failed[0]
            self._breach = Breach(
                kind=BREACH if violated else UNAVAILABLE,
                check=culprit.name,
                detail=culprit.detail,
                round=self._count,
            )
            breach = self._breach
        logger.warning(
            "SLO monitor tripped",
            extra={
                "kind": breach.kind,
                "check": breach.check,
                "detail": breach.detail,
                "rounds": breach.round,
            },
        )
        return True
