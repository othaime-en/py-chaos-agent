"""
Typed, bounded schemas for all kaos configuration.

One set of models validates every way a value can enter the system:

* the YAML file (``load_config``)
* ``PATCH /config`` and ``PATCH /config/failures/{type}``
* ``POST /inject/manual`` overrides

Rules, applied uniformly:

* Unknown keys are rejected (a typo like ``duraton_seconds`` is an error,
  not a silent default).
* Types are strict: ``"10"`` is not an int, ``true`` is not a number.
* Every numeric field has a hard range from ``src.limits``.
* Validated output has all defaults filled in, so downstream code never hits
  a ``KeyError`` mid-injection.
"""

import datetime
import ipaddress
import re
from urllib.parse import urlparse
from typing import Annotated, Any, Dict, List, Literal, Mapping, Optional, Type, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
)
from pydantic import model_validator

from . import limits
from .targets import (
    MAX_SELECTOR_ITEMS,
    MAX_SELECTOR_VALUES,
    TargetLabelsError,
    validate_label_key,
    validate_label_value,
    validate_selector,
)
from .failures.process import validate_target_name

FAILURE_TYPES = ("cpu", "memory", "process", "network")

# Same alphabet as src.failures.network.validate_interface_name
_INTERFACE_PATTERN = rf"^[A-Za-z0-9._:-]{{1,{limits.MAX_INTERFACE_LENGTH}}}$"
# Printable process-name characters; no whitespace, quotes, or shell metacharacters
_TARGET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._@+:-]+$")

Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
DurationSeconds = Annotated[
    int, Field(ge=limits.MIN_DURATION_SECONDS, le=limits.MAX_DURATION_SECONDS)
]


class ConfigValidationError(ValueError):
    """
    Raised when configuration fails validation.

    ``errors`` is a list of ``{"loc": [...], "msg": "..."}`` dicts, the same
    shape FastAPI uses for request errors, so the API can return it as-is.
    """

    def __init__(self, errors: List[Dict[str, Any]]):
        self.errors = errors
        lines = [
            f"  {'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
            for e in errors
        ]
        super().__init__("Invalid configuration:\n" + "\n".join(lines))


def _clean_errors(exc: ValidationError, prefix: tuple = ()) -> List[Dict[str, Any]]:
    """Convert a pydantic error into sanitized {loc, msg} dicts (no input echo)."""
    cleaned = []
    for err in exc.errors():
        msg = err["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        cleaned.append({"loc": list(prefix) + list(err["loc"]), "msg": msg})
    return cleaned


def check_http_url(value: str, field: str) -> str:
    """
    Validate an http(s) URL that the agent will call on its own initiative.

    Shared by the kill switch health URL and the experiment steady-state checks,
    so both reject the same things: a bad scheme, no host, whitespace, and
    credentials (which would end up in logs).
    """
    value = value.strip()
    if not value or len(value) > limits.MAX_HEALTH_URL_LENGTH:
        raise ValueError(
            f"{field} must be 1 to {limits.MAX_HEALTH_URL_LENGTH} characters"
        )
    if any(c.isspace() for c in value):
        raise ValueError(f"{field} must not contain whitespace")
    try:
        parsed = urlparse(value)
    except ValueError:
        raise ValueError(f"{field} is not a valid URL") from None
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field} must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError(f"{field} must include a host")
    if parsed.username or parsed.password:
        raise ValueError(
            f"{field} must not contain credentials; they would be written to logs"
        )
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# ---------------------------------------------------------------------------
# Failure models
# ---------------------------------------------------------------------------


class _FailureBase(_StrictModel):
    enabled: bool = False
    probability: Probability = 0.0


class CpuFailureConfig(_FailureBase):
    cores: int = Field(default=1, ge=limits.MIN_CORES, le=limits.MAX_CORES)
    duration_seconds: DurationSeconds


class MemoryFailureConfig(_FailureBase):
    mb: int = Field(default=100, ge=limits.MIN_MEMORY_MB, le=limits.MAX_MEMORY_MB)
    duration_seconds: DurationSeconds


class ProcessFailureConfig(_FailureBase):
    """
    Kill exactly one process.

    ``target_name`` is matched EXACTLY against process names (never as a
    substring, never against arguments). ``target_pid`` selects one specific
    process; with both, the process must match both. ``target_selection``
    decides which one when several match.
    """

    target_name: Optional[str] = None
    # PID 1 is never a valid target, so the lowest useful PID is 2
    target_pid: Optional[int] = Field(default=None, ge=2)
    target_selection: Literal["oldest", "newest"] = "oldest"

    @field_validator("target_name")
    @classmethod
    def _check_target_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if len(value) > limits.MAX_TARGET_NAME_LENGTH:
            raise ValueError(
                f"target_name is too long (max {limits.MAX_TARGET_NAME_LENGTH})"
            )
        if not _TARGET_NAME_PATTERN.match(value):
            raise ValueError(
                "target_name may only contain letters, digits, and . _ @ + : -"
            )
        is_valid, error = validate_target_name(value)
        if not is_valid:
            raise ValueError(error)
        return value

    @model_validator(mode="after")
    def _require_target_when_enabled(self) -> "ProcessFailureConfig":
        if self.enabled and not self.target_name and self.target_pid is None:
            raise ValueError(
                "target_name or target_pid is required when process failure is enabled"
            )
        return self


_UNSAFE_DESTINATIONS = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, includes cloud metadata endpoints
        "224.0.0.0/4",  # multicast
        "0.0.0.0/8",  # "this network", includes the unspecified address
        "::1/128",
        "fe80::/10",
        "ff00::/8",
        "::/128",
    )
]


class NetworkDestination(_StrictModel):
    """
    Delay only traffic to this destination, not the whole interface.

    ``cidr`` is an address or a network (``10.0.4.7``, ``10.0.4.0/24``,
    ``2001:db8::/48``). With ``port`` only that destination port is delayed;
    ``protocol`` is then ``tcp`` unless you say ``udp``. Without a port the whole
    destination is delayed, for ``protocol`` or for any protocol if it is unset.

    Loopback, link-local (cloud metadata), multicast and the unspecified address
    are refused, as are networks wider than /16 (IPv4) or /48 (IPv6): a delay that
    broad is not "one dependency" and would also hit the agent's own API and the
    platform's health probes.
    """

    cidr: str
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    protocol: Optional[Literal["tcp", "udp"]] = None

    @field_validator("cidr")
    @classmethod
    def _valid_destination(cls, value: str) -> str:
        try:
            network = ipaddress.ip_network(value.strip(), strict=True)
        except ValueError:
            raise ValueError(
                "cidr must be an IP address or a network such as 10.0.4.0/24 "
                "(no host bits set, no hostnames)"
            ) from None
        if any(
            network.version == bad.version and network.overlaps(bad)
            for bad in _UNSAFE_DESTINATIONS
        ):
            raise ValueError(
                "cidr must not include loopback, link-local, multicast or the "
                "unspecified address"
            )
        minimum = (
            limits.MIN_DESTINATION_PREFIX_V4
            if network.version == 4
            else limits.MIN_DESTINATION_PREFIX_V6
        )
        if network.prefixlen < minimum:
            raise ValueError(
                f"cidr is too wide: use /{minimum} or narrower, or no destinations "
                "to delay the whole interface"
            )
        return str(network)

    @model_validator(mode="after")
    def _port_needs_a_protocol(self) -> "NetworkDestination":
        if self.port is not None and self.protocol is None:
            self.protocol = "tcp"
        return self


class NetworkFailureConfig(_FailureBase):
    interface: str = Field(default="eth0", pattern=_INTERFACE_PATTERN)
    delay_ms: int = Field(default=100, ge=limits.MIN_DELAY_MS, le=limits.MAX_DELAY_MS)
    duration_seconds: DurationSeconds
    # Empty: delay everything leaving the interface (the original behaviour).
    destinations: List[NetworkDestination] = Field(
        default_factory=list, max_length=limits.MAX_NETWORK_DESTINATIONS
    )

    @field_validator("destinations")
    @classmethod
    def _no_duplicate_destinations(
        cls, value: List[NetworkDestination]
    ) -> List[NetworkDestination]:
        seen = set()
        for d in value:
            key = (d.cidr, d.port, d.protocol)
            if key in seen:
                raise ValueError(f"duplicate destination {d.cidr}")
            seen.add(key)
        return value


FAILURE_MODELS: Dict[str, Type[_FailureBase]] = {
    "cpu": CpuFailureConfig,
    "memory": MemoryFailureConfig,
    "process": ProcessFailureConfig,
    "network": NetworkFailureConfig,
}


# ---------------------------------------------------------------------------
# Top-level models
# ---------------------------------------------------------------------------


class SelectorExpression(_StrictModel):
    """``In`` and ``NotIn`` need values; ``Exists`` and ``DoesNotExist`` take none."""

    key: str
    operator: Literal["In", "NotIn", "Exists", "DoesNotExist"]
    values: List[str] = Field(default_factory=list, max_length=MAX_SELECTOR_VALUES)

    @field_validator("key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        return validate_label_key(value)

    @field_validator("values")
    @classmethod
    def _valid_values(cls, value: List[str]) -> List[str]:
        for item in value:
            validate_label_value(item)
        return value

    @model_validator(mode="after")
    def _values_fit_the_operator(self) -> "SelectorExpression":
        if self.operator in ("In", "NotIn") and not self.values:
            raise ValueError(f"operator {self.operator} needs at least one value")
        if self.operator in ("Exists", "DoesNotExist") and self.values:
            raise ValueError(f"operator {self.operator} takes no values")
        return self


class TargetSelector(_StrictModel):
    """
    Where an experiment may run, by the labels of the agent's target.

    Every condition must hold. The experiment is refused on an agent whose labels
    (``CHAOS_TARGET_LABELS`` / ``CHAOS_TARGET_LABELS_FILE``) do not satisfy it. See
    ``src.targets`` for the rules.
    """

    match_labels: Dict[str, str] = Field(
        default_factory=dict, max_length=MAX_SELECTOR_ITEMS
    )
    match_expressions: List[SelectorExpression] = Field(
        default_factory=list, max_length=MAX_SELECTOR_ITEMS
    )
    # Of the agents that match, only this share runs the experiment. Each agent
    # decides for itself from a stable hash of its identity, the experiment name
    # and the run's sample key, so the same agents are picked every time unless the
    # key changes. See src.selection.
    sample_percent: Optional[float] = Field(
        default=None, gt=0, le=100, allow_inf_nan=False
    )

    @field_validator("match_labels")
    @classmethod
    def _valid_labels(cls, value: Dict[str, str]) -> Dict[str, str]:
        for key, item in value.items():
            validate_label_key(key)
            validate_label_value(item, key)
        return value

    @model_validator(mode="after")
    def _not_empty(self) -> "TargetSelector":
        if not self.match_labels and not self.match_expressions:
            if self.sample_percent is None:
                raise ValueError(
                    "a selector needs match_labels, match_expressions or "
                    "sample_percent; an empty selector would match every agent"
                )
            return self
        try:
            validate_selector(self.model_dump())
        except TargetLabelsError as e:
            raise ValueError(str(e)) from None
        return self


class AgentSettings(_StrictModel):
    interval_seconds: int = Field(
        default=10,
        ge=limits.MIN_INTERVAL_SECONDS,
        le=limits.MAX_INTERVAL_SECONDS,
    )
    dry_run: bool = False
    # API mode only: start injecting as soon as the API starts, instead of
    # waiting for POST /agent/start. The standalone agent always runs.
    auto_start: bool = False
    # Opt-in gate for everything this agent does (experiments, the loop and manual
    # injections): chaos only runs while the agent's labels satisfy it. A pod that
    # forgot the label is safe. Operator-only, like the safety limits; it cannot be
    # changed through the API.
    eligibility: Optional[TargetSelector] = None

    @field_validator("eligibility")
    @classmethod
    def _eligibility_is_labels_only(
        cls, value: Optional[TargetSelector]
    ) -> Optional[TargetSelector]:
        if value is not None and value.sample_percent is not None:
            raise ValueError("eligibility cannot use sample_percent")
        if value is not None and not (value.match_labels or value.match_expressions):
            raise ValueError("eligibility needs match_labels or match_expressions")
        return value


class FailuresSection(_StrictModel):
    cpu: Optional[CpuFailureConfig] = None
    memory: Optional[MemoryFailureConfig] = None
    process: Optional[ProcessFailureConfig] = None
    network: Optional[NetworkFailureConfig] = None


class SafetyConfig(_StrictModel):
    """
    Operator-only safety knobs (see src/resources.py). Not settable through
    the API, so a remote caller cannot loosen them.
    """

    cpu_fraction: float = Field(
        default=limits.DEFAULT_CPU_FRACTION,
        ge=limits.MIN_SAFETY_FRACTION,
        le=limits.MAX_CPU_FRACTION,
        allow_inf_nan=False,
    )
    memory_fraction: float = Field(
        default=limits.DEFAULT_MEMORY_FRACTION,
        ge=limits.MIN_SAFETY_FRACTION,
        le=limits.MAX_MEMORY_FRACTION,
        allow_inf_nan=False,
    )
    require_cgroup_limits: bool = False


class KillSwitchSettings(_StrictModel):
    """
    The kill switch (see src/kill_switch.py). Operator-only: no API request can
    change these, so a remote caller cannot weaken the protection.
    """

    enabled: bool = True
    target_health_url: str = "http://localhost:8080/health"
    health_check_interval_seconds: int = Field(
        default=10,
        ge=limits.MIN_KS_INTERVAL_SECONDS,
        le=limits.MAX_KS_INTERVAL_SECONDS,
    )
    health_check_timeout_seconds: int = Field(
        default=2,
        ge=limits.MIN_KS_TIMEOUT_SECONDS,
        le=limits.MAX_KS_TIMEOUT_SECONDS,
    )
    consecutive_failures_threshold: int = Field(
        default=3, ge=limits.MIN_KS_THRESHOLD, le=limits.MAX_KS_THRESHOLD
    )
    grace_after_process_kill_seconds: int = Field(
        default=60, ge=limits.MIN_KS_GRACE_SECONDS, le=limits.MAX_KS_GRACE_SECONDS
    )
    grace_after_agent_start_seconds: int = Field(
        default=30, ge=limits.MIN_KS_GRACE_SECONDS, le=limits.MAX_KS_GRACE_SECONDS
    )
    recovery_test_interval_seconds: int = Field(
        default=120,
        ge=limits.MIN_KS_RECOVERY_INTERVAL_SECONDS,
        le=limits.MAX_KS_RECOVERY_INTERVAL_SECONDS,
    )
    recovery_success_threshold: int = Field(
        default=3, ge=limits.MIN_KS_THRESHOLD, le=limits.MAX_KS_THRESHOLD
    )
    # True: chaos pauses while the breaker is tripped and resumes by itself once
    # the target has recovered. False: chaos stays off after a trip until an
    # operator calls POST /kill-switch/reset (or restarts a standalone agent).
    resume_after_recovery: bool = True
    # False: nobody can turn the kill switch off through the API.
    allow_api_disable: bool = False

    @field_validator("target_health_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return check_http_url(value, "target_health_url")

    @model_validator(mode="after")
    def _timeout_fits_interval(self) -> "KillSwitchSettings":
        if self.health_check_timeout_seconds > self.health_check_interval_seconds:
            raise ValueError(
                "health_check_timeout_seconds must not exceed "
                "health_check_interval_seconds"
            )
        return self


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------

_EXPERIMENT_NAME_PATTERN = (
    rf"^[a-z0-9][a-z0-9._-]{{0,{limits.MAX_EXPERIMENT_NAME_LENGTH - 1}}}$"
)
_CHECK_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_START_TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
_END_TIME_PATTERN = r"^(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$"


class HttpCheckConfig(_StrictModel):
    """
    One steady-state check: an HTTP request that must succeed.

    Healthy means the status matches ``expected_status`` (any 2xx when it is
    not set) and, if ``max_latency_ms`` is set, the response arrived in time.
    Redirects are not followed.
    """

    type: Literal["http"] = "http"
    name: str = Field(pattern=_CHECK_NAME_PATTERN)
    url: str
    expected_status: Optional[int] = Field(default=None, ge=100, le=599)
    max_latency_ms: Optional[int] = Field(
        default=None, ge=limits.MIN_CHECK_LATENCY_MS, le=limits.MAX_CHECK_LATENCY_MS
    )
    timeout_seconds: int = Field(
        default=2,
        ge=limits.MIN_CHECK_TIMEOUT_SECONDS,
        le=limits.MAX_CHECK_TIMEOUT_SECONDS,
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return check_http_url(value, "url")

    @model_validator(mode="after")
    def _latency_fits_timeout(self) -> "HttpCheckConfig":
        if (
            self.max_latency_ms is not None
            and self.max_latency_ms > self.timeout_seconds * 1000
        ):
            raise ValueError(
                "max_latency_ms must not exceed timeout_seconds * 1000, or the "
                "limit could never be reached"
            )
        return self


class PrometheusCheckConfig(_StrictModel):
    """
    A check on a Prometheus (or compatible) query: an error rate, a latency
    percentile, anything PromQL can say as one number.

    ``query`` is an instant query that must return a single value (aggregate it
    with ``sum()`` or ``histogram_quantile()``). The check passes while
    ``value <operator> threshold`` holds, for example::

        query: 'sum(rate(errors_total[1m])) / sum(rate(requests_total[1m]))'
        operator: "<"
        threshold: 0.01

    A query that cannot be answered (the server is down, the query is wrong, it
    returns several series) is reported as *unavailable* rather than as a breach,
    and so is "no data" unless ``on_no_data: pass``. Authentication is a bearer
    token read from the environment variable named in ``bearer_token_env``; the
    token itself never goes in the config.
    """

    type: Literal["prometheus"]
    name: str = Field(pattern=_CHECK_NAME_PATTERN)
    url: str
    query: str = Field(min_length=1, max_length=limits.MAX_PROMETHEUS_QUERY_LENGTH)
    operator: Literal["<", "<=", ">", ">="]
    threshold: float = Field(allow_inf_nan=False)
    timeout_seconds: int = Field(
        default=5,
        ge=limits.MIN_CHECK_TIMEOUT_SECONDS,
        le=limits.MAX_CHECK_TIMEOUT_SECONDS,
    )
    on_no_data: Literal["fail", "pass"] = "fail"
    bearer_token_env: Optional[str] = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$"
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return check_http_url(value, "url")

    @field_validator("query")
    @classmethod
    def _printable_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        if any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("query must not contain control characters")
        return value


def _check_kind(value: Any) -> str:
    """The check type: ``http`` when a config does not say (the original form)."""
    if isinstance(value, dict):
        return str(value.get("type", "http"))
    return str(getattr(value, "type", "http"))


# One entry in a list of checks. ``type`` defaults to http so existing configs,
# which never wrote it, keep working.
Check = Annotated[
    Union[
        Annotated[HttpCheckConfig, Tag("http")],
        Annotated[PrometheusCheckConfig, Tag("prometheus")],
    ],
    Discriminator(
        _check_kind,
        custom_error_type="unknown_check_type",
        custom_error_message="unknown check type; use 'http' or 'prometheus'",
    ),
]


def _unique_names(checks: List[Any]) -> List[Any]:
    seen = set()
    for check in checks:
        if check.name in seen:
            raise ValueError(f"duplicate check name '{check.name}'")
        seen.add(check.name)
    return checks


class SloConfig(_StrictModel):
    """
    Limits the system must stay within *while the chaos runs*. They are checked
    every ``check_interval_seconds``; if they fail ``breach_after`` rounds in a row
    the experiment is aborted at once, its effects are removed, and the run is
    recorded as falsified (the system did not stay within its limits). A check that
    could not be evaluated at all aborts the run too, as inconclusive, because
    continuing blind is not safe.

    ``grace_seconds`` delays the first round after the chaos starts, for metrics
    that need time to show an effect. Dry runs evaluate the checks and report, but
    are never aborted.
    """

    checks: List[Check] = Field(min_length=1, max_length=limits.MAX_SLO_CHECKS)
    check_interval_seconds: int = Field(
        default=5,
        ge=limits.MIN_SLO_INTERVAL_SECONDS,
        le=limits.MAX_SLO_INTERVAL_SECONDS,
    )
    breach_after: int = Field(
        default=2,
        ge=limits.MIN_SLO_BREACH_AFTER,
        le=limits.MAX_SLO_BREACH_AFTER,
    )
    grace_seconds: int = Field(
        default=0,
        ge=limits.MIN_SLO_GRACE_SECONDS,
        le=limits.MAX_SLO_GRACE_SECONDS,
    )

    @field_validator("checks")
    @classmethod
    def _unique_check_names(cls, value: List[Any]) -> List[Any]:
        return _unique_names(value)


class SteadyStateConfig(_StrictModel):
    """
    What "the system is healthy" means for an experiment.

    The state is checked before any chaos starts (the experiment is refused if
    the target is not healthy to begin with) and again after it ends, for up to
    ``recovery_timeout_seconds``. It counts as reached once every check passes
    ``required_consecutive_successes`` rounds in a row.
    """

    checks: List[Check] = Field(min_length=1, max_length=limits.MAX_STEADY_STATE_CHECKS)
    check_interval_seconds: int = Field(
        default=2,
        ge=limits.MIN_STEADY_CHECK_INTERVAL_SECONDS,
        le=limits.MAX_STEADY_CHECK_INTERVAL_SECONDS,
    )
    required_consecutive_successes: int = Field(
        default=2,
        ge=limits.MIN_REQUIRED_SUCCESSES,
        le=limits.MAX_REQUIRED_SUCCESSES,
    )
    baseline_timeout_seconds: int = Field(
        default=30,
        ge=limits.MIN_BASELINE_TIMEOUT_SECONDS,
        le=limits.MAX_BASELINE_TIMEOUT_SECONDS,
    )
    recovery_timeout_seconds: int = Field(
        default=60,
        ge=limits.MIN_RECOVERY_TIMEOUT_SECONDS,
        le=limits.MAX_RECOVERY_TIMEOUT_SECONDS,
    )

    @field_validator("checks")
    @classmethod
    def _unique_check_names(cls, value: List[Any]) -> List[Any]:
        return _unique_names(value)


class ExperimentSchedule(_StrictModel):
    """
    How an experiment's chaos is paced.

    The faults are rolled every ``interval_seconds`` (the first roll happens
    right away) until ``duration_seconds`` have passed or ``max_injections``
    injections have started, whichever comes first.
    """

    duration_seconds: int = Field(
        ge=limits.MIN_EXPERIMENT_DURATION_SECONDS,
        le=limits.MAX_EXPERIMENT_DURATION_SECONDS,
    )
    interval_seconds: int = Field(
        default=10,
        ge=limits.MIN_INTERVAL_SECONDS,
        le=limits.MAX_INTERVAL_SECONDS,
    )
    max_injections: Optional[int] = Field(
        default=None,
        ge=limits.MIN_EXPERIMENT_INJECTIONS,
        le=limits.MAX_EXPERIMENT_INJECTIONS,
    )
    # At most this many injections running at once (they are already limited to
    # one per type). 1 means a single fault at a time, never cpu and network
    # together. None: no cap beyond one per type.
    max_concurrent_injections: Optional[int] = Field(
        default=None,
        ge=limits.MIN_CONCURRENT_INJECTIONS,
        le=limits.MAX_CONCURRENT_INJECTIONS,
    )


# Inside an experiment a listed fault is meant to run, so the defaults differ
# from the global ``failures`` section (disabled, probability 0). Everything
# else, including every hard limit, is inherited unchanged.


class ExperimentCpuFault(CpuFailureConfig):
    enabled: bool = True
    probability: Probability = 1.0


class ExperimentMemoryFault(MemoryFailureConfig):
    enabled: bool = True
    probability: Probability = 1.0


class ExperimentProcessFault(ProcessFailureConfig):
    enabled: bool = True
    probability: Probability = 1.0


class ExperimentNetworkFault(NetworkFailureConfig):
    enabled: bool = True
    probability: Probability = 1.0


class ExperimentFaults(_StrictModel):
    cpu: Optional[ExperimentCpuFault] = None
    memory: Optional[ExperimentMemoryFault] = None
    process: Optional[ExperimentProcessFault] = None
    network: Optional[ExperimentNetworkFault] = None

    @model_validator(mode="after")
    def _something_can_fire(self) -> "ExperimentFaults":
        faults = [getattr(self, name) for name in FAILURE_TYPES]
        if not any(f is not None and f.enabled and f.probability > 0 for f in faults):
            raise ValueError(
                "an experiment needs at least one fault that is enabled with a "
                "probability above 0"
            )
        return self


def _every_day() -> List[Weekday]:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class TimeWindow(_StrictModel):
    """
    Weekdays plus a start and an end time. See ``src.windows`` for the rules
    (end exclusive, past midnight when end is not after start, ``24:00`` ok).
    """

    days: List[Weekday] = Field(default_factory=_every_day, min_length=1, max_length=7)
    start: str = Field(pattern=_START_TIME_PATTERN)
    end: str = Field(pattern=_END_TIME_PATTERN)

    @field_validator("days")
    @classmethod
    def _unique_days(cls, value: List[str]) -> List[str]:
        if len(set(value)) != len(value):
            raise ValueError("days must not repeat")
        return value

    @model_validator(mode="after")
    def _not_empty(self) -> "TimeWindow":
        if self.start == self.end:
            raise ValueError(
                "start and end must differ (use 00:00 to 24:00 for a whole day)"
            )
        return self


class Blackout(_StrictModel):
    """
    A period when the experiment must not run, whatever the windows say: a deploy
    freeze, a sale, a holiday. ``from`` and ``to`` are dates, both inclusive, whole
    days in the windows' time zone.
    """

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    reason: str = Field(default="", max_length=limits.MAX_BLACKOUT_REASON_LENGTH)

    @field_validator("from_", "to", mode="before")
    @classmethod
    def _yaml_dates(cls, value: Any) -> Any:
        # An unquoted 2026-12-20 in YAML arrives as a date, not a string.
        if isinstance(value, datetime.date) and not isinstance(
            value, datetime.datetime
        ):
            return value.isoformat()
        return value

    @field_validator("from_", "to")
    @classmethod
    def _real_date(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("must be a date written YYYY-MM-DD")
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"'{value}' is not a real date") from None
        return value

    @model_validator(mode="after")
    def _ordered_and_bounded(self) -> "Blackout":
        first = datetime.date.fromisoformat(self.from_)
        last = datetime.date.fromisoformat(self.to)
        if last < first:
            raise ValueError("'to' must not be before 'from'")
        if (last - first).days + 1 > limits.MAX_BLACKOUT_DAYS:
            raise ValueError(
                f"a blackout can last at most {limits.MAX_BLACKOUT_DAYS} days; "
                "use several"
            )
        return self


class ExperimentWindows(_StrictModel):
    """
    When an experiment may run. Outside every ``allow`` window, or inside a
    blackout, it will not start. Without ``allow`` it may run at any time except
    during a blackout.
    """

    timezone: str = "UTC"
    allow: List[TimeWindow] = Field(
        default_factory=list, max_length=limits.MAX_TIME_WINDOWS
    )
    blackouts: List[Blackout] = Field(
        default_factory=list, max_length=limits.MAX_BLACKOUTS
    )

    @model_validator(mode="after")
    def _says_something(self) -> "ExperimentWindows":
        if not self.allow and not self.blackouts:
            raise ValueError("windows needs allow windows or blackouts")
        return self

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        if not value or len(value) > limits.MAX_TIMEZONE_LENGTH:
            raise ValueError(
                f"timezone must be 1 to {limits.MAX_TIMEZONE_LENGTH} characters"
            )
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(
                f"unknown timezone '{value}'; use an IANA name such as "
                "'UTC' or 'Europe/Berlin'"
            ) from None
        return value


class ExperimentConfig(_StrictModel):
    """
    A named, self-contained chaos experiment.

    Hypothesis: a sentence saying what you expect, for example "the service
    answers /health again within 30 seconds after its CPU was saturated". The
    run verdict says whether the steady state held up that expectation.
    """

    name: str = Field(pattern=_EXPERIMENT_NAME_PATTERN)
    description: str = Field(default="", max_length=limits.MAX_DESCRIPTION_LENGTH)
    hypothesis: str = Field(min_length=1, max_length=limits.MAX_HYPOTHESIS_LENGTH)
    steady_state: SteadyStateConfig
    faults: ExperimentFaults
    schedule: ExperimentSchedule
    selector: Optional[TargetSelector] = None
    windows: Optional[ExperimentWindows] = None
    slo: Optional[SloConfig] = None
    dry_run: bool = False

    @field_validator("hypothesis")
    @classmethod
    def _hypothesis_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("hypothesis must not be blank")
        return value

    def to_dict(self) -> Dict[str, Any]:
        """Plain dict with defaults filled in; only the faults actually used."""
        data = self.model_dump(exclude={"faults"}, by_alias=True)
        data["faults"] = {
            name: getattr(self.faults, name).model_dump()
            for name in FAILURE_TYPES
            if getattr(self.faults, name) is not None
        }
        return data


class AppConfig(_StrictModel):
    agent: AgentSettings
    failures: FailuresSection
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    # Validated elsewhere (logging_config). Passed through as-is.
    logging: Optional[Dict[str, Any]] = None
    kill_switch: KillSwitchSettings = Field(default_factory=KillSwitchSettings)
    experiments: List[ExperimentConfig] = Field(
        default_factory=list, max_length=limits.MAX_EXPERIMENTS
    )

    @field_validator("experiments")
    @classmethod
    def _unique_experiment_names(
        cls, value: List[ExperimentConfig]
    ) -> List[ExperimentConfig]:
        seen = set()
        for experiment in value:
            if experiment.name in seen:
                raise ValueError(f"duplicate experiment name '{experiment.name}'")
            seen.add(experiment.name)
        return value


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def validate_config_dict(raw: Any) -> Dict[str, Any]:
    """
    Validate a whole config document and return it normalized (defaults filled
    in). The ``logging`` section passes through untouched.

    Raises:
        ConfigValidationError: with every problem found, not just the first.
    """
    if not isinstance(raw, Mapping):
        raise ConfigValidationError(
            [{"loc": [], "msg": "configuration must be a mapping at the top level"}]
        )

    try:
        model = AppConfig.model_validate(dict(raw))
    except ValidationError as exc:
        raise ConfigValidationError(_clean_errors(exc)) from None

    normalized: Dict[str, Any] = {
        "agent": model.agent.model_dump(),
        "failures": {
            name: getattr(model.failures, name).model_dump()
            for name in FAILURE_TYPES
            if getattr(model.failures, name) is not None
        },
        "safety": model.safety.model_dump(),
        "kill_switch": model.kill_switch.model_dump(),
    }
    if model.logging is not None:
        normalized["logging"] = model.logging
    if model.experiments:
        normalized["experiments"] = [e.to_dict() for e in model.experiments]
    return normalized


def validate_failure_config(
    failure_type: str, data: Mapping[str, Any]
) -> Dict[str, Any]:
    """
    Validate one failure's full config and return it normalized.

    Raises:
        ConfigValidationError: on any violation.
        KeyError: if ``failure_type`` is not a known failure type.
    """
    model_cls = FAILURE_MODELS[failure_type]
    try:
        model = model_cls.model_validate(dict(data))
    except ValidationError as exc:
        raise ConfigValidationError(_clean_errors(exc, (failure_type,))) from None
    dumped: Dict[str, Any] = model.model_dump()
    return dumped


def merge_failure_config(
    failure_type: str, current: Mapping[str, Any], updates: Mapping[str, Any]
) -> Dict[str, Any]:
    """
    Overlay ``updates`` on ``current`` and validate the *result*.

    Validating the merged whole (not just the delta) means a partial update
    can never leave a failure in an invalid state, for example enabling the
    process failure without a target.
    """
    return validate_failure_config(failure_type, {**current, **updates})
