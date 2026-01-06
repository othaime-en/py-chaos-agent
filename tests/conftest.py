"""Shared fixtures and configuration for all tests."""

import pytest
from src.metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE


@pytest.fixture(autouse=True)
def reset_metrics():
    """Reset Prometheus metrics before each test."""
    INJECTIONS_TOTAL._metrics.clear()
    INJECTION_ACTIVE._metrics.clear()
    yield
