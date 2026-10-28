"""
Kill switch tests. A fake clock and a fake health check make every state
transition deterministic; the monitor thread and the real HTTP check are
covered separately.
"""

import http.server
import threading
import time

import pytest
import requests

from src.kill_switch import (
    CircuitState,
    FailureContext,
    KillSwitch,
    KillSwitchConfig,
    KillSwitchOpen,
    wrap_injection_with_context,
)
from src.metrics import KILL_SWITCH_STATE, KILL_SWITCH_TRIPS, metric_value


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Target:
    """A controllable stand-in for the target's health."""

    def __init__(self, healthy=True):
        self.healthy = healthy
        self.checks = 0

    def __call__(self):
        self.checks += 1
        return self.healthy


def make(
    clock=None,
    target=None,
    armed=True,
    **overrides,
):
    clock = clock or Clock()
    target = target or Target()
    base = dict(
        grace_after_agent_start_seconds=0,
        grace_after_process_kill_seconds=60,
        consecutive_failures_threshold=3,
        recovery_test_interval_seconds=100,
        recovery_success_threshold=3,
    )
    base.update(overrides)
    state = {"armed": armed}
    ks = KillSwitch(
        KillSwitchConfig(**base),
        is_armed=lambda: state["armed"],
        health_check=target,
        clock=clock,
    )
    ks.armed_state = state
    return ks, clock, target


def trip(ks, target):
    target.healthy = False
    for _ in range(ks.config.consecutive_failures_threshold):
        ks._tick()
    assert ks.circuit_state == CircuitState.OPEN


def gauge():
    return metric_value(KILL_SWITCH_STATE)


def trips():
    return metric_value(KILL_SWITCH_TRIPS)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


class TestGating:
    def test_allows_when_closed(self):
        ks, _, _ = make()
        assert ks.allows_injection() == (True, "")
        assert ks.paused is False

    def test_refuses_when_open_with_reason(self):
        ks, _, target = make()
        trip(ks, target)
        allowed, reason = ks.allows_injection()
        assert allowed is False
        assert "open" in reason and "3 consecutive failed health checks" in reason
        assert ks.paused is True

    def test_refuses_when_half_open(self):
        ks, clock, target = make()
        trip(ks, target)
        clock.advance(100)
        ks._tick()
        assert ks.circuit_state == CircuitState.HALF_OPEN
        allowed, reason = ks.allows_injection()
        assert not allowed and "half-open" in reason

    def test_disabled_kill_switch_always_allows(self):
        ks, _, target = make(enabled=False)
        ks.circuit_state = CircuitState.OPEN
        assert ks.allows_injection() == (True, "")

    def test_injection_raises_when_not_allowed(self):
        ks, _, target = make()
        trip(ks, target)
        with pytest.raises(KillSwitchOpen) as exc:
            with ks.injection("cpu"):
                pytest.fail("body must not run")
        assert "open" in exc.value.reason
        assert ks.failure_context.get_active_injections() == set()

    def test_injection_tracks_active_injection(self):
        ks, _, _ = make()
        with ks.injection("network"):
            assert ks.failure_context.get_active_injections() == {"network"}
        assert ks.failure_context.get_active_injections() == set()

    def test_injection_unmarks_on_exception(self):
        ks, _, _ = make()
        with pytest.raises(RuntimeError):
            with ks.injection("cpu"):
                raise RuntimeError("boom")
        assert ks.failure_context.get_active_injections() == set()

    def test_dry_run_is_neither_gated_nor_tracked(self):
        ks, _, target = make()
        trip(ks, target)
        with ks.injection("process", dry_run=True):
            assert ks.failure_context.get_active_injections() == set()
        assert not ks.failure_context.is_in_process_kill_grace_period(60)

    def test_process_injection_starts_grace_period(self):
        ks, _, _ = make()
        with ks.injection("process"):
            pass
        assert ks.failure_context.is_in_process_kill_grace_period(60)


# ---------------------------------------------------------------------------
# Tripping
# ---------------------------------------------------------------------------


class TestTripping:
    def test_healthy_target_never_trips(self):
        ks, _, target = make()
        for _ in range(20):
            ks._tick()
        assert ks.circuit_state == CircuitState.CLOSED
        assert target.checks == 20

    def test_failures_below_threshold_do_not_trip(self):
        ks, _, target = make()
        target.healthy = False
        ks._tick()
        ks._tick()
        assert ks.circuit_state == CircuitState.CLOSED
        assert ks.consecutive_failures == 2

    def test_a_success_resets_the_failure_count(self):
        ks, _, target = make()
        target.healthy = False
        ks._tick()
        ks._tick()
        target.healthy = True
        ks._tick()
        target.healthy = False
        ks._tick()
        ks._tick()
        assert ks.circuit_state == CircuitState.CLOSED

    def test_trips_exactly_at_threshold(self):
        before = trips()
        ks, _, target = make()
        trip(ks, target)
        assert ks.trip_count == 1
        assert ks.last_trip_reason == "3 consecutive failed health checks"
        assert ks.last_trip_time is not None
        assert trips() == before + 1
        assert gauge() == 1

    def test_state_is_open_before_the_callback_runs(self):
        ks, _, target = make()
        seen = []
        ks._on_trip = lambda: seen.append(ks.circuit_state)
        trip(ks, target)
        assert seen == [CircuitState.OPEN]

    def test_callback_called_once_per_trip(self):
        ks, _, target = make()
        calls = []
        ks._on_trip = lambda: calls.append(1)
        trip(ks, target)
        ks._tick()
        ks._tick()
        assert calls == [1]

    def test_failing_callback_still_leaves_the_breaker_open(self):
        ks, _, target = make()

        def boom():
            raise RuntimeError("callback bug")

        ks._on_trip = boom
        trip(ks, target)  # must not raise
        assert ks.allows_injection()[0] is False

    def test_threshold_is_configurable(self):
        ks, _, target = make(consecutive_failures_threshold=1)
        target.healthy = False
        ks._tick()
        assert ks.circuit_state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# Grace periods and idle behavior
# ---------------------------------------------------------------------------


class TestGraceAndIdle:
    def test_no_health_checks_during_startup_grace(self):
        ks, clock, target = make(grace_after_agent_start_seconds=30)
        target.healthy = False
        ks._tick()  # arms, grace starts
        clock.advance(29)
        ks._tick()
        assert target.checks == 0
        assert ks.circuit_state == CircuitState.CLOSED

    def test_checks_begin_after_startup_grace(self):
        ks, clock, target = make(grace_after_agent_start_seconds=30)
        ks._tick()
        clock.advance(30)
        ks._tick()
        assert target.checks == 1

    def test_idle_does_not_check_or_count(self):
        ks, _, target = make(armed=False)
        target.healthy = False
        for _ in range(10):
            ks._tick()
        assert target.checks == 0
        assert ks.circuit_state == CircuitState.CLOSED

    def test_going_idle_clears_failures(self):
        ks, _, target = make()
        target.healthy = False
        ks._tick()
        ks._tick()
        assert ks.consecutive_failures == 2
        ks.armed_state["armed"] = False
        ks._tick()
        assert ks.consecutive_failures == 0

    def test_rearming_restarts_the_startup_grace(self):
        ks, clock, target = make(grace_after_agent_start_seconds=30)
        ks._tick()
        clock.advance(40)
        ks._tick()
        assert target.checks == 1
        ks.armed_state["armed"] = False
        ks._tick()
        ks.armed_state["armed"] = True
        ks._tick()  # newly armed: grace restarts
        clock.advance(10)
        ks._tick()
        assert target.checks == 1

    def test_process_kill_grace_skips_health_checks(self):
        ks, clock, target = make(grace_after_process_kill_seconds=60)
        with ks.injection("process"):
            pass
        target.healthy = False
        for _ in range(5):
            clock.advance(10)
            ks._tick()
        assert target.checks == 0
        assert ks.circuit_state == CircuitState.CLOSED

    def test_checks_resume_after_process_kill_grace(self):
        ks, clock, target = make(grace_after_process_kill_seconds=60)
        with ks.injection("process"):
            pass
        clock.advance(61)
        ks._tick()
        assert target.checks == 1

    def test_other_injections_do_not_earn_a_grace_period(self):
        ks, _, target = make()
        with ks.injection("network"):
            ks._tick()
        assert target.checks == 1


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_waits_the_recovery_interval_without_checking_health(self):
        ks, clock, target = make(recovery_test_interval_seconds=100)
        trip(ks, target)
        checks = target.checks
        clock.advance(99)
        ks._tick()
        assert ks.circuit_state == CircuitState.OPEN
        assert target.checks == checks

    def test_moves_to_half_open_after_the_interval(self):
        ks, clock, target = make(recovery_test_interval_seconds=100)
        trip(ks, target)
        clock.advance(100)
        ks._tick()
        assert ks.circuit_state == CircuitState.HALF_OPEN
        assert gauge() == 2

    def test_closes_after_the_success_threshold(self):
        ks, clock, target = make(recovery_success_threshold=3)
        trip(ks, target)
        clock.advance(100)
        ks._tick()  # -> half-open
        target.healthy = True
        ks._tick()
        ks._tick()
        assert ks.circuit_state == CircuitState.HALF_OPEN
        ks._tick()
        assert ks.circuit_state == CircuitState.CLOSED
        assert gauge() == 0
        assert ks.allows_injection() == (True, "")

    def test_one_failure_in_half_open_reopens_immediately(self):
        ks, clock, target = make(recovery_success_threshold=3)
        trip(ks, target)
        clock.advance(100)
        ks._tick()
        target.healthy = True
        ks._tick()
        ks._tick()
        target.healthy = False
        ks._tick()
        assert ks.circuit_state == CircuitState.OPEN
        assert ks.consecutive_successes == 0

    def test_reopening_restarts_the_wait(self):
        ks, clock, target = make(recovery_test_interval_seconds=100)
        trip(ks, target)
        clock.advance(100)
        ks._tick()
        ks._tick()  # unhealthy -> reopen, timer restarts now
        clock.advance(99)
        ks._tick()
        assert ks.circuit_state == CircuitState.OPEN
        clock.advance(1)
        ks._tick()
        assert ks.circuit_state == CircuitState.HALF_OPEN

    def test_recovery_does_not_count_a_trip(self):
        ks, clock, target = make()
        trip(ks, target)
        clock.advance(100)
        ks._tick()
        ks._tick()  # reopen
        assert ks.trip_count == 1

    def test_closed_again_gets_a_fresh_startup_grace(self):
        ks, clock, target = make(
            grace_after_agent_start_seconds=30, recovery_success_threshold=1
        )
        ks._tick()
        clock.advance(31)
        trip(ks, target)
        clock.advance(100)
        ks._tick()  # half-open
        target.healthy = True
        ks._tick()  # closed
        assert ks.circuit_state == CircuitState.CLOSED
        target.checks = 0
        ks._tick()  # re-armed: new grace
        assert target.checks == 0

    def test_full_cycle_trip_recover_trip_again(self):
        ks, clock, target = make(recovery_success_threshold=2)
        trip(ks, target)
        clock.advance(100)
        ks._tick()
        target.healthy = True
        ks._tick()
        ks._tick()
        assert ks.circuit_state == CircuitState.CLOSED
        trip(ks, target)
        assert ks.trip_count == 2


class TestLatching:
    def test_chaos_resumes_by_default_after_recovery(self):
        ks, clock, target = make(recovery_success_threshold=1)
        trip(ks, target)
        assert ks.latched is False
        clock.advance(100)
        ks._tick()
        target.healthy = True
        ks._tick()
        assert ks.allows_injection()[0] is True

    def test_latches_when_resume_is_disabled(self):
        ks, clock, target = make(
            resume_after_recovery=False, recovery_success_threshold=1
        )
        trip(ks, target)
        assert ks.latched is True
        clock.advance(100)
        ks._tick()
        target.healthy = True
        ks._tick()
        assert ks.circuit_state == CircuitState.CLOSED
        allowed, reason = ks.allows_injection()
        assert not allowed and "latched" in reason

    def test_reset_clears_the_latch(self):
        ks, clock, target = make(
            resume_after_recovery=False, recovery_success_threshold=1
        )
        trip(ks, target)
        clock.advance(100)
        ks._tick()
        target.healthy = True
        ks._tick()
        assert ks.reset() == (True, "kill switch reset")
        assert ks.allows_injection()[0] is True


class TestReset:
    def test_reset_requires_a_healthy_target(self):
        ks, _, target = make()
        trip(ks, target)
        ok, message = ks.reset()
        assert ok is False and "not healthy" in message
        assert ks.circuit_state == CircuitState.OPEN

    def test_reset_closes_when_target_is_healthy(self):
        ks, _, target = make()
        trip(ks, target)
        target.healthy = True
        ok, _ = ks.reset()
        assert ok and ks.circuit_state == CircuitState.CLOSED
        assert ks.consecutive_failures == 0 and not ks.latched

    def test_reset_without_verification_always_closes(self):
        ks, _, target = make()
        trip(ks, target)
        ok, _ = ks.reset(verify=False)
        assert ok and ks.circuit_state == CircuitState.CLOSED

    def test_reset_does_not_clear_the_trip_history(self):
        ks, _, target = make()
        trip(ks, target)
        target.healthy = True
        ks.reset()
        assert ks.trip_count == 1


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status):
        self.status_code = status


@pytest.fixture
def probe():
    """A kill switch using its real HTTP check."""
    return KillSwitch(
        KillSwitchConfig(
            target_health_url="http://target.invalid/health",
            health_check_timeout_seconds=3,
        )
    )


class TestHealthCheck:
    @pytest.mark.parametrize("status", [200, 201, 204, 299])
    def test_2xx_is_healthy(self, probe, monkeypatch, status):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(status))
        assert probe._check_target_health() is True

    @pytest.mark.parametrize("status", [199, 301, 302, 400, 404, 500, 503])
    def test_everything_else_is_unhealthy(self, probe, monkeypatch, status):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(status))
        assert probe._check_target_health() is False

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ConnectionError("refused"),
            requests.exceptions.Timeout("slow"),
            requests.exceptions.SSLError("bad cert"),
        ],
    )
    def test_network_errors_are_unhealthy(self, probe, monkeypatch, exc):
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(requests, "get", boom)
        assert probe._check_target_health() is False

    def test_unexpected_errors_fail_closed(self, probe, monkeypatch):
        """A bug in the safety check must not leave chaos running unprotected."""

        def boom(*a, **k):
            raise ValueError("bug")

        monkeypatch.setattr(requests, "get", boom)
        assert probe._check_target_health() is False

    def test_uses_configured_url_timeout_and_no_redirects(self, probe, monkeypatch):
        seen = {}

        def capture(url, **kwargs):
            seen.update(url=url, **kwargs)
            return FakeResponse(200)

        monkeypatch.setattr(requests, "get", capture)
        probe._check_target_health()
        assert seen == {
            "url": "http://target.invalid/health",
            "timeout": 3,
            "allow_redirects": False,
        }

    def test_records_the_last_result(self, probe, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(503))
        probe._check_target_health()
        last = probe.get_status()["last_health_check"]
        assert last["healthy"] is False and last["detail"] == "HTTP 503"

    def test_probe_counts_checks_in_metrics(self):
        from src.metrics import KILL_SWITCH_HEALTH_CHECKS

        def count(result):
            return metric_value(KILL_SWITCH_HEALTH_CHECKS, result=result)

        ks, _, target = make()
        healthy, unhealthy = count("healthy"), count("unhealthy")
        ks._probe()
        target.healthy = False
        ks._probe()
        assert count("healthy") == healthy + 1
        assert count("unhealthy") == unhealthy + 1


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def real_target():
    handler = type("H", (_Handler,), {"status": 200})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield handler, f"http://127.0.0.1:{server.server_address[1]}/health"
    server.shutdown()
    server.server_close()


class TestRealHttpCheck:
    def test_healthy_then_unhealthy_then_down(self, real_target):
        handler, url = real_target
        ks = KillSwitch(KillSwitchConfig(target_health_url=url))
        assert ks._check_target_health() is True
        handler.status = 500
        assert ks._check_target_health() is False
        handler.status = 204
        assert ks._check_target_health() is True

    def test_connection_refused_is_unhealthy(self):
        ks = KillSwitch(
            KillSwitchConfig(
                target_health_url="http://127.0.0.1:1/health",
                health_check_timeout_seconds=1,
            )
        )
        assert ks._check_target_health() is False


# ---------------------------------------------------------------------------
# Monitor thread
# ---------------------------------------------------------------------------


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestMonitorThread:
    def fast(self, **kw):
        target = Target()
        ks = KillSwitch(
            KillSwitchConfig(
                health_check_interval_seconds=0.05,
                grace_after_agent_start_seconds=0,
                consecutive_failures_threshold=2,
                recovery_test_interval_seconds=0.2,
                recovery_success_threshold=2,
                **kw,
            ),
            health_check=target,
        )
        return ks, target

    def test_real_thread_trips_recovers_and_stops(self):
        ks, target = self.fast()
        calls = []
        ks.start_monitoring(lambda: calls.append(1))
        try:
            target.healthy = False
            assert wait_for(lambda: ks.circuit_state != CircuitState.CLOSED)
            assert calls == [1]
            target.healthy = True
            assert wait_for(lambda: ks.circuit_state == CircuitState.CLOSED)
            assert ks.allows_injection()[0] is True
        finally:
            ks.stop_monitoring()
        assert not ks.get_status()["monitoring_active"]

    def test_start_is_idempotent(self):
        ks, _ = self.fast()
        ks.start_monitoring()
        first = ks._monitor_thread
        ks.start_monitoring()
        try:
            assert ks._monitor_thread is first
            assert (
                sum(
                    t.name == "kill-switch-monitor" and t.is_alive()
                    for t in threading.enumerate()
                )
                >= 1
            )
        finally:
            ks.stop_monitoring()

    def test_stop_returns_promptly(self):
        ks = KillSwitch(
            KillSwitchConfig(health_check_interval_seconds=3600),
            health_check=Target(),
        )
        ks.start_monitoring()
        started = time.time()
        ks.stop_monitoring()
        assert time.time() - started < 3

    def test_disabled_does_not_start_a_thread(self):
        ks = KillSwitch(KillSwitchConfig(enabled=False))
        ks.start_monitoring()
        assert ks._monitor_thread is None

    def test_monitor_survives_an_exception_in_a_tick(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return True

        ks = KillSwitch(
            KillSwitchConfig(
                health_check_interval_seconds=0.05, grace_after_agent_start_seconds=0
            ),
            health_check=flaky,
        )
        ks.start_monitoring()
        try:
            assert wait_for(lambda: calls["n"] >= 3)
        finally:
            ks.stop_monitoring()

    def test_update_config_keeps_breaker_state(self):
        ks, _, target = make()
        trip(ks, target)
        ks.update_config(KillSwitchConfig(consecutive_failures_threshold=9))
        assert ks.circuit_state == CircuitState.OPEN
        assert ks.config.consecutive_failures_threshold == 9


# ---------------------------------------------------------------------------
# Status and compatibility
# ---------------------------------------------------------------------------


class TestStatus:
    def test_shape_when_closed(self):
        ks, _, _ = make()
        status = ks.get_status()
        assert status["circuit_state"] == "closed"
        assert status["allows_injection"] is True
        assert status["paused_reason"] is None
        assert status["trip_count"] == 0
        assert status["seconds_until_recovery_test"] is None

    def test_reports_countdown_to_recovery_test(self):
        ks, clock, target = make(recovery_test_interval_seconds=100)
        trip(ks, target)
        clock.advance(40)
        status = ks.get_status()
        assert status["circuit_state"] == "open"
        assert status["seconds_until_recovery_test"] == 60.0
        assert status["allows_injection"] is False
        assert status["last_trip_reason"]

    def test_lists_active_injections(self):
        ks, _, _ = make()
        with ks.injection("cpu"):
            assert ks.get_status()["active_injections"] == ["cpu"]


class TestCompatibility:
    def test_wrap_injection_with_context_still_works(self):
        ctx = FailureContext()
        seen = []
        result = wrap_injection_with_context(
            "cpu", lambda x: seen.append(ctx.get_active_injections()) or x, ctx, 7
        )
        assert result == 7
        assert seen == [{"cpu"}]
        assert ctx.get_active_injections() == set()
