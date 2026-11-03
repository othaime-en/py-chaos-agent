"""The experiment runner: phases, verdicts, aborts, and what it never leaves behind."""

import copy
import threading
import time
from types import SimpleNamespace

import pytest

from src import experiments as experiments_module
from src.audit import audit
from src.experiments import ExperimentBusy, ExperimentRunner
from src.kill_switch import KillSwitch, KillSwitchConfig
from src.lifecycle import lifecycle
from src.metrics import EXPERIMENTS_TOTAL, metric_value
from src.schemas import validate_config_dict
from tests.http_target import Target


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def target():
    t = Target()
    yield t
    t.close()


def make_experiment(target, **overrides):
    """A schema-validated experiment, sped up so tests take fractions of a second."""
    raw = {
        "agent": {},
        "failures": {},
        "experiments": [
            {
                "name": "cpu-spike",
                "hypothesis": "The target comes back after CPU pressure",
                "steady_state": {"checks": [{"name": "health", "url": target.url}]},
                "faults": {"cpu": {"cores": 1, "duration_seconds": 5}},
                "schedule": {"duration_seconds": 5, "interval_seconds": 1},
            }
        ],
    }
    exp = validate_config_dict(raw)["experiments"][0]
    exp["steady_state"].update(
        check_interval_seconds=0.05,
        required_consecutive_successes=1,
        baseline_timeout_seconds=0,
        recovery_timeout_seconds=0,
    )
    exp["schedule"].update(duration_seconds=0.3, interval_seconds=0.05)
    for section, values in overrides.items():
        if isinstance(values, dict) and isinstance(exp.get(section), dict):
            exp[section].update(values)
        else:
            exp[section] = values
    return exp


class Injector:
    """A stand-in injector that records calls and can act on the target."""

    def __init__(self):
        self.calls = []
        self.on_call = None
        self.hold_seconds = 0.0
        self.completed = []
        self.cut_short = []
        self.fail = None

    def __call__(self, name, cfg, dry_run):
        self.calls.append((name, dry_run))
        if self.fail:
            raise self.fail
        if self.on_call:
            self.on_call()
        with lifecycle.injection(name, enabled=not dry_run) as ticket:
            if self.hold_seconds and ticket is not None:
                if ticket.sleep(self.hold_seconds):
                    self.cut_short.append(name)
                    return
            self.completed.append(name)


@pytest.fixture
def runners():
    created = []

    def make(**kwargs):
        r = ExperimentRunner(**kwargs)
        created.append(r)
        return r

    yield make
    for r in created:
        r.stop(timeout=10)


def finished(runner, run_id, timeout=15):
    assert wait_for(lambda: runner.get(run_id)["finished_at"] is not None, timeout)
    return runner.get(run_id)


def run_to_end(runner, exp, **kwargs):
    run = runner.start(exp, **kwargs)
    return finished(runner, run["id"])


class TestVerdicts:
    def test_a_healthy_run_confirms_the_hypothesis(self, runners, target):
        inj = Injector()
        result = run_to_end(runners(injector=inj), make_experiment(target))
        assert result["status"] == "completed"
        assert result["verdict"] == "confirmed"
        assert result["reason"] is None
        assert result["baseline"]["reached"] is True
        assert result["recovery"]["reached"] is True
        assert result["injections_started"] >= 1
        assert len(inj.calls) == result["injections_started"]

    def test_chaos_only_starts_after_the_baseline_passes(self, runners, target):
        seen = []
        inj = Injector()
        inj.on_call = lambda: seen.append(target.hits)
        run_to_end(runners(injector=inj), make_experiment(target))
        assert seen and seen[0] >= 1, "the baseline was not checked before chaos"

    def test_an_unhealthy_target_refuses_the_run(self, runners, target):
        target.status = 503
        inj = Injector()
        result = run_to_end(runners(injector=inj), make_experiment(target))
        assert result["status"] == "refused"
        assert result["verdict"] == "inconclusive"
        assert "no chaos was injected" in result["reason"]
        assert inj.calls == []
        assert result["recovery"] is None

    def test_a_target_that_does_not_come_back_falsifies_it(self, runners, target):
        inj = Injector()
        inj.on_call = lambda: setattr(target, "status", 503)
        result = run_to_end(runners(injector=inj), make_experiment(target))
        assert result["status"] == "completed"
        assert result["verdict"] == "falsified"
        assert "not restored" in result["reason"]
        assert result["recovery"]["reached"] is False

    def test_a_target_that_recovers_in_time_confirms_it(self, runners, target):
        inj = Injector()

        def break_then_heal():
            target.status = 503
            threading.Timer(0.4, lambda: setattr(target, "status", 200)).start()

        inj.on_call = break_then_heal
        exp = make_experiment(target, steady_state={"recovery_timeout_seconds": 10})
        result = run_to_end(runners(injector=inj), exp)
        assert result["verdict"] == "confirmed"
        assert result["recovery"]["rounds"] >= 2

    def test_a_failing_injector_does_not_fail_the_run(self, runners, target):
        inj = Injector()
        inj.fail = RuntimeError("boom")
        result = run_to_end(runners(injector=inj), make_experiment(target))
        assert result["status"] == "completed"
        assert result["verdict"] == "confirmed"

    def test_an_internal_error_is_inconclusive_and_frees_the_runner(
        self, runners, target, monkeypatch
    ):
        def boom(*a, **k):
            raise RuntimeError("secret detail")

        monkeypatch.setattr(experiments_module, "wait_for_steady_state", boom)
        runner = runners(injector=Injector())
        result = run_to_end(runner, make_experiment(target))
        assert result["status"] == "error"
        assert result["verdict"] == "inconclusive"
        assert result["reason"] == "internal error: RuntimeError"
        assert "secret" not in str(result)
        assert wait_for(lambda: not runner.is_running)
        monkeypatch.undo()
        run_to_end(runner, make_experiment(target))  # and it can run again


class TestSchedule:
    def test_the_injection_cap_ends_the_chaos_early(self, runners, target):
        inj = Injector()
        exp = make_experiment(
            target, schedule={"duration_seconds": 30, "max_injections": 2}
        )
        start = time.monotonic()
        result = run_to_end(runners(injector=inj), exp)
        assert result["injections_started"] == 2
        assert time.monotonic() - start < 5, "ran for the whole duration"
        assert result["verdict"] == "confirmed"

    def test_injections_that_outlast_the_schedule_finish_by_themselves(
        self, runners, target
    ):
        """The schedule ends, but a fault that is mid-way is not cut off."""
        inj = Injector()
        inj.hold_seconds = 0.6
        exp = make_experiment(
            target, schedule={"duration_seconds": 0.2, "max_injections": 1}
        )
        result = run_to_end(runners(injector=inj), exp)
        assert inj.completed == ["cpu"]
        assert inj.cut_short == []
        assert result["status"] == "completed"

    def test_dry_run_comes_from_the_experiment(self, runners, target):
        inj = Injector()
        run_to_end(runners(injector=inj), make_experiment(target, dry_run=True))
        assert inj.calls and all(dry for _, dry in inj.calls)

    def test_the_caller_can_force_a_dry_run(self, runners, target):
        inj = Injector()
        result = run_to_end(
            runners(injector=inj), make_experiment(target), dry_run=True
        )
        assert result["dry_run"] is True
        assert all(dry for _, dry in inj.calls)

    def test_the_caller_can_override_a_dry_run_experiment(self, runners, target):
        inj = Injector()
        exp = make_experiment(target, dry_run=True)
        result = run_to_end(runners(injector=inj), exp, dry_run=False)
        assert result["dry_run"] is False
        assert inj.calls and not any(dry for _, dry in inj.calls)

    def test_a_started_run_is_a_private_copy_of_the_experiment(self, runners, target):
        inj = Injector()
        inj.hold_seconds = 0.2
        exp = make_experiment(target)
        runner = runners(injector=inj)
        run = runner.start(exp)
        exp["faults"].clear()  # a config reload replacing the experiment
        result = finished(runner, run["id"])
        assert result["injections_started"] >= 1
        assert inj.calls[0][0] == "cpu"


class TestAbort:
    def test_abort_during_chaos_cuts_the_injection_short(self, runners, target):
        inj = Injector()
        inj.hold_seconds = 30
        runner = runners(injector=inj)
        run = runner.start(
            make_experiment(
                target, schedule={"duration_seconds": 60, "interval_seconds": 1}
            )
        )
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        assert runner.abort() is True
        result = finished(runner, run["id"])
        assert result["status"] == "aborted"
        assert result["verdict"] == "inconclusive"
        assert result["reason"] == "aborted by an operator"
        assert inj.cut_short == ["cpu"], "the injection kept running after the abort"
        assert lifecycle.active_types() == []

    def test_an_abort_still_records_where_the_target_stands(self, runners, target):
        inj = Injector()
        inj.hold_seconds = 30
        runner = runners(injector=inj)
        run = runner.start(make_experiment(target, schedule={"duration_seconds": 60}))
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        runner.abort()
        result = finished(runner, run["id"])
        assert result["recovery"] is not None
        assert result["recovery"]["rounds"] == 1, "an abort must not wait for recovery"

    def test_abort_during_the_baseline_injects_nothing(self, runners, target):
        target.status = 503
        inj = Injector()
        runner = runners(injector=inj)
        exp = make_experiment(target, steady_state={"baseline_timeout_seconds": 60})
        run = runner.start(exp)
        assert wait_for(lambda: runner.get(run["id"])["status"] == "checking_baseline")
        time.sleep(0.2)
        assert runner.abort("changed my mind") is True
        result = finished(runner, run["id"], timeout=5)
        assert result["status"] == "aborted"
        assert result["reason"] == "changed my mind"
        assert inj.calls == []

    def test_abort_with_nothing_running_is_false(self, runners):
        assert runners().abort() is False

    def test_stop_aborts_and_waits(self, runners, target):
        inj = Injector()
        inj.hold_seconds = 30
        runner = runners(injector=inj)
        run = runner.start(make_experiment(target, schedule={"duration_seconds": 60}))
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        assert runner.stop(timeout=10) is True
        result = runner.get(run["id"])
        assert result["status"] == "aborted"
        assert result["recovery"] is None, "no recovery check while shutting down"
        assert not runner.is_running

    def test_process_shutdown_aborts_the_run(self, runners, target):
        inj = Injector()
        inj.hold_seconds = 30
        runner = runners(injector=inj)
        run = runner.start(make_experiment(target, schedule={"duration_seconds": 60}))
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        lifecycle.shutdown("test")
        result = finished(runner, run["id"])
        assert result["status"] == "aborted"
        assert "shutting down" in result["reason"]


class TestKillSwitch:
    def make_ks(self):
        cfg = KillSwitchConfig(
            health_check_interval_seconds=3600,
            grace_after_agent_start_seconds=0,
            recovery_test_interval_seconds=3600,
            recovery_success_threshold=1,
            consecutive_failures_threshold=1,
        )
        state = SimpleNamespace(healthy=True)
        ks = KillSwitch(cfg, is_armed=lambda: True, health_check=lambda: state.healthy)
        return ks, state

    def test_a_tripped_breaker_aborts_the_experiment(self, runners, target):
        ks, state = self.make_ks()
        inj = Injector()
        inj.hold_seconds = 30
        runner = runners(injector=inj, kill_switch_provider=lambda: ks)
        run = runner.start(
            make_experiment(
                target, schedule={"duration_seconds": 60, "interval_seconds": 1}
            )
        )
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        state.healthy = False
        ks._tick()
        assert ks.circuit_state.value == "open"
        lifecycle.abort_active("kill switch triggered")  # what the API wires up
        result = finished(runner, run["id"])
        assert result["status"] == "aborted"
        assert result["verdict"] == "inconclusive"
        assert "kill switch" in result["reason"]
        assert lifecycle.active_types() == []

    def test_a_dry_run_is_not_gated(self, runners, target):
        ks, state = self.make_ks()
        state.healthy = False
        ks._tick()
        inj = Injector()
        runner = runners(injector=inj, kill_switch_provider=lambda: ks)
        result = run_to_end(runner, make_experiment(target), dry_run=True)
        assert result["status"] == "completed"
        assert inj.calls


class TestOneAtATime:
    def test_a_second_start_is_refused(self, runners, target):
        inj = Injector()
        inj.hold_seconds = 30
        runner = runners(injector=inj)
        runner.start(make_experiment(target, schedule={"duration_seconds": 60}))
        with pytest.raises(ExperimentBusy, match="cpu-spike"):
            runner.start(make_experiment(target))

    def test_it_can_run_again_afterwards(self, runners, target):
        runner = runners(injector=Injector())
        first = run_to_end(runner, make_experiment(target))
        second = run_to_end(runner, make_experiment(target))
        assert first["id"] != second["id"]

    def test_nothing_is_left_behind(self, runners, target):
        inj = Injector()
        runner = runners(injector=inj)
        run_to_end(runner, make_experiment(target))
        assert wait_for(lambda: not runner.is_running)
        assert lifecycle.active_types() == []
        leftovers = [
            t for t in threading.enumerate() if t.name.startswith("experiment-")
        ]
        assert wait_for(lambda: not [t for t in leftovers if t.is_alive()], 5)


class TestRecords:
    def test_history_is_newest_first_and_includes_the_running_one(
        self, runners, target
    ):
        runner = runners(injector=Injector())
        a = run_to_end(runner, make_experiment(target))
        inj = Injector()
        inj.hold_seconds = 30
        runner._injector = inj
        b = runner.start(make_experiment(target, schedule={"duration_seconds": 60}))
        history = runner.history()
        assert [r["id"] for r in history] == [b["id"], a["id"]]
        assert runner.current()["id"] == b["id"]
        runner.abort()
        finished(runner, b["id"])
        assert runner.current() is None

    def test_history_is_bounded(self, runners, target):
        runner = runners(injector=Injector(), history_size=2)
        ids = [run_to_end(runner, make_experiment(target))["id"] for _ in range(3)]
        assert [r["id"] for r in runner.history()] == [ids[2], ids[1]]
        assert runner.get(ids[0]) is None

    def test_history_limit(self, runners, target):
        runner = runners(injector=Injector())
        for _ in range(3):
            run_to_end(runner, make_experiment(target))
        assert len(runner.history(limit=2)) == 2
        assert runner.history(limit=0) == []

    def test_an_unknown_run_is_none(self, runners):
        assert runners().get("nope") is None

    def test_the_run_describes_itself(self, runners, target):
        result = run_to_end(
            runners(injector=Injector()),
            make_experiment(target),
            principal="alice",
            role="operator",
        )
        assert result["experiment"] == "cpu-spike"
        assert result["hypothesis"] == "The target comes back after CPU pressure"
        assert result["principal"] == "alice" and result["role"] == "operator"
        assert result["created_at"].endswith("Z") and result["finished_at"].endswith(
            "Z"
        )
        assert result["duration_seconds"] >= 0

    def test_metrics_count_finished_runs_by_outcome(self, runners, target):
        runner = runners(injector=Injector())
        run_to_end(runner, make_experiment(target))
        target.status = 503
        run_to_end(runner, make_experiment(target))
        assert (
            metric_value(EXPERIMENTS_TOTAL, status="completed", verdict="confirmed")
            == 1
        )
        assert (
            metric_value(EXPERIMENTS_TOTAL, status="refused", verdict="inconclusive")
            == 1
        )

    def test_the_active_gauge_follows_the_run(self, runners, target):
        from src.metrics import EXPERIMENT_ACTIVE

        inj = Injector()
        inj.hold_seconds = 30
        runner = runners(injector=inj)
        run = runner.start(make_experiment(target, schedule={"duration_seconds": 60}))
        assert metric_value(EXPERIMENT_ACTIVE) == 1
        runner.abort()
        finished(runner, run["id"])
        assert wait_for(lambda: metric_value(EXPERIMENT_ACTIVE) == 0)


class TestAudit:
    def test_start_and_finish_are_recorded_with_the_person(self, runners, target):
        run_to_end(
            runners(injector=Injector()),
            make_experiment(target),
            principal="alice",
            role="operator",
        )
        assert wait_for(lambda: audit.recent(event="experiment.finished"))
        started = audit.recent(event="experiment.started")[0]
        assert started["principal"] == "alice" and started["role"] == "operator"
        assert started["experiment"] == "cpu-spike"
        assert started["hypothesis"]
        done = audit.recent(event="experiment.finished")[0]
        assert done["status"] == "completed" and done["verdict"] == "confirmed"
        assert done["run_id"] == started["run_id"]

    def test_injections_are_attributed_to_the_person_who_started_the_run(
        self, runners, target
    ):
        run = run_to_end(
            runners(injector=Injector()),
            make_experiment(target),
            principal="alice",
            role="operator",
        )
        assert wait_for(lambda: audit.recent(event="injection.completed"))
        entry = audit.recent(event="injection.started")[0]
        assert entry["principal"] == "alice"
        assert entry["source"] == "experiment"
        assert entry["experiment"] == "cpu-spike"
        assert entry["run_id"] == run["id"]

    def test_a_refused_run_is_still_recorded(self, runners, target):
        target.status = 503
        run_to_end(runners(injector=Injector()), make_experiment(target))
        assert wait_for(lambda: audit.recent(event="experiment.finished"))
        assert audit.recent(event="experiment.finished")[0]["status"] == "refused"


def test_make_experiment_is_a_valid_schema_document(target):
    """Guards the helper: the sped-up values are the only deviation from the schema."""
    exp = make_experiment(target)
    assert copy.deepcopy(exp)["faults"]["cpu"]["enabled"] is True


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------

import datetime as dt  # noqa: E402

from src.experiments import OutsideWindow, chaos_budget_seconds  # noqa: E402

UTC = dt.timezone.utc


class FakeClock:
    def __init__(self, text):
        self.now = dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)

    def __call__(self):
        return self.now

    def set(self, text):
        self.now = dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


# Monday 2026-11-02.
MON_10_TO_16 = {
    "timezone": "UTC",
    "allow": [{"days": ["mon"], "start": "10:00", "end": "16:00"}],
}


def windowed(target, **overrides):
    return make_experiment(target, windows=MON_10_TO_16, **overrides)


class TestChaosBudget:
    def exp(self, **schedule):
        return {
            "schedule": {
                "duration_seconds": 60,
                "interval_seconds": 10,
                "max_injections": None,
                **schedule,
            },
            "faults": {"cpu": {"duration_seconds": 20}, "process": {}},
        }

    def test_duration_plus_the_longest_fault(self):
        assert chaos_budget_seconds(self.exp()) == 80

    def test_a_cap_ends_the_schedule_earlier(self):
        # The third and last injection starts at t=20.
        assert chaos_budget_seconds(self.exp(max_injections=3)) == 40

    def test_a_cap_never_extends_the_schedule(self):
        assert chaos_budget_seconds(self.exp(max_injections=100)) == 80

    def test_one_injection_starts_at_once(self):
        assert chaos_budget_seconds(self.exp(max_injections=1)) == 20

    def test_faults_without_a_duration_count_as_instant(self):
        exp = {"schedule": self.exp()["schedule"], "faults": {"process": {}}}
        assert chaos_budget_seconds(exp) == 60


class TestWindowGate:
    def test_inside_the_window_it_runs(self, runners, target):
        runner = runners(injector=Injector(), clock=FakeClock("2026-11-02 12:00"))
        result = run_to_end(runner, windowed(target))
        assert result["status"] == "completed"
        assert result["window_closes_at"] == "2026-11-02T16:00:00.000Z".replace(
            ".000", ""
        )

    def test_outside_the_window_it_is_refused_before_anything_starts(
        self, runners, target
    ):
        inj = Injector()
        runner = runners(injector=inj, clock=FakeClock("2026-11-02 09:00"))
        with pytest.raises(OutsideWindow, match="opens Mon 2026-11-02 10:00 \\(UTC\\)"):
            runner.start(windowed(target))
        assert runner.history() == [], "a refused start must not leave a run behind"
        assert not runner.is_running
        assert inj.calls == [] and target.hits == 0, "nothing may even be probed"

    def test_the_window_end_is_exclusive(self, runners, target):
        runner = runners(injector=Injector(), clock=FakeClock("2026-11-02 16:00"))
        with pytest.raises(OutsideWindow):
            runner.start(windowed(target))

    def test_the_wrong_day(self, runners, target):
        runner = runners(
            injector=Injector(), clock=FakeClock("2026-11-03 12:00")
        )  # Tuesday
        with pytest.raises(OutsideWindow, match="2026-11-09 10:00"):
            runner.start(windowed(target))

    def test_it_is_refused_when_the_chaos_would_outlast_the_window(
        self, runners, target
    ):
        runner = runners(injector=Injector(), clock=FakeClock("2026-11-02 15:59"))
        exp = windowed(target, schedule={"duration_seconds": 120})
        with pytest.raises(OutsideWindow, match="Not enough time left") as exc:
            runner.start(exp)
        assert "closes Mon 2026-11-02 16:00 (UTC), in 60s" in str(exc.value)
        assert "up to 125s" in str(exc.value)  # 120s of schedule + the 5s fault

    def test_it_runs_when_the_chaos_fits_in_the_time_left(self, runners, target):
        # Needs 0.3 + 5 = 5.3s and 60s are left.
        clock = FakeClock("2026-11-02 15:59")
        runner = runners(injector=Injector(), clock=clock)
        result = run_to_end(runner, windowed(target))
        assert result["status"] == "completed"

    def test_a_dry_run_ignores_the_windows(self, runners, target):
        inj = Injector()
        runner = runners(injector=inj, clock=FakeClock("2026-11-07 03:00"))
        result = run_to_end(runner, windowed(target), dry_run=True)
        assert result["status"] == "completed"
        assert result["window_closes_at"] is None
        assert inj.calls

    def test_an_experiment_that_is_dry_run_in_config_ignores_them_too(
        self, runners, target
    ):
        runner = runners(injector=Injector(), clock=FakeClock("2026-11-07 03:00"))
        result = run_to_end(runner, windowed(target, dry_run=True))
        assert result["status"] == "completed"

    def test_no_windows_means_no_gate(self, runners, target):
        runner = runners(injector=Injector(), clock=FakeClock("2026-11-07 03:00"))
        assert run_to_end(runner, make_experiment(target))["status"] == "completed"

    def test_busy_is_reported_before_the_window(self, runners, target):
        inj = Injector()
        inj.hold_seconds = 30
        clock = FakeClock("2026-11-02 12:00")
        runner = runners(injector=inj, clock=clock)
        runner.start(windowed(target, schedule={"duration_seconds": 60}))
        clock.set("2026-11-02 20:00")
        with pytest.raises(ExperimentBusy):
            runner.start(windowed(target))


class TestWindowClosesDuringARun:
    def test_the_run_is_aborted_and_its_effects_removed(self, runners, target):
        inj = Injector()
        inj.hold_seconds = 30
        clock = FakeClock("2026-11-02 12:00")
        runner = runners(injector=inj, clock=clock)
        run = runner.start(
            windowed(target, schedule={"duration_seconds": 60, "interval_seconds": 1})
        )
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        clock.set("2026-11-02 16:00")
        result = finished(runner, run["id"])
        assert result["status"] == "aborted"
        assert result["verdict"] == "inconclusive"
        assert result["reason"] == "the allowed time window closed"
        assert inj.cut_short == ["cpu"], "the injection outlived its window"
        assert lifecycle.active_types() == []
        assert (
            result["recovery"]["rounds"] == 1
        ), "no waiting for recovery after an abort"

    def test_a_window_that_closed_during_the_baseline_refuses_the_run(
        self, runners, target
    ):
        target.status = 503
        inj = Injector()
        clock = FakeClock("2026-11-02 12:00")
        runner = runners(injector=inj, clock=clock)
        exp = windowed(target, steady_state={"baseline_timeout_seconds": 60})
        run = runner.start(exp)
        assert wait_for(lambda: runner.get(run["id"])["status"] == "checking_baseline")
        clock.set("2026-11-02 16:30")
        target.status = 200  # the baseline now passes, but too late
        result = finished(runner, run["id"], timeout=5)
        assert result["status"] == "refused"
        assert "window closed before the chaos could start" in result["reason"]
        assert inj.calls == []

    def test_the_window_is_not_checked_on_every_run_forever(self, runners, target):
        """A run that finishes inside its window is unaffected by later closing."""
        clock = FakeClock("2026-11-02 12:00")
        runner = runners(injector=Injector(), clock=clock)
        result = run_to_end(runner, windowed(target))
        clock.set("2026-11-02 18:00")
        assert runner.get(result["id"])["status"] == "completed"


# ---------------------------------------------------------------------------
# Target selectors
# ---------------------------------------------------------------------------

from src import targets  # noqa: E402
from src.experiments import TargetMismatch  # noqa: E402

STAGING = {"match_labels": {"env": "staging"}}


class Labels:
    """Labels the test can change while an experiment runs."""

    def __init__(self, **labels):
        self.labels = dict(labels)

    def __call__(self):
        return dict(self.labels)


def selected(target, selector=STAGING, **overrides):
    return make_experiment(target, selector=selector, **overrides)


class TestSelectorGate:
    def test_a_matching_agent_runs_it(self, runners, target):
        labels = Labels(env="staging", app="checkout")
        runner = runners(injector=Injector(), labels_provider=labels)
        result = run_to_end(runner, selected(target))
        assert result["status"] == "completed"
        assert result["target_labels"] == {"env": "staging", "app": "checkout"}

    def test_a_different_agent_is_refused_before_anything_starts(self, runners, target):
        inj = Injector()
        runner = runners(injector=inj, labels_provider=Labels(env="prod"))
        with pytest.raises(TargetMismatch, match="env must be 'staging' but is 'prod'"):
            runner.start(selected(target))
        assert runner.history() == [], "a refused start must not leave a run behind"
        assert inj.calls == [] and target.hits == 0

    def test_an_agent_without_labels_matches_no_selector(self, runners, target):
        runner = runners(injector=Injector(), labels_provider=Labels())
        with pytest.raises(
            TargetMismatch, match="env must be 'staging' but is not set"
        ):
            runner.start(selected(target))

    def test_every_unmet_condition_is_reported(self, runners, target):
        selector = {
            "match_labels": {"env": "staging"},
            "match_expressions": [{"key": "chaos-ok", "operator": "Exists"}],
        }
        runner = runners(injector=Injector(), labels_provider=Labels(env="prod"))
        with pytest.raises(TargetMismatch) as exc:
            runner.start(selected(target, selector))
        assert "env must be" in str(exc.value) and "chaos-ok must be set" in str(
            exc.value
        )

    def test_expressions_are_honoured(self, runners, target):
        selector = {
            "match_expressions": [
                {"key": "tier", "operator": "NotIn", "values": ["prod"]}
            ]
        }
        runner = runners(injector=Injector(), labels_provider=Labels(tier="canary"))
        assert run_to_end(runner, selected(target, selector))["status"] == "completed"
        runner = runners(injector=Injector(), labels_provider=Labels(tier="prod"))
        with pytest.raises(TargetMismatch):
            runner.start(selected(target, selector))

    def test_no_selector_means_no_gate_and_no_recorded_labels(self, runners, target):
        runner = runners(injector=Injector(), labels_provider=Labels())
        result = run_to_end(runner, make_experiment(target))
        assert result["status"] == "completed"
        assert result["target_labels"] is None

    def test_a_dry_run_ignores_the_selector(self, runners, target):
        inj = Injector()
        runner = runners(injector=inj, labels_provider=Labels(env="prod"))
        result = run_to_end(runner, selected(target), dry_run=True)
        assert result["status"] == "completed"
        assert result["target_labels"] is None
        assert inj.calls

    def test_the_target_is_checked_before_the_window(self, runners, target):
        runner = runners(
            injector=Injector(),
            labels_provider=Labels(env="prod"),
            clock=FakeClock("2026-11-07 03:00"),
        )
        with pytest.raises(TargetMismatch):
            runner.start(selected(target, windows=MON_10_TO_16))

    def test_the_default_labels_come_from_the_environment(
        self, runners, target, monkeypatch
    ):
        monkeypatch.setenv("CHAOS_TARGET_LABELS", "env=staging")
        targets.agent_labels.reset()
        try:
            runner = runners(injector=Injector())
            assert run_to_end(runner, selected(target))["status"] == "completed"
            monkeypatch.setenv("CHAOS_TARGET_LABELS", "env=prod")
            targets.agent_labels.reset()
            with pytest.raises(TargetMismatch):
                runner.start(selected(target))
        finally:
            targets.agent_labels.reset()

    def test_the_labels_are_in_the_audit_log(self, runners, target):
        runner = runners(injector=Injector(), labels_provider=Labels(env="staging"))
        run_to_end(runner, selected(target))
        assert wait_for(lambda: audit.recent(event="experiment.finished"))
        assert audit.recent(event="experiment.started")[0]["target_labels"] == {
            "env": "staging"
        }


class TestLabelsChangeDuringARun:
    def test_removing_the_label_aborts_the_run_and_its_effects(self, runners, target):
        """The remote off switch: relabel the pod and the chaos stops."""
        labels = Labels(env="staging")
        inj = Injector()
        inj.hold_seconds = 30
        runner = runners(injector=inj, labels_provider=labels)
        run = runner.start(
            selected(target, schedule={"duration_seconds": 60, "interval_seconds": 1})
        )
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        labels.labels.pop("env")
        result = finished(runner, run["id"])
        assert result["status"] == "aborted"
        assert result["verdict"] == "inconclusive"
        assert "no longer match the selector" in result["reason"]
        assert "env must be 'staging' but is not set" in result["reason"]
        assert inj.cut_short == ["cpu"], "the injection outlived its label"
        assert lifecycle.active_types() == []

    def test_changing_the_label_to_another_value_aborts_too(self, runners, target):
        labels = Labels(env="staging")
        inj = Injector()
        inj.hold_seconds = 30
        runner = runners(injector=inj, labels_provider=labels)
        run = runner.start(selected(target, schedule={"duration_seconds": 60}))
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        labels.labels["env"] = "prod"
        assert finished(runner, run["id"])["status"] == "aborted"

    def test_unrelated_label_changes_do_not_matter(self, runners, target):
        labels = Labels(env="staging")
        inj = Injector()
        inj.hold_seconds = 30
        runner = runners(injector=inj, labels_provider=labels)
        run = runner.start(selected(target, schedule={"duration_seconds": 60}))
        assert wait_for(lambda: lifecycle.is_active("cpu"))
        labels.labels["pod-template-hash"] = "abc"
        time.sleep(0.4)
        assert runner.get(run["id"])["status"] == "running"
        runner.abort()
        finished(runner, run["id"])

    def test_labels_lost_during_the_baseline_refuse_the_run(self, runners, target):
        target.status = 503
        labels = Labels(env="staging")
        inj = Injector()
        runner = runners(injector=inj, labels_provider=labels)
        run = runner.start(
            selected(target, steady_state={"baseline_timeout_seconds": 60})
        )
        assert wait_for(lambda: runner.get(run["id"])["status"] == "checking_baseline")
        labels.labels.clear()
        target.status = 200
        result = finished(runner, run["id"], timeout=5)
        assert result["status"] == "refused"
        assert "no longer match the selector" in result["reason"]
        assert inj.calls == []

    def test_a_dry_run_is_not_aborted_by_label_changes(self, runners, target):
        labels = Labels(env="staging")
        inj = Injector()
        runner = runners(injector=inj, labels_provider=labels)
        run = runner.start(selected(target), dry_run=True)
        labels.labels.clear()
        assert finished(runner, run["id"])["status"] == "completed"
