"""
Process-level tests: real processes, real signals.

These start the agent as a subprocess with a stand-in `tc` that records rules in
a state file, then check that the rule is gone after SIGTERM, SIGINT, an API
stop, and a restart after SIGKILL. The stand-in is used because CI runners and
containers often lack the netem kernel module; what is under test is whether
the cleanup code runs at the right moment, not the kernel's qdisc handling.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "p" * 40

FAKE_TC = """#!{python}
import json, os, sys
args = sys.argv[1:]
state_path = os.environ["FAKE_TC_STATE"]
log_path = os.environ["FAKE_TC_LOG"]
with open(log_path, "a") as f:
    f.write(" ".join(args) + "\\n")
state = json.load(open(state_path)) if os.path.exists(state_path) else {{}}
def save():
    json.dump(state, open(state_path, "w"))
if args[:2] not in (["qdisc", "show"], ["qdisc", "add"], ["qdisc", "del"],
                    ["qdisc", "replace"]):
    sys.exit(0)
dev = args[args.index("dev") + 1]
verb = args[1]
if verb == "show":
    if dev in state:
        print("qdisc netem 8001: root refcnt 2 limit 1000 " + state[dev])
    else:
        print("qdisc noqueue 0: root refcnt 2")
    sys.exit(0)
if verb == "add":
    if dev in state:
        sys.stderr.write("RTNETLINK answers: File exists\\n")
        sys.exit(2)
    state[dev] = " ".join(args[args.index("netem") + 1:])
    save()
    sys.exit(0)
if verb == "replace":
    state[dev] = " ".join(args[args.index("netem") + 1:])
    save()
    sys.exit(0)
if verb == "del":
    if dev not in state:
        sys.stderr.write("Error: Cannot delete qdisc with handle of zero.\\n")
        sys.exit(2)
    del state[dev]
    save()
    sys.exit(0)
"""

FAKE_IP = "#!/bin/sh\nexit 0\n"

CONFIG = textwrap.dedent("""
    agent:
      interval_seconds: 1
    failures:
      network:
        enabled: true
        probability: 1.0
        interface: eth0
        delay_ms: 100
        duration_seconds: 60
    """)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    """A working directory with a config, fake tc/ip, and helpers."""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in (
            ("tc", FAKE_TC.format(python=sys.executable)),
            ("ip", FAKE_IP),
        ):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        (tmp_path / "config.yaml").write_text(CONFIG)
        self.state = tmp_path / "tc_state.json"
        self.log = tmp_path / "tc_log.txt"
        self.procs = []

    @property
    def env(self):
        env = dict(os.environ)
        env.update(
            PATH=f"{self.dir / 'bin'}{os.pathsep}{os.environ['PATH']}",
            PYTHONPATH=str(REPO_ROOT),
            FAKE_TC_STATE=str(self.state),
            FAKE_TC_LOG=str(self.log),
            CHAOS_API_TOKEN=TOKEN,
            PYTHONUNBUFFERED="1",
        )
        return env

    def rules(self) -> dict:
        try:
            return json.loads(self.state.read_text())
        except (OSError, ValueError):
            return {}

    def tc_calls(self) -> list:
        try:
            return self.log.read_text().splitlines()
        except OSError:
            return []

    def start(self, *args) -> subprocess.Popen:
        out = open(self.dir / f"out{len(self.procs)}.log", "w")
        proc = subprocess.Popen(
            [sys.executable, *args],
            cwd=self.dir,
            env=self.env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        self.procs.append((proc, out))
        return proc

    def start_agent(self) -> subprocess.Popen:
        return self.start("-m", "src.agent")

    def start_api(self):
        port = free_port()
        proc = self.start(
            "-m",
            "src.api_server",
            "--port",
            str(port),
            "--metrics-port",
            str(free_port()),
            "--log-level",
            "INFO",
        )
        wait_for(lambda: http(port, "/health")[0] == 200, 20, "API did not start")
        return proc, port

    def output(self, index=-1) -> str:
        proc, out = self.procs[index]
        out.flush()
        return Path(out.name).read_text()

    def cleanup(self):
        for proc, out in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
            out.close()


def http(port, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except (urllib.error.URLError, ConnectionError, OSError):
        return 0, {}


def wait_for(predicate, timeout, message="condition not met"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message)


def wait_exit(proc, timeout):
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)
        raise AssertionError(f"process did not exit within {timeout}s")


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


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
    # Kill switch off: it would poll a health URL that does not exist here.
    assert http(port, "/agent/start?enable_kill_switch=false", "POST")[0] == 200
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
