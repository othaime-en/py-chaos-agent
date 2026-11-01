"""Steady-state probes, against a real local HTTP server."""

import threading
import time

import pytest

from tests.http_target import Target

from src.steady_state import (
    run_check,
    run_http_check,
    run_round,
    wait_for_steady_state,
)


@pytest.fixture
def target():
    t = Target()
    yield t
    t.close()


def check(target, **overrides):
    return {
        "type": "http",
        "name": "health",
        "url": target.url,
        "expected_status": None,
        "max_latency_ms": None,
        "timeout_seconds": 2,
        **overrides,
    }


def steady(target, **overrides):
    return {
        "checks": [check(target)],
        "check_interval_seconds": 1,
        "required_consecutive_successes": 1,
        "baseline_timeout_seconds": 0,
        "recovery_timeout_seconds": 0,
        **overrides,
    }


class TestHttpCheck:
    def test_a_2xx_passes(self, target):
        result = run_http_check(check(target))
        assert result.ok and result.name == "health"
        assert "HTTP 200" in result.detail
        assert result.latency_ms is not None

    @pytest.mark.parametrize("status", [199, 300, 404, 500, 503])
    def test_anything_but_2xx_fails_by_default(self, target, status):
        target.status = status
        result = run_http_check(check(target))
        assert not result.ok
        assert f"HTTP {status}" in result.detail

    def test_an_expected_status_replaces_the_2xx_rule(self, target):
        target.status = 204
        assert not run_http_check(check(target, expected_status=200)).ok
        assert run_http_check(check(target, expected_status=204)).ok

    def test_a_slow_answer_fails_the_latency_limit(self, target):
        target.delay = 0.3
        result = run_http_check(check(target, max_latency_ms=100))
        assert not result.ok
        assert "limit 100 ms" in result.detail

    def test_a_fast_answer_meets_the_limit(self, target):
        assert run_http_check(check(target, max_latency_ms=1000)).ok

    def test_a_timeout_fails(self, target):
        target.delay = 1.5
        result = run_http_check(check(target, timeout_seconds=1))
        assert not result.ok
        assert "timed out" in result.detail

    def test_a_refused_connection_fails_without_leaking_the_message(self):
        result = run_http_check(
            {"name": "x", "url": "http://127.0.0.1:1/health", "timeout_seconds": 1}
        )
        assert not result.ok
        assert result.detail == "ConnectionError"
        assert "127.0.0.1" not in result.detail

    def test_redirects_are_not_followed(self, target):
        other = Target()
        try:
            target.status = 302
            target.location = other.url
            result = run_http_check(check(target))
            assert not result.ok
            assert "HTTP 302" in result.detail
            assert other.hits == 0
        finally:
            other.close()

    def test_a_redirect_can_be_the_expected_answer(self, target):
        target.status = 302
        target.location = "http://127.0.0.1:1/never"
        assert run_http_check(check(target, expected_status=302)).ok

    def test_a_broken_probe_counts_as_unhealthy(self, monkeypatch, target):
        def boom(*a, **k):
            raise RuntimeError("secret detail")

        monkeypatch.setattr("src.steady_state.requests.get", boom)
        result = run_http_check(check(target))
        assert not result.ok
        assert result.detail == "unexpected RuntimeError"

    def test_unknown_types_fail_closed(self):
        result = run_check({"type": "nope", "name": "x"})
        assert not result.ok and "unknown check type" in result.detail


class TestRound:
    def test_results_keep_their_order(self, target):
        checks = [check(target, name=n) for n in ("a", "b", "c")]
        assert [r.name for r in run_round(checks)] == ["a", "b", "c"]

    def test_checks_run_in_parallel(self, target):
        target.delay = 0.4
        checks = [check(target, name=f"c{i}") for i in range(4)]
        start = time.monotonic()
        results = run_round(checks)
        assert all(r.ok for r in results)
        assert time.monotonic() - start < 1.2, "checks ran one after another"

    def test_no_checks_is_an_empty_round(self):
        assert run_round([]) == []


class TestWaitForSteadyState:
    def test_a_healthy_target_is_reached_at_once(self, target):
        result = wait_for_steady_state(steady(target), timeout_seconds=5)
        assert result.reached and result.rounds == 1
        assert result.checks[0].ok and not result.cancelled

    def test_an_unhealthy_target_with_no_timeout_is_one_look(self, target):
        target.status = 503
        result = wait_for_steady_state(steady(target), timeout_seconds=0)
        assert not result.reached
        assert result.rounds == 1
        assert target.hits == 1

    def test_it_waits_for_recovery(self, target):
        target.status = 503
        threading.Timer(0.4, lambda: setattr(target, "status", 200)).start()
        result = wait_for_steady_state(steady(target), timeout_seconds=10)
        assert result.reached
        assert result.rounds >= 2

    def test_it_gives_up_at_the_timeout(self, target):
        target.status = 503
        start = time.monotonic()
        result = wait_for_steady_state(steady(target), timeout_seconds=1.5)
        assert not result.reached
        assert 1.4 <= time.monotonic() - start < 4
        assert not result.checks[0].ok

    def test_it_needs_consecutive_passes(self, target):
        result = wait_for_steady_state(
            steady(target, required_consecutive_successes=3), timeout_seconds=10
        )
        assert result.reached and result.rounds == 3

    def test_a_pass_in_progress_may_finish_after_the_timeout(self, target):
        """Timeout 0 is one look, but a passing look can complete its streak."""
        result = wait_for_steady_state(
            steady(target, required_consecutive_successes=2), timeout_seconds=0
        )
        assert result.reached and result.rounds == 2

    def test_a_failure_resets_the_streak(self, target):
        """pass, fail, pass, pass: the first pass must not count."""
        target.script = [200, 503, 200, 200]
        result = wait_for_steady_state(
            steady(target, required_consecutive_successes=2), timeout_seconds=10
        )
        assert result.reached
        assert result.rounds == 4

    def test_every_check_must_pass(self, target):
        other = Target()
        other.status = 500
        try:
            ss = steady(target, checks=[check(target), check(other, name="other")])
            result = wait_for_steady_state(ss, timeout_seconds=0)
            assert not result.reached
            assert [c.ok for c in result.checks] == [True, False]
        finally:
            other.close()

    def test_cancel_stops_the_wait(self, target):
        target.status = 503
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        start = time.monotonic()
        result = wait_for_steady_state(
            steady(target), timeout_seconds=60, cancel=cancel
        )
        assert result.cancelled and not result.reached
        assert time.monotonic() - start < 3

    def test_a_cancelled_wait_still_looks_once(self, target):
        cancel = threading.Event()
        cancel.set()
        result = wait_for_steady_state(
            steady(target), timeout_seconds=60, cancel=cancel
        )
        assert result.rounds == 1
        assert result.reached, "a healthy target is still reported as healthy"

    def test_the_result_serializes(self, target):
        result = wait_for_steady_state(steady(target), timeout_seconds=0)
        data = result.to_dict()
        assert data["reached"] is True
        assert data["checks"][0]["name"] == "health"
        assert set(data) == {
            "reached",
            "rounds",
            "elapsed_seconds",
            "cancelled",
            "checks",
        }
