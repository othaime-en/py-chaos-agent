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
            "max_concurrent_injections": None,
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
        assert "cpu-recovery" in config.experiments
        for name, exp in config.experiments.items():
            assert exp["name"] == name


class TestWindows:
    def windows(self, *allow, **extra):
        return {"allow": list(allow), **extra}

    def valid(self, windows):
        out = validate_config_dict(doc(experiment(windows=windows)))
        return out["experiments"][0]["windows"]

    def test_windows_are_optional_and_off_by_default(self):
        out = validate_config_dict(doc(experiment()))["experiments"][0]
        assert out["windows"] is None

    def test_defaults_are_utc_and_every_day(self):
        out = self.valid(self.windows({"start": "10:00", "end": "16:00"}))
        assert out["timezone"] == "UTC"
        assert out["allow"][0]["days"] == [
            "mon",
            "tue",
            "wed",
            "thu",
            "fri",
            "sat",
            "sun",
        ]

    def test_a_full_window(self):
        out = self.valid(
            self.windows(
                {"days": ["mon", "fri"], "start": "09:30", "end": "24:00"},
                timezone="Africa/Dar_es_Salaam",
            )
        )
        assert out["timezone"] == "Africa/Dar_es_Salaam"
        assert out["allow"][0] == {
            "days": ["mon", "fri"],
            "start": "09:30",
            "end": "24:00",
        }

    def test_a_window_past_midnight_is_fine(self):
        self.valid(self.windows({"start": "22:00", "end": "02:00"}))

    @pytest.mark.parametrize(
        "field, value",
        [
            ("start", "9:00"),
            ("start", "24:00"),
            ("start", "25:00"),
            ("start", "10:60"),
            ("start", "10:00:00"),
            ("start", ""),
            ("end", "24:01"),
            ("end", "10"),
            ("end", "noon"),
        ],
    )
    def test_bad_times(self, field, value):
        window = {"start": "10:00", "end": "16:00", field: value}
        raw = doc(experiment(windows=self.windows(window)))
        assert f"experiments.0.windows.allow.0.{field}" in paths(raw)

    def test_start_and_end_must_differ(self):
        raw = doc(experiment(windows=self.windows({"start": "10:00", "end": "10:00"})))
        assert "must differ" in str(errors_for(raw))

    @pytest.mark.parametrize(
        "days", [[], ["Mon"], ["monday"], ["mon", "mon"], ["mon", 1]]
    )
    def test_bad_days(self, days):
        raw = doc(
            experiment(
                windows=self.windows({"days": days, "start": "10:00", "end": "11:00"})
            )
        )
        assert "experiments.0.windows.allow.0.days" in {
            ".".join(str(p) for p in e["loc"][:6]) for e in errors_for(raw).errors
        }

    @pytest.mark.parametrize(
        "zone", ["Mars/Base", "utc+3", "../etc/passwd", "", "x" * 65, "Europe"]
    )
    def test_bad_timezones(self, zone):
        raw = doc(
            experiment(
                windows=self.windows({"start": "10:00", "end": "11:00"}, timezone=zone)
            )
        )
        assert "experiments.0.windows.timezone" in paths(raw)

    def test_the_timezone_error_says_what_to_use(self):
        raw = doc(
            experiment(
                windows=self.windows(
                    {"start": "10:00", "end": "11:00"}, timezone="Mars/Base"
                )
            )
        )
        assert "IANA name" in str(errors_for(raw))

    def test_windows_must_say_something(self):
        for windows in ({"allow": []}, {}):
            raw = doc(experiment(windows=windows))
            assert "needs allow windows or blackouts" in str(errors_for(raw))

    def test_too_many_windows(self):
        many = [{"start": "10:00", "end": "11:00"}] * (limits.MAX_TIME_WINDOWS + 1)
        assert "experiments.0.windows.allow" in paths(
            doc(experiment(windows={"allow": many}))
        )

    def test_unknown_keys(self):
        window = {"start": "10:00", "end": "11:00", "weekday": "mon"}
        raw = doc(experiment(windows=self.windows(window)))
        assert "experiments.0.windows.allow.0.weekday" in paths(raw)


class TestSelector:
    def valid(self, selector):
        out = validate_config_dict(doc(experiment(selector=selector)))
        return out["experiments"][0]["selector"]

    def bad(self, selector):
        return paths(doc(experiment(selector=selector)))

    def test_selectors_are_optional_and_off_by_default(self):
        out = validate_config_dict(doc(experiment()))["experiments"][0]
        assert out["selector"] is None

    def test_match_labels(self):
        out = self.valid(
            {"match_labels": {"env": "staging", "app.kubernetes.io/name": "checkout"}}
        )
        assert out["match_labels"] == {
            "env": "staging",
            "app.kubernetes.io/name": "checkout",
        }
        assert out["match_expressions"] == []

    def test_expressions(self):
        out = self.valid(
            {
                "match_expressions": [
                    {"key": "tier", "operator": "In", "values": ["canary", "preview"]},
                    {"key": "chaos-optout", "operator": "DoesNotExist"},
                ]
            }
        )
        assert out["match_expressions"][1] == {
            "key": "chaos-optout",
            "operator": "DoesNotExist",
            "values": [],
        }

    def test_both_together(self):
        self.valid(
            {
                "match_labels": {"env": "staging"},
                "match_expressions": [{"key": "chaos-ok", "operator": "Exists"}],
            }
        )

    @pytest.mark.parametrize(
        "selector", [{}, {"match_labels": {}}, {"match_expressions": []}]
    )
    def test_an_empty_selector_is_rejected(self, selector):
        raw = doc(experiment(selector=selector))
        assert "would match every agent" in str(errors_for(raw))

    @pytest.mark.parametrize("key", ["bad key", "-x", "", "a/b/c", "x" * 64])
    def test_bad_label_keys(self, key):
        assert "experiments.0.selector.match_labels" in self.bad(
            {"match_labels": {key: "v"}}
        )

    @pytest.mark.parametrize("value", ["bad value", "-x", "x" * 64, "a,b"])
    def test_bad_label_values(self, value):
        assert "experiments.0.selector.match_labels" in self.bad(
            {"match_labels": {"k": value}}
        )

    def test_label_values_must_be_strings(self):
        assert errors_for(doc(experiment(selector={"match_labels": {"k": 1}}))).errors

    @pytest.mark.parametrize(
        "expr",
        [
            {"key": "a", "operator": "In"},
            {"key": "a", "operator": "NotIn", "values": []},
            {"key": "a", "operator": "Exists", "values": ["x"]},
            {"key": "a", "operator": "DoesNotExist", "values": ["x"]},
        ],
    )
    def test_operator_and_values_must_agree(self, expr):
        raw = doc(experiment(selector={"match_expressions": [expr]}))
        assert errors_for(raw).errors

    def test_unknown_operator(self):
        expr = {"key": "a", "operator": "Equals", "values": ["x"]}
        assert "experiments.0.selector.match_expressions.0.operator" in self.bad(
            {"match_expressions": [expr]}
        )

    def test_a_bad_expression_key_or_value_points_at_it(self):
        expr = {"key": "bad key", "operator": "In", "values": ["ok"]}
        assert "experiments.0.selector.match_expressions.0.key" in self.bad(
            {"match_expressions": [expr]}
        )
        expr = {"key": "ok", "operator": "In", "values": ["bad value"]}
        assert "experiments.0.selector.match_expressions.0.values" in self.bad(
            {"match_expressions": [expr]}
        )

    def test_limits(self):
        many = {f"k{i}": "v" for i in range(21)}
        assert "experiments.0.selector.match_labels" in self.bad({"match_labels": many})
        exprs = [{"key": f"k{i}", "operator": "Exists"} for i in range(21)]
        assert "experiments.0.selector.match_expressions" in self.bad(
            {"match_expressions": exprs}
        )
        values = {"key": "a", "operator": "In", "values": ["v"] * 21}
        assert errors_for(
            doc(experiment(selector={"match_expressions": [values]}))
        ).errors

    def test_unknown_keys(self):
        assert "experiments.0.selector.labels" in self.bad({"labels": {"a": "b"}})


class TestSampleAndConcurrency:
    def selector(self, **fields):
        out = validate_config_dict(doc(experiment(selector=fields)))
        return out["experiments"][0]["selector"]

    @pytest.mark.parametrize("percent", [0.01, 1, 10, 12.5, 100])
    def test_good_percentages(self, percent):
        assert self.selector(sample_percent=percent)["sample_percent"] == percent

    @pytest.mark.parametrize(
        "percent", [0, -1, 100.01, 101, float("nan"), float("inf"), "10", True]
    )
    def test_bad_percentages(self, percent):
        raw = doc(experiment(selector={"sample_percent": percent}))
        assert (
            "experiments.0.selector.sample_percent" in paths(raw)
            or errors_for(raw).errors
        )

    def test_a_sample_alone_is_a_valid_selector(self):
        out = self.selector(sample_percent=10)
        assert out["match_labels"] == {} and out["match_expressions"] == []

    def test_a_sample_with_labels(self):
        out = self.selector(match_labels={"env": "staging"}, sample_percent=10)
        assert out["match_labels"] == {"env": "staging"}

    def test_a_selector_with_nothing_is_still_rejected(self):
        raw = doc(experiment(selector={}))
        assert "would match every agent" in str(errors_for(raw))

    def test_the_percentage_is_optional(self):
        assert self.selector(match_labels={"env": "x"})["sample_percent"] is None

    @pytest.mark.parametrize("value", [1, 2, 4])
    def test_good_concurrency_caps(self, value):
        raw = doc(
            experiment(
                schedule={"duration_seconds": 30, "max_concurrent_injections": value}
            )
        )
        out = validate_config_dict(raw)["experiments"][0]
        assert out["schedule"]["max_concurrent_injections"] == value

    @pytest.mark.parametrize("value", [0, 5, -1, "1", True])
    def test_bad_concurrency_caps(self, value):
        raw = doc(
            experiment(
                schedule={"duration_seconds": 30, "max_concurrent_injections": value}
            )
        )
        assert "experiments.0.schedule.max_concurrent_injections" in paths(raw)


class TestAgentEligibility:
    def agent(self, **fields):
        return validate_config_dict({"agent": fields, "failures": {}})["agent"]

    def test_off_by_default(self):
        assert self.agent()["eligibility"] is None

    def test_an_opt_in_label(self):
        out = self.agent(eligibility={"match_labels": {"chaos-enabled": "true"}})
        assert out["eligibility"]["match_labels"] == {"chaos-enabled": "true"}

    def test_expressions(self):
        out = self.agent(
            eligibility={
                "match_expressions": [
                    {"key": "chaos", "operator": "NotIn", "values": ["opt-out"]}
                ]
            }
        )
        assert out["eligibility"]["match_expressions"][0]["operator"] == "NotIn"

    def test_it_cannot_be_empty(self):
        with pytest.raises(ConfigValidationError):
            self.agent(eligibility={})

    def test_a_sample_makes_no_sense_here(self):
        with pytest.raises(ConfigValidationError, match="cannot use sample_percent"):
            self.agent(eligibility={"match_labels": {"a": "b"}, "sample_percent": 50})

    def test_a_sample_alone_is_not_an_eligibility_rule(self):
        with pytest.raises(ConfigValidationError):
            self.agent(eligibility={"sample_percent": 50})

    def test_label_rules_apply(self):
        with pytest.raises(ConfigValidationError):
            self.agent(eligibility={"match_labels": {"bad key": "x"}})

    def test_it_loads_into_the_config(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "agent": {
                        "eligibility": {"match_labels": {"chaos-enabled": "true"}}
                    },
                    "failures": {},
                }
            )
        )
        assert load_config(str(path)).agent.eligibility["match_labels"] == {
            "chaos-enabled": "true"
        }


class TestBlackouts:
    def windows(self, *blackouts, **extra):
        return {"blackouts": list(blackouts), **extra}

    def valid(self, *blackouts, **extra):
        out = validate_config_dict(
            doc(experiment(windows=self.windows(*blackouts, **extra)))
        )
        return out["experiments"][0]["windows"]

    def test_blackouts_alone_are_enough(self):
        out = self.valid(
            {"from": "2026-12-20", "to": "2027-01-03", "reason": "holiday freeze"}
        )
        assert out["allow"] == []
        assert out["blackouts"] == [
            {"from": "2026-12-20", "to": "2027-01-03", "reason": "holiday freeze"}
        ]

    def test_with_allow_windows(self):
        out = self.valid(
            {"from": "2026-12-24", "to": "2026-12-24"},
            allow=[{"start": "10:00", "end": "16:00"}],
        )
        assert len(out["allow"]) == 1 and out["blackouts"][0]["reason"] == ""

    def test_the_output_uses_from_and_to(self):
        out = self.valid({"from": "2026-12-24", "to": "2026-12-24"})
        assert set(out["blackouts"][0]) == {"from", "to", "reason"}

    def test_unquoted_yaml_dates_work(self):
        raw = yaml.safe_load("""
agent: {}
failures: {}
experiments:
  - name: x
    hypothesis: h
    steady_state: {checks: [{name: a, url: "http://localhost:1/h"}]}
    faults: {cpu: {duration_seconds: 5}}
    schedule: {duration_seconds: 10}
    windows:
      blackouts:
        - from: 2026-12-20
          to: 2027-01-03
""")
        out = validate_config_dict(raw)["experiments"][0]["windows"]["blackouts"][0]
        assert (out["from"], out["to"]) == ("2026-12-20", "2027-01-03")

    @pytest.mark.parametrize(
        "bad",
        [
            "2026-02-30",
            "2026-13-01",
            "2026-1-5",
            "12/20/2026",
            "20261220",
            "2026-12-20T10:00",
            "tomorrow",
            "",
            20261220,
        ],
    )
    def test_bad_dates(self, bad):
        raw = doc(experiment(windows=self.windows({"from": bad, "to": "2027-01-03"})))
        assert errors_for(raw).errors

    def test_to_must_not_be_before_from(self):
        raw = doc(
            experiment(windows=self.windows({"from": "2026-12-20", "to": "2026-12-19"}))
        )
        assert "must not be before 'from'" in str(errors_for(raw))

    def test_a_blackout_can_be_at_most_a_year(self):
        self.valid({"from": "2026-01-01", "to": "2026-12-31"})
        raw = doc(
            experiment(windows=self.windows({"from": "2026-01-01", "to": "2027-01-02"}))
        )
        assert "at most 366 days" in str(errors_for(raw))

    def test_missing_dates(self):
        raw = doc(experiment(windows=self.windows({"from": "2026-12-20"})))
        assert "experiments.0.windows.blackouts.0.to" in paths(raw)

    def test_the_reason_is_capped(self):
        raw = doc(
            experiment(
                windows=self.windows(
                    {"from": "2026-12-20", "to": "2026-12-21", "reason": "x" * 101}
                )
            )
        )
        assert "experiments.0.windows.blackouts.0.reason" in paths(raw)

    def test_unknown_keys(self):
        raw = doc(
            experiment(
                windows=self.windows(
                    {"from": "2026-12-20", "to": "2026-12-21", "why": "x"}
                )
            )
        )
        assert "experiments.0.windows.blackouts.0.why" in paths(raw)

    def test_too_many(self):
        many = [{"from": "2026-12-20", "to": "2026-12-21"}] * (limits.MAX_BLACKOUTS + 1)
        assert "experiments.0.windows.blackouts" in paths(
            doc(experiment(windows={"blackouts": many}))
        )

    def test_it_loads_through_the_config_file(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "agent: {}\nfailures: {}\nexperiments:\n"
            "  - name: x\n    hypothesis: h\n"
            "    steady_state: {checks: [{name: a, url: 'http://localhost:1/h'}]}\n"
            "    faults: {cpu: {duration_seconds: 5}}\n    schedule: {duration_seconds: 10}\n"
            "    windows:\n      blackouts:\n"
            "        - {from: 2026-12-20, to: 2027-01-03, reason: freeze}\n"
        )
        config = load_config(str(path))
        assert (
            config.experiments["x"]["windows"]["blackouts"][0]["from"] == "2026-12-20"
        )
