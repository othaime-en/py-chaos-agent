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

import json
import math
import operator
import os
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
    # True when the check could not be evaluated at all (a metrics server that is
    # down, a rejected query) as opposed to evaluating and failing. Only
    # Prometheus checks can be unavailable; an http check that fails is a failure.
    unavailable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }
        if self.unavailable:
            data["unavailable"] = True
        return data


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


_OPERATORS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


class _Unavailable(Exception):
    """The query could not be answered; the message says why."""


def _query_value(payload: Any) -> Optional[float]:
    """
    The single number a Prometheus instant-query response holds, or ``None`` for
    "no data". Raises _Unavailable for anything that is not exactly one value.
    """
    if not isinstance(payload, dict):
        raise _Unavailable("response is not a Prometheus result")
    if payload.get("status") != "success":
        raise _Unavailable(f"Prometheus error: {payload.get('errorType', 'unknown')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _Unavailable("response has no data")
    kind = data.get("resultType")
    result: Any = data.get("result")
    try:
        if kind == "scalar":
            text = result[1]
        elif kind == "vector":
            if not isinstance(result, list) or len(result) == 0:
                return None
            if len(result) > 1:
                raise _Unavailable(
                    f"query returned {len(result)} series, expected 1 "
                    "(aggregate it with sum, avg or max)"
                )
            text = result[0]["value"][1]
        else:
            raise _Unavailable(f"unsupported result type '{kind}'")
        value = float(text)
    except _Unavailable:
        raise
    except (KeyError, IndexError, TypeError, ValueError):
        raise _Unavailable("response is not a number") from None
    # 0/0 and similar come back as NaN: that is "no data", not a value.
    return None if math.isnan(value) else value


def run_prometheus_check(check: Mapping[str, Any]) -> CheckResult:
    """
    Run one Prometheus check. Never raises.

    The check passes while ``value <operator> threshold`` holds. A value that
    violates it is a failure; anything that stops the query being answered (the
    server is down, a rejected query, several series, no data unless
    ``on_no_data: pass``) is *unavailable*.
    """
    name = str(check.get("name", "check"))
    timeout = check.get("timeout_seconds", 5)

    def unavailable(detail: str, latency: Optional[int] = None) -> CheckResult:
        return CheckResult(name, False, detail, latency, unavailable=True)

    headers: Dict[str, str] = {}
    token_env = check.get("bearer_token_env")
    if token_env:
        token = os.environ.get(token_env, "")
        if not token:
            return unavailable(f"{token_env} is not set")
        headers["Authorization"] = f"Bearer {token}"

    started = time.monotonic()
    limit = 1024 * 1024
    try:
        response = requests.get(
            check["url"].rstrip("/") + "/api/v1/query",
            params={"query": check["query"]},
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        try:
            status = response.status_code
            raw = response.raw.read(limit + 1, decode_content=True)
        finally:
            response.close()
    except requests.exceptions.Timeout:
        return unavailable(f"timed out after {timeout}s")
    except requests.exceptions.RequestException as e:
        return unavailable(type(e).__name__)
    except Exception as e:  # a broken probe must not look like a healthy target
        logger.error("Unexpected error in Prometheus check", exc_info=True)
        return unavailable(f"unexpected {type(e).__name__}")
    latency_ms = int((time.monotonic() - started) * 1000)

    if status != 200:
        return unavailable(f"HTTP {status}", latency_ms)
    if len(raw) > limit:
        return unavailable("response too large", latency_ms)
    try:
        value = _query_value(json.loads(raw))
    except _Unavailable as e:
        return unavailable(str(e), latency_ms)
    except ValueError:
        return unavailable("response is not JSON", latency_ms)

    if value is None:
        if check.get("on_no_data") == "pass":
            return CheckResult(name, True, "no data (treated as passing)", latency_ms)
        return unavailable("no data returned", latency_ms)

    op = check["operator"]
    threshold = float(check["threshold"])
    if _OPERATORS[op](value, threshold):
        return CheckResult(name, True, f"{value:g} {op} {threshold:g}", latency_ms)
    return CheckResult(name, False, f"{value:g} is not {op} {threshold:g}", latency_ms)


_RUNNERS: Dict[str, Callable[[Mapping[str, Any]], CheckResult]] = {
    "http": run_http_check,
    "prometheus": run_prometheus_check,
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
