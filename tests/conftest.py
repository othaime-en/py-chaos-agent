"""Shared fixtures and configuration for all tests."""

import pytest
import logging
from src.metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE


@pytest.fixture(autouse=True)
def reset_metrics():
    """
    Reset Prometheus metrics before each test.
    
    CRITICAL: We must explicitly set counters to zero, not just clear the dict.
    Clearing only removes the internal storage, but Prometheus counters are
    lazy-initialized and may retain state across clears in some scenarios.
    """
    # Clear the internal metrics dictionaries
    INJECTIONS_TOTAL._metrics.clear()
    INJECTION_ACTIVE._metrics.clear()
    
    # Explicitly initialize all label combinations to zero
    # This ensures a clean slate for every test
    failure_types = ["cpu", "memory", "process", "network"]
    statuses = ["success", "failed", "skipped"]
    
    for failure_type in failure_types:
        # Initialize counter labels
        for status in statuses:
            INJECTIONS_TOTAL.labels(failure_type=failure_type, status=status).inc(0)
        
        # Initialize gauge labels
        INJECTION_ACTIVE.labels(failure_type=failure_type).set(0)
    
    yield
    
    # Optional: Clean up after test (helps with test isolation)
    INJECTIONS_TOTAL._metrics.clear()
    INJECTION_ACTIVE._metrics.clear()


@pytest.fixture
def caplog_setup(caplog):
    """Configure logging capture for tests."""
    caplog.set_level(logging.INFO)
    return caplog


@pytest.fixture(autouse=True)
def setup_logging():
    """Setup basic logging for all tests."""
    # Configure root logger to output to console for test capture
    logging.basicConfig(
        level=logging.DEBUG, format="%(levelname)s - %(message)s", force=True
    )
    yield
    # Clean up handlers after test
    logging.getLogger().handlers.clear()