"""
Typed, bounded schemas for all Py-Chaos-Agent configuration.

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

import re
from urllib.parse import urlparse
from typing import Annotated, Any, Dict, List, Literal, Mapping, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic import model_validator

from . import limits
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


class NetworkFailureConfig(_FailureBase):
    interface: str = Field(default="eth0", pattern=_INTERFACE_PATTERN)
    delay_ms: int = Field(default=100, ge=limits.MIN_DELAY_MS, le=limits.MAX_DELAY_MS)
    duration_seconds: DurationSeconds


FAILURE_MODELS: Dict[str, Type[_FailureBase]] = {
    "cpu": CpuFailureConfig,
    "memory": MemoryFailureConfig,
    "process": ProcessFailureConfig,
    "network": NetworkFailureConfig,
}


# ---------------------------------------------------------------------------
# Top-level models
# ---------------------------------------------------------------------------


class AgentSettings(_StrictModel):
    interval_seconds: int = Field(
        default=10,
        ge=limits.MIN_INTERVAL_SECONDS,
        le=limits.MAX_INTERVAL_SECONDS,
    )
    dry_run: bool = False


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
        value = value.strip()
        if not value or len(value) > limits.MAX_HEALTH_URL_LENGTH:
            raise ValueError(
                f"target_health_url must be 1 to {limits.MAX_HEALTH_URL_LENGTH} "
                "characters"
            )
        if any(c.isspace() for c in value):
            raise ValueError("target_health_url must not contain whitespace")
        try:
            parsed = urlparse(value)
        except ValueError:
            raise ValueError("target_health_url is not a valid URL") from None
        if parsed.scheme not in ("http", "https"):
            raise ValueError("target_health_url must start with http:// or https://")
        if not parsed.hostname:
            raise ValueError("target_health_url must include a host")
        if parsed.username or parsed.password:
            raise ValueError(
                "target_health_url must not contain credentials; they would be "
                "written to logs"
            )
        return value

    @model_validator(mode="after")
    def _timeout_fits_interval(self) -> "KillSwitchSettings":
        if self.health_check_timeout_seconds > self.health_check_interval_seconds:
            raise ValueError(
                "health_check_timeout_seconds must not exceed "
                "health_check_interval_seconds"
            )
        return self


class AppConfig(_StrictModel):
    agent: AgentSettings
    failures: FailuresSection
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    # Validated elsewhere (logging_config). Passed through as-is.
    logging: Optional[Dict[str, Any]] = None
    kill_switch: KillSwitchSettings = Field(default_factory=KillSwitchSettings)


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
