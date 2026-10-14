"""
Process targeting against REAL processes (no mocks, no privileges needed).

The apps are started detached, so they are not children of the test process.
(The agent never targets its own children, and under pytest the test process
plays the agent.)
"""

import os
import shutil
import subprocess
import sys
import textwrap
import time
import uuid

import psutil
import pytest

from src.failures.process import find_target_processes, inject_process, select_target
from src.metrics import INJECTIONS_TOTAL

# Real processes with real start-time gaps: slower than unit tests.
# Skip with: pytest -m "not slow"
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("tail") is None or shutil.which("sh") is None,
        reason="needs sh and tail",
    ),
]


def count(status):
    return INJECTIONS_TOTAL.labels(failure_type="process", status=status)._value.get()


def gone(pid):
    """True if the process no longer exists (a zombie counts as gone)."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def wait_gone(pid, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if gone(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def world(tmp_path):
    """Start detached processes and clean them all up afterwards."""
    started = []
    uid = uuid.uuid4().hex[:8]

    class World:
        id = uid

        def script(self, stem, body="import time\ntime.sleep(300)\n"):
            path = tmp_path / f"{stem}.py"
            path.write_text(body)
            return path

        def detach(self, *argv):
            """Run argv detached (reparented away from the test process)."""
            line = " ".join(shlex_quote(a) for a in argv) + " >/dev/null 2>&1 & echo $!"
            pid = int(subprocess.check_output(["sh", "-c", line]).strip())
            started.append(pid)
            # Until it execs, the new process still looks like its parent `sh`.
            # Wait for the real command line so a scan cannot catch it mid-exec.
            wanted = os.path.basename(argv[-1])
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    if any(wanted in a for a in psutil.Process(pid).cmdline()):
                        break
                except psutil.NoSuchProcess:
                    break
                time.sleep(0.02)
            time.sleep(0.05)  # distinct start times between successive apps
            return pid

        def app(self, stem, body="import time\ntime.sleep(300)\n"):
            return self.detach(sys.executable, str(self.script(stem, body)))

        def tail(self, stem):
            log = tmp_path / f"{stem}.log"
            log.write_text("x\n")
            return self.detach("tail", "-f", str(log))

    def shlex_quote(s):
        import shlex

        return shlex.quote(s)

    yield World()
    for pid in started:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


class TestExactMatchingOnRealProcesses:
    def test_finds_the_app_but_not_a_process_that_only_mentions_it(self, world):
        name = f"tgt-{world.id}"
        a = world.app(name)
        viewer = world.tail(name)  # `tail -f .../tgt-xxxx.log`: mentions the name
        found = {p.info["pid"] for p in find_target_processes(name)[0]}
        assert found == {a}
        assert viewer not in found

    def test_a_prefix_of_the_name_matches_nothing(self, world):
        name = f"tgt-{world.id}"
        world.app(name)
        assert find_target_processes(name[:-2])[0] == []

    def test_apps_whose_names_contain_critical_words_are_targetable(self, world):
        """Both used to be silently treated as critical system processes."""
        for stem in (f"initialize-{world.id}", f"truncate-job-{world.id}"):
            pid = world.app(stem)
            found = [p.info["pid"] for p in find_target_processes(stem)[0]]
            assert found == [pid], stem

    def test_never_matches_the_agent_or_its_children(self, world):
        name = f"mine-{world.id}"
        path = world.script(name)
        child = subprocess.Popen([sys.executable, str(path)])  # our own child
        try:
            time.sleep(0.2)
            assert find_target_processes(name)[0] == []
        finally:
            child.kill()
            child.wait()


class TestDeterministicSelection:
    def test_oldest_and_newest_follow_start_order_not_pid_order(self, world):
        name = f"tgt-{world.id}"
        first, second, third = (world.app(name) for _ in range(3))
        found, _ = find_target_processes(name)
        assert {p.info["pid"] for p in found} == {first, second, third}
        assert select_target(found)[0].info["pid"] == first
        assert select_target(found, "newest")[0].info["pid"] == third

    def test_kills_exactly_the_oldest_and_nothing_else(self, world):
        name = f"tgt-{world.id}"
        first, second, third = (world.app(name) for _ in range(3))
        inject_process({"target_name": name})
        assert wait_gone(first)
        assert not gone(second) and not gone(third)
        assert count("success") == 1

    def test_kills_exactly_the_newest_when_asked(self, world):
        name = f"tgt-{world.id}"
        first, second, third = (world.app(name) for _ in range(3))
        inject_process({"target_name": name, "target_selection": "newest"})
        assert wait_gone(third)
        assert not gone(first) and not gone(second)

    def test_repeated_dry_runs_always_choose_the_same_process(self, world, caplog):
        import logging

        caplog.set_level(logging.INFO)
        name = f"tgt-{world.id}"
        pids = [world.app(name) for _ in range(3)]
        for _ in range(5):
            inject_process({"target_name": name}, dry_run=True)
        chosen = {r.pid for r in caplog.records if "DRY RUN" in r.message}
        assert chosen == {pids[0]}


class TestExplicitPid:
    def test_kills_the_one_process_with_that_pid(self, world):
        name = f"tgt-{world.id}"
        first, second, third = (world.app(name) for _ in range(3))
        inject_process({"target_name": name, "target_pid": second})
        assert wait_gone(second)
        assert not gone(first) and not gone(third)

    def test_pid_and_name_must_agree(self, world):
        a = world.app(f"one-{world.id}")
        b = world.app(f"two-{world.id}")
        inject_process({"target_name": f"one-{world.id}", "target_pid": b})
        assert not gone(a) and not gone(b)
        assert count("skipped") == 1

    def test_pid_alone(self, world):
        pid = world.app(f"solo-{world.id}")
        inject_process({"target_pid": pid})
        assert wait_gone(pid)

    def test_a_pid_that_does_not_exist_is_skipped(self):
        inject_process({"target_pid": 4194000})
        assert count("skipped") == 1 and count("success") == 0

    def test_pid_of_a_critical_process_is_refused(self):
        # PID 1 is excluded by the schema, but a direct caller must be safe too
        inject_process({"target_pid": 1})
        assert count("success") == 0


def test_a_process_that_ignores_sigterm_is_force_killed(world):
    name = f"stubborn-{world.id}"
    pid = world.app(
        name,
        textwrap.dedent("""
            import signal, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(300)
            """),
    )
    inject_process({"target_name": name})
    assert wait_gone(pid)
    assert count("success") == 1
