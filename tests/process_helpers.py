"""Shared helpers for tests that run the agent and API server as real processes."""

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

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
    kill_switch:
      enabled: false
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
