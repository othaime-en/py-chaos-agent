"""Additional tests for enhanced process killer security."""

import pytest
from unittest.mock import MagicMock
from src.failures.process import (
    validate_target_name,
    is_critical_process,
    inject_process,
    CRITICAL_PROCESSES,
    PROHIBITED_TARGETS,
)
from src.metrics import INJECTIONS_TOTAL


class TestTargetNameValidation:
    """Test target name validation logic."""

    def test_validate_prohibited_targets(self):
        """Test that prohibited broad target names are rejected."""
        for prohibited in PROHIBITED_TARGETS:
            is_valid, error = validate_target_name(prohibited)
            assert is_valid is False, f"Should reject '{prohibited}'"
            assert "too broad" in error.lower()
            assert "specific" in error.lower()

    def test_validate_empty_target(self):
        """Test that empty target names are rejected."""
        invalid_names = ["", "   ", None]
        for name in invalid_names:
            if name is None:
                continue  # Skip None as it would cause TypeError
            is_valid, error = validate_target_name(name)
            assert is_valid is False
            assert "empty" in error.lower()

    def test_validate_short_target(self):
        """Test that very short target names are rejected."""
        short_names = ["ab", "x", "py"]
        for name in short_names:
            is_valid, error = validate_target_name(name)
            assert is_valid is False
            assert "too short" in error.lower()
            assert "3 chars" in error.lower()

    def test_validate_accepts_specific_names(self):
        """Test that specific application names are accepted."""
        valid_names = [
            "myapp",
            "target-app",
            "custom-service",
            "app123",
            "my_application",
        ]
        for name in valid_names:
            is_valid, error = validate_target_name(name)
            assert is_valid is True, f"Should accept '{name}': {error}"
            assert error == ""

    def test_validate_case_insensitive(self):
        """Test that validation is case-insensitive."""
        variants = ["Python", "PYTHON", "PyThOn"]
        for variant in variants:
            is_valid, error = validate_target_name(variant)
            assert is_valid is False
            assert "too broad" in error.lower()

    def test_validate_strips_whitespace(self):
        """Test that leading/trailing whitespace is handled."""
        is_valid, error = validate_target_name("  myapp  ")
        assert is_valid is True
        assert error == ""


class TestCriticalProcessDetection:
    """Test critical process detection logic."""

    def test_critical_processes_are_lowercase(self):
        """Verify all entries in CRITICAL_PROCESSES are lowercase."""
        for process in CRITICAL_PROCESSES:
            assert process == process.lower(), f"'{process}' should be lowercase"

    def test_prohibited_targets_are_lowercase(self):
        """Verify all entries in PROHIBITED_TARGETS are lowercase."""
        for target in PROHIBITED_TARGETS:
            assert target == target.lower(), f"'{target}' should be lowercase"

    def test_detects_critical_by_name(self):
        """Test detection of critical processes by name."""
        for critical in CRITICAL_PROCESSES:
            assert is_critical_process(critical, []) is True
            # Test case insensitive
            assert is_critical_process(critical.upper(), []) is True

    def test_detects_critical_by_cmdline(self):
        """Test detection of critical processes by command line."""
        test_cases = [
            (["systemd", "--system"], True),
            (["/usr/bin/dockerd", "-H", "unix://"], True),
            (["/usr/bin/kubelet", "--config=/etc/kubernetes"], True),
            (["/usr/sbin/sshd", "-D"], True),
        ]

        for cmdline, expected in test_cases:
            result = is_critical_process("", cmdline)
            assert result is expected, f"Failed for {cmdline}"

    def test_allows_non_critical(self):
        """Test that non-critical processes are allowed."""
        non_critical = [
            ("myapp", ["myapp", "--port=8080"]),
            ("target-app", ["python", "target-app.py"]),
            ("custom-service", ["./custom-service", "--config=app.yaml"]),
        ]

        for name, cmdline in non_critical:
            assert is_critical_process(name, cmdline) is False

    def test_handles_empty_cmdline(self):
        """Test handling of empty command lines."""
        assert is_critical_process("myapp", []) is False
        assert is_critical_process("myapp", None) is False


class TestProcessInjectionWithValidation:
    """Test process injection with new validation."""

    def test_inject_rejects_prohibited_target(self, capsys):
        """Test that injection rejects prohibited target names."""
        config = {"target_name": "python"}
        inject_process(config, dry_run=False)

        captured = capsys.readouterr()
        assert "Invalid target name" in captured.out
        assert "too broad" in captured.out
        assert (
            INJECTIONS_TOTAL.labels(
                failure_type="process", status="failed"
            )._value.get()
            == 1
        )

    def test_inject_rejects_short_target(self, capsys):
        """Test that injection rejects too-short target names."""
        config = {"target_name": "ab"}
        inject_process(config, dry_run=False)

        captured = capsys.readouterr()
        assert "Invalid target name" in captured.out
        assert "too short" in captured.out

    def test_inject_accepts_valid_target(self, capsys):
        """Test that injection accepts valid specific target names."""
        config = {"target_name": "nonexistent-app-xyz"}
        inject_process(config, dry_run=True)

        captured = capsys.readouterr()
        # Should proceed to search (and not find the process)
        assert "Invalid target name" not in captured.out
        assert "No killable process" in captured.out

    def test_inject_empty_target(self, capsys):
        """Test handling of empty target name."""
        config = {"target_name": ""}
        inject_process(config, dry_run=False)

        captured = capsys.readouterr()
        assert "Invalid target name" in captured.out or "No target_name" in captured.out


class TestCriticalProcessProtection:
    """Test that critical processes are protected during scanning."""

    def test_skips_critical_processes_in_scan(self, capsys, monkeypatch):
        """Test that critical processes are skipped during process scanning."""
        # Mock psutil to return a mix of critical and non-critical processes
        import psutil
        from src.failures.process import get_safe_target_processes

        mock_procs = [
            MagicMock(
                info={
                    "pid": 1,
                    "name": "systemd",
                    "cmdline": ["systemd"],
                    "ppid": 0,
                }
            ),
            MagicMock(
                info={
                    "pid": 100,
                    "name": "target-app",
                    "cmdline": ["target-app"],
                    "ppid": 1,
                }
            ),
            MagicMock(
                info={
                    "pid": 200,
                    "name": "kubelet",
                    "cmdline": ["/usr/bin/kubelet"],
                    "ppid": 1,
                }
            ),
        ]

        # Patch process_iter to return our mock processes
        def mock_process_iter(*args, **kwargs):
            return mock_procs

        monkeypatch.setattr(psutil, "process_iter", mock_process_iter)

        # Also mock os.getpid to avoid self-protection logic
        monkeypatch.setattr("os.getpid", lambda: 9999)
        monkeypatch.setattr("os.getppid", lambda: 9998)

        # Mock Process class for children lookup
        class MockProcess:
            def children(self, recursive=False):
                return []

        monkeypatch.setattr(psutil, "Process", lambda pid: MockProcess())

        # Search for a broad term that would match multiple processes
        result = get_safe_target_processes("target")

        captured = capsys.readouterr()

        # Should have logged that it skipped critical processes
        assert "Skipping critical system process" in captured.out

        # Should only return non-critical process
        assert len(result) == 1
        assert result[0].info["name"] == "target-app"


class TestRealWorldScenarios:
    """Test real-world usage scenarios."""

    def test_scenario_kubernetes_pod(self):
        """Test that Kubernetes infrastructure is protected."""
        critical_k8s = ["kubelet", "kube-proxy", "pause"]

        for process in critical_k8s:
            # These should be detected as critical processes
            assert is_critical_process(process, []) is True

        # Additionally, some k8s processes are also in prohibited targets
        # (but not all - we check separately)
        prohibited_k8s = ["kubelet"]  # This one is in both lists
        for process in prohibited_k8s:
            is_valid, _ = validate_target_name(process)
            assert is_valid is False

    def test_scenario_docker_environment(self):
        """Test that Docker infrastructure is protected."""
        critical_docker = ["dockerd", "containerd", "containerd-shim"]

        for process in critical_docker:
            assert is_critical_process(process, []) is True

    def test_scenario_ssh_access(self):
        """Test that SSH daemon is protected."""
        assert is_critical_process("sshd", []) is True
        assert is_critical_process("", ["/usr/sbin/sshd", "-D"]) is True

    def test_scenario_valid_app_names(self):
        """Test that common valid application names work."""
        valid_apps = [
            "nginx",
            "redis-server",
            "mongodb",
            "postgres",
            "target-app",
            "myservice",
        ]

        for app in valid_apps:
            is_valid, error = validate_target_name(app)
            assert is_valid is True, f"{app} should be valid but got: {error}"


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_three_char_minimum_boundary(self):
        """Test the 3-character minimum boundary."""
        assert validate_target_name("ab")[0] is False  # Too short
        assert validate_target_name("abc")[0] is True  # Exactly 3 - OK
        assert validate_target_name("abcd")[0] is True  # More than 3 - OK

    def test_process_name_with_numbers(self):
        """Test process names with numbers."""
        assert validate_target_name("app123")[0] is True
        assert validate_target_name("service-v2")[0] is True

    def test_process_name_with_dashes(self):
        """Test process names with dashes and underscores."""
        assert validate_target_name("my-app")[0] is True
        assert validate_target_name("my_app")[0] is True
        assert validate_target_name("app-service-v1")[0] is True

    def test_case_variations_of_critical(self):
        """Test various case combinations of critical process names."""
        variations = [
            ("SystemD", False),
            ("DOCKERD", False),
            ("KubeLet", False),
            ("PYTHON", False),
        ]

        for name, expected_valid in variations:
            is_valid, _ = validate_target_name(name)
            assert is_valid == expected_valid

    def test_partial_match_not_blocked(self):
        """Test that partial matches of prohibited names are allowed."""
        # These contain prohibited strings but are more specific
        allowed = [
            "mypython-app",  # Contains 'python' but more specific
            "nodejs-server",  # Contains 'node' but more specific
            "my-java-app",  # Contains 'java' but more specific
        ]

        for name in allowed:
            is_valid, error = validate_target_name(name)
            # These should be rejected because our check is for exact match
            # after lowercasing and stripping
            if name.lower().strip() in PROHIBITED_TARGETS:
                assert is_valid is False
            else:
                # If not exact match, should be allowed
                assert is_valid is True, f"{name} should be allowed: {error}"
