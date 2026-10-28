"""The shared chaos engine: overlap, non-blocking ticks, stop, kill switch, config."""

import threading
import time
from types import SimpleNamespace

import pytest

from src import engine as engine_module
from src.engine import (
    ChaosEngine,
    Settings,
    resolve_injector,
    run_injection,
    snapshot_settings,
)
from src.kill_switch import KillSwitch, KillSwitchConfig
from src.lifecycle import lifecycle
from src.metrics import INJECTIONS_TOTAL, metric_value


def failures(**enabled):
    """A failures mapping where each named type is enabled at probability 1.0."""
    return {
        name: {"enabled": on, "probability": 1.0}
        for name, on in {
            "cpu": False,
            "memory": False,
            "process": False,
            "network": False,
            **enabled,
        }.items()
    }


def settings(interval=0.05, dry_run=False, **enabled):
    return Settings(
        interval_seconds=interval, dry_run=dry_run, failures=failures(**enabled)
    )


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def engines():
    """Engines created by a test are stopped afterwards so none leak."""
    created = []

    def make(*args, **kwargs):
        kwargs.setdefault("error_backoff_seconds", 0.05)
        e = ChaosEngine(*args, **kwargs)
        created.append(e)
        return e

    yield make
    for e in created:
        e.stop(timeout=5)


def run_injection_with_guard(name, cfg, dry_run):
    """Behaves like a real injector: asks the lifecycle guard first."""
    lifecycle.begin_or_skip(name)


class Recorder:
    """An injector that records calls and can be told to hold or fail."""

    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.hold = False
        self.fail = None
        self.running = 0
        self.max_running = 0
        self._lock = threading.Lock()

    def __call__(self, name, cfg, dry_run):
        with self._lock:
            self.calls.append((name, dry_run))
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            if self.fail:
                raise self.fail
            if self.hold:
                self.gate.wait(timeout=10)
        finally:
            with self._lock:
                self.running -= 1

    def names(self):
        return [name for name, _ in self.calls]


class TestRunningInjections:
    def test_a_tick_does_not_wait_for_its_injections(self, engines):
        rec = Recorder()
        rec.hold = True
        e = engines(lambda: settings(cpu=True), injector=rec)
        start = time.time()
        result = e.run_tick(settings(cpu=True), 1)
        assert time.time() - start < 1.0
        assert result.started == 1
        assert wait_for(lambda: rec.calls)
        assert e.in_flight() == 1
        rec.gate.set()
        assert wait_for(lambda: e.in_flight() == 0)

    def test_different_types_overlap(self, engines):
        rec = Recorder()
        rec.hold = True
        e = engines(lambda: settings(), injector=rec)
        e.run_tick(settings(cpu=True, memory=True, network=True), 1)
        assert wait_for(lambda: rec.max_running == 3), "injections ran one by one"
        rec.gate.set()

    def test_the_loop_keeps_ticking_while_an_injection_runs(self, engines):
        rec = Recorder()
        rec.hold = True
        e = engines(lambda: settings(interval=0.05, cpu=True), injector=rec)
        e.start()
        assert wait_for(lambda: e.iteration >= 3), "the loop was blocked"
        assert e.in_flight() >= 1
        rec.gate.set()

    def test_the_same_type_never_stacks(self, engines):
        """A tick leaves a type alone while its last injection is running."""
        entered = []
        release = threading.Event()

        def injector(name, cfg, dry_run):
            with lifecycle.injection(name) as ticket:
                if ticket is None:
                    return
                entered.append(name)
                release.wait(timeout=10)

        e = engines(lambda: settings(interval=0.02, cpu=True), injector=injector)
        e.start()
        assert wait_for(lambda: e.iteration >= 5)
        assert entered == ["cpu"], "a second cpu injection started on top of the first"
        release.set()

    def test_a_busy_type_is_not_a_warning_or_a_skip(self, engines, caplog):
        """Waiting for a long injection is normal, so it is neither noisy nor
        counted as a skipped injection."""
        release = threading.Event()

        def injector(name, cfg, dry_run):
            with lifecycle.injection(name):
                release.wait(timeout=10)

        e = engines(lambda: None, injector=injector)
        e.run_tick(settings(cpu=True), 1)
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        caplog.clear()
        results = [e.run_tick(settings(cpu=True), n) for n in (2, 3)]
        assert [r.busy for r in results] == [1, 1]
        assert [r.started for r in results] == [0, 0]
        assert metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="skipped") == 0
        assert not [r for r in caplog.records if r.levelname == "WARNING"]
        release.set()

    def test_the_guard_still_refuses_a_request_that_races_past_the_check(self, engines):
        """Between the tick's check and the worker starting, something else (a
        manual injection) can take the guard; that is refused and counted."""
        e = engines(lambda: None, injector=run_injection_with_guard)
        ticket, _ = lifecycle.begin("cpu")  # taken after the tick's check below
        real = lifecycle.is_active
        try:
            lifecycle.is_active = lambda name: False  # the tick sees it free
            e.run_tick(settings(cpu=True), 1)
        finally:
            lifecycle.is_active = real
        assert wait_for(
            lambda: metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="skipped")
            == 1
        )
        lifecycle.end(ticket)

    def test_dry_runs_ignore_a_running_injection(self, engines):
        rec = Recorder()
        e = engines(lambda: None, injector=rec)
        ticket, _ = lifecycle.begin("cpu")
        result = e.run_tick(settings(dry_run=True, cpu=True), 1)
        assert result.started == 1 and result.busy == 0
        lifecycle.end(ticket)

    def test_probability_zero_never_fires(self, engines, monkeypatch):
        rec = Recorder()
        monkeypatch.setattr(engine_module.random, "random", lambda: 0.0)
        e = engines(lambda: None, injector=rec)
        s = Settings(1, False, {"cpu": {"enabled": True, "probability": 0.0}})
        e.run_tick(s, 1)
        assert rec.calls == []

    def test_disabled_types_are_skipped(self, engines):
        rec = Recorder()
        e = engines(lambda: None, injector=rec)
        e.run_tick(settings(cpu=False, memory=True), 1)
        assert wait_for(lambda: rec.names() == ["memory"])

    def test_a_failing_injection_does_not_stop_the_loop(self, engines):
        rec = Recorder()
        rec.fail = RuntimeError("boom")
        e = engines(lambda: settings(interval=0.02, cpu=True), injector=rec)
        e.start()
        assert wait_for(lambda: len(rec.calls) >= 3)
        assert e.is_running

    def test_dry_run_is_passed_through(self, engines):
        rec = Recorder()
        e = engines(lambda: None, injector=rec)
        e.run_tick(settings(dry_run=True, cpu=True), 1)
        assert wait_for(lambda: rec.calls == [("cpu", True)])


class TestStartAndStop:
    def test_start_twice_is_refused(self, engines):
        e = engines(lambda: settings())
        e.start()
        with pytest.raises(RuntimeError, match="already running"):
            e.start()

    def test_can_restart_after_stop(self, engines):
        e = engines(lambda: settings())
        e.start()
        assert e.stop()
        assert not e.is_running
        e.start()
        assert e.is_running

    def test_stop_when_never_started_is_harmless(self, engines):
        assert engines(lambda: settings()).stop()

    def test_stop_aborts_running_injections_and_waits_for_them(self, engines):
        """The injection's own finally runs before stop() returns."""
        cleaned = threading.Event()

        def injector(name, cfg, dry_run):
            with lifecycle.injection(name) as ticket:
                try:
                    ticket.sleep(30)
                finally:
                    cleaned.set()

        e = engines(lambda: settings(cpu=True), injector=injector)
        e.start()
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        started = time.time()
        assert e.stop(timeout=5) is True
        assert time.time() - started < 3, "stop waited out the injection"
        assert cleaned.is_set()
        assert not lifecycle.is_active("cpu")

    def test_stop_reports_false_when_an_injection_ignores_the_abort(self, engines):
        release = threading.Event()
        e = engines(
            lambda: settings(cpu=True),
            injector=lambda name, cfg, dry: release.wait(timeout=10),
        )
        e.start()
        assert wait_for(lambda: e.in_flight() == 1)
        assert e.stop(timeout=0.2) is False
        release.set()

    def test_process_shutdown_ends_the_loop(self, engines):
        e = engines(lambda: settings(interval=30))
        e.start()
        assert wait_for(lambda: e.iteration >= 1)
        started = time.time()
        lifecycle.shutdown("test")
        e.join(timeout=5)
        assert not e.is_running
        assert time.time() - started < 2, "the loop slept through the shutdown"

    def test_on_exit_runs_when_the_loop_ends(self, engines):
        exited = threading.Event()
        e = engines(lambda: settings(), on_exit=exited.set)
        e.start()
        e.stop()
        assert exited.is_set()

    def test_start_time_and_iteration_are_reported(self, engines):
        e = engines(lambda: settings(interval=0.02))
        assert e.start_time is None
        e.start()
        assert wait_for(lambda: e.iteration >= 2)
        assert e.start_time is not None


class TestBadConfig:
    def test_a_missing_config_is_retried_not_fatal(self, engines):
        state = {"ready": False}
        rec = Recorder()

        def provider():
            return settings(interval=0.02, cpu=True) if state["ready"] else None

        e = engines(provider, injector=rec)
        e.start()
        time.sleep(0.2)
        assert e.is_running and rec.calls == []
        state["ready"] = True
        assert wait_for(lambda: rec.calls), "never recovered once the config appeared"

    def test_a_provider_that_raises_is_retried(self, engines):
        state = {"calls": 0}
        rec = Recorder()

        def provider():
            state["calls"] += 1
            if state["calls"] < 3:
                raise ValueError("config is being reloaded")
            return settings(interval=0.02, cpu=True)

        e = engines(provider, injector=rec)
        e.start()
        assert wait_for(lambda: rec.calls)


class TestKillSwitch:
    def make_ks(self, healthy=False):
        cfg = KillSwitchConfig(
            health_check_interval_seconds=3600,
            grace_after_agent_start_seconds=0,
            recovery_test_interval_seconds=0,
            recovery_success_threshold=1,
            consecutive_failures_threshold=1,
        )
        target = SimpleNamespace(healthy=healthy)
        ks = KillSwitch(cfg, is_armed=lambda: True, health_check=lambda: target.healthy)
        return ks, target

    def trip(self, ks, target):
        target.healthy = False
        ks._tick()
        assert ks.circuit_state.value == "open"

    def test_an_open_breaker_pauses_the_tick(self, engines):
        ks, target = self.make_ks()
        self.trip(ks, target)
        rec = Recorder()
        e = engines(lambda: None, kill_switch_provider=lambda: ks, injector=rec)
        result = e.run_tick(settings(cpu=True, memory=True), 1)
        assert result.paused is True
        assert result.started == 0
        assert e.paused is True
        time.sleep(0.1)
        assert rec.calls == []

    def test_chaos_resumes_after_recovery(self, engines):
        ks, target = self.make_ks()
        self.trip(ks, target)
        rec = Recorder()
        e = engines(lambda: None, kill_switch_provider=lambda: ks, injector=rec)
        e.run_tick(settings(cpu=True), 1)
        assert e.paused

        target.healthy = True
        ks._tick()  # OPEN -> HALF_OPEN
        ks._tick()  # HALF_OPEN -> CLOSED
        assert ks.allows_injection()[0]

        e.run_tick(settings(cpu=True), 2)
        assert e.paused is False
        assert wait_for(lambda: rec.names() == ["cpu"])

    def test_dry_runs_are_not_gated(self, engines):
        ks, target = self.make_ks()
        self.trip(ks, target)
        rec = Recorder()
        e = engines(lambda: None, kill_switch_provider=lambda: ks, injector=rec)
        result = e.run_tick(settings(dry_run=True, cpu=True), 1)
        assert result.paused is False
        assert wait_for(lambda: rec.calls == [("cpu", True)])

    def test_a_breaker_that_trips_before_the_worker_starts_still_wins(self, engines):
        """The tick saw a closed breaker; it opens before the worker checks."""
        ks, target = self.make_ks(healthy=True)
        rec = Recorder()
        e = engines(lambda: None, kill_switch_provider=lambda: ks, injector=rec)

        real = ks.allows_injection
        seen = {"n": 0}

        def allows_then_trip():
            seen["n"] += 1
            result = real()
            if seen["n"] == 1:  # the tick's own check passes ...
                self.trip(ks, target)  # ... then the breaker opens
            return result

        ks.allows_injection = allows_then_trip
        e.run_tick(settings(cpu=True), 1)
        assert wait_for(lambda: e.in_flight() == 0)
        assert rec.calls == [], "injected into a target already found unhealthy"
        assert e.paused is True

    def test_injections_are_recorded_for_the_process_kill_grace_period(self, engines):
        ks, _ = self.make_ks(healthy=True)
        release = threading.Event()
        e = engines(
            lambda: None,
            kill_switch_provider=lambda: ks,
            injector=lambda n, c, d: release.wait(timeout=10),
        )
        e.run_tick(settings(process=True), 1)
        assert wait_for(lambda: "process" in ks.failure_context.get_active_injections())
        release.set()
        assert wait_for(lambda: not ks.failure_context.get_active_injections())

    def test_a_kill_switch_swapped_in_later_is_honored(self, engines):
        ks, target = self.make_ks()
        self.trip(ks, target)
        holder = {"ks": None}
        rec = Recorder()
        e = engines(
            lambda: None, kill_switch_provider=lambda: holder["ks"], injector=rec
        )
        e.run_tick(settings(cpu=True), 1)  # no kill switch: runs
        assert wait_for(lambda: rec.names() == ["cpu"])
        holder["ks"] = ks
        e.run_tick(settings(memory=True), 2)  # tripped one: held back
        time.sleep(0.1)
        assert rec.names() == ["cpu"]


class TestSettingsSnapshot:
    def make_config(self):
        return SimpleNamespace(
            agent=SimpleNamespace(interval_seconds=7, dry_run=True),
            failures={"cpu": {"enabled": True, "probability": 0.5, "cores": 1}},
        )

    def test_snapshot_copies_the_values(self):
        s = snapshot_settings(self.make_config())
        assert s.interval_seconds == 7
        assert s.dry_run is True
        assert s.failures["cpu"]["cores"] == 1

    def test_later_changes_to_the_config_do_not_reach_a_snapshot(self):
        config = self.make_config()
        s = snapshot_settings(config)
        config.failures["cpu"]["cores"] = 99
        config.failures["cpu"] = {"enabled": False}
        config.agent.interval_seconds = 1
        assert s.failures["cpu"]["cores"] == 1
        assert s.interval_seconds == 7

    def test_snapshot_is_immutable_at_the_top_level(self):
        with pytest.raises(Exception):
            snapshot_settings(self.make_config()).interval_seconds = 1

    def test_a_tick_uses_one_snapshot(self, engines):
        """Config edits during a tick cannot change what that tick runs."""
        rec = Recorder()
        config = self.make_config()
        config.failures["cpu"]["probability"] = 1.0
        taken = snapshot_settings(config)
        e = engines(lambda: taken, injector=rec)
        config.failures["cpu"]["enabled"] = False
        e.run_tick(taken, 1)
        assert wait_for(lambda: rec.names() == ["cpu"])


class TestInjectorLookup:
    @pytest.mark.parametrize("name", ["cpu", "memory", "process", "network"])
    def test_resolves_every_failure_type(self, name):
        assert callable(resolve_injector(name))

    def test_unknown_type_is_a_clear_error(self):
        with pytest.raises(ValueError, match="Unknown failure type 'disk'"):
            resolve_injector("disk")

    def test_lookup_is_at_call_time_so_patches_work(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.failures.cpu.inject_cpu",
            lambda cfg, dry_run=False: seen.append((cfg, dry_run)),
        )
        run_injection("cpu", {"cores": 1}, True)
        assert seen == [({"cores": 1}, True)]
