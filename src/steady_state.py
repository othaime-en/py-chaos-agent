"""
Steady-state probes: is the system healthy right now?

A chaos experiment is only meaningful against a known-good baseline. This module
answers "is the target in its steady state?" by running the checks an experiment
defines (see ``SteadyStateConfig`` in ``src.schemas``) and answers "has it come
back?" by running them repeatedly until they pass or time runs out.

Rules:

* One *round* runs every check once, in parallel, so a round takes as long as
  the slowest check (at most ``timeout_seconds``), not the sum.
* A round passes only if every check in it passes.
* The state is *reached* after ``required_consecutive_successes`` passing rounds
  in a row. One failed round starts the count again, so a flapping target does
  not count as recovered.
* Checks fail closed: a timeout, a connection error, a bad status, a slow
  response, or a bug in the probe itself all count as unhealthy. Only the
  exception class name is recorded, never its message.
* Redirects are not followed, so a check cannot be bounced to another host.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

import requests

from .logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check in one round."""

    name: str
    ok: bool
    detail: str
    latency_ms: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class SteadyStateResult:
    """
    Whether the steady state was reached, and what the last round saw.

    ``rounds`` is how many rounds were run; ``checks`` holds the results of the
    last one. ``cancelled`` is True if the wait was cut short on request.
    """

    reached: bool
    rounds: int
    elapsed_seconds: float
    checks: List[CheckResult] = field(default_factory=list)
    cancelled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reached": self.reached,
            "rounds": self.rounds,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "cancelled": self.cancelled,
            "checks": [c.to_dict() for c in self.checks],
        }


def run_http_check(check: Mapping[str, Any]) -> CheckResult:
    """Run one http check and say whether it passed. Never raises."""
    name = str(check.get("name", "check"))
    timeout = check.get("timeout_seconds", 2)
    started = time.monotonic()
    try:
        # stream=True: only the headers are needed, so a large body is never
        # downloaded. The latency is the time until they arrive.
        response = requests.get(
            check["url"], timeout=timeout, allow_redirects=False, stream=True
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        status = response.status_code
        response.close()
    except requests.exceptions.Timeout:
        return CheckResult(name, False, f"timed out after {timeout}s")
    except requests.exceptions.RequestException as e:
        return CheckResult(name, False, type(e).__name__)
    except Exception as e:  # a broken probe must not look like a healthy target
        logger.error("Unexpected error in steady-state check", exc_info=True)
        return CheckResult(name, False, f"unexpected {type(e).__name__}")

    expected = check.get("expected_status")
    if expected is not None:
        if status != expected:
            detail = f"HTTP {status} (expected {expected})"
            return CheckResult(name, False, detail, latency_ms)
    elif not 200 <= status < 300:
        return CheckResult(name, False, f"HTTP {status} (expected 2xx)", latency_ms)

    limit = check.get("max_latency_ms")
    if limit is not None and latency_ms > limit:
        detail = f"HTTP {status} in {latency_ms} ms (limit {limit} ms)"
        return CheckResult(name, False, detail, latency_ms)
    return CheckResult(name, True, f"HTTP {status} in {latency_ms} ms", latency_ms)


_RUNNERS: Dict[str, Callable[[Mapping[str, Any]], CheckResult]] = {
    "http": run_http_check,
}


def run_check(check: Mapping[str, Any]) -> CheckResult:
    """Run one check of any known type."""
    runner = _RUNNERS.get(str(check.get("type", "http")))
    if runner is None:
        return CheckResult(
            str(check.get("name", "check")),
            False,
            f"unknown check type {check.get('type')!r}",
        )
    return runner(check)


def run_round(checks: List[Mapping[str, Any]]) -> List[CheckResult]:
    """Run every check once, in parallel, and return the results in order."""
    if not checks:
        return []
    if len(checks) == 1:
        return [run_check(checks[0])]
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        return list(pool.map(run_check, checks))


def wait_for_steady_state(
    steady_state: Mapping[str, Any],
    timeout_seconds: float,
    cancel: Optional[threading.Event] = None,
) -> SteadyStateResult:
    """
    Run rounds until the steady state is reached, time runs out, or ``cancel``
    is set.

    ``timeout_seconds`` is how long to keep trying. A run of passing rounds that
    is already under way is allowed to finish (it needs at most
    ``required_consecutive_successes`` rounds), so a timeout of 0 means "one
    look, and a pass can still complete its required rounds". At least one round
    always runs.
    """
    cancel = cancel or threading.Event()
    checks = list(steady_state["checks"])
    interval = steady_state["check_interval_seconds"]
    required = steady_state["required_consecutive_successes"]

    started = time.monotonic()
    streak = 0
    rounds = 0
    results: List[CheckResult] = []
    while True:
        results = run_round(checks)
        rounds += 1
        streak = streak + 1 if all(r.ok for r in results) else 0
        elapsed = time.monotonic() - started
        if streak >= required:
            return SteadyStateResult(True, rounds, elapsed, results)
        if cancel.is_set() or (elapsed >= timeout_seconds and streak == 0):
            break
        if cancel.wait(interval):
            break
    return SteadyStateResult(
        False, rounds, time.monotonic() - started, results, cancelled=cancel.is_set()
    )
