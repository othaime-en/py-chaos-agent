"""CPU workers are real processes: check abort and parent-death behavior."""

import multiprocessing
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import psutil
import pytest

from src.failures import cpu
from src.lifecycle import lifecycle

REPO_ROOT = Path(__file__).resolve().parent.parent


def alive_children():
    return [c for c in psutil.Process().children(recursive=True) if c.is_running()]


def test_worker_exits_immediately_if_its_parent_is_gone():
    """A worker told that its parent is some other pid sees the parent as dead."""
    started = time.time()
    cpu._worker(30, parent_pid=os.getpid() + 999999)
    assert time.time() - started < 1


def test_worker_runs_its_full_duration_when_the_parent_is_alive():
    started = time.time()
    cpu._worker(0.5, parent_pid=os.getppid())
    assert 0.45 <= time.time() - started < 2


def test_worker_without_a_parent_pid_still_honors_the_duration():
    started = time.time()
    cpu._worker(0.3)
    assert 0.25 <= time.time() - started < 1.5


def test_abort_stops_workers_promptly_and_leaves_no_children():
    result = []

    def run():
        with lifecycle.injection("cpu"):
            cpu._cpu_hog(2, 60)
        result.append("done")

    t = threading.Thread(target=run)
    t.start()
    deadline = time.time() + 5
    while len(alive_children()) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert len(alive_children()) >= 2

    started = time.time()
    lifecycle.abort_active("test")
    t.join(10)

    assert result == ["done"]
    assert time.time() - started < 5
    assert alive_children() == []


def test_workers_are_terminated_when_the_caller_is_interrupted(monkeypatch):
    """SystemExit (what the signal handler raises) must not orphan workers."""

    def raise_exit(seconds):
        raise SystemExit(0)

    monkeypatch.setattr(cpu.lifecycle, "sleep", raise_exit)
    with pytest.raises(SystemExit):
        cpu._cpu_hog(2, 60)
    assert alive_children() == []


def test_workers_are_daemons():
    captured = []
    real = multiprocessing.Process

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.append(self)

    cpu.multiprocessing.Process = Spy
    try:
        cpu._cpu_hog(1, 0)
    finally:
        cpu.multiprocessing.Process = real
    assert captured and all(p.daemon for p in captured)


def test_workers_exit_when_the_parent_is_sigkilled(tmp_path):
    """No code runs in a SIGKILLed parent, so the workers must notice."""
    script = tmp_path / "parent.py"
    script.write_text(textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(REPO_ROOT)!r})
            from src.failures.cpu import _cpu_hog
            _cpu_hog(2, 60)
            """))
    parent = subprocess.Popen([sys.executable, str(script)])
    try:
        ps = psutil.Process(parent.pid)
        deadline = time.time() + 10
        while len(ps.children()) < 2 and time.time() < deadline:
            time.sleep(0.05)
        workers = ps.children()
        assert len(workers) == 2

        parent.send_signal(signal.SIGKILL)
        parent.wait(5)

        deadline = time.time() + 5
        while any(w.is_running() and w.status() != "zombie" for w in workers):
            if time.time() > deadline:
                pytest.fail("orphaned CPU workers kept running after parent died")
            time.sleep(0.1)
    finally:
        if parent.poll() is None:
            parent.kill()
