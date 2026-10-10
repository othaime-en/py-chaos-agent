"""Tests for the config schemas and hard limits."""

import math
from pathlib import Path

import pytest
import yaml

from src import limits
from src.config import load_config
from src.schemas import (
    ConfigValidationError,
    merge_failure_config,
    validate_config_dict,
    validate_failure_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def doc(**failures):
    """Build a minimal valid config document."""
    return {"agent": {}, "failures": failures}


def errors_for(raw):
    with pytest.raises(ConfigValidationError) as exc:
        validate_config_dict(raw)
    return exc.value


def error_paths(raw):
    return {".".join(str(p) for p in e["loc"]) for e in errors_for(raw).errors}


# ---------------------------------------------------------------------------
# Boundaries: every numeric field accepts its min and max, rejects just outside
# ---------------------------------------------------------------------------

BOUNDS = [
    # (failure, field, min, max, base config without the field)
    ("cpu", "cores", limits.MIN_CORES, limits.MAX_CORES, {"duration_seconds": 5}),
    (
        "cpu",
        "duration_seconds",
        limits.MIN_DURATION_SECONDS,
        limits.MAX_DURATION_SECONDS,
        {},
    ),
    (
        "memory",
        "mb",
        limits.MIN_MEMORY_MB,
        limits.MAX_MEMORY_MB,
        {"duration_seconds": 5},
    ),
    (
        "memory",
        "duration_seconds",
        limits.MIN_DURATION_SECONDS,
        limits.MAX_DURATION_SECONDS,
        {},
    ),
    (
        "network",
        "delay_ms",
        limits.MIN_DELAY_MS,
        limits.MAX_DELAY_MS,
        {"duration_seconds": 5},
    ),
    (
        "network",
        "duration_seconds",
        limits.MIN_DURATION_SECONDS,
        limits.MAX_DURATION_SECONDS,
        {},
    ),
]


@pytest.mark.parametrize("failure,field,low,high,base", BOUNDS)
def test_numeric_bounds(failure, field, low, high, base):
    for ok in (low, high):
        validate_failure_config(failure, {**base, field: ok})
    for bad in (low - 1, high + 1):
        with pytest.raises(ConfigValidationError):
            validate_failure_config(failure, {**base, field: bad})


def test_interval_bounds():
    for ok in (limits.MIN_INTERVAL_SECONDS, limits.MAX_INTERVAL_SECONDS):
        validate_config_dict({"agent": {"interval_seconds": ok}, "failures": {}})
    for bad in (0, -5, limits.MAX_INTERVAL_SECONDS + 1):
        assert "agent.interval_seconds" in error_paths(
            {"agent": {"interval_seconds": bad}, "failures": {}}
        )


@pytest.mark.parametrize("p", [0, 0.0, 0.5, 1, 1.0])
def test_probability_accepts_valid(p):
    validate_failure_config("cpu", {"duration_seconds": 5, "probability": p})


@pytest.mark.parametrize("p", [-0.1, 1.01, 5, math.nan, math.inf, -math.inf])
def test_probability_rejects_invalid(p):
    with pytest.raises(ConfigValidationError):
        validate_failure_config("cpu", {"duration_seconds": 5, "probability": p})


# ---------------------------------------------------------------------------
# Strict typing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["2", 2.0, True, None, [2]])
def test_cores_must_be_a_real_int(bad):
    with pytest.raises(ConfigValidationError):
        validate_failure_config("cpu", {"duration_seconds": 5, "cores": bad})


@pytest.mark.parametrize("bad", ["true", "yes", 1, 0, None])
def test_enabled_must_be_a_real_bool(bad):
    with pytest.raises(ConfigValidationError):
        validate_failure_config("cpu", {"duration_seconds": 5, "enabled": bad})


def test_interval_string_rejected():
    assert "agent.interval_seconds" in error_paths(
        {"agent": {"interval_seconds": "10"}, "failures": {}}
    )


def test_dry_run_string_rejected():
    assert "agent.dry_run" in error_paths(
        {"agent": {"dry_run": "false"}, "failures": {}}
    )


# ---------------------------------------------------------------------------
# Unknown / missing keys
# ---------------------------------------------------------------------------


def test_typo_in_failure_key_is_an_error():
    paths = error_paths(doc(cpu={"duraton_seconds": 5, "duration_seconds": 5}))
    assert "failures.cpu.duraton_seconds" in paths


def test_unknown_failure_type_rejected():
    assert "failures.disk" in error_paths(doc(disk={"enabled": True}))


def test_unknown_top_level_section_rejected():
    assert "surprise" in error_paths({"agent": {}, "failures": {}, "surprise": {}})


def test_unknown_agent_key_rejected():
    assert "agent.interval" in error_paths({"agent": {"interval": 5}, "failures": {}})


@pytest.mark.parametrize("failure", ["cpu", "memory", "network"])
def test_duration_is_required(failure):
    assert f"failures.{failure}.duration_seconds" in error_paths(doc(**{failure: {}}))


def test_agent_and_failures_sections_required():
    assert "agent" in error_paths({"failures": {}})
    assert "failures" in error_paths({"agent": {}})


@pytest.mark.parametrize("raw", [None, [], "text", 5])
def test_non_mapping_root_rejected(raw):
    with pytest.raises(ConfigValidationError):
        validate_config_dict(raw)


def test_all_errors_reported_at_once():
    paths = error_paths(
        {
            "agent": {"interval_seconds": 0},
            "failures": {
                "cpu": {"duration_seconds": 9999, "cores": 1000},
                "network": {"duration_seconds": 5, "delay_ms": -1},
            },
        }
    )
    assert paths == {
        "agent.interval_seconds",
        "failures.cpu.duration_seconds",
        "failures.cpu.cores",
        "failures.network.delay_ms",
    }


# ---------------------------------------------------------------------------
# Process target
# ---------------------------------------------------------------------------


def test_process_target_required_when_enabled():
    assert "failures.process" in error_paths(doc(process={"enabled": True}))


def test_process_target_optional_when_disabled():
    out = validate_config_dict(doc(process={"enabled": False}))
    assert out["failures"]["process"]["target_name"] is None


@pytest.mark.parametrize("name", ["python", "python3", "systemd", "ab", ""])
def test_process_target_rejects_broad_or_short(name):
    with pytest.raises(ConfigValidationError):
        validate_failure_config("process", {"target_name": name})


@pytest.mark.parametrize(
    "name", ["my app", "x;rm -rf /", "a$(id)b", "name\nnewline", "q'uote", "a/b"]
)
def test_process_target_rejects_unsafe_characters(name):
    with pytest.raises(ConfigValidationError):
        validate_failure_config("process", {"target_name": name})


def test_process_target_length_cap():
    with pytest.raises(ConfigValidationError):
        validate_failure_config(
            "process", {"target_name": "a" * (limits.MAX_TARGET_NAME_LENGTH + 1)}
        )


def test_process_target_is_stripped():
    out = validate_failure_config("process", {"target_name": "  my-app  "})
    assert out["target_name"] == "my-app"


@pytest.mark.parametrize("name", ["target-app", "my_app.v2", "svc@host", "web:8080"])
def test_process_target_accepts_normal_names(name):
    assert validate_failure_config("process", {"target_name": name})["target_name"]


# ---------------------------------------------------------------------------
# Network interface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["eth0", "ens5", "br-1a2b", "veth.1", "a" * 15])
def test_interface_accepts_valid(name):
    out = validate_failure_config("network", {"duration_seconds": 5, "interface": name})
    assert out["interface"] == name


@pytest.mark.parametrize(
    "name", ["", "a" * 16, "eth0; rm -rf /", "eth 0", "eth0`id`", "$(x)", "eth0\n", 5]
)
def test_interface_rejects_invalid(name):
    with pytest.raises(ConfigValidationError):
        validate_failure_config("network", {"duration_seconds": 5, "interface": name})


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_defaults_are_filled_in():
    out = validate_config_dict(
        doc(
            cpu={"duration_seconds": 5},
            memory={"duration_seconds": 5},
            network={"duration_seconds": 5},
        )
    )
    assert out["agent"] == {"interval_seconds": 10, "dry_run": False}
    assert out["failures"]["cpu"] == {
        "enabled": False,
        "probability": 0.0,
        "cores": 1,
        "duration_seconds": 5,
    }
    assert out["failures"]["memory"]["mb"] == 100
    assert out["failures"]["network"]["interface"] == "eth0"
    assert out["failures"]["network"]["delay_ms"] == 100


def test_only_configured_failures_appear():
    out = validate_config_dict(doc(cpu={"duration_seconds": 5}))
    assert list(out["failures"]) == ["cpu"]


def test_logging_and_kill_switch_pass_through_untouched():
    raw = doc()
    raw["logging"] = {"level": "DEBUG", "anything": {"goes": 1}}
    raw["kill_switch"] = {"enabled": True, "whatever": 3}
    out = validate_config_dict(raw)
    assert out["logging"] == raw["logging"]
    assert out["kill_switch"] == raw["kill_switch"]


def test_input_is_not_mutated():
    raw = doc(cpu={"duration_seconds": 5})
    validate_config_dict(raw)
    assert raw == doc(cpu={"duration_seconds": 5})


# ---------------------------------------------------------------------------
# Error object
# ---------------------------------------------------------------------------


def test_error_shape_and_message():
    err = errors_for(doc(cpu={"duration_seconds": 5, "cores": 1000}))
    assert err.errors == [
        {"loc": ["failures", "cpu", "cores"], "msg": err.errors[0]["msg"]}
    ]
    assert "failures.cpu.cores" in str(err)
    assert isinstance(err, ValueError)


def test_error_does_not_echo_input_field():
    err = errors_for(doc(cpu={"duration_seconds": 5, "cores": 123456789}))
    assert all(set(e) == {"loc", "msg"} for e in err.errors)


# ---------------------------------------------------------------------------
# merge_failure_config
# ---------------------------------------------------------------------------


def test_merge_applies_partial_update():
    current = validate_failure_config("cpu", {"duration_seconds": 5, "cores": 2})
    merged = merge_failure_config("cpu", current, {"cores": 4})
    assert merged["cores"] == 4
    assert merged["duration_seconds"] == 5


def test_merge_validates_the_result_not_just_the_delta():
    current = validate_failure_config("process", {"enabled": False})
    with pytest.raises(ConfigValidationError):
        merge_failure_config("process", current, {"enabled": True})


def test_merge_rejects_unknown_key():
    current = validate_failure_config("cpu", {"duration_seconds": 5})
    with pytest.raises(ConfigValidationError):
        merge_failure_config("cpu", current, {"cores_typo": 2})


def test_merge_does_not_mutate_current():
    current = validate_failure_config("cpu", {"duration_seconds": 5})
    snapshot = dict(current)
    merge_failure_config("cpu", current, {"cores": 3})
    assert current == snapshot


# ---------------------------------------------------------------------------
# limits helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, True),
        (16, True),
        (0, True),
        (16.0, True),
        (17, False),
        (True, False),
        ("4", False),
        (None, False),
        (float("inf"), False),
    ],
)
def test_within_upper_bound(value, expected):
    assert limits.within_upper_bound(value, 16) is expected


# ---------------------------------------------------------------------------
# Files shipped in the repo must satisfy their own schema
# ---------------------------------------------------------------------------


def test_shipped_config_yaml_is_valid():
    load_config(str(REPO_ROOT / "config.yaml"))


def test_kubernetes_configmap_is_valid():
    docs = yaml.safe_load_all((REPO_ROOT / "k8s" / "chaos-demo.yaml").read_text())
    found = False
    for d in docs:
        if d and d.get("kind") == "ConfigMap" and "config.yaml" in d.get("data", {}):
            validate_config_dict(yaml.safe_load(d["data"]["config.yaml"]))
            found = True
    assert found, "no ConfigMap with config.yaml found"


# ---------------------------------------------------------------------------
# safety section
# ---------------------------------------------------------------------------


def test_safety_defaults_are_normalized_in():
    out = validate_config_dict(doc())
    assert out["safety"] == {
        "cpu_fraction": limits.DEFAULT_CPU_FRACTION,
        "memory_fraction": limits.DEFAULT_MEMORY_FRACTION,
        "require_cgroup_limits": False,
    }


def test_safety_accepts_valid_values():
    raw = doc()
    raw["safety"] = {
        "cpu_fraction": limits.MAX_CPU_FRACTION,
        "memory_fraction": limits.MIN_SAFETY_FRACTION,
        "require_cgroup_limits": True,
    }
    assert validate_config_dict(raw)["safety"]["require_cgroup_limits"] is True


@pytest.mark.parametrize(
    "key,bad",
    [
        ("cpu_fraction", 0.05),
        ("cpu_fraction", 0.95),
        ("cpu_fraction", 1.0),
        ("cpu_fraction", float("nan")),
        ("memory_fraction", 0.05),
        ("memory_fraction", 0.85),
        ("memory_fraction", 1.0),
        ("memory_fraction", "0.5"),
        ("require_cgroup_limits", "true"),
        ("require_cgroup_limits", 1),
    ],
)
def test_safety_rejects_out_of_range_or_wrong_type(key, bad):
    raw = doc()
    raw["safety"] = {key: bad}
    assert f"safety.{key}" in error_paths(raw)


def test_safety_rejects_unknown_key():
    raw = doc()
    raw["safety"] = {"cpu_fractionn": 0.5}
    assert "safety.cpu_fractionn" in error_paths(raw)
