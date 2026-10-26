from src.metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE, metric_value
from src.failures.cpu import inject_cpu


class TestMetrics:
    """Test metrics functionality."""

    def test_metrics_isolation(self):
        """Test that metrics are properly isolated between tests."""
        # This test verifies the reset_metrics fixture works
        assert metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="skipped") == 0
        assert metric_value(INJECTION_ACTIVE, failure_type="cpu") == 0

    def test_injection_active_gauge(self):
        """Test that INJECTION_ACTIVE gauge is set and reset correctly."""
        config = {"duration_seconds": 1, "cores": 1}

        inject_cpu(config, dry_run=False)

        # After completion, gauge should be reset to 0
        assert metric_value(INJECTION_ACTIVE, failure_type="cpu") == 0

    def test_counter_increments(self):
        """Test that counters increment correctly."""
        config = {"duration_seconds": 1, "cores": 1}

        initial = metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="success")

        inject_cpu(config, dry_run=False)

        final = metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="success")
        assert final == initial + 1

    def test_dry_run_metrics(self):
        """Test that dry run mode updates skipped metrics."""
        config = {"duration_seconds": 1, "cores": 1}

        inject_cpu(config, dry_run=True)

        skipped = metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="skipped")
        assert skipped == 1
