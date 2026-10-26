"""The metrics server binds before it reports success, and the helpers use
prometheus's public API only."""

import inspect
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

from src import metrics
from src.metrics import (
    INJECTION_ACTIVE,
    INJECTIONS_CLAMPED,
    INJECTIONS_TOTAL,
    KILL_SWITCH_STATE,
    MetricsServerError,
    metric_value,
    metrics_port_from_env,
    reset_metrics,
    start_metrics_server,
)

REPO = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def busy_port():
    """A port that something is already listening on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    yield s.getsockname()[1]
    s.close()


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("CHAOS_METRICS_PORT", raising=False)


class TestServerStart:
    def test_a_port_in_use_raises_in_the_caller(self, busy_port):
        """It used to fail silently inside a thread while the caller logged
        'started successfully'."""
        with pytest.raises(MetricsServerError) as exc:
            start_metrics_server(busy_port, addr="127.0.0.1")
        message = str(exc.value)
        assert str(busy_port) in message
        assert "in use" in message
        assert "CHAOS_METRICS_PORT" in message

    def test_the_error_is_not_swallowed_by_a_background_thread(self, busy_port):
        import threading

        before = threading.active_count()
        with pytest.raises(MetricsServerError):
            start_metrics_server(busy_port, addr="127.0.0.1")
        assert threading.active_count() <= before

    def test_a_free_port_serves_metrics_once_the_call_returns(self):
        server, _thread = start_metrics_server(0, addr="127.0.0.1")
        try:
            port = server.server_address[1]
            body = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/metrics", timeout=5
            ).read()
            assert b"chaos_injections_total" in body
        finally:
            server.shutdown()
            server.server_close()

    def test_the_default_address_works(self):
        """A bad default address once stopped the server starting at all, and
        every test that passed an explicit address missed it."""
        port = free_port()
        server, _thread = start_metrics_server(port)  # default address
        try:
            body = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/metrics", timeout=5
            ).read()
            assert b"chaos_injections_total" in body
        finally:
            server.shutdown()
            server.server_close()

    def test_the_default_address_is_all_interfaces_as_before(self):
        assert metrics.DEFAULT_METRICS_ADDR == "0.0.0.0"

    def test_only_a_port_conflict_gets_the_free_port_hint(self, busy_port):
        with pytest.raises(MetricsServerError) as conflict:
            start_metrics_server(busy_port, addr="127.0.0.1")
        assert "free port" in str(conflict.value)

        with pytest.raises(MetricsServerError) as other:
            start_metrics_server(free_port(), addr="not a host")
        assert "free port" not in str(other.value)
        assert "not a host" in str(other.value)

    def test_the_error_chains_the_underlying_os_error(self, busy_port):
        with pytest.raises(MetricsServerError) as exc:
            start_metrics_server(busy_port, addr="127.0.0.1")
        assert isinstance(exc.value.__cause__, OSError)


class TestPortFromEnvironment:
    def test_default(self):
        assert metrics_port_from_env() == 8000

    def test_custom_default(self):
        assert metrics_port_from_env(9100) == 9100

    def test_environment_variable(self, monkeypatch):
        monkeypatch.setenv("CHAOS_METRICS_PORT", "9464")
        assert metrics_port_from_env() == 9464

    def test_surrounding_whitespace_is_ignored(self, monkeypatch):
        monkeypatch.setenv("CHAOS_METRICS_PORT", " 9464 ")
        assert metrics_port_from_env() == 9464

    def test_an_empty_value_means_default(self, monkeypatch):
        monkeypatch.setenv("CHAOS_METRICS_PORT", "")
        assert metrics_port_from_env() == 8000

    @pytest.mark.parametrize("bad", ["abc", "80.5", "-1", "0", "65536", "99999"])
    def test_invalid_values_are_rejected(self, monkeypatch, bad):
        monkeypatch.setenv("CHAOS_METRICS_PORT", bad)
        with pytest.raises(MetricsServerError, match="CHAOS_METRICS_PORT"):
            metrics_port_from_env()

    @pytest.mark.parametrize("good", ["1", "8000", "65535"])
    def test_boundaries_are_accepted(self, monkeypatch, good):
        monkeypatch.setenv("CHAOS_METRICS_PORT", good)
        assert metrics_port_from_env() == int(good)


class TestMetricHelpers:
    def test_an_untouched_series_reads_zero_without_being_created(self):
        # A label set the shared test fixtures do not pre-create
        labels = dict(failure_type="never-used", status="success")
        assert metric_value(INJECTIONS_TOTAL, **labels) == 0.0
        # the old `.labels(...)._value.get()` created the series as a side effect
        assert REGISTRY.get_sample_value("chaos_injections_total", labels) is None

    def test_reads_a_counter(self):
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="success").inc(3)
        assert metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="success") == 3

    def test_series_with_different_labels_are_independent(self):
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="success").inc()
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="failed").inc(2)
        assert metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="success") == 1
        assert metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="failed") == 2
        assert (
            metric_value(INJECTIONS_TOTAL, failure_type="network", status="success")
            == 0
        )

    def test_reads_a_labelled_gauge(self):
        INJECTION_ACTIVE.labels(failure_type="memory").set(1)
        assert metric_value(INJECTION_ACTIVE, failure_type="memory") == 1

    def test_reads_an_unlabelled_gauge(self):
        KILL_SWITCH_STATE.set(2)
        try:
            assert metric_value(KILL_SWITCH_STATE) == 2
        finally:
            KILL_SWITCH_STATE.set(0)

    def test_reads_the_clamped_counter(self):
        INJECTIONS_CLAMPED.labels(failure_type="cpu").inc()
        assert metric_value(INJECTIONS_CLAMPED, failure_type="cpu") == 1

    def test_reset_clears_every_injection_series(self):
        INJECTIONS_TOTAL.labels(failure_type="cpu", status="success").inc()
        INJECTIONS_CLAMPED.labels(failure_type="cpu").inc()
        INJECTION_ACTIVE.labels(failure_type="cpu").set(1)
        reset_metrics()
        assert metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="success") == 0
        assert metric_value(INJECTIONS_CLAMPED, failure_type="cpu") == 0
        assert metric_value(INJECTION_ACTIVE, failure_type="cpu") == 0

    def test_reset_leaves_the_kill_switch_metrics_alone(self):
        KILL_SWITCH_STATE.set(1)
        try:
            reset_metrics()
            assert metric_value(KILL_SWITCH_STATE) == 1
        finally:
            KILL_SWITCH_STATE.set(0)


class TestNoPrivatePrometheusAttributes:
    """`_value` and `_metrics` are not part of prometheus_client's public API."""

    def test_src_does_not_touch_private_attributes(self):
        import re

        offenders = []
        for path in (REPO / "src").rglob("*.py"):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"\._value\b|\._metrics\b", line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert offenders == []

    def test_tests_do_not_touch_private_attributes_either(self):
        import re

        this_file = Path(__file__).name
        offenders = []
        for path in (REPO / "tests").glob("*.py"):
            if path.name == this_file:
                continue  # this file names them in the regex above
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"\._value\b|\._metrics\b", line):
                    offenders.append(f"{path.name}:{number}")
        assert offenders == []

    def test_the_helpers_use_the_public_registry(self):
        assert "REGISTRY.get_sample_value" in inspect.getsource(metrics.metric_value)
        assert ".clear()" in inspect.getsource(metrics.reset_metrics)


# ---------------------------------------------------------------------------
# Real processes: a taken metrics port stops startup
# ---------------------------------------------------------------------------


def run_entry_point(args, busy_port, extra_env=None, timeout=60):
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(REPO),
        CHAOS_API_TOKEN="m" * 40,
        PYTHONUNBUFFERED="1",
        ENABLE_FILE_LOGGING="false",
    )
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_api_server_refuses_to_start_when_the_metrics_port_is_taken(busy_port):
    result = run_entry_point(
        ["-m", "src.api_server", "--port", "0", "--metrics-port", str(busy_port)],
        busy_port,
    )
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Refusing to start" in output
    assert "cannot start the metrics server" in output


def test_standalone_agent_exits_when_the_metrics_port_is_taken(busy_port):
    result = run_entry_point(
        ["-m", "src.agent"], busy_port, {"CHAOS_METRICS_PORT": str(busy_port)}
    )
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert "Cannot start the metrics server" in output
    assert "Metrics server started" not in output


def test_api_server_reads_the_port_from_the_environment(busy_port):
    result = run_entry_point(
        ["-m", "src.api_server", "--port", "0"],
        busy_port,
        {"CHAOS_METRICS_PORT": str(busy_port)},
    )
    assert result.returncode != 0
    assert str(busy_port) in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Real processes: a FREE port must work (the negative tests above cannot tell
# "port in use" from "startup is broken")
# ---------------------------------------------------------------------------


def start_and_scrape(args, port, extra_env=None, timeout=30):
    """Start an entry point, wait for its metrics, then stop it cleanly."""
    import signal
    import time

    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(REPO),
        CHAOS_API_TOKEN="m" * 40,
        PYTHONUNBUFFERED="1",
        ENABLE_FILE_LOGGING="false",
    )
    env.update(extra_env or {})
    proc = subprocess.Popen(
        [sys.executable, *args],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"exited early with {proc.returncode}:\n{proc.stdout.read()}"
                )
            try:
                body = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/metrics", timeout=2
                ).read()
                return body
            except OSError:
                time.sleep(0.2)
        raise AssertionError("metrics never became reachable")
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)


def test_standalone_agent_serves_metrics_on_a_free_port():
    port = free_port()
    body = start_and_scrape(
        ["-m", "src.agent"], port, {"CHAOS_METRICS_PORT": str(port)}
    )
    assert b"chaos_injections_total" in body


def test_api_server_serves_metrics_on_a_free_port():
    port = free_port()
    body = start_and_scrape(
        [
            "-m",
            "src.api_server",
            "--port",
            str(free_port()),
            "--metrics-port",
            str(port),
        ],
        port,
    )
    assert b"chaos_injections_total" in body


def test_api_server_takes_its_metrics_port_from_the_environment():
    port = free_port()
    body = start_and_scrape(
        ["-m", "src.api_server", "--port", str(free_port())],
        port,
        {"CHAOS_METRICS_PORT": str(port)},
    )
    assert b"chaos_injections_total" in body
