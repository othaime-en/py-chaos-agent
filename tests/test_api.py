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
    starts the engine again, the old thread resumes and keeps injecting into
    every later test, bumping their metrics and reservations.
    """
    if agent_state.kill_switch:
        agent_state.kill_switch.stop_monitoring()
        agent_state.kill_switch = None
    if not agent_state.engine.stop(timeout=30):
        pytest.fail("agent loop did not stop; it would leak into later tests")
    agent_state.enabled = False


@pytest.fixture(autouse=True)
def reset_agent_state(monkeypatch):
    """
    Reset agent state around every test.

    Injectors are replaced with no-ops so a loop started through the API cannot
    burn CPU, allocate memory or touch the network while tests run. Tests that
    check injection behavior patch the injectors themselves.
    """
    for name in ("cpu", "memory", "process", "network"):
        monkeypatch.setattr(f"src.failures.{name}.inject_{name}", lambda *a, **k: None)

    _stop_agent_and_wait()
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
        monkeypatch.setattr(
            "src.failures.cpu.inject_cpu", lambda *a, **k: called.append(a)
        )
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
            "src.failures.cpu.inject_cpu", lambda cfg, dry_run=False: seen.append(cfg)
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
        monkeypatch.setattr(
            "src.failures.memory.inject_memory", lambda *a, **k: called.append(1)
        )
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
        monkeypatch.setattr("src.failures.memory.inject_memory", lambda *a, **k: None)
        response = client.post(
            "/inject/manual",
            json={"failure_type": "memory", "config": {"mb": 100}},
        )
        assert response.status_code == 200

    def test_manual_cpu_over_budget_is_clamped_and_reported(self, client, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.failures.cpu.inject_cpu", lambda cfg, dry_run=False: seen.append(cfg)
        )
        response = client.post(
            "/inject/manual",
            json={"failure_type": "cpu", "config": {"cores": 12}},
        )
        assert response.status_code == 200
        assert response.json()["clamped_cores"] == 3

    def test_manual_cpu_within_budget_has_no_clamp_field(self, client, monkeypatch):
        monkeypatch.setattr("src.failures.cpu.inject_cpu", lambda *a, **k: None)
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
        monkeypatch.setattr("src.failures.process.inject_process", lambda *a, **k: None)
        monkeypatch.setattr("src.failures.network.inject_network", lambda *a, **k: None)
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
        monkeypatch.setattr(
            "src.failures.network.inject_network", lambda *a, **k: called.append(1)
        )
        lifecycle.begin("network")
        response = client.post("/inject/manual", json={"failure_type": "network"})
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]
        assert called == []

    def test_other_types_are_not_blocked(self, client, monkeypatch):
        from src.lifecycle import lifecycle

        monkeypatch.setattr("src.failures.cpu.inject_cpu", lambda *a, **k: None)
        lifecycle.begin("network")
        response = client.post("/inject/manual", json={"failure_type": "cpu"})
        assert response.status_code == 200

    def test_dry_run_is_not_blocked(self, client, monkeypatch):
        from src.lifecycle import lifecycle

        monkeypatch.setattr("src.failures.network.inject_network", lambda *a, **k: None)
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

        assert client.post("/agent/start").status_code == 200
        ticket, _ = lifecycle.begin("network")
        assert client.post("/agent/stop").status_code == 200
        assert ticket.interrupted

    def test_kill_switch_trip_aborts_in_flight_injections(self, client):
        from src.lifecycle import lifecycle

        assert client.post("/agent/start").status_code == 200
        ks = agent_state.kill_switch
        ks.config.grace_after_agent_start_seconds = 0
        ks._health_check = lambda: False
        ticket, _ = lifecycle.begin("cpu")
        for _ in range(ks.config.consecutive_failures_threshold):
            ks._tick()
        assert ticket.interrupted
        # Chaos is paused, not stopped: the loop is still running
        assert agent_state.enabled is True
        assert agent_state.engine.is_running

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


class Target:
    """Controllable health for the kill switch under test."""

    def __init__(self, healthy=True):
        self.healthy = healthy

    def __call__(self):
        return self.healthy


def make_kill_switch(start_monitor=False, **overrides):
    """
    Create the process-wide kill switch from the loaded config, with a fake
    health check and no grace period. Tests then drive the state machine with
    _tick().

    The monitor thread is NOT started unless asked for: it ticks once the moment
    it starts, which would add a stray health check to a test's own ticks.
    """
    from src.api import apply_kill_switch_config
    from src.kill_switch import KillSwitch

    ks_cfg = agent_state.config.kill_switch
    ks_cfg.health_check_interval_seconds = 3600
    ks_cfg.grace_after_agent_start_seconds = 0
    ks_cfg.recovery_test_interval_seconds = 3600
    ks_cfg.recovery_success_threshold = 1
    for key, value in overrides.items():
        setattr(ks_cfg, key, value)

    target = Target()
    if start_monitor:
        apply_kill_switch_config(agent_state.config)
        ks = agent_state.kill_switch
        ks._health_check = target
        ks._is_armed = lambda: True
    else:
        ks = KillSwitch(ks_cfg, is_armed=lambda: True, health_check=target)
        agent_state.kill_switch = ks
    return ks, target


def trip_it(ks, target):
    target.healthy = False
    for _ in range(ks.config.consecutive_failures_threshold):
        ks._tick()
    assert ks.circuit_state.value == "open"


class TestKillSwitchApi:
    def test_created_at_startup_and_stopped_at_shutdown(self):
        with TestClient(app):
            assert agent_state.kill_switch is not None
            assert agent_state.kill_switch.get_status()["monitoring_active"]
        assert agent_state.kill_switch is None

    def test_not_created_when_disabled_in_config(self, client):
        from src.api import apply_kill_switch_config

        agent_state.config.kill_switch.enabled = False
        apply_kill_switch_config(agent_state.config)
        assert agent_state.kill_switch is None
        body = client.get("/kill-switch/status").json()
        assert body["enabled"] is False

    def test_status_endpoint_reports_state(self, client):
        ks, target = make_kill_switch()
        body = client.get("/kill-switch/status").json()
        assert body["circuit_state"] == "closed"
        assert body["allows_injection"] is True
        assert body["bypassed"] is False
        trip_it(ks, target)
        body = client.get("/kill-switch/status").json()
        assert body["circuit_state"] == "open" and body["trip_count"] == 1

    def test_agent_status_reports_paused(self, client):
        ks, target = make_kill_switch()
        assert client.get("/status").json()["paused_by_kill_switch"] is False
        trip_it(ks, target)
        assert client.get("/status").json()["paused_by_kill_switch"] is True

    def test_kill_switch_cannot_be_configured_through_the_api(self, client):
        response = client.patch("/config", json={"kill_switch": {"enabled": False}})
        assert response.status_code == 422

    # --- manual injections -------------------------------------------------

    def test_manual_injection_refused_while_tripped(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(
            "src.failures.cpu.inject_cpu", lambda *a, **k: called.append(1)
        )
        ks, target = make_kill_switch()
        trip_it(ks, target)
        response = client.post("/inject/manual", json={"failure_type": "cpu"})
        assert response.status_code == 409
        assert "Chaos paused" in response.json()["detail"]
        assert called == []

    def test_manual_dry_run_allowed_while_tripped(self, client, monkeypatch):
        monkeypatch.setattr("src.failures.cpu.inject_cpu", lambda *a, **k: None)
        ks, target = make_kill_switch()
        trip_it(ks, target)
        response = client.post(
            "/inject/manual", json={"failure_type": "cpu", "dry_run": True}
        )
        assert response.status_code == 200

    def test_manual_injection_allowed_when_closed(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(
            "src.failures.cpu.inject_cpu", lambda *a, **k: called.append(1)
        )
        make_kill_switch()
        assert (
            client.post("/inject/manual", json={"failure_type": "cpu"}).status_code
            == 200
        )
        assert called == [1]

    def test_manual_injection_is_tracked_in_the_failure_context(
        self, client, monkeypatch
    ):
        ks, _ = make_kill_switch()
        seen = []
        monkeypatch.setattr(
            "src.failures.cpu.inject_cpu",
            lambda *a, **k: seen.append(ks.failure_context.get_active_injections()),
        )
        client.post("/inject/manual", json={"failure_type": "cpu"})
        assert seen == [{"cpu"}]
        assert ks.failure_context.get_active_injections() == set()

    def test_manual_process_kill_earns_a_grace_period(self, client, monkeypatch):
        monkeypatch.setattr("src.failures.process.inject_process", lambda *a, **k: None)
        ks, _ = make_kill_switch()
        client.post("/inject/manual", json={"failure_type": "process"})
        assert ks.failure_context.is_in_process_kill_grace_period(60)

    def test_manual_dry_run_process_earns_no_grace(self, client, monkeypatch):
        monkeypatch.setattr("src.failures.process.inject_process", lambda *a, **k: None)
        ks, _ = make_kill_switch()
        client.post("/inject/manual", json={"failure_type": "process", "dry_run": True})
        assert not ks.failure_context.is_in_process_kill_grace_period(60)

    def test_breaker_tripping_before_the_background_task_runs(
        self, client, monkeypatch
    ):
        """The pre-flight passed, then the breaker opened before the task ran."""
        from src.kill_switch import KillSwitchOpen

        called = []
        monkeypatch.setattr(
            "src.failures.cpu.inject_cpu", lambda *a, **k: called.append(1)
        )
        ks, _ = make_kill_switch()

        def refuse(*a, **k):
            raise KillSwitchOpen("tripped meanwhile")

        monkeypatch.setattr(ks, "injection", refuse)
        response = client.post("/inject/manual", json={"failure_type": "cpu"})
        assert response.status_code == 200  # accepted; skipped quietly in background
        assert called == []

    # --- starting, stopping, bypass ---------------------------------------

    def test_start_reports_kill_switch_state(self, client):
        make_kill_switch()
        body = client.post("/agent/start").json()
        assert body["kill_switch_enabled"] is True
        assert body["kill_switch_state"] == "closed"
        assert body["paused_by_kill_switch"] is False

    def test_disabling_through_the_api_is_refused_by_default(self, client):
        make_kill_switch()
        response = client.post("/agent/start?enable_kill_switch=false")
        assert response.status_code == 403
        assert "allow_api_disable" in response.json()["detail"]
        assert agent_state.enabled is False

    def test_disabling_through_the_api_when_the_config_allows_it(
        self, client, monkeypatch
    ):
        ks, target = make_kill_switch(allow_api_disable=True)
        trip_it(ks, target)
        body = client.post("/agent/start?enable_kill_switch=false").json()
        assert body["kill_switch_enabled"] is False
        # Bypassed: manual injection is no longer gated
        monkeypatch.setattr("src.failures.cpu.inject_cpu", lambda *a, **k: None)
        assert (
            client.post("/inject/manual", json={"failure_type": "cpu"}).status_code
            == 200
        )
        assert client.get("/kill-switch/status").json()["bypassed"] is True

    def test_bypass_does_not_outlive_the_run(self, client):
        make_kill_switch(allow_api_disable=True)
        client.post("/agent/start?enable_kill_switch=false")
        client.post("/agent/stop")
        client.post("/agent/start")
        assert agent_state.kill_switch_bypass is False

    def test_disabling_is_harmless_when_the_config_already_disabled_it(self, client):
        agent_state.config.kill_switch.enabled = False
        assert client.post("/agent/start?enable_kill_switch=false").status_code == 200

    def test_stop_and_start_do_not_reset_a_tripped_breaker(self, client):
        ks, target = make_kill_switch()
        client.post("/agent/start")
        trip_it(ks, target)
        client.post("/agent/stop")
        body = client.post("/agent/start").json()
        assert agent_state.kill_switch is ks, "kill switch was replaced"
        assert body["kill_switch_state"] == "open"
        assert body["paused_by_kill_switch"] is True

    def test_restart_does_not_reset_a_tripped_breaker(self, client):
        ks, target = make_kill_switch()
        client.post("/agent/start")
        trip_it(ks, target)
        assert client.post("/agent/restart").status_code == 200
        assert agent_state.kill_switch is ks
        assert ks.circuit_state.value == "open"

    def test_stop_leaves_the_monitor_running(self, client):
        ks, _ = make_kill_switch(start_monitor=True)
        client.post("/agent/start")
        client.post("/agent/stop")
        assert ks.get_status()["monitoring_active"] is True

    def test_repeated_starts_do_not_leak_monitor_threads(self, client):
        import threading

        make_kill_switch(start_monitor=True)
        for _ in range(3):
            client.post("/agent/start")
            client.post("/agent/stop")
        monitors = [t for t in threading.enumerate() if t.name == "kill-switch-monitor"]
        assert len(monitors) == 1

    # --- reset -------------------------------------------------------------

    def test_reset_without_a_kill_switch_is_400(self, client):
        agent_state.config.kill_switch.enabled = False
        assert client.post("/kill-switch/reset").status_code == 400

    def test_reset_refused_while_the_target_is_unhealthy(self, client):
        ks, target = make_kill_switch()
        trip_it(ks, target)
        response = client.post("/kill-switch/reset")
        assert response.status_code == 409
        assert "not healthy" in response.json()["detail"]
        assert ks.circuit_state.value == "open"

    def test_reset_closes_when_the_target_is_healthy(self, client):
        ks, target = make_kill_switch()
        trip_it(ks, target)
        target.healthy = True
        response = client.post("/kill-switch/reset")
        assert response.status_code == 200
        assert response.json()["circuit_state"] == "closed"
        assert ks.allows_injection()[0] is True

    def test_reset_requires_auth(self):
        assert TestClient(app).post("/kill-switch/reset").status_code == 401

    # --- trip behavior and the loop ---------------------------------------

    def test_trip_leaves_the_loop_running_but_paused(self, client):
        ks, target = make_kill_switch()
        client.post("/agent/start")
        trip_it(ks, target)
        assert agent_state.enabled is True
        assert agent_state.engine.is_running
        assert client.get("/status").json()["paused_by_kill_switch"] is True

    def test_loop_pauses_while_tripped_and_resumes_after_recovery(
        self, client, monkeypatch
    ):
        import time as _time

        count = []
        monkeypatch.setattr(
            "src.failures.cpu.inject_cpu", lambda *a, **k: count.append(1)
        )
        for name, cfg in agent_state.config.failures.items():
            cfg["enabled"] = name == "cpu"
        agent_state.config.failures["cpu"]["probability"] = 1.0
        agent_state.config.agent.interval_seconds = 1

        ks, target = make_kill_switch(recovery_test_interval_seconds=0)
        client.post("/agent/start")

        def wait_for(pred, timeout=6):
            deadline = _time.time() + timeout
            while _time.time() < deadline:
                if pred():
                    return True
                _time.sleep(0.05)
            return False

        assert wait_for(lambda: len(count) >= 1), "loop never injected"

        trip_it(ks, target)
        _time.sleep(0.3)  # let any tick already in flight finish
        frozen = len(count)
        _time.sleep(2.5)  # two full intervals
        assert len(count) == frozen, "chaos kept running while the breaker was open"

        target.healthy = True
        ks._tick()  # OPEN -> HALF_OPEN (interval 0)
        ks._tick()  # HALF_OPEN -> CLOSED (threshold 1)
        assert ks.allows_injection()[0] is True
        assert wait_for(lambda: len(count) > frozen), "chaos did not resume"

    # --- reload ------------------------------------------------------------

    def test_reload_updates_settings_but_keeps_breaker_state(self, client, monkeypatch):
        from src.config import Config

        ks, target = make_kill_switch()
        trip_it(ks, target)
        reloaded = Config(
            {
                "agent": {},
                "failures": {},
                "kill_switch": {"consecutive_failures_threshold": 9},
            }
        )
        monkeypatch.setattr("src.api.load_config", lambda: reloaded)
        assert client.post("/config/reload").status_code == 200
        assert agent_state.kill_switch is ks
        assert ks.config.consecutive_failures_threshold == 9
        assert ks.circuit_state.value == "open", "reload must not clear a trip"


class TestKillSwitchWiring:
    def test_chaos_is_armed_while_the_loop_runs(self):
        from src.api import _chaos_armed

        assert _chaos_armed() is False
        agent_state.enabled = True
        assert _chaos_armed() is True

    def test_chaos_is_armed_while_an_injection_runs(self):
        from src.api import _chaos_armed
        from src.lifecycle import lifecycle

        assert _chaos_armed() is False
        ticket, _ = lifecycle.begin("network")
        assert _chaos_armed() is True
        lifecycle.end(ticket)
        assert _chaos_armed() is False

    def test_apply_creates_one_kill_switch_and_reuses_it(self):
        from src.api import apply_kill_switch_config

        apply_kill_switch_config(agent_state.config)
        first = agent_state.kill_switch
        apply_kill_switch_config(agent_state.config)
        assert agent_state.kill_switch is first

    def test_apply_with_disabled_config_creates_nothing(self):
        from src.api import apply_kill_switch_config

        agent_state.config.kill_switch.enabled = False
        apply_kill_switch_config(agent_state.config)
        assert agent_state.kill_switch is None

    def test_disabled_kill_switch_does_not_gate(self, client, monkeypatch):
        from src.kill_switch import KillSwitch

        ks, target = make_kill_switch()
        trip_it(ks, target)
        ks.config.enabled = False  # e.g. config reloaded with enabled: false
        monkeypatch.setattr("src.failures.cpu.inject_cpu", lambda *a, **k: None)
        assert (
            client.post("/inject/manual", json={"failure_type": "cpu"}).status_code
            == 200
        )
        assert isinstance(ks, KillSwitch)


class TestProcessTargetingApi:
    """Manual process injections validate their targeting options."""

    def test_pid_1_is_rejected_up_front(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(
            "src.failures.process.inject_process", lambda *a, **k: called.append(1)
        )
        response = client.post(
            "/inject/manual",
            json={"failure_type": "process", "config": {"target_pid": 1}},
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["process", "target_pid"]
        assert called == []

    def test_unknown_selection_is_rejected(self, client):
        response = client.post(
            "/inject/manual",
            json={"failure_type": "process", "config": {"target_selection": "random"}},
        )
        assert response.status_code == 422

    def test_pid_override_is_merged_onto_the_stored_target(self, client, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.failures.process.inject_process",
            lambda cfg, dry_run=False: seen.append(cfg),
        )
        response = client.post(
            "/inject/manual",
            json={"failure_type": "process", "config": {"target_pid": 4242}},
        )
        assert response.status_code == 200
        assert seen[0]["target_pid"] == 4242
        # the stored name is kept, so the process must match both
        assert (
            seen[0]["target_name"]
            == agent_state.config.failures["process"]["target_name"]
        )
        assert seen[0]["target_selection"] == "oldest"

    def test_selection_can_be_changed_with_patch(self, client):
        response = client.patch(
            "/config/failures/process", json={"target_selection": "newest"}
        )
        assert response.status_code == 200
        assert agent_state.config.failures["process"]["target_selection"] == "newest"

    def test_broad_names_are_still_refused(self, client):
        response = client.patch(
            "/config/failures/process", json={"target_name": "python"}
        )
        assert response.status_code == 422


class TestReloadNote:
    """The note used to claim the agent 'must be restarted' for every reload."""

    def _reload(self, client, monkeypatch, logging_section):
        from src.config import Config

        reloaded = Config({"agent": {}, "failures": {}, "logging": logging_section})
        monkeypatch.setattr("src.api.load_config", lambda: reloaded)
        return client.post("/config/reload").json()

    def test_no_note_when_nothing_needs_a_restart(self, client, monkeypatch):
        agent_state.enabled = True
        same = agent_state.config.get_logging_config()
        body = self._reload(client, monkeypatch, same)
        assert body["status"] == "reloaded"
        assert body["note"] is None

    def test_never_says_the_agent_must_be_restarted_for_everything(
        self, client, monkeypatch
    ):
        agent_state.enabled = True
        body = self._reload(client, monkeypatch, {"level": "DEBUG"})
        assert "must be restarted" not in str(body)

    def test_a_logging_change_says_logging_needs_a_restart(self, client, monkeypatch):
        body = self._reload(client, monkeypatch, {"level": "DEBUG"})
        assert "Logging" in body["note"] and "restart" in body["note"]
        assert "Everything else is already in effect" in body["note"]

    def test_failure_settings_really_do_apply_without_a_restart(
        self, client, monkeypatch
    ):
        """The claim in the note: the loop re-reads the config each iteration."""
        from src.config import Config

        reloaded = Config(
            {
                "agent": {"interval_seconds": 7},
                "failures": {
                    "cpu": {
                        "enabled": True,
                        "probability": 0.9,
                        "cores": 1,
                        "duration_seconds": 1,
                    }
                },
            }
        )
        monkeypatch.setattr("src.api.load_config", lambda: reloaded)
        assert client.post("/config/reload").status_code == 200
        import src.api as api

        assert api.get_config() is reloaded
        assert api.get_config().agent.interval_seconds == 7
        assert api.get_config().failures["cpu"]["probability"] == 0.9


class TestAutoStart:
    """In API mode nothing injected until POST /agent/start, and the README
    never said so."""

    def _with_auto_start(self, monkeypatch, value):
        from src.config import load_config

        config = load_config()
        config.agent.auto_start = value
        monkeypatch.setattr("src.api.load_config", lambda: config)

    def test_default_config_does_not_auto_start(self):
        from src.config import load_config

        assert load_config().agent.auto_start is False

    def test_the_agent_stays_idle_by_default(self, monkeypatch):
        self._with_auto_start(monkeypatch, False)
        with TestClient(app):
            assert agent_state.enabled is False
            assert not agent_state.engine.is_running

    def test_auto_start_begins_the_loop_when_the_api_starts(self, monkeypatch):
        self._with_auto_start(monkeypatch, True)
        with TestClient(app) as client:
            assert agent_state.enabled is True
            assert agent_state.engine.is_running
            headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
            assert client.get("/status", headers=headers).json()["enabled"] is True

    def test_start_reports_already_running_after_an_auto_start(self, monkeypatch):
        self._with_auto_start(monkeypatch, True)
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
            response = client.post("/agent/start", headers=headers)
            assert response.status_code == 400
            assert "already running" in response.json()["detail"]

    def test_the_kill_switch_is_in_place_before_the_loop_starts(self, monkeypatch):
        self._with_auto_start(monkeypatch, True)
        with TestClient(app):
            assert agent_state.kill_switch is not None

    def test_shutdown_stops_an_auto_started_loop(self, monkeypatch):
        self._with_auto_start(monkeypatch, True)
        with TestClient(app):
            thread = agent_state.engine.thread
        thread.join(timeout=10)
        assert not thread.is_alive()

    def test_auto_start_can_be_stopped_and_started_like_any_run(self, monkeypatch):
        self._with_auto_start(monkeypatch, True)
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
            assert client.post("/agent/stop", headers=headers).status_code == 200
            assert client.post("/agent/start", headers=headers).status_code == 200


class TestProbabilityInTheRealLoop:
    def test_probability_zero_never_fires_even_for_a_roll_of_zero(
        self, client, monkeypatch
    ):
        import time as _time

        calls = []
        monkeypatch.setattr(
            "src.failures.cpu.inject_cpu", lambda *a, **k: calls.append(1)
        )
        monkeypatch.setattr("random.random", lambda: 0.0)
        for name, cfg in agent_state.config.failures.items():
            cfg["enabled"] = name == "cpu"
        agent_state.config.failures["cpu"]["probability"] = 0.0
        agent_state.config.agent.interval_seconds = 1

        client.post("/agent/start")
        _time.sleep(2.5)  # at least two loop iterations
        assert calls == []

        # And the loop itself is working: probability 1.0 fires
        agent_state.config.failures["cpu"]["probability"] = 1.0
        deadline = _time.time() + 5
        while not calls and _time.time() < deadline:
            _time.sleep(0.05)
        assert calls, "the loop never injected at probability 1.0"


class TestMetricsEndpoints:
    def test_summary_reports_aborted_and_a_total_that_includes_it(self, client):
        from src.metrics import INJECTIONS_TOTAL

        INJECTIONS_TOTAL.labels(failure_type="network", status="success").inc(2)
        INJECTIONS_TOTAL.labels(failure_type="network", status="aborted").inc(3)
        INJECTIONS_TOTAL.labels(failure_type="network", status="failed").inc(1)
        INJECTIONS_TOTAL.labels(failure_type="network", status="skipped").inc(4)
        summary = client.get("/metrics/summary").json()["metrics"]["network"]
        assert summary["success"] == 2
        assert summary["aborted"] == 3
        assert summary["failed"] == 1
        assert summary["skipped"] == 4
        assert summary["total"] == 10

    def test_summary_timestamp_is_utc_and_does_not_warn(self, client):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            stamp = client.get("/metrics/summary").json()["timestamp"]
        assert stamp.endswith("Z") and "+00:00" not in stamp

    def test_reset_clears_the_counts(self, client):
        from src.metrics import INJECTIONS_TOTAL, metric_value

        INJECTIONS_TOTAL.labels(failure_type="cpu", status="success").inc(5)
        assert client.post("/metrics/reset").status_code == 200
        assert metric_value(INJECTIONS_TOTAL, failure_type="cpu", status="success") == 0


class TestSharedEngine:
    """The API drives the same engine as the standalone agent."""

    def _only(self, *names):
        for name, cfg in agent_state.config.failures.items():
            cfg["enabled"] = name in names
            cfg["probability"] = 1.0
        agent_state.config.agent.interval_seconds = 1
        agent_state.config.agent.dry_run = False

    def _holding_injector(self, name, entered):
        from src.lifecycle import lifecycle

        def inject(cfg, dry_run=False):
            with lifecycle.injection(name) as ticket:
                if ticket is None:
                    return
                entered.append(name)
                ticket.sleep(30)

        return inject

    def _wait_for(self, predicate, timeout=6):
        import time as _time

        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if predicate():
                return True
            _time.sleep(0.05)
        return False

    def test_faults_overlap_and_status_lists_them(self, client, monkeypatch):
        entered = []
        for name in ("cpu", "memory"):
            monkeypatch.setattr(
                f"src.failures.{name}.inject_{name}",
                self._holding_injector(name, entered),
            )
        self._only("cpu", "memory")
        client.post("/agent/start")
        assert self._wait_for(lambda: sorted(entered) == ["cpu", "memory"])
        active = client.get("/status").json()["active_injections"]
        assert active == ["cpu", "memory"]

    def test_the_loop_is_not_blocked_by_a_long_injection(self, client, monkeypatch):
        entered = []
        monkeypatch.setattr(
            "src.failures.cpu.inject_cpu", self._holding_injector("cpu", entered)
        )
        self._only("cpu")
        client.post("/agent/start")
        assert self._wait_for(lambda: entered == ["cpu"])
        assert self._wait_for(lambda: agent_state.engine.iteration >= 3, timeout=8)

    def test_stop_removes_running_injections_before_returning(
        self, client, monkeypatch
    ):
        import time as _time

        entered = []
        finished = []

        def inject(cfg, dry_run=False):
            from src.lifecycle import lifecycle

            with lifecycle.injection("cpu") as ticket:
                if ticket is None:
                    return
                entered.append(1)
                try:
                    ticket.sleep(30)
                finally:
                    finished.append(1)

        monkeypatch.setattr("src.failures.cpu.inject_cpu", inject)
        self._only("cpu")
        client.post("/agent/start")
        assert self._wait_for(lambda: entered)
        started = _time.time()
        assert client.post("/agent/stop").status_code == 200
        assert _time.time() - started < 5
        assert finished, "stop returned before the injection cleaned up"
        assert agent_state.engine.in_flight() == 0
        assert agent_state.enabled is False

    def test_the_loop_ending_clears_enabled(self, client):
        client.post("/agent/start")
        assert agent_state.enabled is True
        agent_state.engine.stop(timeout=5)
        assert self._wait_for(lambda: agent_state.enabled is False)

    def test_status_uptime_comes_from_the_engine(self, client):
        client.post("/agent/start")
        body = client.get("/status").json()
        assert body["enabled"] is True
        assert body["uptime_seconds"] is not None
        assert body["uptime_seconds"] >= 0

    def test_a_missing_config_does_not_kill_the_loop(self, client, monkeypatch):
        from src import api

        client.post("/agent/start")
        saved = agent_state.config
        agent_state.config = None
        try:
            assert api._settings_snapshot() is None
            import time as _time

            _time.sleep(0.3)
            assert agent_state.engine.is_running
        finally:
            agent_state.config = saved


class TestConfigIsReadAtomically:
    def test_the_snapshot_waits_for_a_write_in_progress(self):
        import threading

        from src import api

        done = threading.Event()
        taken = []

        def reader():
            taken.append(api._settings_snapshot())
            done.set()

        with agent_state.config_lock:
            t = threading.Thread(target=reader)
            t.start()
            assert not done.wait(0.3), "snapshot did not wait for the config lock"
        assert done.wait(5)
        t.join()
        assert taken[0] is not None

    def test_patching_never_exposes_a_half_applied_config(self, client):
        """
        Alternate between two internally consistent configs through PATCH while
        a reader snapshots continuously. Each snapshot must match one of them in
        full, never a mixture.
        """
        import threading

        from src import api

        a = {"interval_seconds": 1, "dry_run": False, "cpu": 0.1}
        b = {"interval_seconds": 2, "dry_run": True, "cpu": 0.9}
        stop = threading.Event()
        torn = []

        def reader():
            while not stop.is_set():
                s = api._settings_snapshot()
                pair = (s.interval_seconds, s.dry_run, s.failures["cpu"]["probability"])
                if pair not in {(1, False, 0.1), (2, True, 0.9)}:
                    torn.append(pair)

        # Start from a known state so every snapshot is one of the two.
        client.patch(
            "/config",
            json={
                "interval_seconds": a["interval_seconds"],
                "dry_run": a["dry_run"],
                "failures": {"cpu": {"probability": a["cpu"]}},
            },
        )
        t = threading.Thread(target=reader)
        t.start()
        try:
            for i in range(60):
                c = a if i % 2 else b
                response = client.patch(
                    "/config",
                    json={
                        "interval_seconds": c["interval_seconds"],
                        "dry_run": c["dry_run"],
                        "failures": {"cpu": {"probability": c["cpu"]}},
                    },
                )
                assert response.status_code == 200
        finally:
            stop.set()
            t.join()
        assert torn == []


class TestPausedWinsOverAlreadyRunning:
    def test_a_tripped_breaker_is_reported_even_while_the_aborted_injection_unwinds(
        self, client
    ):
        """
        After a trip, the aborted injection removes its effect before it
        releases its guard. A request in that gap must hear "paused".
        """
        from src.lifecycle import lifecycle

        ks, target = make_kill_switch()
        ticket, _ = lifecycle.begin("network")  # the injection still unwinding
        trip_it(ks, target)
        response = client.post("/inject/manual", json={"failure_type": "network"})
        assert response.status_code == 409
        assert "Chaos paused" in response.json()["detail"]
        lifecycle.end(ticket)

    def test_already_running_is_still_reported_when_the_breaker_is_closed(self, client):
        from src.lifecycle import lifecycle

        make_kill_switch()
        ticket, _ = lifecycle.begin("network")
        response = client.post("/inject/manual", json={"failure_type": "network"})
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]
        lifecycle.end(ticket)
