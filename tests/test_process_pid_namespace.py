"""
PID 1 handling, tested in REAL PID namespaces.

This reproduces the Docker Compose topology (`pid: service:target-app`): the
chaos agent runs inside the same PID namespace as the target, where the
target's init is PID 1. Needs root, `unshare` and `nsenter`; skipped otherwise
(so it is skipped on most CI runners and in unprivileged containers).

Kernel facts these tests rely on, verified by hand and by the canary below:
  * A signal sent to PID 1 from inside its own PID namespace is discarded unless
    PID 1 has a handler for it; SIGKILL is always discarded. `kill` still
    reports success.
  * When PID 1 of a namespace exits, the kernel kills every process in the
    namespace, including any that joined it (the chaos agent).
"""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def can_use_pid_namespaces() -> bool:
    if os.geteuid() != 0 or not shutil.which("unshare") or not shutil.which("nsenter"):
        return False
    try:
        result = subprocess.run(
            ["unshare", "-p", "-f", "--mount-proc", "true"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not can_use_pid_namespaces(),
        reason="needs root, unshare and nsenter with PID namespaces",
    ),
]

PROBE = """
import json, logging, sys
sys.path.insert(0, {repo!r})
import warnings; warnings.filterwarnings("ignore")
from src.failures.process import inject_process
from src.metrics import INJECTIONS_TOTAL, metric_value

records = []
class Capture(logging.Handler):
    def emit(self, record):
        if "DRY RUN" in record.getMessage():
            records.append({{k: getattr(record, k, None)
                            for k in ("is_pid_1", "would_be_refused", "pid")}})
proc_logger = logging.getLogger("src.failures.process")
proc_logger.setLevel(logging.INFO)
proc_logger.addHandler(Capture())

config = json.loads(sys.argv[1])
dry_run = sys.argv[2] == "dry"
inject_process(config, dry_run=dry_run)
count = lambda s: metric_value(INJECTIONS_TOTAL, failure_type="process", status=s)
print("RESULT " + json.dumps({{
    "success": count("success"), "failed": count("failed"),
    "skipped": count("skipped"), "dry_run_records": records,
}}))
"""


class Namespace:
    """A PID namespace whose PID 1 runs `init_cmd`, plus a way to run code in it."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.unshare = None
        self.init_host_pid = None
        probe = tmp_path / "probe.py"
        probe.write_text(PROBE.format(repo=str(REPO_ROOT)))
        self.probe = probe

    def start(self, *init_cmd):
        self.unshare = subprocess.Popen(
            ["unshare", "-p", "-f", "--mount-proc", *init_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            kids = psutil.Process(self.unshare.pid).children()
            if kids:
                self.init_host_pid = kids[0].pid
                time.sleep(0.5)  # let init start its children
                return self.init_host_pid
            time.sleep(0.05)
        raise RuntimeError("namespace init did not start")

    def children_of_init(self):
        return psutil.Process(self.init_host_pid).children(recursive=True)

    def wait_for_child(self, text, timeout=10):
        """Wait until a descendant of init has `text` in its command line."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for child in self.children_of_init():
                try:
                    if text in " ".join(child.cmdline()):
                        return child
                except psutil.NoSuchProcess:
                    pass
            time.sleep(0.05)
        raise AssertionError(f"no process with {text!r} started under init")

    def run(self, *args, timeout=60):
        """Run a command INSIDE the namespace, like a container that joined it."""
        return subprocess.run(
            ["nsenter", "-t", str(self.init_host_pid), "-p", "-m", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def inject(self, config, dry_run=False):
        result = self.run(
            sys.executable,
            str(self.probe),
            json.dumps(config),
            "dry" if dry_run else "run",
        )
        line = next(
            (ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")),
            None,
        )
        assert line, f"probe printed no result:\n{result.stdout}\n{result.stderr}"
        return json.loads(line[len("RESULT ") :])

    def init_alive(self):
        try:
            return psutil.Process(self.init_host_pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def stop(self):
        if self.init_host_pid:
            try:
                os.kill(self.init_host_pid, 9)
            except OSError:
                pass
        if self.unshare:
            self.unshare.kill()
            self.unshare.wait()


@pytest.fixture
def ns(tmp_path):
    n = Namespace(tmp_path)
    yield n
    n.stop()


def test_canary_kernel_discards_signals_to_pid_1_from_inside(ns):
    """If this ever fails, the kernel behavior the refusal relies on has changed."""
    ns.start("sleep", "300")
    result = ns.run("sh", "-c", "kill -9 1; echo rc=$?; kill -TERM 1; echo rc=$?")
    assert result.stdout.split() == ["rc=0", "rc=0"], "kill reports success"
    time.sleep(0.5)
    assert ns.init_alive(), "PID 1 survived SIGKILL and SIGTERM from inside"


def test_canary_pid_1_exit_kills_processes_that_joined_the_namespace(ns):
    ns.start("sleep", "300")
    joined = subprocess.Popen(
        ["nsenter", "-t", str(ns.init_host_pid), "-p", "-m", "sleep", "600"]
    )
    time.sleep(1)
    assert joined.poll() is None, "the joined process should be running"
    os.kill(ns.init_host_pid, 9)  # what a container stop or crash does
    time.sleep(1.5)
    assert joined.poll() is not None, "the joined process should have been killed"


def test_pid_1_target_is_refused_and_reported_truthfully(ns):
    ns.start("sleep", "300")
    result = ns.inject({"target_name": "sleep"})
    assert result["failed"] == 1 and result["success"] == 0
    assert ns.init_alive(), "nothing was signalled, PID 1 is untouched"


def test_dry_run_flags_that_a_real_run_would_be_refused(ns):
    ns.start("sleep", "300")
    result = ns.inject({"target_name": "sleep"}, dry_run=True)
    (record,) = result["dry_run_records"]
    assert record["is_pid_1"] is True and record["would_be_refused"] is True
    assert result["failed"] == 0 and result["skipped"] == 1


def test_an_app_that_is_not_pid_1_is_killed_and_init_survives(ns, tmp_path):
    name = f"tgt-{uuid.uuid4().hex[:8]}"
    script = tmp_path / f"{name}.py"
    script.write_text("import time\ntime.sleep(300)\n")
    # init-like PID 1 (an init: true / pause container) with the app as its child
    ns.start(
        "sh",
        "-c",
        f"{sys.executable} {script} & while true; do sleep 1; done",
    )
    app = ns.wait_for_child(name)

    result = ns.inject({"target_name": name})

    assert result["success"] == 1 and result["failed"] == 0
    deadline = time.time() + 5
    while psutil.pid_exists(app.pid) and psutil.Process(app.pid).status() != "zombie":
        assert time.time() < deadline, "the app is still running"
        time.sleep(0.1)
    assert ns.init_alive(), "init stays up, so the namespace (and the agent) survive"


def test_target_pid_inside_the_namespace_is_matched_by_its_inner_pid(ns, tmp_path):
    name = f"tgt-{uuid.uuid4().hex[:8]}"
    script = tmp_path / f"{name}.py"
    script.write_text("import time\ntime.sleep(300)\n")
    ns.start("sh", "-c", f"{sys.executable} {script} & while true; do sleep 1; done")
    ns.wait_for_child(name)
    # inside the namespace init is 1 and the app is the next PID
    result = ns.inject({"target_pid": 2})
    assert result["success"] == 1
    assert ns.init_alive()
