"""Shared fixtures and configuration for all tests."""

import pytest
import logging
from src.metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE


@pytest.fixture(autouse=True)
def reset_metrics():
    """Reset Prometheus metrics before each test."""
    INJECTIONS_TOTAL._metrics.clear()
    INJECTION_ACTIVE._metrics.clear()
    yield


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
