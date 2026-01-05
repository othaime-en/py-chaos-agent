"""Tests for failure injection modules."""
import pytest
import time
from unittest.mock import patch, MagicMock
from src.failures.cpu import inject_cpu
from src.failures.memory import inject_memory
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

class TestMemoryFailures:
    """Test memory failure injection."""

    def test_inject_memory_dry_run(self, capsys):
        """Test memory injection in dry run mode."""
        config = {'duration_seconds': 1, 'mb': 50}
        inject_memory(config, dry_run=True)
        captured = capsys.readouterr()
        assert "DRY RUN] Would allocate 50 MB" in captured.out
        assert INJECTIONS_TOTAL.labels(failure_type='memory', status='skipped')._value.get() == 1

    @pytest.mark.parametrize("mb", [10, 50, 100])
    def test_inject_memory_various_sizes(self, mb, capsys):
        """Test memory injection with different sizes in dry run."""
        config = {'duration_seconds': 1, 'mb': mb}
        inject_memory(config, dry_run=True)
        captured = capsys.readouterr()
        assert f"Would allocate {mb} MB" in captured.out

    def test_inject_memory_actual_small(self, capsys):
        """Test actual memory injection with small allocation."""
        config = {'duration_seconds': 1, 'mb': 10}
        inject_memory(config, dry_run=False)
        time.sleep(1.5)  # Wait for thread to complete
        
        captured = capsys.readouterr()
        assert "[MEMORY] Starting memory injection" in captured.out
        # Check that it eventually completes
        assert INJECTIONS_TOTAL.labels(failure_type='memory', status='success')._value.get() == 1

    def test_inject_memory_default_value(self, capsys):
        """Test memory injection with default MB value."""
        config = {'duration_seconds': 1}
        inject_memory(config, dry_run=True)
        captured = capsys.readouterr()
        assert "100 MB" in captured.out  # Default is 100

    def test_inject_memory_threaded_behavior(self):
        """Test that memory injection doesn't block the main thread."""
        config = {'duration_seconds': 2, 'mb': 10}
        
        start = time.time()
        inject_memory(config, dry_run=False)
        elapsed = time.time() - start
        
        # Should return immediately (not block for 2 seconds)
        assert elapsed < 0.5

    def test_memory_zero_size(self, capsys):
        """Test memory injection with zero MB."""
        config = {'duration_seconds': 1, 'mb': 0}
        inject_memory(config, dry_run=True)
        captured = capsys.readouterr()
        assert "0 MB" in captured.out
