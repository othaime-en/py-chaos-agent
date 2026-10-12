from prometheus_client import start_http_server, Counter, Gauge
import threading
import time

# Metrics
INJECTIONS_TOTAL = Counter(
    "chaos_injections_total",
    "Total number of chaos injections",
    ["failure_type", "status"],  # status: success, skipped, failed
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


def start_metrics_server(port: int = 8000):
    def _run():
        start_http_server(port)
        print(f"[Metrics] Prometheus exporter running on :{port}")
        while True:
            time.sleep(1)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
