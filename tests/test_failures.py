"""Tests for failure injection modules."""
import pytest
import time
from unittest.mock import patch, MagicMock
from src.failures.cpu import inject_cpu
from src.metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE

class TestCPUFailures:
    """Test CPU failure injection."""

    @pytest.mark.parametrize("cores", [1, 2, 4])
    def test_inject_cpu_dry_run(self, cores, capsys):
        """Test CPU injection in dry run mode."""
        config = {
            'duration_seconds': 1,
            'cores': cores,
        }
        inject_cpu(config, dry_run=True)
        captured = capsys.readouterr()
        assert f"DRY RUN] Would hog {cores} CPU" in captured.out
        assert INJECTIONS_TOTAL.labels(failure_type='cpu', status='skipped')._value.get() == 1

    def test_inject_cpu_actual(self, capsys):
        """Test actual CPU injection (brief to avoid slowdown)."""
        config = {
            'duration_seconds': 1,
            'cores': 1,
        }
        start_time = time.time()
        inject_cpu(config, dry_run=False)
        duration = time.time() - start_time
        
        captured = capsys.readouterr()
        assert "[CPU] Hogging" in captured.out
        assert duration >= 1  # Should take at least 1 second
        assert INJECTIONS_TOTAL.labels(failure_type='cpu', status='success')._value.get() == 1

    def test_inject_cpu_default_cores(self, capsys):
        """Test CPU injection with default cores value."""
        config = {'duration_seconds': 1}
        inject_cpu(config, dry_run=True)
        captured = capsys.readouterr()
        assert "1 CPU core(s)" in captured.out

    def test_inject_cpu_metrics(self):
        """Test that CPU injection updates metrics correctly."""
        config = {'duration_seconds': 1, 'cores': 1}
        
        inject_cpu(config, dry_run=False)
        
        # After completion, active should be back to 0
        final_active = INJECTION_ACTIVE.labels(failure_type='cpu')._value.get()
        assert final_active == 0
        
        # Success counter should have incremented
        assert INJECTIONS_TOTAL.labels(failure_type='cpu', status='success')._value.get() == 1

    def test_cpu_zero_duration(self, capsys):
        """Test CPU injection with zero duration."""
        config = {'duration_seconds': 0, 'cores': 1}
        inject_cpu(config, dry_run=False)
        # Should complete immediately without error
        captured = capsys.readouterr()
        assert "[CPU] Hogging" in captured.out
