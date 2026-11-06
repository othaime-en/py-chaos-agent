"""The SLO monitor: when it trips, what it reports, and that it stops cleanly."""

import threading
import time

import pytest

from src.slo import BREACH, UNAVAILABLE, Breach, SloMonitor
from src.steady_state import CheckResult


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def slo(**overrides):
    return {
        "checks": [{"type": "http", "name": "health", "url": "http://x"}],
        "check_interval_seconds": 0.01,
        "breach_after": 2,
        "grace_seconds": 0,
        **overrides,
    }


OK = CheckResult("health", True, "HTTP 200 in 3 ms")
BAD = CheckResult("health", False, "HTTP 503 (expected 2xx)")
GONE = CheckResult("error-rate", False, "ConnectionError", unavailable=True)
OVER = CheckResult("error-rate", False, "0.05 is not < 0.01")


class Script:
    """Rounds that return a prepared sequence, then repeat the last one."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.calls = 0

    def __call__(self, checks):
        self.calls += 1
        index = min(self.calls - 1, len(self.rounds) - 1)
        return self.rounds[index]


@pytest.fixture
def monitors():
    made = []

    def make(config, script):
        m = SloMonitor(config, rounds=script)
        made.append(m)
        return m

    yield make
    for m in made:
        m.stop()


class TestTripping:
    def test_healthy_rounds_never_trip(self, monitors):
        script = Script([OK])
        m = monitors(slo(), script)
        m.start()
        assert wait_for(lambda: script.calls >= 10)
        assert m.breach is None
        assert m.summary()["breached"] is False

    def test_it_trips_after_breach_after_failing_rounds_in_a_row(self, monitors):
        m = monitors(slo(breach_after=3), Script([BAD]))
        m.start()
        assert wait_for(lambda: m.breach is not None)
        assert m.breach.round == 3

    def test_one_failure_is_not_enough(self, monitors):
        script = Script([OK], [BAD], [OK])
        m = monitors(slo(breach_after=2), script)
        m.start()
        assert wait_for(lambda: script.calls >= 8)
        assert m.breach is None

    def test_a_passing_round_resets_the_count(self, monitors):
        # fail, pass, fail, pass ...: never two in a row.
        script = Script([BAD], [OK], [BAD], [OK], [BAD], [OK], [OK])
        m = monitors(slo(breach_after=2), script)
        m.start()
        assert wait_for(lambda: script.calls >= 8)
        assert m.breach is None

    def test_it_trips_on_a_later_run_of_failures(self, monitors):
        m = monitors(slo(breach_after=2), Script([OK], [BAD], [OK], [BAD], [BAD]))
        m.start()
        assert wait_for(lambda: m.breach is not None)
        assert m.breach.round == 5

    def test_breach_after_one_trips_at_once(self, monitors):
        m = monitors(slo(breach_after=1), Script([BAD]))
        m.start()
        assert wait_for(lambda: m.breach is not None)
        assert m.breach.round == 1

    def test_it_stops_evaluating_once_tripped(self, monitors):
        script = Script([BAD])
        m = monitors(slo(breach_after=2), script)
        m.start()
        assert wait_for(lambda: m.breach is not None)
        calls = script.calls
        time.sleep(0.1)
        assert script.calls == calls

    def test_any_failing_check_fails_the_round(self, monitors):
        m = monitors(slo(breach_after=1), Script([OK, OVER]))
        m.start()
        assert wait_for(lambda: m.breach is not None)
        assert m.breach.check == "error-rate"


class TestWhatItMeans:
    def test_a_violated_limit_is_a_breach(self, monitors):
        m = monitors(slo(breach_after=1), Script([OVER]))
        m.start()
        assert wait_for(lambda: m.breach is not None)
        assert m.breach.kind == BREACH
        assert m.breach.reason() == "SLO breached: error-rate: 0.05 is not < 0.01"

    def test_a_failing_http_check_is_a_breach(self, monitors):
        m = monitors(slo(breach_after=1), Script([BAD]))
        m.start()
        assert wait_for(lambda: m.breach is not None)
        assert m.breach.kind == BREACH

    def test_only_unevaluable_checks_make_it_unavailable(self, monitors):
        m = monitors(slo(breach_after=1), Script([GONE]))
        m.start()
        assert wait_for(lambda: m.breach is not None)
        assert m.breach.kind == UNAVAILABLE
        assert (
            m.breach.reason() == "SLO could not be checked: error-rate: ConnectionError"
        )

    def test_a_real_violation_wins_over_an_unavailable_check(self, monitors):
        m = monitors(slo(breach_after=1), Script([GONE, BAD]))
        m.start()
        assert wait_for(lambda: m.breach is not None)
        assert m.breach.kind == BREACH and m.breach.check == "health"

    def test_unavailable_rounds_count_towards_the_streak(self, monitors):
        m = monitors(slo(breach_after=3), Script([GONE]))
        m.start()
        assert wait_for(lambda: m.breach is not None)
        assert m.breach.round == 3 and m.breach.kind == UNAVAILABLE


class TestGrace:
    def test_nothing_is_evaluated_during_the_grace_period(self, monitors):
        script = Script([BAD])
        m = monitors(slo(grace_seconds=0.4, breach_after=1), script)
        m.start()
        time.sleep(0.25)
        assert script.calls == 0 and m.breach is None
        assert wait_for(lambda: m.breach is not None, timeout=3)

    def test_stopping_during_grace_never_evaluates(self, monitors):
        script = Script([BAD])
        m = monitors(slo(grace_seconds=30, breach_after=1), script)
        m.start()
        start = time.monotonic()
        m.stop()
        assert time.monotonic() - start < 3
        assert script.calls == 0


class TestStopping:
    def test_stop_ends_the_thread_promptly(self, monitors):
        m = monitors(slo(check_interval_seconds=30), Script([OK]))
        m.start()
        assert wait_for(lambda: m.summary()["rounds"] >= 1)
        start = time.monotonic()
        m.stop()
        assert time.monotonic() - start < 3
        assert not m._thread.is_alive()

    def test_a_round_finishing_after_stop_is_ignored(self, monitors):
        gate = threading.Event()
        started = threading.Event()

        def slow(checks):
            started.set()
            gate.wait(5)
            return [BAD]

        m = monitors(slo(breach_after=1), slow)
        m.start()
        assert started.wait(5)
        m._stop.set()  # the chaos ended while the query was in flight
        gate.set()
        m._thread.join(5)
        assert (
            m.breach is None
        ), "a result that arrived after the chaos ended must not trip it"

    def test_stop_without_start_is_harmless(self):
        SloMonitor(slo(), rounds=Script([OK])).stop()


class TestSummary:
    def test_before_anything_ran(self):
        s = SloMonitor(slo(), rounds=Script([OK])).summary()
        assert s == {
            "rounds": 0, "streak": 0, "breach_after": 2, "breached": False,
            "kind": None, "check": None, "detail": None, "last": [],
        }  # fmt: skip

    def test_it_reports_the_last_round(self, monitors):
        m = monitors(slo(check_interval_seconds=30), Script([OK]))
        m.start()
        assert wait_for(lambda: m.summary()["rounds"] == 1)
        s = m.summary()
        assert s["last"] == [OK.to_dict()] and s["streak"] == 0

    def test_it_reports_a_breach(self, monitors):
        m = monitors(slo(breach_after=1), Script([OVER]))
        m.start()
        assert wait_for(lambda: m.breach is not None)
        s = m.summary()
        assert s["breached"] and s["kind"] == BREACH and s["check"] == "error-rate"
        assert s["detail"] == "0.05 is not < 0.01"

    def test_the_breach_is_a_value_object(self):
        b = Breach(BREACH, "x", "y", 3)
        assert b == Breach(BREACH, "x", "y", 3)
