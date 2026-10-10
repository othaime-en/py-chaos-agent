"""
Injectors respect the container's real resource budget:
CPU requests are clamped, memory requests are refused, reservations are
released, and dry runs reserve nothing.
"""

import threading

import pytest

from src.failures import cpu, memory
from src.metrics import INJECTIONS_CLAMPED, INJECTIONS_TOTAL
from src.resources import MIB, CgroupInfo, SafetySettings, governor


def set_container(
    monkeypatch, cpu_limit=4.0, mem_limit_mb=1000, ws_mb=0, cpus=8, **settings
):
    """Make the global governor see a container with the given limits."""
    info = CgroupInfo(
        2,
        cpu_limit,
        None if mem_limit_mb is None else mem_limit_mb * MIB,
        None if mem_limit_mb is None else ws_mb * MIB,
    )
    monkeypatch.setattr(governor, "_cgroup_reader", lambda: info)
    monkeypatch.setattr(governor, "_cpu_reader", lambda: cpus)
    monkeypatch.setattr(governor, "_memory_reader", lambda: 64 * 1024 * MIB)
    if settings:
        governor.configure(SafetySettings(**settings))


def counter(kind, status):
    return INJECTIONS_TOTAL.labels(failure_type=kind, status=status)._value.get()


def clamped(kind):
    return INJECTIONS_CLAMPED.labels(failure_type=kind)._value.get()


def reserved():
    return governor.snapshot()["reserved"]


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------


class TestCpuBudget:
    def test_oversized_request_is_clamped_to_budget(self, monkeypatch):
        set_container(monkeypatch, cpu_limit=4.0)  # budget 3
        seen = []
        monkeypatch.setattr(cpu, "_cpu_hog", lambda c, d: seen.append(c))
        cpu.inject_cpu({"cores": 12, "duration_seconds": 1})
        assert seen == [3]
        assert clamped("cpu") == 1
        assert counter("cpu", "success") == 1

    def test_request_within_budget_is_untouched(self, monkeypatch):
        set_container(monkeypatch, cpu_limit=4.0)
        seen = []
        monkeypatch.setattr(cpu, "_cpu_hog", lambda c, d: seen.append(c))
        cpu.inject_cpu({"cores": 2, "duration_seconds": 1})
        assert seen == [2]
        assert clamped("cpu") == 0

    def test_reservation_released_after_success(self, monkeypatch):
        set_container(monkeypatch)
        monkeypatch.setattr(cpu, "_cpu_hog", lambda c, d: None)
        cpu.inject_cpu({"cores": 2, "duration_seconds": 1})
        assert reserved()["cores"] == 0

    def test_reservation_released_when_workers_fail(self, monkeypatch):
        set_container(monkeypatch)

        def boom(c, d):
            raise RuntimeError("spawn failed")

        monkeypatch.setattr(cpu, "_cpu_hog", boom)
        cpu.inject_cpu({"cores": 2, "duration_seconds": 1})
        assert reserved()["cores"] == 0
        assert counter("cpu", "failed") == 1

    def test_refused_when_budget_already_in_use(self, monkeypatch):
        set_container(monkeypatch, cpu_limit=4.0)  # budget 3
        governor.acquire_cores(3)
        monkeypatch.setattr(
            cpu, "_cpu_hog", lambda *a: pytest.fail("must not start workers")
        )
        cpu.inject_cpu({"cores": 1, "duration_seconds": 1})
        assert counter("cpu", "failed") == 1
        assert reserved()["cores"] == 3  # untouched by the refused call

    def test_refused_without_limits_when_required(self, monkeypatch):
        set_container(
            monkeypatch, cpu_limit=None, mem_limit_mb=None, require_cgroup_limits=True
        )
        monkeypatch.setattr(
            cpu, "_cpu_hog", lambda *a: pytest.fail("must not start workers")
        )
        cpu.inject_cpu({"cores": 1, "duration_seconds": 1})
        assert counter("cpu", "failed") == 1

    def test_dry_run_reserves_nothing_and_reports_effective_cores(
        self, monkeypatch, caplog
    ):
        import logging

        caplog.set_level(logging.INFO)
        set_container(monkeypatch, cpu_limit=4.0)
        cpu.inject_cpu({"cores": 12, "duration_seconds": 1}, dry_run=True)
        assert reserved()["cores"] == 0
        assert counter("cpu", "skipped") == 1
        record = next(r for r in caplog.records if "DRY RUN" in r.message)
        assert record.cores == 12
        assert record.effective_cores == 3

    def test_dry_run_still_refuses_when_budget_exhausted(self, monkeypatch):
        set_container(monkeypatch, cpu_limit=4.0)
        governor.acquire_cores(3)
        cpu.inject_cpu({"cores": 1, "duration_seconds": 1}, dry_run=True)
        assert counter("cpu", "failed") == 1
        assert counter("cpu", "skipped") == 0


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def wait_for_memory_threads():
    for t in threading.enumerate():
        if t.name == "memory-injection":
            t.join(timeout=5)


class TestMemoryBudget:
    def test_oversized_request_is_refused(self, monkeypatch):
        set_container(monkeypatch, mem_limit_mb=1000)  # budget 500
        monkeypatch.setattr(
            memory.threading.Thread,
            "start",
            lambda self: pytest.fail("must not allocate"),
        )
        memory.inject_memory({"mb": 501, "duration_seconds": 1})
        assert counter("memory", "failed") == 1
        assert reserved()["memory_mb"] == 0

    def test_refusal_logs_a_reason(self, monkeypatch, caplog):
        import logging

        caplog.set_level(logging.ERROR)
        set_container(monkeypatch, mem_limit_mb=1000)
        memory.inject_memory({"mb": 900, "duration_seconds": 1})
        record = next(r for r in caplog.records if "refused" in r.message)
        assert "900 MB requested" in record.reason

    def test_request_within_budget_runs_and_releases(self, monkeypatch):
        set_container(monkeypatch, mem_limit_mb=1000)
        monkeypatch.setattr(memory, "_hold_memory", lambda mb, d: None)
        memory.inject_memory({"mb": 400, "duration_seconds": 1})
        wait_for_memory_threads()
        assert counter("memory", "success") == 1
        assert reserved()["memory_mb"] == 0

    def test_reservation_held_while_running_then_released(self, monkeypatch):
        set_container(monkeypatch, mem_limit_mb=1000)
        started, finish = threading.Event(), threading.Event()

        def slow_hold(mb, d):
            started.set()
            finish.wait(5)

        monkeypatch.setattr(memory, "_hold_memory", slow_hold)
        memory.inject_memory({"mb": 300, "duration_seconds": 1})
        assert started.wait(5)
        assert reserved()["memory_mb"] == 300

        # A second injection that would push past the budget is refused
        memory.inject_memory({"mb": 300, "duration_seconds": 1})
        assert counter("memory", "failed") == 1
        assert reserved()["memory_mb"] == 300

        finish.set()
        wait_for_memory_threads()
        assert reserved()["memory_mb"] == 0

    def test_reservation_released_when_allocation_fails(self, monkeypatch):
        set_container(monkeypatch, mem_limit_mb=1000)

        def boom(mb, d):
            raise MemoryError("nope")

        monkeypatch.setattr(memory, "_hold_memory", boom)
        memory.inject_memory({"mb": 100, "duration_seconds": 1})
        wait_for_memory_threads()
        assert counter("memory", "failed") == 1
        assert reserved()["memory_mb"] == 0

    def test_reservation_released_when_thread_cannot_start(self, monkeypatch):
        set_container(monkeypatch, mem_limit_mb=1000)

        def cannot_start(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(memory.threading.Thread, "start", cannot_start)
        with pytest.raises(RuntimeError):
            memory.inject_memory({"mb": 100, "duration_seconds": 1})
        assert reserved()["memory_mb"] == 0

    def test_refused_without_limits_when_required(self, monkeypatch):
        set_container(
            monkeypatch, cpu_limit=None, mem_limit_mb=None, require_cgroup_limits=True
        )
        memory.inject_memory({"mb": 10, "duration_seconds": 1})
        assert counter("memory", "failed") == 1

    def test_dry_run_reserves_nothing(self, monkeypatch):
        set_container(monkeypatch, mem_limit_mb=1000)
        memory.inject_memory({"mb": 400, "duration_seconds": 1}, dry_run=True)
        assert reserved()["memory_mb"] == 0
        assert counter("memory", "skipped") == 1

    def test_dry_run_is_refused_when_it_would_not_fit(self, monkeypatch):
        set_container(monkeypatch, mem_limit_mb=1000)
        memory.inject_memory({"mb": 900, "duration_seconds": 1}, dry_run=True)
        assert counter("memory", "failed") == 1
        assert counter("memory", "skipped") == 0
