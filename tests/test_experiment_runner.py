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
