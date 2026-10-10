"""Tests for the injection lifecycle: guard, abort, shutdown, cleanup registry."""

import threading
import time

import pytest

from src.lifecycle import Lifecycle
from src.metrics import INJECTIONS_TOTAL


@pytest.fixture
def lc():
    return Lifecycle()


def skipped(kind):
    return INJECTIONS_TOTAL.labels(failure_type=kind, status="skipped")._value.get()


class TestGuard:
    def test_first_begin_succeeds(self, lc):
        ticket, reason = lc.begin("network")
        assert ticket is not None and reason == ""

    def test_second_of_same_type_is_refused(self, lc):
        lc.begin("network")
        ticket, reason = lc.begin("network")
        assert ticket is None
        assert "network injection is already running" in reason

    def test_different_types_do_not_block_each_other(self, lc):
        assert lc.begin("network")[0] is not None
        assert lc.begin("cpu")[0] is not None
        assert lc.begin("memory")[0] is not None

    def test_end_frees_the_slot(self, lc):
        ticket, _ = lc.begin("cpu")
        lc.end(ticket)
        assert lc.begin("cpu")[0] is not None

    def test_ending_a_stale_ticket_does_not_free_a_newer_one(self, lc):
        old, _ = lc.begin("cpu")
        lc.end(old)
        new, _ = lc.begin("cpu")
        lc.end(old)  # late duplicate end
        assert lc.is_active("cpu")
        lc.end(new)
        assert not lc.is_active("cpu")

    def test_active_types_sorted(self, lc):
        lc.begin("network")
        lc.begin("cpu")
        assert lc.active_types() == ["cpu", "network"]

    def test_refused_after_shutdown(self, lc):
        lc.shutdown("test")
        ticket, reason = lc.begin("cpu")
        assert ticket is None and "shutting down" in reason

    def test_begin_or_skip_counts_a_skip(self, lc):
        lc.begin("cpu")
        assert lc.begin_or_skip("cpu") is None
        assert skipped("cpu") == 1

    def test_concurrent_begins_admit_exactly_one(self, lc):
        winners = []

        def worker():
            ticket, _ = lc.begin("network")
            if ticket:
                winners.append(ticket)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(winners) == 1


class TestInjectionContext:
    def test_holds_then_releases(self, lc):
        with lc.injection("cpu") as ticket:
            assert ticket is not None and lc.is_active("cpu")
        assert not lc.is_active("cpu")

    def test_releases_on_exception(self, lc):
        with pytest.raises(RuntimeError):
            with lc.injection("cpu"):
                raise RuntimeError("boom")
        assert not lc.is_active("cpu")

    def test_releases_on_system_exit(self, lc):
        with pytest.raises(SystemExit):
            with lc.injection("cpu"):
                raise SystemExit(0)
        assert not lc.is_active("cpu")

    def test_refusal_yields_none_and_counts(self, lc):
        with lc.injection("cpu"):
            with lc.injection("cpu") as second:
                assert second is None
        assert skipped("cpu") == 1

    def test_disabled_holds_nothing_and_never_refuses(self, lc):
        """Dry runs pass enabled=False: no guard taken, no skip counted."""
        with lc.injection("cpu"):
            with lc.injection("cpu", enabled=False) as ticket:
                assert ticket is None
        assert skipped("cpu") == 0


class TestInterruptibleSleep:
    def test_sleeps_full_duration_when_not_interrupted(self, lc):
        ticket, _ = lc.begin("cpu")
        started = time.time()
        assert ticket.sleep(0.2) is False
        assert time.time() - started >= 0.19

    def test_abort_wakes_sleepers_immediately(self, lc):
        ticket, _ = lc.begin("network")
        result = []
        t = threading.Thread(target=lambda: result.append(ticket.sleep(30)))
        t.start()
        time.sleep(0.1)
        started = time.time()
        assert lc.abort_active("test") == 1
        t.join(5)
        assert result == [True]
        assert time.time() - started < 1

    def test_abort_does_not_affect_injections_started_afterwards(self, lc):
        lc.abort_active()
        ticket, _ = lc.begin("cpu")
        assert ticket.sleep(0.05) is False
        assert ticket.interrupted is False

    def test_abort_marks_running_ticket_interrupted(self, lc):
        ticket, _ = lc.begin("cpu")
        assert ticket.interrupted is False
        lc.abort_active()
        assert ticket.interrupted is True

    def test_abort_with_nothing_running_returns_zero(self, lc):
        assert lc.abort_active() == 0

    def test_shutdown_wakes_sleepers(self, lc):
        ticket, _ = lc.begin("network")
        result = []
        t = threading.Thread(target=lambda: result.append(ticket.sleep(30)))
        t.start()
        time.sleep(0.1)
        lc.shutdown("test")
        t.join(5)
        assert result == [True]

    def test_module_sleep_uses_the_current_ticket(self, lc):
        with lc.injection("cpu"):
            result = []
            t = threading.Thread(target=lambda: result.append(lc.sleep(0.01)))
            # a different thread has no ticket: just sleeps, not interrupted
            t.start()
            t.join()
            assert result == [False]

    def test_sleep_outside_an_injection_still_wakes_on_shutdown(self, lc):
        result = []
        t = threading.Thread(target=lambda: result.append(lc.sleep(30)))
        t.start()
        time.sleep(0.1)
        lc.shutdown("test")
        t.join(5)
        assert result == [True]

    def test_bound_ticket_is_used_by_worker_threads(self, lc):
        ticket, _ = lc.begin("memory")
        result = []

        def worker():
            with lc.bound(ticket):
                result.append(lc.sleep(30))

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.1)
        lc.abort_active()
        t.join(5)
        assert result == [True]


class TestShutdownAndCleanups:
    def test_shutdown_runs_cleanups(self, lc):
        ran = []
        lc.register_cleanup("a", lambda: ran.append("a"))
        lc.shutdown("test")
        assert ran == ["a"]
        assert lc.is_shutting_down()

    def test_one_failing_cleanup_does_not_stop_the_rest(self, lc):
        ran = []

        def bad():
            raise RuntimeError("boom")

        lc.register_cleanup("bad", bad)
        lc.register_cleanup("good", lambda: ran.append("good"))
        lc.run_cleanups()
        assert ran == ["good"]

    def test_shutdown_is_idempotent(self, lc):
        ran = []
        lc.register_cleanup("a", lambda: ran.append(1))
        lc.shutdown("one")
        lc.shutdown("two")
        assert lc.is_shutting_down()
        assert len(ran) == 2  # cleanups must be idempotent; both calls run them

    def test_registering_the_same_name_replaces(self, lc):
        ran = []
        lc.register_cleanup("a", lambda: ran.append("old"))
        lc.register_cleanup("a", lambda: ran.append("new"))
        lc.run_cleanups()
        assert ran == ["new"]

    def test_cleanup_can_run_from_inside_a_cleanup_lock_holder(self, lc):
        """Signal handlers re-enter on the same thread: must not deadlock."""
        ran = []
        lc.register_cleanup("a", lambda: ran.append(1))
        with lc._cond:  # simulate being interrupted inside a critical section
            lc.shutdown("signal")
        assert ran == [1]

    def test_reset_keeps_cleanups_but_clears_state(self, lc):
        ran = []
        lc.register_cleanup("a", lambda: ran.append(1))
        lc.begin("cpu")
        lc.shutdown("x")
        lc.reset()
        assert not lc.is_shutting_down()
        assert lc.active_types() == []
        lc.run_cleanups()
        assert ran == [1, 1]

    def test_install_atexit_is_idempotent(self, lc, monkeypatch):
        calls = []
        monkeypatch.setattr("src.lifecycle.atexit.register", calls.append)
        lc.install_atexit()
        lc.install_atexit()
        assert calls == [lc.run_cleanups]
