"""
Process-level tests: real processes, real signals.

These start the agent as a subprocess with a stand-in `tc` that records rules in
a state file, then check that the rule is gone after SIGTERM, SIGINT, an API
stop, and a restart after SIGKILL. The stand-in is used because CI runners and
containers often lack the netem kernel module; what is under test is whether
the cleanup code runs at the right moment, not the kernel's qdisc handling.
"""

import signal
import time

import pytest

from tests.process_helpers import (  # noqa: E402
    CONFIG,
    http,
    wait_exit,
    wait_for,
)

# Real processes and real signals: slower than unit tests.
# Skip with: pytest -m "not slow"
pytestmark = pytest.mark.slow


def inject_network_via_api(port, duration=60):
    return http(
        port,
        "/inject/manual",
        "POST",
        {"failure_type": "network", "config": {"duration_seconds": duration}},
    )


# ---------------------------------------------------------------------------
# Standalone agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"]
)
def test_agent_removes_rule_on_signal_mid_injection(env, sig):
    proc = env.start_agent()
    wait_for(lambda: "eth0" in env.rules(), 15, "rule was never applied")

    started = time.time()
    proc.send_signal(sig)
    code = wait_exit(proc, 15)

    assert code == 0
    assert env.rules() == {}, "tc rule leaked after signal"
    assert time.time() - started < 10, "shutdown waited out the 60s injection"


# ---------------------------------------------------------------------------
# API server
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"]
)
def test_api_removes_rule_and_exits_promptly_on_signal(env, sig):
    proc, port = env.start_api()
    status, _ = inject_network_via_api(port)
    assert status == 200
    wait_for(lambda: "eth0" in env.rules(), 15, "rule was never applied")

    started = time.time()
    proc.send_signal(sig)
    wait_exit(proc, 15)

    assert env.rules() == {}, "tc rule leaked after signal"
    assert time.time() - started < 10, "server waited out the 60s injection"


def test_api_stop_aborts_in_flight_injection(env):
    proc, port = env.start_api()
    assert http(port, "/agent/start", "POST")[0] == 200
    wait_for(lambda: "eth0" in env.rules(), 15, "loop never applied the rule")

    assert http(port, "/agent/stop", "POST")[0] == 200
    wait_for(lambda: env.rules() == {}, 10, "stop did not remove the rule")

    # And the loop really stopped: nothing re-applies it on later ticks.
    time.sleep(2.5)
    assert env.rules() == {}


def test_api_abort_endpoint_removes_manual_injection_promptly(env):
    proc, port = env.start_api()
    inject_network_via_api(port)
    wait_for(lambda: "eth0" in env.rules(), 15, "rule was never applied")

    status, body = http(port, "/inject/abort", "POST")
    assert status == 200 and body["aborted"] == 1
    wait_for(lambda: env.rules() == {}, 10, "abort did not remove the rule")
    wait_for(
        lambda: http(port, "/status")[1].get("active_injections") == [],
        10,
        "injection still listed as active",
    )


def test_second_network_injection_is_refused_while_one_runs(env):
    proc, port = env.start_api()
    assert inject_network_via_api(port)[0] == 200
    wait_for(lambda: "eth0" in env.rules(), 15, "rule was never applied")
    assert http(port, "/status")[1]["active_injections"] == ["network"]

    status, body = inject_network_via_api(port)
    assert status == 409
    assert "already running" in body["detail"]
    # The first injection's rule is untouched by the refused request
    assert "eth0" in env.rules()
    # tc was never asked to delete or add anything for the refused request
    adds = [c for c in env.tc_calls() if c.startswith("qdisc add")]
    assert len(adds) == 1


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["agent", "api"])
def test_stale_rule_after_sigkill_is_removed_on_next_start(env, mode):
    if mode == "agent":
        proc = env.start_agent()
    else:
        proc, port = env.start_api()
        inject_network_via_api(port)
    wait_for(lambda: "eth0" in env.rules(), 15, "rule was never applied")

    proc.kill()  # SIGKILL: no code runs, so the rule is orphaned
    proc.wait(5)
    assert "eth0" in env.rules(), "expected the rule to be orphaned by SIGKILL"

    # Restart with the injection disabled so any later change is startup cleanup
    (env.dir / "config.yaml").write_text(
        CONFIG.replace("enabled: true", "enabled: false")
    )
    if mode == "agent":
        env.start_agent()
    else:
        env.start_api()
    wait_for(lambda: env.rules() == {}, 15, "stale rule was not removed at startup")
