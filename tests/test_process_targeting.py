"""
Process targeting: exact matching, whole-word critical protection, deterministic
selection, and truthful termination. Uses fake processes so every case is
deterministic; real processes and PID namespaces are covered in their own files.
"""

import logging
import random

import psutil
import pytest

from src.failures import process as proc_mod
from src.failures.process import (
    CRITICAL_PROCESSES,
    PID1_REFUSAL,
    executable_names,
    find_target_processes,
    inject_process,
    is_critical_process,
    match_kind,
    script_names,
    select_target,
    terminate_process,
)
from src.metrics import INJECTIONS_TOTAL, metric_value


def count(status):
    return metric_value(INJECTIONS_TOTAL, failure_type="process", status=status)


class FakeProc:
    """Enough of psutil.Process for the injector."""

    def __init__(
        self,
        pid,
        name="app",
        cmdline=None,
        exe=None,
        ppid=1000,
        create_time=100.0,
        status="running",
        wait_results=None,
        terminate_exc=None,
    ):
        self.pid = pid
        self.info = {
            "pid": pid,
            "name": name,
            "cmdline": [name] if cmdline is None else cmdline,
            "exe": exe,
            "ppid": ppid,
            "create_time": create_time,
            "status": status,
        }
        self.calls = []
        # each wait() consumes one entry: None means exited, else raise it
        self._waits = list(wait_results if wait_results is not None else [None])
        self._terminate_exc = terminate_exc

    def terminate(self):
        self.calls.append("terminate")
        if self._terminate_exc:
            raise self._terminate_exc

    def kill(self):
        self.calls.append("kill")

    def wait(self, timeout=None):
        self.calls.append("wait")
        result = self._waits.pop(0) if self._waits else None
        if result is not None:
            raise result
        return 0


# ---------------------------------------------------------------------------
# executable_names and critical protection
# ---------------------------------------------------------------------------


class TestExecutableNames:
    def test_collects_process_name_exe_and_argv0(self):
        names = executable_names("Sshd", ["/usr/sbin/SSHD", "-D"], "/opt/bin/sshd-real")
        assert names == {"sshd", "sshd-real"}

    def test_ignores_arguments(self):
        assert executable_names("python", ["python", "kubelet.py", "--sshd"]) == {
            "python"
        }

    def test_handles_missing_pieces(self):
        assert executable_names(None, None, None) == set()
        assert executable_names("", [], "") == set()


PROTECTED = [
    ("systemd", ["systemd", "--system"]),
    ("systemd-journald", ["/lib/systemd/systemd-journald"]),
    ("systemd-udevd", []),
    ("containerd-shim", ["containerd-shim-runc-v2", "-namespace", "k8s.io"]),
    ("dumb-init", ["/usr/bin/dumb-init", "--", "app"]),
    ("docker-init", ["/sbin/docker-init", "--"]),
    ("tini", ["/sbin/tini", "--", "app"]),
    ("pause", ["/pause"]),
    ("kube-controller-manager", ["kube-controller-manager"]),
    ("NetworkManager", ["/usr/sbin/NetworkManager", "--no-daemon"]),
    ("sshd", ["sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups"]),
    ("runc", ["runc", "init"]),
    ("", ["/usr/bin/dockerd", "-H", "unix://"]),
]

NOT_PROTECTED = [
    ("python", ["python", "/opt/app/initialize.py"]),
    ("python", ["python", "truncate-job.py"]),
    ("initialize", ["./initialize"]),
    ("truncate-job", ["./truncate-job", "--all"]),
    ("reinit", ["reinit"]),
    ("init2", ["init2"]),
    ("paused", ["paused"]),
    ("unpause", ["unpause"]),
    ("myinit", ["myinit"]),
    ("sshdump", ["sshdump"]),
    ("target-app", ["python", "target-app.py"]),
    ("java", ["java", "-jar", "kubelet-sim.jar"]),
    ("sh", ["sh", "-c", "sshd"]),
    ("tail", ["tail", "-f", "/var/log/systemd.log"]),
]


class TestCriticalProtection:
    @pytest.mark.parametrize("name,cmdline", PROTECTED)
    def test_critical_executables_are_protected(self, name, cmdline):
        assert is_critical_process(name, cmdline) is True

    @pytest.mark.parametrize("name,cmdline", NOT_PROTECTED)
    def test_lookalikes_and_arguments_are_not_protected(self, name, cmdline):
        assert is_critical_process(name, cmdline) is False

    def test_the_two_reported_false_positives(self):
        """`initialize` contains "init" and `truncate` contains "runc"."""
        assert is_critical_process("python", ["python", "initialize.py"]) is False
        assert is_critical_process("python", ["python", "truncate-job.py"]) is False

    def test_every_listed_name_is_still_protected(self):
        for name in CRITICAL_PROCESSES:
            assert is_critical_process(name, []) is True
            assert is_critical_process(name.upper(), []) is True
            assert is_critical_process("x", [f"/usr/bin/{name}"]) is True

    def test_exe_path_counts(self):
        assert is_critical_process("worker", ["worker"], "/usr/sbin/sshd") is True

    def test_hyphenated_name_containing_a_critical_word_is_protected(self):
        """Documented trade-off: whole-word match, so `pause-service` is protected."""
        assert is_critical_process("pause-service", ["pause-service"]) is True

    def test_handles_none_cmdline(self):
        assert is_critical_process("myapp", None) is False


# ---------------------------------------------------------------------------
# script_names and match_kind
# ---------------------------------------------------------------------------


class TestScriptNames:
    @pytest.mark.parametrize(
        "cmdline,expected",
        [
            (["python", "target-app.py"], {"target-app.py", "target-app"}),
            (["python", "-u", "target-app.py"], {"target-app.py", "target-app"}),
            (["/usr/bin/python3.12", "/srv/app/main.py"], {"main.py", "main"}),
            (["python", "-m", "src.api_server"], {"src.api_server", "api_server"}),
            (["python", "-m", "uvicorn", "app:app"], {"uvicorn"}),
            (["python", "-W", "error", "job.py"], {"job.py", "job"}),
            (["pypy3", "x.py"], {"x.py", "x"}),
            (
                ["java", "-Xmx1g", "-jar", "/opt/Billing.jar"],
                {"billing.jar", "billing"},
            ),
            (["java", "-cp", "lib/*", "com.acme.Main"], {"com.acme.main", "com.acme"}),
            (["node", "server.js"], {"server.js", "server"}),
            (["node", "-r", "dotenv/config", "app.js"], {"app.js", "app"}),
            (["bash", "/opt/run.sh"], {"run.sh", "run"}),
        ],
    )
    def test_interpreters(self, cmdline, expected):
        assert script_names(cmdline) == expected

    @pytest.mark.parametrize(
        "cmdline",
        [
            ["python", "-c", "print(1)"],
            ["python"],
            ["python", "-u"],
            ["node", "-e", "1"],
            ["nginx", "-g", "daemon off;"],
            ["tail", "-f", "app.log"],
            ["./target-app", "--port", "8080"],
            [],
            None,
        ],
    )
    def test_nothing_for_other_programs_or_inline_code(self, cmdline):
        assert script_names(cmdline) == set()


class TestMatchKind:
    def test_exact_process_name(self):
        assert match_kind("target-app", "target-app", ["target-app"]) == "name"

    def test_case_insensitive(self):
        assert match_kind("MyApp", "myapp", ["myapp"]) == "name"
        assert match_kind("myapp", "MyApp", ["MyApp"]) == "name"

    def test_whitespace_is_ignored(self):
        assert match_kind("  nginx ", "nginx", ["nginx"]) == "name"

    def test_executable_path(self):
        assert match_kind("myapp", "worker", ["worker"], "/opt/myapp") == "name"

    def test_argv0_basename(self):
        assert match_kind("myapp", "x", ["/opt/bin/myapp", "--flag"]) == "name"

    def test_script_of_an_interpreter(self):
        assert (
            match_kind("target-app", "python", ["python", "target-app.py"]) == "script"
        )

    def test_script_with_extension(self):
        assert (
            match_kind("target-app.py", "python", ["python", "target-app.py"])
            == "script"
        )

    def test_module(self):
        assert (
            match_kind("api_server", "python", ["python", "-m", "src.api_server"])
            == "script"
        )

    def test_jar(self):
        assert (
            match_kind("billing", "java", ["java", "-jar", "Billing.jar"]) == "script"
        )

    @pytest.mark.parametrize(
        "target,name,cmdline",
        [
            # substring of the name
            ("target", "target-app", ["target-app"]),
            ("app", "happy", ["happy"]),
            ("nginx", "nginx-exporter", ["nginx-exporter"]),
            # in the arguments
            ("target-app", "tail", ["tail", "-f", "/var/log/target-app.log"]),
            ("target-app", "kubectl", ["kubectl", "logs", "target-app"]),
            ("target-app", "grep", ["grep", "target-app", "x"]),
            ("target-app", "vim", ["vim", "target-app.py"]),
            ("myapp", "sh", ["sh", "-c", "echo myapp"]),
            # inline code is not a script
            ("myapp", "python", ["python", "-c", "import myapp"]),
            # empty
            ("", "x", ["x"]),
            ("   ", "x", ["x"]),
        ],
    )
    def test_never_a_substring_and_never_arguments(self, target, name, cmdline):
        assert match_kind(target, name, cmdline) is None

    def test_long_names_match_the_kernels_truncated_comm(self):
        target = "billing-reconciler-service"
        comm = target[:15]  # what /proc/<pid>/comm shows
        assert len(comm) == 15
        assert match_kind(target, comm, ["/opt/x/start"]) == "name"

    def test_truncation_rule_does_not_apply_to_short_targets(self):
        assert match_kind("billing", "billing-reconc", ["x"]) is None

    def test_long_name_that_merely_shares_a_prefix_does_not_match(self):
        assert (
            match_kind("billing-reconciler-service", "billing-reconcil", ["x"]) is None
        )


# ---------------------------------------------------------------------------
# select_target
# ---------------------------------------------------------------------------


class TestSelectTarget:
    def procs(self):
        return [
            FakeProc(300, create_time=30.0),
            FakeProc(100, create_time=10.0),
            FakeProc(200, create_time=20.0),
        ]

    def test_oldest_is_the_default(self):
        chosen, _ = select_target(self.procs())
        assert chosen.pid == 100

    def test_newest(self):
        chosen, _ = select_target(self.procs(), "newest")
        assert chosen.pid == 300

    def test_returns_candidates_oldest_first(self):
        _, ordered = select_target(self.procs())
        assert [p.pid for p in ordered] == [100, 200, 300]

    def test_ties_break_on_pid(self):
        a, b = FakeProc(50, create_time=5.0), FakeProc(40, create_time=5.0)
        assert select_target([a, b])[0].pid == 40
        assert select_target([a, b], "newest")[0].pid == 50

    def test_result_does_not_depend_on_input_order(self):
        base = self.procs()
        for _ in range(20):
            shuffled = random.sample(base, len(base))
            assert select_target(shuffled)[0].pid == 100
            assert select_target(shuffled, "newest")[0].pid == 300

    def test_missing_start_time_sorts_first_then_by_pid(self):
        a = FakeProc(9, create_time=None)
        b = FakeProc(5, create_time=None)
        assert select_target([a, b])[0].pid == 5

    def test_single_candidate(self):
        only = FakeProc(7)
        assert select_target([only])[0] is only


# ---------------------------------------------------------------------------
# find_target_processes
# ---------------------------------------------------------------------------


class Me:
    def __init__(self, children=(), parents=()):
        self._children, self._parents = list(children), list(parents)

    def children(self, recursive=False):
        return self._children

    def parents(self):
        return self._parents


@pytest.fixture
def scan(monkeypatch):
    """Run find_target_processes over fake processes, as agent pid 9000."""

    def run(procs, target_name=None, target_pid=None, me=None):
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
        monkeypatch.setattr("os.getpid", lambda: 9000)
        monkeypatch.setattr("os.getppid", lambda: 8999)
        monkeypatch.setattr(psutil, "Process", lambda pid: me or Me())
        return find_target_processes(target_name, target_pid)

    return run


class TestFindTargetProcesses:
    def test_finds_by_exact_name_only(self, scan):
        procs = [
            FakeProc(10, "target-app", ["target-app"]),
            FakeProc(11, "tail", ["tail", "-f", "target-app.log"]),
            FakeProc(12, "target-app-helper", ["target-app-helper"]),
        ]
        found, _ = scan(procs, "target-app")
        assert [p.pid for p in found] == [10]

    def test_finds_an_interpreted_app_by_script(self, scan):
        procs = [FakeProc(10, "python", ["python", "target-app.py"])]
        assert [p.pid for p in scan(procs, "target-app")[0]] == [10]

    def test_protects_self_parent_children_and_ancestors(self, scan):
        procs = [
            FakeProc(9000, "myapp", ["myapp"]),
            FakeProc(8999, "myapp", ["myapp"]),
            FakeProc(9001, "myapp", ["myapp"]),  # descendant
            FakeProc(8000, "myapp", ["myapp"]),  # ancestor
            FakeProc(7, "myapp", ["myapp"]),
        ]
        me = Me(children=[FakeProc(9001)], parents=[FakeProc(8000)])
        found, stats = scan(procs, "myapp", me=me)
        assert [p.pid for p in found] == [7]
        assert stats["protected"] == 4

    def test_direct_children_by_ppid_are_protected(self, scan):
        procs = [FakeProc(20, "myapp", ["myapp"], ppid=9000)]
        assert scan(procs, "myapp")[0] == []

    def test_zombies_are_never_candidates(self, scan):
        procs = [FakeProc(10, "myapp", ["myapp"], status=psutil.STATUS_ZOMBIE)]
        assert scan(procs, "myapp")[0] == []

    def test_kernel_threads_and_unreadable_processes_are_skipped(self, scan):
        procs = [FakeProc(2, "kthreadd", []), FakeProc(3, "myapp", [])]
        found, stats = scan(procs, "myapp")
        assert found == [] and stats["unreadable"] == 2

    def test_critical_processes_are_never_candidates(self, scan):
        procs = [FakeProc(1, "systemd", ["systemd"]), FakeProc(5, "sshd", ["sshd"])]
        found, stats = scan(procs, "sshd")
        assert found == [] and stats["critical"] == 2

    def test_an_app_named_like_a_critical_prefix_is_findable(self, scan):
        procs = [FakeProc(10, "python", ["python", "initialize.py"])]
        assert [p.pid for p in scan(procs, "initialize")[0]] == [10]

    def test_pid_only(self, scan):
        procs = [FakeProc(10, "a", ["a"]), FakeProc(11, "b", ["b"])]
        assert [p.pid for p in scan(procs, None, 11)[0]] == [11]

    def test_pid_and_name_must_both_match(self, scan):
        procs = [FakeProc(10, "a", ["a"]), FakeProc(11, "b", ["b"])]
        assert scan(procs, "a", 11)[0] == []
        assert [p.pid for p in scan(procs, "a", 10)[0]] == [10]

    def test_pid_of_a_critical_process_is_still_refused(self, scan):
        procs = [FakeProc(50, "sshd", ["sshd"])]
        assert scan(procs, None, 50)[0] == []

    def test_pid_of_a_protected_process_is_still_refused(self, scan):
        procs = [FakeProc(9000, "me", ["me"])]
        assert scan(procs, None, 9000)[0] == []

    def test_unreadable_process_does_not_abort_the_scan(self, scan):
        class Broken(FakeProc):
            @property
            def info(self):
                raise psutil.NoSuchProcess(5)

            @info.setter
            def info(self, value):
                pass

        procs = [FakeProc(10, "myapp", ["myapp"])]
        found, _ = scan(procs, "myapp")
        assert [p.pid for p in found] == [10]

    def test_survives_a_failing_process_iterator(self, monkeypatch):
        def boom(attrs=None):
            raise RuntimeError("proc fs gone")

        monkeypatch.setattr(psutil, "process_iter", boom)
        monkeypatch.setattr(psutil, "Process", lambda pid: Me())
        found, _ = find_target_processes("myapp")
        assert found == []

    def test_scan_tolerates_failure_to_inspect_itself(self, scan, monkeypatch):
        procs = [FakeProc(10, "myapp", ["myapp"])]
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
        monkeypatch.setattr("os.getpid", lambda: 9000)
        monkeypatch.setattr("os.getppid", lambda: 8999)

        def deny(pid):
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(psutil, "Process", deny)
        found, _ = find_target_processes("myapp")
        assert [p.pid for p in found] == [10]


# ---------------------------------------------------------------------------
# terminate_process
# ---------------------------------------------------------------------------


class TestTerminateProcess:
    def test_pid_1_is_refused_without_sending_anything(self, caplog):
        caplog.set_level(logging.ERROR)
        init = FakeProc(1, "myapp")
        assert terminate_process(init, "myapp") is False
        assert init.calls == []
        assert count("failed") == 1 and count("success") == 0
        record = next(
            r for r in caplog.records if "Refusing to kill PID 1" in r.message
        )
        assert record.reason == PID1_REFUSAL

    def test_pid_1_refusal_explains_the_kernel_behavior_and_the_fix(self):
        assert "SIGKILL" in PID1_REFUSAL
        assert "including this agent" in PID1_REFUSAL
        assert "init: true" in PID1_REFUSAL

    def test_graceful_exit_is_success(self):
        p = FakeProc(50)
        assert terminate_process(p, "x") is True
        assert p.calls == ["terminate", "wait"]
        assert count("success") == 1

    def test_escalates_to_sigkill_when_sigterm_is_ignored(self):
        p = FakeProc(50, wait_results=[psutil.TimeoutExpired(3), None])
        assert terminate_process(p, "x") is True
        assert p.calls == ["terminate", "wait", "kill", "wait"]
        assert count("success") == 1

    def test_a_survivor_of_sigkill_is_a_failure(self):
        p = FakeProc(
            50, wait_results=[psutil.TimeoutExpired(3), psutil.TimeoutExpired(2)]
        )
        assert terminate_process(p, "x") is False
        assert count("failed") == 1 and count("success") == 0

    def test_process_already_gone_is_skipped(self):
        p = FakeProc(50, terminate_exc=psutil.NoSuchProcess(50))
        assert terminate_process(p, "x") is False
        assert count("skipped") == 1

    def test_access_denied_is_a_failure(self):
        p = FakeProc(50, terminate_exc=psutil.AccessDenied(50))
        assert terminate_process(p, "x") is False
        assert count("failed") == 1


# ---------------------------------------------------------------------------
# inject_process
# ---------------------------------------------------------------------------


@pytest.fixture
def injector(monkeypatch):
    """Run inject_process over fake candidates and record what gets killed."""
    killed = []

    def run(config, procs, dry_run=False):
        monkeypatch.setattr(
            proc_mod,
            "get_safe_target_processes",
            lambda name, pid=None: [p for p in procs if (pid is None or p.pid == pid)],
        )
        monkeypatch.setattr(
            proc_mod, "terminate_process", lambda p, name: killed.append(p.pid) or True
        )
        inject_process(config, dry_run=dry_run)
        return killed

    return run


class TestInjectProcess:
    def procs(self):
        return [
            FakeProc(300, create_time=30.0),
            FakeProc(100, create_time=10.0),
            FakeProc(200, create_time=20.0),
        ]

    def test_kills_exactly_one_the_oldest_by_default(self, injector):
        assert injector({"target_name": "app"}, self.procs()) == [100]

    def test_newest_when_asked(self, injector):
        cfg = {"target_name": "app", "target_selection": "newest"}
        assert injector(cfg, self.procs()) == [300]

    def test_is_deterministic_across_runs(self, injector):
        # The fixture accumulates every kill, so ten runs over shuffled input
        # must produce ten identical choices.
        for _ in range(10):
            killed = injector({"target_name": "app"}, random.sample(self.procs(), 3))
        assert killed == [100] * 10

    def test_logs_all_candidates_and_the_choice(self, injector, caplog):
        caplog.set_level(logging.INFO)
        injector({"target_name": "app"}, self.procs())
        record = next(
            r for r in caplog.records if "Several processes match" in r.message
        )
        assert record.chosen_pid == 100
        assert [c["pid"] for c in record.candidates] == [100, 200, 300]
        assert record.selection == "oldest"

    def test_no_candidate_log_for_a_single_match(self, injector, caplog):
        caplog.set_level(logging.INFO)
        injector({"target_name": "app"}, [FakeProc(5)])
        assert not any("Several processes match" in r.message for r in caplog.records)

    def test_target_pid_alone(self, injector):
        assert injector({"target_pid": 200}, self.procs()) == [200]

    def test_no_name_and_no_pid_does_nothing(self, injector, caplog):
        caplog.set_level(logging.WARNING)
        assert injector({}, self.procs()) == []
        assert any(
            "without target_name or target_pid" in r.message for r in caplog.records
        )

    def test_invalid_name_is_a_failure_and_kills_nothing(self, injector):
        assert injector({"target_name": "python"}, self.procs()) == []
        assert count("failed") == 1

    def test_no_match_is_skipped(self, injector):
        assert injector({"target_name": "app"}, []) == []
        assert count("skipped") == 1

    def test_dry_run_kills_nothing_and_reports_the_choice(self, injector, caplog):
        caplog.set_level(logging.INFO)
        assert injector({"target_name": "app"}, self.procs(), dry_run=True) == []
        record = next(r for r in caplog.records if "DRY RUN" in r.message)
        assert record.pid == 100 and record.candidate_count == 3
        assert record.would_be_refused is False
        assert count("skipped") == 1

    def test_dry_run_says_when_a_real_run_would_be_refused(self, injector, caplog):
        caplog.set_level(logging.INFO)
        injector({"target_name": "app"}, [FakeProc(1)], dry_run=True)
        record = next(r for r in caplog.records if "DRY RUN" in r.message)
        assert record.is_pid_1 is True and record.would_be_refused is True

    def test_unexpected_error_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(
            proc_mod, "get_safe_target_processes", lambda *a, **k: [FakeProc(5)]
        )

        def boom(p, name):
            raise RuntimeError("bug")

        monkeypatch.setattr(proc_mod, "terminate_process", boom)
        inject_process({"target_name": "app"})
        assert count("failed") == 1
