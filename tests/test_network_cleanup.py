"""Network rule ownership, locking, shutdown cleanup, and stale-rule repair."""

import subprocess
import threading
import time

import pytest

from src.failures import network
from src.lifecycle import lifecycle
from src.metrics import INJECTIONS_TOTAL


class FakeTc:
    """Records tc commands and models one root qdisc per interface."""

    def __init__(self, existing=None):
        self.rules = dict(existing or {})  # interface -> "netem ..." | "fq_codel"
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, args):
        with self.lock:
            self.calls.append(list(args))
            verb, dev = args[2], args[args.index("dev") + 1]
            if verb == "show":
                kind = self.rules.get(dev)
                out = f"qdisc {kind} 8001: root" if kind else "qdisc noqueue 0: root"
                return subprocess.CompletedProcess(args, 0, out, "")
            if verb == "add":
                if dev in self.rules:
                    return subprocess.CompletedProcess(args, 2, "", "File exists")
                self.rules[dev] = "netem"
                return subprocess.CompletedProcess(args, 0, "", "")
            if verb == "del":
                if dev not in self.rules:
                    return subprocess.CompletedProcess(
                        args, 2, "", "Cannot delete qdisc with handle of zero."
                    )
                del self.rules[dev]
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")

    def dels(self):
        return [c for c in self.calls if c[2] == "del"]


@pytest.fixture
def tc(monkeypatch):
    fake = FakeTc()
    monkeypatch.setattr(network, "_run_cmd", fake)
    monkeypatch.setattr(network, "verify_interface_exists", lambda i: (True, None))
    network._applied.clear()
    yield fake
    network._applied.clear()


def count(status):
    return INJECTIONS_TOTAL.labels(failure_type="network", status=status)._value.get()


CFG = {"interface": "eth0", "delay_ms": 100, "duration_seconds": 30}


def run_in_thread(cfg=CFG):
    t = threading.Thread(target=network.inject_network, args=(cfg,))
    t.start()
    return t


def wait_until(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


class TestInjectionCleansUpAfterItself:
    def test_rule_is_removed_after_normal_completion(self, tc):
        network.inject_network({**CFG, "duration_seconds": 0})
        assert tc.rules == {}
        assert network._applied == {}

    def test_rule_is_removed_when_add_fails(self, tc, monkeypatch):
        original = tc.__call__

        def failing(args):
            if args[2] == "add":
                return subprocess.CompletedProcess(args, 1, "", "no netem")
            return original(args)

        monkeypatch.setattr(network, "_run_cmd", failing)
        network.inject_network({**CFG, "duration_seconds": 0})
        assert network._applied == {}
        assert count("failed") == 1

    def test_abort_ends_the_hold_and_removes_the_rule(self, tc):
        t = run_in_thread()
        wait_until(lambda: "eth0" in tc.rules)
        started = time.time()
        assert lifecycle.abort_active("test") == 1
        t.join(5)
        assert tc.rules == {}
        assert time.time() - started < 2

    def test_shutdown_removes_the_rule_even_before_the_thread_wakes(self, tc):
        t = run_in_thread()
        wait_until(lambda: "eth0" in tc.rules)
        lifecycle.run_cleanups()  # what a signal handler does first
        assert tc.rules == {}
        lifecycle.shutdown("test")
        t.join(5)
        assert tc.rules == {}


class TestOwnership:
    def test_release_by_a_non_owner_does_nothing(self, tc):
        tc.rules["eth0"] = "netem"
        network._applied["eth0"] = "owner-a"
        network._release_rule("eth0", "someone-else")
        assert "eth0" in tc.rules and tc.dels() == []

    def test_release_by_the_owner_removes_the_rule(self, tc):
        tc.rules["eth0"] = "netem"
        network._applied["eth0"] = "owner-a"
        assert network._release_rule("eth0", "owner-a") == (True, None)
        assert tc.rules == {} and network._applied == {}

    def test_release_after_shutdown_already_removed_it_is_a_noop(self, tc):
        """The injection thread wakes after shutdown cleanup ran. It must not
        delete a rule that a newer injection has applied since."""
        network._applied["eth0"] = "old-owner"
        tc.rules["eth0"] = "netem"
        network.cleanup_all_network_rules()
        # a newer injection claims and applies
        network._applied["eth0"] = "new-owner"
        tc.rules["eth0"] = "netem"
        network._release_rule("eth0", "old-owner")
        assert "eth0" in tc.rules

    def test_ownership_dropped_even_if_delete_fails(self, tc, monkeypatch):
        network._applied["eth0"] = "me"
        monkeypatch.setattr(
            network,
            "_run_cmd",
            lambda a: subprocess.CompletedProcess(a, 1, "", "permission denied"),
        )
        success, _ = network._release_rule("eth0", "me")
        assert not success
        assert network._applied == {}


class TestOverlapGuard:
    def test_second_injection_is_skipped_and_touches_nothing(self, tc):
        t = run_in_thread()
        wait_until(lambda: "eth0" in tc.rules)
        calls_before = len(tc.calls)

        network.inject_network(CFG)  # refused by the guard

        assert count("skipped") == 1
        assert len(tc.calls) == calls_before
        assert "eth0" in tc.rules
        lifecycle.abort_active()
        t.join(5)
        assert tc.rules == {}

    def test_injection_allowed_again_after_the_first_finishes(self, tc):
        network.inject_network({**CFG, "duration_seconds": 0})
        network.inject_network({**CFG, "duration_seconds": 0})
        assert count("skipped") == 0
        assert len([c for c in tc.calls if c[2] == "add"]) == 2

    def test_dry_run_is_never_blocked(self, tc):
        t = run_in_thread()
        wait_until(lambda: "eth0" in tc.rules)
        network.inject_network(CFG, dry_run=True)
        assert count("skipped") == 1  # the dry run itself, counted as dry run
        lifecycle.abort_active()
        t.join(5)

    def test_refused_while_shutting_down(self, tc):
        lifecycle.shutdown("test")
        network.inject_network(CFG)
        assert tc.rules == {} and tc.calls == []


class TestCleanupAll:
    def test_removes_only_rules_this_process_applied(self, tc):
        tc.rules["eth0"] = "netem"
        tc.rules["eth1"] = "fq_codel"  # someone else's, never touched
        network._applied["eth0"] = "me"
        network.cleanup_all_network_rules()
        assert tc.rules == {"eth1": "fq_codel"}
        assert network._applied == {}

    def test_idempotent(self, tc):
        network._applied["eth0"] = "me"
        tc.rules["eth0"] = "netem"
        network.cleanup_all_network_rules()
        network.cleanup_all_network_rules()
        assert len(tc.dels()) == 1

    def test_registered_with_the_lifecycle(self, tc):
        tc.rules["eth0"] = "netem"
        network._applied["eth0"] = "me"
        lifecycle.run_cleanups()
        assert tc.rules == {}


class TestReconcileStaleRules:
    def test_removes_a_stale_netem_rule(self, tc):
        tc.rules["eth0"] = "netem"
        assert network.reconcile_stale_rules(["eth0"]) == ["eth0"]
        assert tc.rules == {}

    def test_never_touches_a_non_netem_qdisc(self, tc):
        tc.rules["eth0"] = "fq_codel"
        assert network.reconcile_stale_rules(["eth0"]) == []
        assert tc.rules == {"eth0": "fq_codel"} and tc.dels() == []

    def test_noop_when_there_is_no_rule(self, tc):
        assert network.reconcile_stale_rules(["eth0"]) == []
        assert tc.dels() == []

    def test_skips_an_interface_being_injected_right_now(self, tc):
        tc.rules["eth0"] = "netem"
        network._applied["eth0"] = "live"
        assert network.reconcile_stale_rules(["eth0"]) == []
        assert "eth0" in tc.rules

    def test_invalid_interface_names_are_ignored(self, tc):
        assert network.reconcile_stale_rules(["eth0; rm -rf /", ""]) == []
        assert tc.calls == []

    def test_duplicates_are_checked_once(self, tc):
        tc.rules["eth0"] = "netem"
        assert network.reconcile_stale_rules(["eth0", "eth0"]) == ["eth0"]
        assert len(tc.dels()) == 1

    def test_missing_tc_binary_does_not_raise(self, monkeypatch):
        def no_tc(args):
            raise Exception("Command execution failed: tc not found")

        monkeypatch.setattr(network, "_run_cmd", no_tc)
        monkeypatch.setattr(network, "verify_interface_exists", lambda i: (True, None))
        assert network.reconcile_stale_rules(["eth0"]) == []

    def test_failed_delete_is_reported_not_raised(self, tc, monkeypatch):
        tc.rules["eth0"] = "netem"
        original = tc.__call__

        def deny(args):
            if args[2] == "del":
                return subprocess.CompletedProcess(args, 1, "", "permission denied")
            return original(args)

        monkeypatch.setattr(network, "_run_cmd", deny)
        assert network.reconcile_stale_rules(["eth0"]) == []
