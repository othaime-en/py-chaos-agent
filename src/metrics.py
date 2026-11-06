import os
from typing import Any, Optional

from prometheus_client import REGISTRY, Counter, Gauge

DEFAULT_METRICS_PORT = 8000
# Loopback only, like the control API. To let Prometheus reach it, set
# CHAOS_METRICS_HOST (for example 0.0.0.0) together with a scrape token; see
# src/metrics_http.py. (It used to listen on every interface with no credentials.)
DEFAULT_METRICS_ADDR = "127.0.0.1"
METRICS_PORT_ENV = "CHAOS_METRICS_PORT"

# Metrics
INJECTIONS_TOTAL = Counter(
    "chaos_injections_total",
    "Total number of chaos injections",
    # status: success (ran for its full duration), aborted (cut short by a stop,
    # the kill switch or shutdown), skipped (dry run, or refused by the overlap
    # guard), failed
    ["failure_type", "status"],
)

INJECTIONS_CLAMPED = Counter(
    "chaos_injections_clamped_total",
    "Injections reduced to fit the container's resource budget",
    ["failure_type"],
)

KILL_SWITCH_STATE = Gauge(
    "chaos_kill_switch_state",
    "Kill switch circuit state: 0 closed (chaos allowed), 1 open (tripped), "
    "2 half-open (testing recovery)",
)

KILL_SWITCH_TRIPS = Counter(
    "chaos_kill_switch_trips_total",
    "Times the kill switch tripped because the target became unhealthy",
)

KILL_SWITCH_HEALTH_CHECKS = Counter(
    "chaos_kill_switch_health_checks_total",
    "Target health checks made by the kill switch",
    ["result"],  # healthy or unhealthy
)

AUTH_FAILURES_TOTAL = Counter(
    "chaos_auth_failures_total",
    "Rejected authentication attempts",
    # surface: api or metrics. reason: missing (no credential sent), invalid
    # (wrong credential), expired (a real token past its expiry) or locked_out
    # (refused during a lockout).
    ["surface", "reason"],
)

AUTH_LOCKOUTS_TOTAL = Counter(
    "chaos_auth_lockouts_total",
    "Clients locked out after repeated failed authentication",
    ["surface"],
)

AUDIT_WRITE_FAILURES_TOTAL = Counter(
    "chaos_audit_write_failures_total",
    "Audit events that could not be written to the audit log file",
)

EXPERIMENTS_TOTAL = Counter(
    "chaos_experiments_total",
    "Experiments that have finished",
    # status: completed, aborted, refused (the target was not healthy to begin
    # with) or error. verdict: confirmed, falsified or inconclusive.
    ["status", "verdict"],
)

SLO_ABORTS_TOTAL = Counter(
    "chaos_slo_aborts_total",
    "Experiments stopped because their SLO checks failed",
    # kind: breach (a limit was violated) or unavailable (it could not be checked).
    ["kind"],
)

EXPERIMENT_ACTIVE = Gauge(
    "chaos_experiment_active", "1 while an experiment is running, otherwise 0"
)

INJECTION_ACTIVE = Gauge(
    "chaos_injection_active", "Currently active chaos injection", ["failure_type"]
)


class MetricsServerError(RuntimeError):
    """The metrics server could not start (for example, the port is in use)."""


def metrics_port_from_env(default: int = DEFAULT_METRICS_PORT) -> int:
    """The metrics port: CHAOS_METRICS_PORT if set, else ``default``."""
    raw = os.environ.get(METRICS_PORT_ENV, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        raise MetricsServerError(
            f"{METRICS_PORT_ENV} must be a port number, got {raw!r}"
        ) from None
    if not 1 <= port <= 65535:
        raise MetricsServerError(f"{METRICS_PORT_ENV} must be 1-65535, got {port}")
    return port


def start_metrics_server(
    port: int = DEFAULT_METRICS_PORT, addr: Optional[str] = None
) -> Any:
    """
    Start the metrics endpoint and return ``(server, thread)`` once it is
    listening.

    ``addr`` defaults to CHAOS_METRICS_HOST, or loopback. Access is controlled by
    CHAOS_METRICS_TOKEN / CHAOS_METRICS_TOKEN_FILE and TLS by the CHAOS_TLS_*
    settings; see src/metrics_http.py for the rules.

    The socket is bound before this function returns, so a port that is already
    in use raises MetricsServerError here, in the caller, instead of failing
    silently in a background thread while the caller logs "started".

    Raises:
        MetricsServerError: if the port cannot be bound, or the access, TLS or
        throttle settings are unsafe or invalid.
    """
    from . import metrics_http

    if addr is None:
        addr = metrics_http.host_from_env(DEFAULT_METRICS_ADDR)
    try:
        return metrics_http.serve(port, addr)
    except metrics_http.MetricsAccessError as e:
        raise MetricsServerError(f"cannot start the metrics server: {e}") from e
    except OSError as e:
        reason, in_use = metrics_http.describe_os_error(e)
        hint = (
            f" Set {METRICS_PORT_ENV} (or --metrics-port) to a free port."
            if in_use
            else ""
        )
        raise MetricsServerError(
            f"cannot start the metrics server on {addr}:{port}: {reason}.{hint}"
        ) from e


def metric_value(collector: Any, **labels: str) -> float:
    """
    Current value of one labelled series, using prometheus's public registry
    API. A series that has never been touched reads as 0.0 (and is not created
    as a side effect, unlike ``collector.labels(...)``).
    """
    described = collector.describe()[0]
    name = described.name + "_total" if described.type == "counter" else described.name
    return REGISTRY.get_sample_value(name, labels) or 0.0


def reset_metrics() -> None:
    """Forget every injection series (useful for tests and the reset endpoint)."""
    for collector in (
        INJECTIONS_TOTAL,
        INJECTIONS_CLAMPED,
        INJECTION_ACTIVE,
        EXPERIMENTS_TOTAL,
        SLO_ABORTS_TOTAL,
    ):
        collector.clear()
