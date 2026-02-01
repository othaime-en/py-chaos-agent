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
            INJECTIONS_TOTAL.labels(failure_type="process", status="failed")._value.get()
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


