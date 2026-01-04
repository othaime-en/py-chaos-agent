import pytest
from src.failures.cpu import inject_cpu
from src.failures.memory import inject_memory
from src.metrics import INJECTIONS_TOTAL

@pytest.fixture(autouse=True)
def reset_metrics():
    """Reset Prometheus metrics before each test."""
    # Clear all metrics by resetting the registry
    INJECTIONS_TOTAL._metrics.clear()
    yield

@pytest.mark.parametrize("cores", [1, 2])
def test_inject_cpu_dry_run(cores, capsys):
    config = {
        'duration_seconds': 1,
        'cores': cores,
    }
    inject_cpu(config, dry_run=True)
    captured = capsys.readouterr()
    assert f"DRY RUN] Would hog {cores} CPU" in captured.out
    assert INJECTIONS_TOTAL.labels(failure_type='cpu', status='skipped')._value.get() == 1

def test_inject_memory_dry_run(capsys):
    config = {'duration_seconds': 1, 'mb': 50}
    inject_memory(config, dry_run=True)
    captured = capsys.readouterr()
    assert "DRY RUN] Would allocate 50 MB" in captured.out