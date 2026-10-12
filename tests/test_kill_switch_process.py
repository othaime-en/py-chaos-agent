"""
End-to-end kill switch tests with real processes.

A real HTTP server plays the target and its health is flipped by the test. The
agent and the API server run as subprocesses with a stand-in `tc` that records
whether chaos is being applied, so each test can see the effect a user would:
chaos stops when the target goes unhealthy, stays off, and resumes (or not)
after it recovers.
"""

import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.process_helpers import http, wait_for

# Real processes and real signals: slower than unit tests.
# Skip with: pytest -m "not slow"
pytestmark = pytest.mark.slow


class Target:
    """A real HTTP server whose health the test controls."""

    def __init__(self):
        outer = self
        self.status = 200

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/health"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def set_healthy(self, healthy):
        self.status = 200 if healthy else 500

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def target():
    t = Target()
    yield t
    t.stop()


def write_config(env, target, duration=4, **kill_switch):
    ks = {
        "enabled": "true",
        "target_health_url": f'"{target.url}"',
        "health_check_interval_seconds": 1,
        "health_check_timeout_seconds": 1,
        "consecutive_failures_threshold": 2,
        "grace_after_agent_start_seconds": 0,
        "grace_after_process_kill_seconds": 0,
        "recovery_test_interval_seconds": 2,
        "recovery_success_threshold": 2,
    }
    ks.update(kill_switch)
    ks_yaml = "\n".join(f"      {k}: {v}" for k, v in ks.items())
    (env.dir / "config.yaml").write_text(textwrap.dedent(f"""
            agent:
              interval_seconds: 1
            failures:
              network:
                enabled: true
                probability: 1.0
                interface: eth0
                delay_ms: 100
                duration_seconds: {duration}
            kill_switch:
            """) + ks_yaml + "\n")


def adds(env):
    return len([c for c in env.tc_calls() if c.startswith("qdisc add")])


def tripped(env):
    return "KILL SWITCH TRIGGERED" in env.output()


# ---------------------------------------------------------------------------
# Standalone agent
# ---------------------------------------------------------------------------


def test_standalone_agent_pauses_when_unhealthy_and_resumes_after_recovery(env, target):
    write_config(env, target)
    proc = env.start_agent()
    wait_for(lambda: adds(env) >= 1, 15, "chaos never started against a healthy target")

    # Target goes down: the breaker trips and the in-flight injection is aborted
    target.set_healthy(False)
    wait_for(lambda: tripped(env), 15, "kill switch never tripped")
    wait_for(lambda: env.rules() == {}, 10, "in-flight injection was not aborted")

    # While tripped, no new injections start
    time.sleep(1.5)
    frozen = adds(env)
    time.sleep(4)
    assert adds(env) == frozen, "chaos kept running while the breaker was open"
    assert proc.poll() is None, "agent exited instead of pausing"

    # Target recovers: chaos resumes by itself
    target.set_healthy(True)
    wait_for(lambda: adds(env) > frozen, 25, "chaos did not resume after recovery")
    assert "Kill switch closed, chaos resuming" in env.output()


def test_standalone_agent_stays_off_when_resume_is_disabled(env, target):
    write_config(env, target, resume_after_recovery="false")
    env.start_agent()
    wait_for(lambda: adds(env) >= 1, 15, "chaos never started")

    target.set_healthy(False)
    wait_for(lambda: tripped(env), 15, "kill switch never tripped")
    wait_for(lambda: env.rules() == {}, 10, "in-flight injection was not aborted")
    frozen = adds(env)

    target.set_healthy(True)
    wait_for(
        lambda: "chaos stays off" in env.output(),
        20,
        "breaker never recovered",
    )
    time.sleep(4)
    assert adds(env) == frozen, "chaos resumed although resume_after_recovery is false"


def test_standalone_agent_ignores_an_unhealthy_target_when_disabled(env, target):
    write_config(env, target, enabled="false")
    target.set_healthy(False)
    env.start_agent()
    wait_for(lambda: adds(env) >= 2, 20, "chaos should keep running")
    assert not tripped(env)
    assert "Kill switch is disabled" in env.output()


def test_standalone_agent_uses_the_configured_health_url(env, target):
    """A URL that is always down trips it; this proves the config is read."""
    write_config(env, target)
    target.set_healthy(False)
    env.start_agent()
    wait_for(
        lambda: tripped(env), 15, "kill switch never tripped on the configured URL"
    )


# ---------------------------------------------------------------------------
# API server
# ---------------------------------------------------------------------------


def status(port):
    return http(port, "/kill-switch/status")[1]


def test_api_protects_manual_injections_even_without_the_agent_loop(env, target):
    write_config(env, target, duration=30)
    proc, port = env.start_api()

    # Healthy: a manual injection runs
    assert http(port, "/inject/manual", "POST", {"failure_type": "network"})[0] == 200
    wait_for(lambda: "eth0" in env.rules(), 15, "manual injection never applied")

    # The target goes down while it runs: it is aborted and the breaker opens
    target.set_healthy(False)
    wait_for(lambda: status(port).get("circuit_state") == "open", 15, "never tripped")
    wait_for(lambda: env.rules() == {}, 10, "in-flight manual injection not aborted")

    # New manual injections are refused with a reason
    code, body = http(port, "/inject/manual", "POST", {"failure_type": "network"})
    assert code == 409 and "Chaos paused" in body["detail"]

    # Reset is refused while the target is still down
    code, body = http(port, "/kill-switch/reset", "POST")
    assert code == 409 and "not healthy" in body["detail"]

    # Once the target is healthy, an operator can reset and chaos works again
    target.set_healthy(True)
    code, body = http(port, "/kill-switch/reset", "POST")
    assert code == 200 and body["circuit_state"] == "closed"
    assert http(port, "/inject/manual", "POST", {"failure_type": "network"})[0] == 200


def test_api_does_not_trip_while_idle(env, target):
    """Nothing to protect: an unhealthy target alone must not trip the breaker."""
    write_config(env, target)
    target.set_healthy(False)
    proc, port = env.start_api()
    time.sleep(5)
    body = status(port)
    assert body["circuit_state"] == "closed"
    assert body["trip_count"] == 0
    assert body["monitoring_active"] is True


def test_api_restart_does_not_clear_a_trip(env, target):
    write_config(env, target)
    proc, port = env.start_api()
    assert http(port, "/agent/start", "POST")[0] == 200
    wait_for(lambda: adds(env) >= 1, 15, "loop never injected")

    target.set_healthy(False)
    wait_for(lambda: status(port).get("circuit_state") == "open", 15, "never tripped")

    assert http(port, "/agent/stop", "POST")[0] == 200
    code, body = http(port, "/agent/start", "POST")
    assert code == 200 and body["kill_switch_state"] == "open"
    assert body["paused_by_kill_switch"] is True
    assert status(port)["trip_count"] == 1


def test_api_loop_resumes_by_itself_after_recovery(env, target):
    write_config(env, target)
    proc, port = env.start_api()
    http(port, "/agent/start", "POST")
    wait_for(lambda: adds(env) >= 1, 15, "loop never injected")

    target.set_healthy(False)
    wait_for(lambda: status(port).get("circuit_state") == "open", 15, "never tripped")
    wait_for(lambda: env.rules() == {}, 10, "in-flight injection not aborted")
    frozen = adds(env)
    time.sleep(3)
    assert adds(env) == frozen
    assert http(port, "/status")[1]["paused_by_kill_switch"] is True

    target.set_healthy(True)
    wait_for(lambda: adds(env) > frozen, 25, "loop did not resume after recovery")
    assert http(port, "/status")[1]["paused_by_kill_switch"] is False
