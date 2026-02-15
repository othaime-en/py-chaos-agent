"""
Basic tests for Py-Chaos-Agent API.

Run with: pytest tests/test_api.py
"""

import pytest
from fastapi.testclient import TestClient
from src.api import app, agent_state
from src.config import load_config


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_agent_state():
    """Reset agent state before each test."""
    agent_state["enabled"] = False
    agent_state["agent_thread"] = None
    agent_state["stop_event"].clear()
    try:
        agent_state["config"] = load_config()
    except:
        pass
    yield
    # Cleanup after test
    if agent_state["enabled"]:
        agent_state["stop_event"].set()
        if agent_state["agent_thread"]:
            agent_state["agent_thread"].join(timeout=2)
        agent_state["enabled"] = False


class TestGeneralEndpoints:
    """Test general API endpoints."""

    def test_root(self, client):
        """Test root endpoint."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "py-chaos-agent"
        assert "version" in data
        assert "status" in data

    def test_health(self, client):
        """Test health endpoint."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "config_loaded" in data
        assert "agent_enabled" in data


class TestAgentControl:
    """Test agent control endpoints."""

    def test_get_status(self, client):
        """Test status endpoint."""
        response = client.get("/status")
        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        assert "config_loaded" in data
        assert "dry_run" in data
        assert "interval_seconds" in data
        assert "enabled_failures" in data

    def test_start_agent(self, client):
        """Test starting agent."""
        response = client.post("/agent/start")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "started"

        # Verify agent is running
        status_response = client.get("/status")
        status_data = status_response.json()
        assert status_data["enabled"] is True

    def test_start_already_running(self, client):
        """Test starting agent when already running."""
        client.post("/agent/start")
        response = client.post("/agent/start")
        assert response.status_code == 400

    def test_stop_agent(self, client):
        """Test stopping agent."""
        client.post("/agent/start")
        response = client.post("/agent/stop")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "stopped"

        # Verify agent is stopped
        status_response = client.get("/status")
        status_data = status_response.json()
        assert status_data["enabled"] is False

    def test_stop_not_running(self, client):
        """Test stopping agent when not running."""
        response = client.post("/agent/stop")
        assert response.status_code == 400


class TestManualInjections:
    """Test manual injection endpoints."""

    def test_inject_cpu_dry_run(self, client):
        """Test manual CPU injection in dry run."""
        response = client.post(
            "/inject/manual",
            json={"failure_type": "cpu", "dry_run": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "injecting"
        assert data["failure_type"] == "cpu"
        assert data["dry_run"] is True

    def test_inject_memory_with_config(self, client):
        """Test memory injection with custom config."""
        response = client.post(
            "/inject/manual",
            json={
                "failure_type": "memory",
                "dry_run": True,
                "config": {"mb": 50, "duration_seconds": 5},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["failure_type"] == "memory"

    def test_inject_invalid_type(self, client):
        """Test injection with invalid failure type."""
        response = client.post(
            "/inject/manual",
            json={"failure_type": "invalid", "dry_run": True},
        )
        assert response.status_code == 422  # Validation error


class TestConfiguration:
    """Test configuration endpoints."""

    def test_get_config(self, client):
        """Test getting configuration."""
        response = client.get("/config")
        assert response.status_code == 200
        data = response.json()
        assert "agent" in data
        assert "failures" in data
        assert "interval_seconds" in data["agent"]
        assert "dry_run" in data["agent"]

    def test_get_failure_config(self, client):
        """Test getting specific failure config."""
        response = client.get("/config/failures/cpu")
        assert response.status_code == 200
        data = response.json()
        assert data["failure_type"] == "cpu"
        assert "enabled" in data
        assert "probability" in data
        assert "config" in data

    def test_update_config(self, client):
        """Test updating configuration."""
        response = client.patch(
            "/config",
            json={"interval_seconds": 15, "dry_run": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "updated"
        assert "changes" in data

        # Verify changes applied
        config_response = client.get("/config")
        config_data = config_response.json()
        assert config_data["agent"]["interval_seconds"] == 15
        assert config_data["agent"]["dry_run"] is True

    def test_update_failure_config(self, client):
        """Test updating specific failure config."""
        response = client.patch(
            "/config/failures/cpu",
            json={"probability": 0.8, "cores": 4},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "updated"
        assert data["failure_type"] == "cpu"

        # Verify changes
        config_response = client.get("/config/failures/cpu")
        config_data = config_response.json()
        assert config_data["config"]["probability"] == 0.8
        assert config_data["config"]["cores"] == 4


class TestMetrics:
    """Test metrics endpoints."""

    def test_get_metrics_summary(self, client):
        """Test getting metrics summary."""
        response = client.get("/metrics/summary")
        assert response.status_code == 200
        data = response.json()
        assert "timestamp" in data
        assert "metrics" in data
        assert "cpu" in data["metrics"]
        assert "memory" in data["metrics"]
        assert "process" in data["metrics"]
        assert "network" in data["metrics"]

        # Check metric structure
        cpu_metrics = data["metrics"]["cpu"]
        assert "success" in cpu_metrics
        assert "failed" in cpu_metrics
        assert "skipped" in cpu_metrics
        assert "active" in cpu_metrics
        assert "total" in cpu_metrics

    def test_reset_metrics(self, client):
        """Test resetting metrics."""
        response = client.post("/metrics/reset")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "reset"


class TestIntegration:
    """Integration tests combining multiple operations."""

    def test_full_workflow(self, client):
        """Test complete workflow: configure -> start -> inject -> stop."""
        # 1. Configure
        config_response = client.patch(
            "/config", json={"dry_run": True, "interval_seconds": 5}
        )
        assert config_response.status_code == 200

        # 2. Start agent
        start_response = client.post("/agent/start")
        assert start_response.status_code == 200

        # 3. Manual injection
        inject_response = client.post(
            "/inject/manual",
            json={"failure_type": "cpu", "dry_run": True},
        )
        assert inject_response.status_code == 200

        # 4. Check status
        status_response = client.get("/status")
        assert status_response.json()["enabled"] is True

        # 5. Stop agent
        stop_response = client.post("/agent/stop")
        assert stop_response.status_code == 200

        # 6. Verify stopped
        final_status = client.get("/status")
        assert final_status.json()["enabled"] is False