"""
Basic tests for Py-Chaos-Agent API.

Run with: pytest tests/test_api.py
"""

import pytest
from fastapi.testclient import TestClient
from src.api import app, agent_state
from src.config import load_config

TEST_TOKEN = "t" * 40


@pytest.fixture(autouse=True)
def api_token(monkeypatch):
    """Configure a valid API token for every API test."""
    monkeypatch.setenv("CHAOS_API_TOKEN", TEST_TOKEN)
    monkeypatch.delenv("CHAOS_API_TOKEN_FILE", raising=False)
    monkeypatch.delenv("CHAOS_API_AUTH_DISABLED", raising=False)


@pytest.fixture
def client():
    """Create an authenticated test client."""
    return TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"})


def _stop_agent_and_wait():
    """
    Stop any agent loop and wait until its thread has really exited.

    Waiting matters: if a loop is still inside an injection when the next test
    calls stop_event.clear(), the old thread resumes and keeps injecting into
    every later test, bumping their metrics and reservations.
    """
    agent_state.stop_event.set()
    if agent_state.kill_switch:
        agent_state.kill_switch.stop_monitoring()
        agent_state.kill_switch = None
    thread = agent_state.agent_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=30)
        if thread.is_alive():
            pytest.fail(
                "agent loop thread did not stop; it would leak into later tests"
            )
    agent_state.enabled = False
    agent_state.agent_thread = None


@pytest.fixture(autouse=True)
def reset_agent_state(monkeypatch):
    """
    Reset agent state around every test.

    Injectors are replaced with no-ops so a loop started through the API cannot
    burn CPU, allocate memory or touch the network while tests run. Tests that
    check injection behavior patch the injectors themselves.
    """
    for name in ("inject_cpu", "inject_memory", "inject_process", "inject_network"):
        monkeypatch.setattr(f"src.api.{name}", lambda *a, **k: None)

    _stop_agent_and_wait()
    agent_state.stop_event.clear()
    try:
        agent_state.config = load_config()
    except Exception:
        pass
    yield
    _stop_agent_and_wait()


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


class TestInputValidation:
    """The API rejects out-of-bounds or malformed input and never half-applies it."""

    def _failures(self):
        return {k: dict(v) for k, v in agent_state.config.failures.items()}

    # --- /inject/manual ---------------------------------------------------

    def test_manual_injection_rejects_unbounded_cores(self, client, monkeypatch):
        called = []
        monkeypatch.setattr("src.api.inject_cpu", lambda *a, **k: called.append(a))
        response = client.post(
            "/inject/manual",
            json={"failure_type": "cpu", "config": {"cores": 100000}},
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["cpu", "cores"]
        assert called == []

    @pytest.mark.parametrize(
        "failure,override",
        [
            ("cpu", {"duration_seconds": 10**9}),
            ("memory", {"mb": 10**6}),
            ("network", {"delay_ms": 10**6}),
            ("network", {"interface": "eth0; rm -rf /"}),
            ("process", {"target_name": "python"}),
        ],
    )
    def test_manual_injection_rejects_dangerous_overrides(
        self, client, failure, override
    ):
        response = client.post(
            "/inject/manual", json={"failure_type": failure, "config": override}
        )
        assert response.status_code == 422

    def test_manual_injection_merges_partial_override(self, client, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.api.inject_cpu", lambda cfg, dry_run=False: seen.append(cfg)
        )
        response = client.post(
            "/inject/manual",
            json={
                "failure_type": "cpu",
                "dry_run": True,
                "config": {"duration_seconds": 3},
            },
        )
        assert response.status_code == 200
        assert seen[0]["duration_seconds"] == 3
        # Untouched keys come from the stored config, not KeyError
        assert "cores" in seen[0]

    def test_manual_injection_rejects_unknown_field(self, client):
        response = client.post(
            "/inject/manual", json={"failure_type": "cpu", "bogus": 1}
        )
        assert response.status_code == 422

    # --- PATCH /config ----------------------------------------------------

    @pytest.mark.parametrize("value", [0, -1, 3601, "10", 1.5])
    def test_patch_interval_bounds(self, client, value):
        before = agent_state.config.agent.interval_seconds
        response = client.patch("/config", json={"interval_seconds": value})
        assert response.status_code == 422
        assert agent_state.config.agent.interval_seconds == before

    @pytest.mark.parametrize(
        "update",
        [
            {"probability": 5},
            {"probability": -0.1},
            {"cores": 0},
            {"cores": 1000},
            {"duration_seconds": 0},
            {"duration_seconds": 100000},
            {"unknown_key": 1},
            {"enabled": "yes"},
        ],
    )
    def test_patch_failure_rejects_invalid(self, client, update):
        before = self._failures()
        response = client.patch("/config", json={"failures": {"cpu": update}})
        assert response.status_code == 422
        assert self._failures() == before

    def test_patch_is_atomic(self, client):
        """A valid change bundled with an invalid one applies neither."""
        before_interval = agent_state.config.agent.interval_seconds
        before = self._failures()
        response = client.patch(
            "/config",
            json={
                "interval_seconds": before_interval + 1,
                "failures": {
                    "memory": {"mb": 64},
                    "cpu": {"cores": 99999},
                },
            },
        )
        assert response.status_code == 422
        assert agent_state.config.agent.interval_seconds == before_interval
        assert self._failures() == before

    def test_patch_reports_every_error(self, client):
        response = client.patch(
            "/config",
            json={"failures": {"cpu": {"cores": 0}, "memory": {"mb": 0}}},
        )
        locs = {tuple(e["loc"]) for e in response.json()["detail"]}
        assert ("cpu", "cores") in locs
        assert ("memory", "mb") in locs

    def test_patch_unknown_failure_type_rejected(self, client):
        response = client.patch("/config", json={"failures": {"disk": {"x": 1}}})
        assert response.status_code == 422

    def test_patch_unconfigured_failure_type_is_404(self, client):
        agent_state.config.failures.pop("memory")
        response = client.patch("/config", json={"failures": {"memory": {"mb": 5}}})
        assert response.status_code == 404

    def test_patch_unknown_top_level_field_rejected(self, client):
        assert client.patch("/config", json={"nope": 1}).status_code == 422

    def test_patch_valid_update_applies(self, client):
        response = client.patch(
            "/config",
            json={
                "interval_seconds": 7,
                "dry_run": True,
                "failures": {"cpu": {"cores": 3, "probability": 0.9}},
            },
        )
        assert response.status_code == 200
        assert agent_state.config.agent.interval_seconds == 7
        assert agent_state.config.agent.dry_run is True
        assert agent_state.config.failures["cpu"]["cores"] == 3
        assert agent_state.config.failures["cpu"]["probability"] == 0.9
        # Untouched keys survive the merge
        assert "duration_seconds" in agent_state.config.failures["cpu"]

    def test_patch_cannot_enable_process_without_target(self, client):
        agent_state.config.failures["process"]["target_name"] = None
        agent_state.config.failures["process"]["enabled"] = False
        response = client.patch(
            "/config", json={"failures": {"process": {"enabled": True}}}
        )
        assert response.status_code == 422
        assert agent_state.config.failures["process"]["enabled"] is False

    def test_patch_cannot_target_broad_process(self, client):
        response = client.patch(
            "/config/failures/process", json={"target_name": "systemd"}
        )
        assert response.status_code == 422

    # --- PATCH /config/failures/{type} -----------------------------------

    def test_patch_single_failure_rejects_invalid(self, client):
        before = self._failures()
        response = client.patch("/config/failures/memory", json={"mb": 10**7})
        assert response.status_code == 422
        assert self._failures() == before

    def test_patch_single_failure_applies_valid(self, client):
        response = client.patch("/config/failures/memory", json={"mb": 256})
        assert response.status_code == 200
        assert agent_state.config.failures["memory"]["mb"] == 256

    def test_patch_single_failure_rejects_unknown_key(self, client):
        response = client.patch("/config/failures/memory", json={"mbb": 256})
        assert response.status_code == 422

    # --- reload -----------------------------------------------------------

    def test_reload_rejects_invalid_file_and_keeps_old_config(
        self, client, monkeypatch
    ):
        from src.schemas import ConfigValidationError

        def boom(*a, **k):
            raise ConfigValidationError(
                [{"loc": ["failures", "cpu", "cores"], "msg": "too big"}]
            )

        monkeypatch.setattr("src.api.load_config", boom)
        previous = agent_state.config
        response = client.post("/config/reload")
        assert response.status_code == 422
        assert agent_state.config is previous


class TestResourceBudgetApi:
    """/limits, manual-injection pre-flight, and operator-only safety settings."""

    @pytest.fixture(autouse=True)
    def container(self, monkeypatch):
        from src.resources import MIB, CgroupInfo, governor

        info = CgroupInfo(2, 4.0, 1000 * MIB, 0)  # cpu budget 3, memory budget 500
        monkeypatch.setattr(governor, "_cgroup_reader", lambda: info)
        monkeypatch.setattr(governor, "_cpu_reader", lambda: 8)
        monkeypatch.setattr(governor, "_memory_reader", lambda: 64 * 1024 * MIB)

    def test_limits_requires_auth(self):
        assert TestClient(app).get("/limits").status_code == 401

    def test_limits_reports_budget(self, client):
        body = client.get("/limits").json()
        assert body["budget"]["max_cores"] == 3
        assert body["budget"]["max_memory_mb"] == 500
        assert body["cgroup"]["cpu_limit_cores"] == 4.0
        assert body["cgroup"]["memory_limit_mb"] == 1000
        assert body["reserved"] == {"cores": 0, "memory_mb": 0}
        assert body["safety"]["require_cgroup_limits"] is False

    def test_manual_memory_over_budget_is_422(self, client, monkeypatch):
        called = []
        monkeypatch.setattr("src.api.inject_memory", lambda *a, **k: called.append(1))
        response = client.post(
            "/inject/manual",
            json={"failure_type": "memory", "config": {"mb": 900}},
        )
        assert response.status_code == 422
        detail = response.json()["detail"][0]
        assert detail["loc"] == ["memory", "mb"]
        assert "900 MB requested" in detail["msg"]
        assert called == []

    def test_manual_memory_within_budget_is_accepted(self, client, monkeypatch):
        monkeypatch.setattr("src.api.inject_memory", lambda *a, **k: None)
        response = client.post(
            "/inject/manual",
            json={"failure_type": "memory", "config": {"mb": 100}},
        )
        assert response.status_code == 200

    def test_manual_cpu_over_budget_is_clamped_and_reported(self, client, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.api.inject_cpu", lambda cfg, dry_run=False: seen.append(cfg)
        )
        response = client.post(
            "/inject/manual",
            json={"failure_type": "cpu", "config": {"cores": 12}},
        )
        assert response.status_code == 200
        assert response.json()["clamped_cores"] == 3

    def test_manual_cpu_within_budget_has_no_clamp_field(self, client, monkeypatch):
        monkeypatch.setattr("src.api.inject_cpu", lambda *a, **k: None)
        response = client.post(
            "/inject/manual",
            json={"failure_type": "cpu", "config": {"cores": 2}},
        )
        assert "clamped_cores" not in response.json()

    def test_manual_cpu_refused_when_budget_exhausted(self, client):
        from src.resources import governor

        governor.acquire_cores(3)
        response = client.post(
            "/inject/manual", json={"failure_type": "cpu", "config": {"cores": 1}}
        )
        assert response.status_code == 422
        assert "exhausted" in response.json()["detail"][0]["msg"]

    @pytest.mark.parametrize("failure", ["process", "network"])
    def test_other_failure_types_skip_the_resource_check(
        self, client, failure, monkeypatch
    ):
        monkeypatch.setattr("src.api.inject_process", lambda *a, **k: None)
        monkeypatch.setattr("src.api.inject_network", lambda *a, **k: None)
        response = client.post(
            "/inject/manual", json={"failure_type": failure, "dry_run": True}
        )
        assert response.status_code == 200

    def test_safety_cannot_be_changed_through_the_api(self, client):
        response = client.patch(
            "/config", json={"safety": {"require_cgroup_limits": False}}
        )
        assert response.status_code == 422

    def test_reload_applies_safety_settings_from_file(self, client, monkeypatch):
        from src.config import Config
        from src.resources import governor

        reloaded = Config(
            {
                "agent": {},
                "failures": {},
                "safety": {
                    "cpu_fraction": 0.25,
                    "memory_fraction": 0.1,
                    "require_cgroup_limits": True,
                },
            }
        )
        monkeypatch.setattr("src.api.load_config", lambda: reloaded)
        assert client.post("/config/reload").status_code == 200
        assert governor.settings.cpu_fraction == 0.25
        assert governor.settings.require_cgroup_limits is True


class TestInjectionLifecycleApi:
    """Overlap refusal, abort, stop, kill switch, and shutdown behavior."""

    def test_abort_requires_auth(self):
        assert TestClient(app).post("/inject/abort").status_code == 401

    def test_abort_reports_how_many_were_running(self, client):
        from src.lifecycle import lifecycle

        ticket, _ = lifecycle.begin("network")
        lifecycle.begin("cpu")
        body = client.post("/inject/abort").json()
        assert body == {"status": "aborted", "aborted": 2}
        assert ticket.interrupted

    def test_abort_with_nothing_running(self, client):
        assert client.post("/inject/abort").json()["aborted"] == 0

    def test_status_lists_active_injections(self, client):
        from src.lifecycle import lifecycle

        assert client.get("/status").json()["active_injections"] == []
        lifecycle.begin("network")
        assert client.get("/status").json()["active_injections"] == ["network"]

    def test_manual_injection_is_409_while_same_type_runs(self, client, monkeypatch):
        from src.lifecycle import lifecycle

        called = []
        monkeypatch.setattr("src.api.inject_network", lambda *a, **k: called.append(1))
        lifecycle.begin("network")
        response = client.post("/inject/manual", json={"failure_type": "network"})
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]
        assert called == []

    def test_other_types_are_not_blocked(self, client, monkeypatch):
        from src.lifecycle import lifecycle

        monkeypatch.setattr("src.api.inject_cpu", lambda *a, **k: None)
        lifecycle.begin("network")
        response = client.post("/inject/manual", json={"failure_type": "cpu"})
        assert response.status_code == 200

    def test_dry_run_is_not_blocked(self, client, monkeypatch):
        from src.lifecycle import lifecycle

        monkeypatch.setattr("src.api.inject_network", lambda *a, **k: None)
        lifecycle.begin("network")
        response = client.post(
            "/inject/manual", json={"failure_type": "network", "dry_run": True}
        )
        assert response.status_code == 200

    def test_manual_injection_is_503_while_shutting_down(self, client):
        from src.lifecycle import lifecycle

        lifecycle.shutdown("test")
        response = client.post("/inject/manual", json={"failure_type": "cpu"})
        assert response.status_code == 503

    def test_stop_aborts_in_flight_injections(self, client):
        from src.lifecycle import lifecycle

        assert client.post("/agent/start?enable_kill_switch=false").status_code == 200
        ticket, _ = lifecycle.begin("network")
        assert client.post("/agent/stop").status_code == 200
        assert ticket.interrupted

    def test_kill_switch_trip_aborts_in_flight_injections(self, client, monkeypatch):
        from src.lifecycle import lifecycle

        captured = {}

        def fake_start(self, callback):
            captured["callback"] = callback

        monkeypatch.setattr("src.api.KillSwitch.start_monitoring", fake_start)
        assert client.post("/agent/start").status_code == 200
        ticket, _ = lifecycle.begin("cpu")
        captured["callback"]()
        assert ticket.interrupted
        assert agent_state.stop_event.is_set()

    def test_lifespan_shutdown_aborts_and_cleans_up(self, monkeypatch):
        from src.lifecycle import lifecycle

        ran = []
        monkeypatch.setattr(lifecycle, "run_cleanups", lambda: ran.append(1))
        ticket, _ = lifecycle.begin("network")
        with TestClient(app):
            pass  # enter and leave: runs startup and shutdown
        assert lifecycle.is_shutting_down()
        assert ticket.interrupted
        assert ran, "shutdown did not run the registered cleanups"

    def test_startup_repairs_stale_rules_on_the_configured_interface(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.api.reconcile_stale_rules", lambda ifaces: seen.append(list(ifaces))
        )
        with TestClient(app):
            pass
        assert seen and seen[0] == [
            agent_state.config.failures["network"].get("interface", "eth0")
        ]
