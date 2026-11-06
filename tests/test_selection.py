"""Selection gates: eligibility, selector, window, sample, state; and the report."""

import datetime as dt

import pytest

from src.selection import (
    GATE_ORDER,
    chaos_budget_seconds,
    eligibility_problem,
    evaluate,
    sample_bucket,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 2, 12, 0, tzinfo=UTC)  # Monday


def experiment(**extra):
    return {
        "name": "cpu-spike",
        "faults": {"cpu": {"duration_seconds": 5}},
        "schedule": {
            "duration_seconds": 30,
            "interval_seconds": 10,
            "max_injections": None,
        },
        **extra,
    }


def run(exp=None, labels=None, **kwargs):
    return evaluate(exp or experiment(), labels=labels or {}, now=NOW, **kwargs)


OPT_IN = {"match_labels": {"chaos-enabled": "true"}}
STAGING = {"match_labels": {"env": "staging"}}


class TestNoGates:
    def test_an_unrestricted_experiment_is_selected_with_no_gates(self):
        result = run()
        assert result.eligible and result.gates == []
        assert result.to_dict() == {"eligible": True, "gates": []}
        assert result.summary == "selected"


class TestEligibility:
    def test_a_missing_opt_in_label_fails_safe(self):
        result = run(eligibility=OPT_IN, labels={"env": "staging"})
        assert not result.eligible
        gate = result.gate("eligibility")
        assert not gate.ok
        assert gate.detail == "chaos-enabled must be 'true' but is not set"

    def test_the_opt_in_label_makes_it_eligible(self):
        result = run(eligibility=OPT_IN, labels={"chaos-enabled": "true"})
        assert result.eligible
        assert (
            result.gate("eligibility").detail
            == "labels satisfy this agent's eligibility rule"
        )

    def test_a_wrong_value_is_not_enough(self):
        assert not run(eligibility=OPT_IN, labels={"chaos-enabled": "false"}).eligible

    def test_exclusion_with_an_opt_out_label(self):
        rule = {
            "match_expressions": [
                {"key": "chaos", "operator": "NotIn", "values": ["opt-out"]}
            ]
        }
        assert run(eligibility=rule, labels={}).eligible
        assert run(eligibility=rule, labels={"chaos": "other"}).eligible
        out = run(eligibility=rule, labels={"chaos": "opt-out"})
        assert not out.eligible and "must not be one of 'opt-out'" in out.summary

    def test_exclusion_of_a_namespace(self):
        rule = {
            "match_expressions": [
                {"key": "namespace", "operator": "NotIn", "values": ["prod"]}
            ]
        }
        assert run(eligibility=rule, labels={"namespace": "staging"}).eligible
        assert not run(eligibility=rule, labels={"namespace": "prod"}).eligible

    def test_the_helper(self):
        assert eligibility_problem(None, {}) is None
        assert eligibility_problem({}, {}) is None
        assert eligibility_problem(OPT_IN, {"chaos-enabled": "true"}) is None
        assert "chaos-enabled must be" in eligibility_problem(OPT_IN, {})


class TestSelector:
    def test_labels(self):
        exp = experiment(selector=STAGING)
        assert run(exp, {"env": "staging"}).gate("selector").ok
        bad = run(exp, {"env": "prod"})
        assert not bad.eligible
        assert bad.gate("selector").detail == "env must be 'staging' but is 'prod'"

    def test_a_sample_only_selector_has_no_label_gate(self):
        exp = experiment(selector={"sample_percent": 100})
        assert run(exp).gate("selector") is None


class TestWindow:
    WINDOW = {
        "timezone": "UTC",
        "allow": [{"days": ["mon"], "start": "10:00", "end": "16:00"}],
    }

    def test_open(self):
        gate = run(experiment(windows=self.WINDOW)).gate("window")
        assert gate.ok and gate.detail == "open until Mon 2026-11-02 16:00 (UTC)"

    def test_closed(self):
        windows = {
            "timezone": "UTC",
            "allow": [{"days": ["tue"], "start": "10:00", "end": "16:00"}],
        }
        gate = run(experiment(windows=windows)).gate("window")
        assert not gate.ok and "opens Tue 2026-11-03 10:00 (UTC)" in gate.detail

    def test_not_enough_time_left(self):
        exp = experiment(
            windows=self.WINDOW,
            schedule={
                "duration_seconds": 5 * 3600,
                "interval_seconds": 10,
                "max_injections": None,
            },
        )
        gate = run(exp).gate("window")
        assert not gate.ok and "Not enough time left" in gate.detail


class TestSample:
    def test_the_bucket_is_between_0_and_100(self):
        for i in range(200):
            assert 0 <= sample_bucket(f"pod-{i}", "x", "k") < 100

    def test_it_is_deterministic(self):
        assert sample_bucket("pod-1", "x", "k") == sample_bucket("pod-1", "x", "k")

    def test_it_depends_on_every_input(self):
        base = sample_bucket("pod-1", "x", "k")
        assert sample_bucket("pod-2", "x", "k") != base
        assert sample_bucket("pod-1", "y", "k") != base
        assert sample_bucket("pod-1", "x", "other") != base

    def test_inputs_cannot_be_confused_with_each_other(self):
        assert sample_bucket("ab", "c", "") != sample_bucket("a", "bc", "")

    @pytest.mark.parametrize("percent", [1, 10, 25, 50, 90])
    def test_the_share_picked_is_close_to_the_percentage(self, percent):
        picked = sum(
            sample_bucket(f"pod-{i}", "cpu-spike", "") < percent for i in range(20000)
        )
        assert abs(picked / 20000 * 100 - percent) < 1.0

    def test_a_new_key_draws_a_new_sample(self):
        a = {i for i in range(2000) if sample_bucket(f"pod-{i}", "x", "day-1") < 10}
        b = {i for i in range(2000) if sample_bucket(f"pod-{i}", "x", "day-2") < 10}
        assert a != b
        assert len(a & b) < len(a) * 0.5, "the samples should mostly differ"

    def test_the_same_key_picks_the_same_agents(self):
        a = {i for i in range(2000) if sample_bucket(f"pod-{i}", "x", "day-1") < 10}
        b = {i for i in range(2000) if sample_bucket(f"pod-{i}", "x", "day-1") < 10}
        assert a == b

    def test_one_hundred_percent_picks_everyone(self):
        exp = experiment(selector={"sample_percent": 100})
        assert all(run(exp, agent_id=f"pod-{i}").eligible for i in range(300))

    def test_the_gate_explains_itself(self):
        exp = experiment(selector={"sample_percent": 10})
        results = [run(exp, agent_id=f"pod-{i}") for i in range(200)]
        picked = next(r for r in results if r.eligible).gate("sample")
        skipped = next(r for r in results if not r.eligible).gate("sample")
        assert picked.ok and "is in the 10% sample (position " in picked.detail
        assert not skipped.ok and "is not in the 10% sample" in skipped.detail
        assert "needs to be below 10" in skipped.detail

    def test_fractional_percentages(self):
        exp = experiment(selector={"sample_percent": 12.5})
        gate = run(exp, agent_id="pod-1").gate("sample")
        assert "12.5%" in gate.detail

    def test_the_key_changes_the_decision(self):
        exp = experiment(selector={"sample_percent": 50})
        decisions = {
            run(exp, agent_id="pod-7", sample_key=f"k{i}").eligible for i in range(40)
        }
        assert decisions == {True, False}


class TestState:
    def test_the_state_gates_appear_when_they_fail(self):
        result = run(
            kill_switch_reason="3 failed health checks",
            busy_reason="an injection is running (cpu)",
        )
        assert [g.name for g in result.gates] == ["kill_switch", "idle"]
        assert (
            result.gate("kill_switch").detail == "Chaos paused: 3 failed health checks"
        )
        assert result.gate("idle").detail == "an injection is running (cpu)"
        assert not result.eligible

    def test_they_are_listed_as_passing_when_asked(self):
        result = run(report_state=True)
        assert [g.name for g in result.gates] == ["kill_switch", "idle"]
        assert result.eligible and all(g.ok for g in result.gates)

    def test_they_are_left_out_otherwise(self):
        assert run().gates == []


class TestOrderAndSummary:
    def test_gates_are_reported_in_the_documented_order(self):
        windows = {
            "timezone": "UTC",
            "allow": [{"days": ["mon"], "start": "10:00", "end": "16:00"}],
        }
        exp = experiment(
            selector={"match_labels": {"env": "staging"}, "sample_percent": 100},
            windows=windows,
        )
        result = run(
            exp,
            {"env": "staging", "chaos-enabled": "true"},
            eligibility=OPT_IN,
            report_state=True,
        )
        assert [g.name for g in result.gates] == list(GATE_ORDER)
        assert result.eligible

    def test_every_failure_is_listed_in_the_summary(self):
        exp = experiment(selector=STAGING)
        result = run(
            exp,
            {"env": "prod"},
            eligibility=OPT_IN,
            busy_reason="the agent loop is running",
        )
        assert [g.name for g in result.failed] == ["eligibility", "selector", "idle"]
        assert result.summary.startswith("eligibility: chaos-enabled must be")
        assert "selector: env must be 'staging' but is 'prod'" in result.summary
        assert "idle: the agent loop is running" in result.summary

    def test_to_dict(self):
        data = run(experiment(selector=STAGING), {"env": "staging"}).to_dict()
        assert data == {
            "eligible": True,
            "gates": [
                {"gate": "selector", "ok": True, "detail": "labels match the selector"}
            ],
        }


class TestChaosBudget:
    def test_it_is_still_exported_from_the_runner_module(self):
        from src.experiments import chaos_budget_seconds as from_runner

        assert from_runner is chaos_budget_seconds
