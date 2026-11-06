"""Running and inspecting experiments through the API."""

import datetime as dt
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import agent_state, app
from src.audit import audit
from src.config import Config, load_config
from src.kill_switch import KillSwitch, KillSwitchConfig
from src.lifecycle import lifecycle
from src.resources import governor
from src.schemas import validate_config_dict
from tests.http_target import Target

TOKEN = "t" * 40


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def api_token(monkeypatch):
    monkeypatch.setenv("CHAOS_API_TOKEN", TOKEN)
    for var in (
        "CHAOS_API_TOKEN_FILE",
        "CHAOS_API_AUTH_DISABLED",
        "CHAOS_API_TOKENS_FILE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def client():
    return TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})


@pytest.fixture
def target():
    t = Target()
    yield t
    t.close()


class Holder:
    """Replaces the real injectors: no-ops by default, or hold until aborted."""

    def __init__(self):
        self.hold = False
        self.calls = []

    def __call__(self, cfg, dry_run=False):
        self.calls.append(dry_run)
        with lifecycle.injection("cpu", enabled=not dry_run) as ticket:
            if self.hold and ticket is not None:
                ticket.sleep(30)


@pytest.fixture
def injector(monkeypatch):
    h = Holder()
    monkeypatch.setattr("src.failures.cpu.inject_cpu", h)
    for name in ("memory", "process", "network"):
        monkeypatch.setattr(f"src.failures.{name}.inject_{name}", lambda *a, **k: None)
    return h


def experiment_doc(target, name="cpu-spike", **schedule):
    return {
        "name": name,
        "description": "Saturate a core",
        "hypothesis": "The target comes back",
        "steady_state": {"checks": [{"name": "health", "url": target.url}]},
        "faults": {"cpu": {"cores": 1, "duration_seconds": 5}},
        "schedule": {"duration_seconds": 5, "interval_seconds": 1, **schedule},
    }


@pytest.fixture
def configured(target, injector):
    """The default config plus one fast experiment, and a runner that is idle."""
    raw = {
        "agent": {},
        "failures": {"cpu": {"duration_seconds": 5}},
        "experiments": [
            experiment_doc(target),
            experiment_doc(target, name="other"),
        ],
    }
    config = Config(validate_config_dict(raw))
    for exp in config.experiments.values():
        exp["steady_state"].update(
            check_interval_seconds=0.05,
            required_consecutive_successes=1,
            baseline_timeout_seconds=0,
            recovery_timeout_seconds=0,
        )
        exp["schedule"].update(duration_seconds=0.3, interval_seconds=0.05)

    _reset()
    agent_state.config = config
    yield config
    _reset()
    try:
        agent_state.config = load_config()
    except Exception:
        pass


def _reset():
    agent_state.experiments.stop(timeout=15)
    if agent_state.kill_switch:
        agent_state.kill_switch.stop_monitoring()
        agent_state.kill_switch = None
    if not agent_state.engine.stop(timeout=15):
        pytest.fail("agent loop did not stop")
    agent_state.enabled = False
    agent_state.kill_switch_bypass = False


def run_id(response):
    return response.json()["run"]["id"]


def wait_finished(client, rid, timeout=15):
    assert wait_for(
        lambda: client.get(f"/experiment-runs/{rid}").json()["finished_at"] is not None,
        timeout,
    )
    return client.get(f"/experiment-runs/{rid}").json()


def hold_experiment(client, injector, name="cpu-spike"):
    """Start an experiment that stays in its chaos phase until aborted."""
    injector.hold = True
    configured_exp = agent_state.config.experiments[name]
    configured_exp["schedule"].update(duration_seconds=60, interval_seconds=1)
    response = client.post(f"/experiments/{name}/run")
    assert response.status_code == 202
    assert wait_for(lambda: lifecycle.is_active("cpu"))
    return run_id(response)


class TestListAndShow:
    def test_list(self, client, configured):
        body = client.get("/experiments").json()
        assert body["count"] == 2
        first = body["experiments"][0]
        assert first == {
            "name": "cpu-spike",
            "description": "Saturate a core",
            "hypothesis": "The target comes back",
            "faults": ["cpu"],
            "duration_seconds": 0.3,
            "dry_run": False,
            "window": None,
            "target": None,
        }

    def test_show_one(self, client, configured):
        body = client.get("/experiments/cpu-spike").json()
        assert body["hypothesis"] == "The target comes back"
        assert body["steady_state"]["checks"][0]["name"] == "health"
        assert body["faults"]["cpu"]["cores"] == 1

    def test_unknown_experiment(self, client, configured):
        assert client.get("/experiments/nope").status_code == 404
        assert client.post("/experiments/nope/run").status_code == 404

    def test_none_defined(self, client):
        agent_state.config = Config(
            validate_config_dict(
                {"agent": {}, "failures": {"cpu": {"duration_seconds": 5}}}
            )
        )
        assert client.get("/experiments").json() == {"count": 0, "experiments": []}

    def test_runs_start_empty(self, client, configured):
        assert client.get("/experiment-runs").json() == {"count": 0, "runs": []}

    def test_unknown_run(self, client, configured):
        assert client.get("/experiment-runs/nope").status_code == 404


class TestRunning:
    def test_run_to_a_verdict(self, client, configured):
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "started"
        assert body["run"]["experiment"] == "cpu-spike"
        assert body["run"]["principal"] == "shared-token"
        result = wait_finished(client, run_id(response))
        assert result["status"] == "completed"
        assert result["verdict"] == "confirmed"
        assert client.get("/experiment-runs").json()["runs"][0]["id"] == result["id"]

    def test_an_unhealthy_target_is_refused_not_failed(
        self, client, configured, target
    ):
        target.status = 503
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 202
        result = wait_finished(client, run_id(response))
        assert result["status"] == "refused"
        assert result["verdict"] == "inconclusive"

    def test_dry_run_can_be_forced(self, client, configured, injector):
        response = client.post("/experiments/cpu-spike/run", json={"dry_run": True})
        assert response.json()["run"]["dry_run"] is True
        wait_finished(client, run_id(response))
        assert injector.calls and all(injector.calls)

    def test_a_dry_run_experiment_can_be_made_real(self, client, configured, injector):
        configured.experiments["cpu-spike"]["dry_run"] = True
        response = client.post("/experiments/cpu-spike/run", json={"dry_run": False})
        assert response.json()["run"]["dry_run"] is False
        wait_finished(client, run_id(response))
        assert injector.calls and not any(injector.calls)

    @pytest.mark.parametrize("body", [{"dry_run": "yes"}, {"dry_run": 1}, {"extra": 1}])
    def test_the_request_body_is_strict(self, client, configured, body):
        assert client.post("/experiments/cpu-spike/run", json=body).status_code == 422
        assert not agent_state.experiments.is_running

    def test_status_shows_the_running_experiment(self, client, configured, injector):
        rid = hold_experiment(client, injector)
        status = client.get("/status").json()
        assert status["active_experiment"] == "cpu-spike"
        assert "cpu" in status["active_injections"]
        client.post("/experiment-runs/abort")
        wait_finished(client, rid)
        assert wait_for(
            lambda: client.get("/status").json()["active_experiment"] is None
        )


class TestExclusion:
    def test_one_experiment_at_a_time(self, client, configured, injector):
        hold_experiment(client, injector)
        response = client.post("/experiments/other/run")
        assert response.status_code == 409
        assert not [
            r
            for r in client.get("/experiment-runs").json()["runs"]
            if r["experiment"] == "other"
        ]

    def test_the_runner_itself_refuses_a_second_run(self, client, configured, injector):
        """The API checks first; this is the backstop for a request that races past."""
        hold_experiment(client, injector)
        with pytest.raises(Exception, match="already running"):
            agent_state.experiments.start(configured.experiments["other"])

    def test_the_agent_loop_blocks_an_experiment(self, client, configured):
        assert client.post("/agent/start").status_code == 200
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 409
        assert "agent loop is running" in response.json()["detail"]

    def test_an_experiment_blocks_the_agent_loop(self, client, configured, injector):
        hold_experiment(client, injector)
        response = client.post("/agent/start")
        assert response.status_code == 409
        assert "experiment is running" in response.json()["detail"]
        assert not agent_state.enabled

    def test_an_experiment_blocks_manual_injections(self, client, configured, injector):
        hold_experiment(client, injector)
        response = client.post("/inject/manual", json={"failure_type": "cpu"})
        assert response.status_code == 409
        assert "experiment is running" in response.json()["detail"]

    def test_but_a_manual_dry_run_is_fine(self, client, configured, injector):
        hold_experiment(client, injector)
        response = client.post(
            "/inject/manual", json={"failure_type": "cpu", "dry_run": True}
        )
        assert response.status_code == 200

    def test_a_running_manual_injection_blocks_an_experiment(
        self, client, configured, injector
    ):
        ticket, _ = lifecycle.begin("cpu")
        try:
            response = client.post("/experiments/cpu-spike/run")
            assert response.status_code == 409
            assert "cpu" in response.json()["detail"]
        finally:
            lifecycle.end(ticket)

    def test_after_it_ends_the_agent_loop_can_start(self, client, configured):
        rid = run_id(client.post("/experiments/cpu-spike/run"))
        wait_finished(client, rid)
        assert wait_for(lambda: not agent_state.experiments.is_running)
        assert client.post("/agent/start").status_code == 200


class TestAbort:
    def test_abort(self, client, configured, injector):
        rid = hold_experiment(client, injector)
        response = client.post("/experiment-runs/abort")
        assert response.status_code == 200
        assert response.json()["status"] == "aborting"
        result = wait_finished(client, rid)
        assert result["status"] == "aborted"
        assert result["verdict"] == "inconclusive"
        assert result["reason"] == "aborted by shared-token"
        assert wait_for(lambda: lifecycle.active_types() == [])

    def test_abort_with_nothing_running(self, client, configured):
        response = client.post("/experiment-runs/abort")
        assert response.status_code == 400
        assert "No experiment" in response.json()["detail"]


class TestSafetyGates:
    def make_ks(self):
        cfg = KillSwitchConfig(
            health_check_interval_seconds=3600,
            grace_after_agent_start_seconds=0,
            recovery_test_interval_seconds=3600,
            consecutive_failures_threshold=1,
        )
        state = SimpleNamespace(healthy=True)
        ks = KillSwitch(cfg, is_armed=lambda: True, health_check=lambda: state.healthy)
        state.healthy = False
        ks._tick()
        assert ks.circuit_state.value == "open"
        return ks

    def test_an_open_kill_switch_refuses_the_run(self, client, configured):
        agent_state.kill_switch = self.make_ks()
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 409
        assert "Chaos paused" in response.json()["detail"]
        assert not agent_state.experiments.is_running

    def test_but_not_a_dry_run(self, client, configured):
        agent_state.kill_switch = self.make_ks()
        response = client.post("/experiments/cpu-spike/run", json={"dry_run": True})
        assert response.status_code == 202
        wait_finished(client, run_id(response))

    def test_a_fault_over_the_resource_budget_is_refused(
        self, client, configured, monkeypatch
    ):
        denied = SimpleNamespace(
            ok=False, reason="not enough memory", clamped=False, granted=0
        )
        monkeypatch.setattr(governor, "preview_memory", lambda mb: denied)
        configured.experiments["cpu-spike"]["faults"]["memory"] = {
            "enabled": True,
            "probability": 1.0,
            "mb": 100,
            "duration_seconds": 5,
        }
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 422
        error = response.json()["detail"][0]
        assert error["loc"] == ["experiments", "cpu-spike", "faults", "memory", "mb"]
        assert "not enough memory" in error["msg"]
        assert not agent_state.experiments.is_running

    def test_clamped_cpu_is_reported(self, client, configured, monkeypatch):
        clamped = SimpleNamespace(ok=True, reason="", clamped=True, granted=1)
        monkeypatch.setattr(governor, "preview_cores", lambda n: clamped)
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 202
        assert response.json()["clamped"] == {"cpu.cores": 1}
        wait_finished(client, run_id(response))

    def test_a_shutting_down_agent_refuses(self, client, configured):
        lifecycle.shutdown("test")
        assert client.post("/experiments/cpu-spike/run").status_code == 503


class TestAudit:
    def test_the_request_and_the_run_are_recorded(self, client, configured, injector):
        rid = run_id(client.post("/experiments/cpu-spike/run"))
        wait_finished(client, rid)
        assert wait_for(lambda: audit.recent(event="experiment.finished"))
        request = [
            e
            for e in audit.recent(event="api.request")
            if e["path"] == "/experiments/cpu-spike/run"
        ]
        assert request and request[0]["detail"]["experiment"] == "cpu-spike"
        assert request[0]["status"] == 202
        started = audit.recent(event="experiment.started")[0]
        assert started["experiment"] == "cpu-spike"
        assert started["principal"] == "shared-token"
        assert started["run_id"] == rid
        injection = audit.recent(event="injection.started")[0]
        assert injection["source"] == "experiment"
        assert injection["principal"] == "shared-token"
        assert injection["run_id"] == rid


class TestTimeWindows:
    WINDOWS = {
        "timezone": "Africa/Dar_es_Salaam",
        "allow": [{"days": ["mon"], "start": "10:00", "end": "16:00"}],
    }

    @pytest.fixture
    def clock(self, monkeypatch, configured):
        """Monday 2026-11-02 12:00 UTC, which is 15:00 in Dar es Salaam."""
        state = {"now": dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)}
        monkeypatch.setattr("src.windows.utcnow", lambda: state["now"])
        configured.experiments["cpu-spike"]["windows"] = self.WINDOWS
        return state

    def test_the_list_shows_whether_each_window_is_open(self, client, clock):
        by_name = {
            e["name"]: e for e in client.get("/experiments").json()["experiments"]
        }
        assert by_name["cpu-spike"]["window"] == {
            "timezone": "Africa/Dar_es_Salaam",
            "open": True,
            "closes_at": "2026-11-02T13:00:00Z",
        }
        assert by_name["other"]["window"] is None

    def test_a_closed_window_says_when_it_opens(self, client, clock):
        clock["now"] = dt.datetime(2026, 11, 2, 15, 0, tzinfo=dt.timezone.utc)
        window = client.get("/experiments").json()["experiments"][0]["window"]
        assert window == {
            "timezone": "Africa/Dar_es_Salaam",
            "open": False,
            "opens_at": "2026-11-09T07:00:00Z",
        }

    def test_show_includes_the_windows_and_their_state(self, client, clock):
        body = client.get("/experiments/cpu-spike").json()
        assert body["windows"] == self.WINDOWS
        assert body["window_status"]["open"] is True
        assert client.get("/experiments/other").json()["window_status"] is None

    def test_inside_the_window_it_runs(self, client, clock):
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 202
        result = wait_finished(client, run_id(response))
        assert result["status"] == "completed"
        assert result["window_closes_at"] == "2026-11-02T13:00:00Z"

    def test_outside_the_window_it_is_a_409_that_says_when(self, client, clock):
        clock["now"] = dt.datetime(
            2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc
        )  # Tuesday
        runs_before = client.get("/experiment-runs").json()["count"]
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "Outside the allowed time window" in detail
        assert "Mon 2026-11-09 10:00 (Africa/Dar_es_Salaam)" in detail
        assert not agent_state.experiments.is_running
        assert client.get("/experiment-runs").json()["count"] == runs_before

    def test_too_little_time_left_is_a_409(self, client, clock):
        clock["now"] = dt.datetime(2026, 11, 2, 12, 59, 58, tzinfo=dt.timezone.utc)
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 409
        assert "Not enough time left" in response.json()["detail"]

    def test_a_dry_run_ignores_the_window(self, client, clock):
        clock["now"] = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc)
        response = client.post("/experiments/cpu-spike/run", json={"dry_run": True})
        assert response.status_code == 202
        wait_finished(client, run_id(response))

    def test_other_experiments_are_not_gated(self, client, clock):
        clock["now"] = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc)
        response = client.post("/experiments/other/run")
        assert response.status_code == 202
        wait_finished(client, run_id(response))


class TestTargetSelectors:
    SELECTOR = {"match_labels": {"env": "staging"}}

    @pytest.fixture
    def labelled(self, monkeypatch, configured):
        """An agent labelled env=staging, and one experiment that selects on it."""
        from src import targets

        monkeypatch.setenv("CHAOS_TARGET_LABELS", "env=staging,app=checkout")
        monkeypatch.delenv("CHAOS_TARGET_LABELS_FILE", raising=False)
        targets.agent_labels.reset()
        configured.experiments["cpu-spike"]["selector"] = self.SELECTOR
        yield monkeypatch
        targets.agent_labels.reset()

    def relabel(self, monkeypatch, value):
        from src import targets

        monkeypatch.setenv("CHAOS_TARGET_LABELS", value)
        targets.agent_labels.reset()

    def test_target_shows_the_agents_labels(self, client, labelled):
        assert client.get("/target").json() == {
            "labels": {"env": "staging", "app": "checkout"},
            "labels_error": None,
        }

    def test_target_without_labels(self, client, configured, monkeypatch):
        from src import targets

        monkeypatch.delenv("CHAOS_TARGET_LABELS", raising=False)
        monkeypatch.delenv("CHAOS_TARGET_LABELS_FILE", raising=False)
        targets.agent_labels.reset()
        assert client.get("/target").json() == {"labels": {}, "labels_error": None}

    def test_target_explains_an_unreadable_labels_file(
        self, client, configured, monkeypatch, tmp_path
    ):
        from src import targets

        monkeypatch.setenv("CHAOS_TARGET_LABELS_FILE", str(tmp_path / "missing"))
        targets.agent_labels.reset()
        try:
            body = client.get("/target").json()
            assert body["labels"] == {}
            assert "cannot read" in body["labels_error"]
            assert str(tmp_path) not in body["labels_error"]
        finally:
            monkeypatch.delenv("CHAOS_TARGET_LABELS_FILE")
            targets.agent_labels.reset()

    def test_the_list_says_whether_this_agent_is_a_target(self, client, labelled):
        by_name = {
            e["name"]: e for e in client.get("/experiments").json()["experiments"]
        }
        assert by_name["cpu-spike"]["target"] == {"matches": True, "problems": []}
        assert by_name["other"]["target"] is None

    def test_a_mismatch_is_listed_with_the_reason(self, client, labelled):
        self.relabel(labelled, "env=prod")
        target = client.get("/experiments").json()["experiments"][0]["target"]
        assert target == {
            "matches": False,
            "problems": ["env must be 'staging' but is 'prod'"],
        }

    def test_show_includes_the_selector_and_target_status(self, client, labelled):
        body = client.get("/experiments/cpu-spike").json()
        assert body["selector"]["match_labels"] == {"env": "staging"}
        assert body["target_status"]["matches"] is True
        assert client.get("/experiments/other").json()["target_status"] is None

    def test_a_matching_agent_runs_it(self, client, labelled):
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 202
        assert response.json()["run"]["target_labels"] == {
            "env": "staging",
            "app": "checkout",
        }
        assert wait_finished(client, run_id(response))["status"] == "completed"

    def test_a_different_agent_gets_a_409_that_says_why(self, client, labelled):
        self.relabel(labelled, "env=prod")
        runs_before = client.get("/experiment-runs").json()["count"]
        response = client.post("/experiments/cpu-spike/run")
        assert response.status_code == 409
        assert "not a target of the experiment" in response.json()["detail"]
        assert "env must be 'staging' but is 'prod'" in response.json()["detail"]
        assert client.get("/experiment-runs").json()["count"] == runs_before
        assert not agent_state.experiments.is_running

    def test_an_unlabelled_agent_is_refused(self, client, labelled):
        labelled.delenv("CHAOS_TARGET_LABELS")
        from src import targets

        targets.agent_labels.reset()
        assert client.post("/experiments/cpu-spike/run").status_code == 409

    def test_a_dry_run_ignores_the_selector(self, client, labelled):
        self.relabel(labelled, "env=prod")
        response = client.post("/experiments/cpu-spike/run", json={"dry_run": True})
        assert response.status_code == 202
        wait_finished(client, run_id(response))

    def test_experiments_without_a_selector_are_not_gated(self, client, labelled):
        self.relabel(labelled, "env=prod")
        response = client.post("/experiments/other/run")
        assert response.status_code == 202
        wait_finished(client, run_id(response))

    def test_removing_the_label_stops_a_running_experiment(
        self, client, labelled, injector
    ):
        rid = hold_experiment(client, injector)
        self.relabel(labelled, "app=checkout")  # env is gone
        result = wait_finished(client, rid, timeout=10)
        assert result["status"] == "aborted"
        assert "no longer match the selector" in result["reason"]
        assert wait_for(lambda: lifecycle.active_types() == [])


class TestStartupRefusesBadLabels:
    def run_server(self, env_extra):
        import os
        import subprocess
        import sys

        env = {**os.environ, "CHAOS_API_TOKEN": "s" * 40, **env_extra}
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "src.api_server",
                "--port",
                "0",
                "--metrics-port",
                "0",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_invalid_labels(self):
        result = self.run_server({"CHAOS_TARGET_LABELS": "bad key=x"})
        assert result.returncode != 0
        assert "Refusing to start" in result.stdout + result.stderr

    def test_an_unreadable_labels_file(self, tmp_path):
        result = self.run_server({"CHAOS_TARGET_LABELS_FILE": str(tmp_path / "nope")})
        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert "Refusing to start" in output and "cannot read" in output
