import errno
import os
from typing import Any

from prometheus_client import REGISTRY, start_http_server, Counter, Gauge

DEFAULT_METRICS_PORT = 8000
# All IPv4 interfaces, as before. (An empty string looks equivalent but does not
# resolve everywhere, which would stop the server from starting at all.)
DEFAULT_METRICS_ADDR = "0.0.0.0"
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
    port: int = DEFAULT_METRICS_PORT, addr: str = DEFAULT_METRICS_ADDR
) -> Any:
    """
    Start the Prometheus exporter and return once it is listening.

    The socket is bound before this function returns, so a port that is already
    in use raises MetricsServerError here, in the caller, instead of failing
    silently in a background thread while the caller logs "started".
    """
    try:
        return start_http_server(port, addr=addr)
    except OSError as e:
        reason = e.strerror or str(e)
        hint = (
            f" Set {METRICS_PORT_ENV} (or --metrics-port) to a free port."
            if e.errno == errno.EADDRINUSE
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
    for collector in (INJECTIONS_TOTAL, INJECTIONS_CLAMPED, INJECTION_ACTIVE):
        collector.clear()
