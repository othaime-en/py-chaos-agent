"""Experiment schema: validation, defaults, limits and config loading."""

import copy

import pytest
import yaml

from src import limits
from src.config import load_config
from src.schemas import ConfigValidationError, validate_config_dict

CHECK = {"name": "health", "url": "http://localhost:8080/health"}

EXPERIMENT = {
    "name": "cpu-spike",
    "description": "Saturate two cores",
    "hypothesis": "The service answers /health again within 60 seconds",
    "steady_state": {"checks": [CHECK]},
    "faults": {"cpu": {"cores": 2, "duration_seconds": 5}},
    "schedule": {"duration_seconds": 30},
}


def experiment(**overrides):
    data = copy.deepcopy(EXPERIMENT)
    data.update(overrides)
    return data


def doc(*experiments):
    return {"agent": {}, "failures": {}, "experiments": list(experiments)}


def errors_for(raw):
    with pytest.raises(ConfigValidationError) as exc:
        validate_config_dict(raw)
    return exc.value


def paths(raw):
    return {".".join(str(p) for p in e["loc"]) for e in errors_for(raw).errors}


class TestDefaults:
    def test_a_minimal_experiment_is_valid_and_filled_in(self):
        out = validate_config_dict(doc(experiment()))["experiments"][0]
        assert out["name"] == "cpu-spike"
        assert out["dry_run"] is False
        assert out["schedule"] == {
            "duration_seconds": 30,
            "interval_seconds": 10,
            "max_injections": None,
        }
        ss = out["steady_state"]
        assert ss["check_interval_seconds"] == 2
        assert ss["required_consecutive_successes"] == 2
        assert ss["baseline_timeout_seconds"] == 30
        assert ss["recovery_timeout_seconds"] == 60
        assert ss["checks"][0] == {
            "type": "http",
            "name": "health",
            "url": "http://localhost:8080/health",
            "expected_status": None,
            "max_latency_ms": None,
            "timeout_seconds": 2,
        }

    def test_listed_faults_are_enabled_and_certain_by_default(self):
        fault = validate_config_dict(doc(experiment()))["experiments"][0]["faults"]
        assert fault["cpu"]["enabled"] is True
        assert fault["cpu"]["probability"] == 1.0

    def test_only_the_listed_faults_are_in_the_output(self):
        faults = validate_config_dict(doc(experiment()))["experiments"][0]["faults"]
        assert list(faults) == ["cpu"]

    def test_no_experiments_means_no_key(self):
        assert "experiments" not in validate_config_dict({"agent": {}, "failures": {}})

    def test_experiments_do_not_touch_the_global_failures(self):
        out = validate_config_dict(doc(experiment()))
        assert out["failures"] == {}


class TestFaultsReuseTheFailureRules:
    def test_hard_limits_apply(self):
        raw = doc(experiment(faults={"cpu": {"cores": 999, "duration_seconds": 5}}))
        assert "experiments.0.faults.cpu.cores" in paths(raw)

    def test_a_missing_duration_is_an_error(self):
        raw = doc(experiment(faults={"cpu": {"cores": 1}}))
        assert "experiments.0.faults.cpu.duration_seconds" in paths(raw)

    def test_unknown_fault_types_are_rejected(self):
        raw = doc(experiment(faults={"disk": {"mb": 1}}))
        assert "experiments.0.faults.disk" in paths(raw)

    def test_unknown_fault_keys_are_rejected(self):
        raw = doc(
            experiment(faults={"cpu": {"duraton_seconds": 5, "duration_seconds": 5}})
        )
        assert "experiments.0.faults.cpu.duraton_seconds" in paths(raw)

    def test_a_process_fault_needs_a_target(self):
        raw = doc(experiment(faults={"process": {}}))
        assert "target_name or target_pid is required" in str(errors_for(raw))

    def test_dangerous_process_names_are_still_refused(self):
        raw = doc(experiment(faults={"process": {"target_name": "systemd"}}))
        assert errors_for(raw).errors

    def test_strict_types(self):
        raw = doc(experiment(faults={"cpu": {"cores": "2", "duration_seconds": 5}}))
        assert "experiments.0.faults.cpu.cores" in paths(raw)

    @pytest.mark.parametrize(
        "faults",
        [
            {},
            {"cpu": {"duration_seconds": 5, "enabled": False}},
            {"cpu": {"duration_seconds": 5, "probability": 0.0}},
        ],
    )
    def test_at_least_one_fault_must_be_able_to_fire(self, faults):
        raw = doc(experiment(faults=faults))
        assert "at least one fault" in str(errors_for(raw))

    def test_every_fault_type_can_be_used(self):
        faults = {
            "cpu": {"duration_seconds": 5},
            "memory": {"duration_seconds": 5},
            "process": {"target_name": "my-app"},
            "network": {"duration_seconds": 5},
        }
        out = validate_config_dict(doc(experiment(faults=faults)))
        assert list(out["experiments"][0]["faults"]) == [
            "cpu",
            "memory",
            "process",
            "network",
        ]


class TestNameAndText:
    @pytest.mark.parametrize(
        "name", ["a", "cpu-spike", "a.b_c-1", "x" * limits.MAX_EXPERIMENT_NAME_LENGTH]
    )
    def test_good_names(self, name):
        validate_config_dict(doc(experiment(name=name)))

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "-a",
            "A",
            "has space",
            "../x",
            "a/b",
            "x" * (limits.MAX_EXPERIMENT_NAME_LENGTH + 1),
        ],
    )
    def test_bad_names(self, name):
        assert "experiments.0.name" in paths(doc(experiment(name=name)))

    def test_duplicate_names_are_rejected(self):
        raw = doc(experiment(), experiment())
        assert "duplicate experiment name 'cpu-spike'" in str(errors_for(raw))

    def test_a_hypothesis_is_required(self):
        data = experiment()
        del data["hypothesis"]
        assert "experiments.0.hypothesis" in paths(doc(data))

    @pytest.mark.parametrize("text", ["", "   "])
    def test_a_blank_hypothesis_is_rejected(self, text):
        assert "experiments.0.hypothesis" in paths(doc(experiment(hypothesis=text)))

    def test_text_lengths_are_capped(self):
        too_long = "x" * (limits.MAX_HYPOTHESIS_LENGTH + 1)
        assert "experiments.0.hypothesis" in paths(doc(experiment(hypothesis=too_long)))
        too_long = "x" * (limits.MAX_DESCRIPTION_LENGTH + 1)
        assert "experiments.0.description" in paths(
            doc(experiment(description=too_long))
        )

    def test_unknown_keys_are_rejected(self):
        assert "experiments.0.hypotesis" in paths(doc(experiment(hypotesis="typo")))

    def test_too_many_experiments(self):
        many = [experiment(name=f"e{i}") for i in range(limits.MAX_EXPERIMENTS + 1)]
        assert "experiments" in paths(doc(*many))


class TestSchedule:
    def test_duration_is_required(self):
        assert "experiments.0.schedule.duration_seconds" in paths(
            doc(experiment(schedule={}))
        )

    @pytest.mark.parametrize(
        "value",
        [
            limits.MIN_EXPERIMENT_DURATION_SECONDS,
            limits.MAX_EXPERIMENT_DURATION_SECONDS,
        ],
    )
    def test_duration_bounds_are_inclusive(self, value):
        validate_config_dict(doc(experiment(schedule={"duration_seconds": value})))

    @pytest.mark.parametrize(
        "value",
        [
            limits.MIN_EXPERIMENT_DURATION_SECONDS - 1,
            limits.MAX_EXPERIMENT_DURATION_SECONDS + 1,
        ],
    )
    def test_duration_outside_the_bounds(self, value):
        raw = doc(experiment(schedule={"duration_seconds": value}))
        assert "experiments.0.schedule.duration_seconds" in paths(raw)

    @pytest.mark.parametrize(
        "field, value",
        [
            ("interval_seconds", 0),
            ("max_injections", 0),
            ("max_injections", limits.MAX_EXPERIMENT_INJECTIONS + 1),
        ],
    )
    def test_other_bounds(self, field, value):
        raw = doc(experiment(schedule={"duration_seconds": 30, field: value}))
        assert f"experiments.0.schedule.{field}" in paths(raw)


class TestSteadyState:
    def test_at_least_one_check(self):
        raw = doc(experiment(steady_state={"checks": []}))
        assert "experiments.0.steady_state.checks" in paths(raw)

    def test_too_many_checks(self):
        checks = [
            {**CHECK, "name": f"c{i}"}
            for i in range(limits.MAX_STEADY_STATE_CHECKS + 1)
        ]
        raw = doc(experiment(steady_state={"checks": checks}))
        assert "experiments.0.steady_state.checks" in paths(raw)

    def test_check_names_must_be_unique(self):
        raw = doc(experiment(steady_state={"checks": [CHECK, CHECK]}))
        assert "duplicate check name 'health'" in str(errors_for(raw))

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://x/health",
            "http://",
            "localhost:8080/health",
            "http://user:pw@host/health",
            "http://host/he alth",
            "",
        ],
    )
    def test_bad_urls(self, url):
        raw = doc(experiment(steady_state={"checks": [{**CHECK, "url": url}]}))
        assert "experiments.0.steady_state.checks.0.url" in paths(raw)

    def test_https_is_fine(self):
        check = {**CHECK, "url": "https://app.internal:8443/healthz"}
        validate_config_dict(doc(experiment(steady_state={"checks": [check]})))

    @pytest.mark.parametrize("status", [99, 600, True, "200"])
    def test_expected_status_must_be_a_real_status(self, status):
        check = {**CHECK, "expected_status": status}
        raw = doc(experiment(steady_state={"checks": [check]}))
        assert "experiments.0.steady_state.checks.0.expected_status" in paths(raw)

    def test_latency_limit_cannot_exceed_the_timeout(self):
        check = {**CHECK, "timeout_seconds": 2, "max_latency_ms": 2001}
        raw = doc(experiment(steady_state={"checks": [check]}))
        assert "could never be reached" in str(errors_for(raw))
        check["max_latency_ms"] = 2000
        validate_config_dict(doc(experiment(steady_state={"checks": [check]})))

    def test_unknown_check_types_are_rejected(self):
        check = {**CHECK, "type": "prometheus"}
        raw = doc(experiment(steady_state={"checks": [check]}))
        assert "experiments.0.steady_state.checks.0.type" in paths(raw)

    @pytest.mark.parametrize(
        "field, low, high",
        [
            (
                "check_interval_seconds",
                limits.MIN_STEADY_CHECK_INTERVAL_SECONDS,
                limits.MAX_STEADY_CHECK_INTERVAL_SECONDS,
            ),
            (
                "required_consecutive_successes",
                limits.MIN_REQUIRED_SUCCESSES,
                limits.MAX_REQUIRED_SUCCESSES,
            ),
            (
                "baseline_timeout_seconds",
                limits.MIN_BASELINE_TIMEOUT_SECONDS,
                limits.MAX_BASELINE_TIMEOUT_SECONDS,
            ),
            (
                "recovery_timeout_seconds",
                limits.MIN_RECOVERY_TIMEOUT_SECONDS,
                limits.MAX_RECOVERY_TIMEOUT_SECONDS,
            ),
        ],
    )
    def test_bounds(self, field, low, high):
        for ok in (low, high):
            validate_config_dict(
                doc(experiment(steady_state={"checks": [CHECK], field: ok}))
            )
        for bad in (low - 1, high + 1):
            raw = doc(experiment(steady_state={"checks": [CHECK], field: bad}))
            assert f"experiments.0.steady_state.{field}" in paths(raw)


class TestErrorsAreAllReported:
    def test_every_problem_is_listed_not_just_the_first(self):
        raw = doc(
            experiment(
                name="BAD NAME",
                hypothesis="",
                schedule={"duration_seconds": 0},
            )
        )
        found = paths(raw)
        assert {
            "experiments.0.name",
            "experiments.0.hypothesis",
            "experiments.0.schedule.duration_seconds",
        } <= found


class TestLoadConfig:
    def write(self, tmp_path, experiments):
        data = {
            "agent": {"interval_seconds": 10},
            "failures": {
                "cpu": {"enabled": False, "probability": 0.1, "duration_seconds": 5}
            },
            "experiments": experiments,
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data))
        return str(path)

    def test_experiments_are_available_by_name(self, tmp_path):
        config = load_config(
            self.write(tmp_path, [experiment(), experiment(name="other")])
        )
        assert list(config.experiments) == ["cpu-spike", "other"]
        assert config.experiments["cpu-spike"]["faults"]["cpu"]["cores"] == 2

    def test_a_config_without_experiments_loads(self, tmp_path):
        config = load_config(self.write(tmp_path, []))
        assert config.experiments == {}

    def test_an_invalid_experiment_stops_the_load(self, tmp_path):
        with pytest.raises(ConfigValidationError):
            load_config(self.write(tmp_path, [experiment(hypothesis="")]))

    def test_the_shipped_example_config_loads(self):
        config = load_config("config.yaml")
        for name, exp in config.experiments.items():
            assert exp["name"] == name
