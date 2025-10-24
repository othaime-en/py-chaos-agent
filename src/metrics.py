from prometheus_client import start_http_server, Counter, Gauge
import threading
import time

# Metrics
INJECTIONS_TOTAL = Counter(
    'chaos_injections_total',
    'Total number of chaos injections',
    ['failure_type', 'status']  # status: success, skipped, failed
)

INJECTION_ACTIVE = Gauge(
    'chaos_injection_active',
    'Currently active chaos injection',
    ['failure_type']
)

def start_metrics_server(port: int = 8000):
    def _run():
        start_http_server(port)
        print(f"[Metrics] Prometheus exporter running on :{port}")
        while True:
            time.sleep(1)
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()