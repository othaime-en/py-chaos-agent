"""The audit log: who did what, and when."""

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from src import auth
from src.api import agent_state, app
from src.audit import AuditError, AuditLog, annotate, audit
from src.config import load_config
from src.engine import ChaosEngine, Settings
from src.metrics import AUDIT_WRITE_FAILURES_TOTAL, metric_value
from src.tokens import generate_token, hash_token

ALICE, BOB, CAROL = generate_token(), generate_token(), generate_token()


# --------------------------------------------------------------------- the log


class TestRecording:
    def test_an_event_has_the_expected_fields(self):
        log = AuditLog()
        event = log.record(
            "api.request", principal="alice", role="admin", path="/x", status=200
        )
        assert event["event"] == "api.request"
        assert event["principal"] == "alice" and event["role"] == "admin"
        assert event["path"] == "/x" and event["status"] == 200
        assert event["time"].endswith("+00:00")

    def test_newest_events_come_first(self):
        log = AuditLog()
        for n in range(3):
            log.record("e", principal=f"p{n}")
        assert [e["principal"] for e in log.recent()] == ["p2", "p1", "p0"]

    def test_the_ring_keeps_only_the_latest(self):
        log = AuditLog(ring_size=5)
        for n in range(20):
            log.record("e", n=n)
        events = log.recent(limit=100)
        assert len(events) == 5 and events[0]["n"] == 19 and events[-1]["n"] == 15

    def test_filters(self):
        log = AuditLog()
        log.record("a", principal="alice")
        log.record("b", principal="bob")
        log.record("a", principal="bob")
        assert len(log.recent(principal="bob")) == 2
        assert len(log.recent(event="a")) == 2
        assert len(log.recent(principal="bob", event="a")) == 1

    def test_limit(self):
        log = AuditLog()
        for _ in range(10):
            log.record("e")
        assert len(log.recent(limit=3)) == 3
        assert log.recent(limit=0) == []

    def test_secrets_are_masked_at_any_depth(self):
        log = AuditLog()
        event = log.record(
            "e",
            detail={
                "token": "t0ps3cret",
                "nested": {"Authorization": "Bearer abc", "ok": 1},
                "items": [{"password": "p"}],
            },
        )
        text = json.dumps(event)
        assert (
            "t0ps3cret" not in text and "Bearer abc" not in text and '"p"' not in text
        )
        assert event["detail"]["nested"]["ok"] == 1

    def test_unserializable_values_become_strings(self):
        event = AuditLog().record("e", detail={"obj": object()})
        json.dumps(event)

    def test_reset_clears_the_ring(self):
        log = AuditLog()
        log.record("e")
        log.reset()
        assert log.recent() == []


class TestFile:
    def test_events_are_appended_as_json_lines(self, tmp_path):
        path = tmp_path / "audit.log"
        log = AuditLog()
        log.configure_file(str(path))
        log.record("a", principal="alice")
        log.record("b", principal="bob")
        lines = path.read_text().splitlines()
        assert [json.loads(line)["principal"] for line in lines] == ["alice", "bob"]

    def test_the_file_is_owner_only(self, tmp_path):
        path = tmp_path / "audit.log"
        AuditLog().configure_file(str(path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_existing_content_is_kept(self, tmp_path):
        path = tmp_path / "audit.log"
        path.write_text('{"event": "old"}\n')
        log = AuditLog()
        log.configure_file(str(path))
        log.record("new")
        assert len(path.read_text().splitlines()) == 2

    def test_rotation_is_respected(self, tmp_path):
        """The file is reopened for each event, so rotating it works."""
        path = tmp_path / "audit.log"
        log = AuditLog()
        log.configure_file(str(path))
        log.record("before")
        os.rename(path, tmp_path / "audit.log.1")
        log.record("after")
        assert "before" in (tmp_path / "audit.log.1").read_text()
        assert "after" in path.read_text() and "before" not in path.read_text()

    def test_an_unwritable_file_stops_startup(self, tmp_path):
        with pytest.raises(AuditError, match="cannot write the audit log"):
            AuditLog().configure_file(str(tmp_path / "missing-dir" / "audit.log"))

    def test_a_directory_is_not_a_log_file(self, tmp_path):
        with pytest.raises(AuditError):
            AuditLog().configure_file(str(tmp_path))

    def test_a_write_failure_later_does_not_raise_but_is_counted(self, tmp_path):
        path = tmp_path / "d" / "audit.log"
        path.parent.mkdir()
        log = AuditLog()
        log.configure_file(str(path))
        path.unlink()
        path.parent.rmdir()  # the directory disappears under us
        before = metric_value(AUDIT_WRITE_FAILURES_TOTAL)
        event = log.record("e")  # must not raise
        assert metric_value(AUDIT_WRITE_FAILURES_TOTAL) == before + 1
        assert log.recent()[0] is event  # still in memory

    def test_from_environment(self, tmp_path, monkeypatch):
        path = tmp_path / "audit.log"
        monkeypatch.setenv("CHAOS_AUDIT_LOG_FILE", str(path))
        log = AuditLog()
        log.configure_from_env()
        assert log.path == str(path)

    def test_no_environment_means_no_file(self, monkeypatch):
        monkeypatch.delenv("CHAOS_AUDIT_LOG_FILE", raising=False)
        log = AuditLog()
        log.configure_from_env()
        assert log.path is None

    def test_events_also_go_to_the_logger(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="chaos.audit"):
            AuditLog().record("api.request", principal="alice")
        assert any("audit api.request" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------------- API


def write_tokens(path, rows):
    lines = ["tokens:"]
    for name, role, token, *extra in rows:
        lines += [
            f"  - name: {name}",
            f"    role: {role}",
            f"    sha256: {hash_token(token)}",
        ]
        if extra:
            lines.append(f"    expires: {extra[0]}")
    path.write_text("\n".join(lines) + "\n")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for var in (auth.TOKEN_ENV, auth.TOKEN_FILE_ENV, auth.AUTH_DISABLED_ENV):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "tokens.yaml"
    write_tokens(
        path,
        [
            ("alice", "admin", ALICE),
            ("bob", "viewer", BOB),
            ("carol", "operator", CAROL),
        ],
    )
    monkeypatch.setenv(auth.TOKENS_FILE_ENV, str(path))
    monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "0")
    for name in ("cpu", "memory", "process", "network"):
        monkeypatch.setattr(f"src.failures.{name}.inject_{name}", lambda *a, **k: None)
    agent_state.config = load_config()
    return path


@pytest.fixture
def client():
    return TestClient(app)


def call(client, token, method, path, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.request(method, path, headers=headers, **kwargs)


def events(event=None):
    return audit.recent(limit=500, event=event)


class TestWhatIsRecorded:
    def test_a_change_is_recorded_with_who_made_it(self, client):
        assert call(client, CAROL, "post", "/agent/stop").status_code == 400
        (e,) = events("api.request")
        assert (e["principal"], e["role"], e["auth"]) == (
            "carol",
            "operator",
            "token-file",
        )
        assert (e["method"], e["path"], e["status"]) == ("POST", "/agent/stop", 400)
        assert e["outcome"] == "error"
        assert e["client"]

    def test_reads_are_not_recorded(self, client):
        for path in ("/status", "/config", "/limits", "/kill-switch/status"):
            call(client, BOB, "get", path)
        assert events() == []

    def test_health_checks_are_not_recorded(self, client):
        client.get("/health")
        assert events() == []

    def test_a_wrong_token_is_recorded_as_anonymous_with_the_reason(self, client):
        call(client, generate_token(), "get", "/status")
        (e,) = events("auth.failed")
        assert e["principal"] == "anonymous" and e["role"] is None
        assert e["detail"]["reason"] == "invalid"
        assert e["status"] == 401 and e["outcome"] == "denied"

    def test_a_missing_token_is_recorded(self, client):
        call(client, None, "post", "/agent/stop")
        (e,) = events("auth.failed")
        assert e["detail"]["reason"] == "missing"

    def test_an_expired_token_names_its_owner_in_the_audit_log_only(self, client, env):
        write_tokens(env, [("old", "admin", ALICE, "2020-01-01")])
        response = call(client, ALICE, "get", "/status")
        (e,) = events("auth.failed")
        assert e["detail"] == {"reason": "expired", "owner": "old"}
        assert "old" not in response.text

    def test_a_lockout_is_recorded(self, client, monkeypatch):
        monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "2")
        for _ in range(3):
            call(client, generate_token(), "get", "/status")
        reasons = [e["detail"]["reason"] for e in reversed(events("auth.failed"))]
        assert reasons == ["invalid", "invalid", "locked_out"]
        assert events("auth.failed")[0]["status"] == 429

    def test_a_forbidden_request_names_the_user_and_the_role_needed(self, client):
        assert call(client, BOB, "post", "/agent/stop").status_code == 403
        (e,) = events("auth.forbidden")
        assert (e["principal"], e["role"], e["status"]) == ("bob", "viewer", 403)
        assert e["detail"]["required_role"] == "operator"

    def test_a_denied_read_is_recorded_too(self, client):
        call(client, None, "get", "/status")
        assert len(events("auth.failed")) == 1

    @pytest.mark.parametrize(
        "method, path, body",
        [
            ("post", "/agent/start", None),
            ("post", "/agent/stop", None),
            ("post", "/agent/restart", None),
            ("post", "/inject/manual", {"failure_type": "cpu", "dry_run": True}),
            ("post", "/inject/abort", None),
            ("patch", "/config", {"dry_run": True}),
            ("patch", "/config/failures/cpu", {"probability": 0.1}),
            ("post", "/config/reload", None),
            ("post", "/kill-switch/reset", None),
            ("post", "/metrics/reset", None),
        ],
    )
    def test_every_state_changing_endpoint_is_audited(self, client, method, path, body):
        call(client, ALICE, method, path, json=body)
        agent_state.engine.stop(timeout=5)
        recorded = [e for e in events("api.request") if e["path"] == path]
        assert len(recorded) >= 1, f"{method.upper()} {path} left no audit event"
        assert recorded[-1]["principal"] == "alice"


class TestDetail:
    def test_manual_injection_records_what_and_how_it_ended(self, client):
        call(
            client,
            CAROL,
            "post",
            "/inject/manual",
            json={"failure_type": "cpu", "dry_run": True},
        )
        request = events("api.request")[0]
        assert request["detail"] == {"failure_type": "cpu", "dry_run": True}
        started, completed = (
            events("injection.started")[0],
            events("injection.completed")[0],
        )
        for e in (started, completed):
            assert (e["principal"], e["role"], e["source"]) == (
                "carol",
                "operator",
                "manual",
            )
            assert e["failure_type"] == "cpu"
        assert started["dry_run"] is True

    def test_a_failed_manual_injection_is_recorded(self, client, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("tc exploded")

        monkeypatch.setattr("src.failures.cpu.inject_cpu", boom)
        call(
            client,
            CAROL,
            "post",
            "/inject/manual",
            json={"failure_type": "cpu", "dry_run": True},
        )
        (e,) = events("injection.failed")
        assert e["principal"] == "carol" and e["error"] == "tc exploded"

    def test_a_refused_manual_injection_is_still_an_api_request(self, client):
        call(
            client,
            CAROL,
            "post",
            "/inject/manual",
            json={"failure_type": "cpu", "dry_run": True},
        )
        assert events("injection.started")  # accepted
        assert (
            call(
                client, CAROL, "post", "/inject/manual", json={"failure_type": "nope"}
            ).status_code
            == 422
        )
        assert any(e["status"] == 422 for e in events("api.request"))

    def test_config_changes_are_recorded(self, client):
        call(client, ALICE, "patch", "/config", json={"interval_seconds": 42})
        (e,) = events("api.request")
        assert e["detail"]["changes"]["interval_seconds"] == 42

    def test_per_failure_config_changes_are_recorded(self, client):
        call(client, ALICE, "patch", "/config/failures/cpu", json={"probability": 0.25})
        (e,) = events("api.request")
        assert e["detail"] == {"failure_type": "cpu", "updates": {"probability": 0.25}}
        assert e["route"] == "/config/failures/{failure_type}"

    def test_start_records_the_kill_switch_choice(self, client):
        call(client, CAROL, "post", "/agent/start?enable_kill_switch=true")
        agent_state.engine.stop(timeout=5)
        e = [x for x in events("api.request") if x["path"] == "/agent/start"][0]
        assert e["detail"]["enable_kill_switch"] is True

    def test_a_safety_refusal_by_the_handler_is_not_a_role_failure(self, client):
        """Disabling the kill switch is refused by the agent's own rules (403),
        for anyone. It is an ordinary request that was denied, not a user whose
        role was too low."""
        response = call(client, ALICE, "post", "/agent/start?enable_kill_switch=false")
        assert response.status_code == 403
        assert not events("auth.forbidden")
        (e,) = events("api.request")
        assert (e["principal"], e["status"], e["outcome"]) == ("alice", 403, "denied")
        assert e["detail"]["enable_kill_switch"] is False

    def test_no_token_or_header_is_ever_recorded(self, client):
        call(client, ALICE, "patch", "/config", json={"interval_seconds": 42})
        call(client, CAROL, "post", "/agent/stop")
        call(client, BOB, "post", "/agent/stop")
        call(client, generate_token(), "get", "/status")
        dump = json.dumps(events())
        for secret in (ALICE, BOB, CAROL, "Bearer", "Authorization", "authorization"):
            assert secret not in dump


class TestAuthDisabled:
    def test_changes_are_recorded_as_anonymous(self, client, monkeypatch):
        monkeypatch.delenv(auth.TOKENS_FILE_ENV)
        monkeypatch.setenv(auth.AUTH_DISABLED_ENV, "true")
        client.post("/agent/stop")
        (e,) = events("api.request")
        assert (e["principal"], e["role"], e["auth"]) == (
            "anonymous",
            "admin",
            "auth-disabled",
        )


class TestSharedToken:
    def test_the_shared_token_is_named_as_such(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, "s" * 40)
        call(client, "s" * 40, "post", "/agent/stop")
        (e,) = events("api.request")
        assert (e["principal"], e["auth"]) == ("shared-token", "shared-token")


class TestReadingTheLog:
    def test_an_admin_can_read_it_newest_first(self, client):
        call(client, CAROL, "post", "/agent/stop")
        call(client, ALICE, "patch", "/config", json={"interval_seconds": 42})
        body = call(client, ALICE, "get", "/audit").json()
        assert body["count"] == len(body["events"])
        principals = [
            e["principal"] for e in body["events"] if e["event"] == "api.request"
        ]
        assert principals[:2] == ["alice", "carol"]

    @pytest.mark.parametrize("token", [BOB, CAROL])
    def test_nobody_below_admin_can_read_it(self, client, token):
        assert call(client, token, "get", "/audit").status_code == 403

    def test_it_needs_a_token(self, client):
        assert call(client, None, "get", "/audit").status_code == 401

    def test_reading_it_is_itself_recorded(self, client):
        call(client, ALICE, "get", "/audit")
        (e,) = events("audit.read")
        assert e["principal"] == "alice"

    def test_a_refused_read_is_recorded_as_forbidden(self, client):
        call(client, BOB, "get", "/audit")
        assert len(events("auth.forbidden")) == 1 and not events("audit.read")

    def test_filters(self, client):
        call(client, CAROL, "post", "/agent/stop")
        call(client, ALICE, "post", "/agent/stop")
        events_ = call(client, ALICE, "get", "/audit?principal=carol").json()["events"]
        assert events_ and {e["principal"] for e in events_} == {"carol"}
        by_type = call(client, ALICE, "get", "/audit?event=api.request").json()[
            "events"
        ]
        assert {e["event"] for e in by_type} == {"api.request"}

    @pytest.mark.parametrize("limit", [0, -1, 501])
    def test_limit_is_bounded(self, client, limit):
        assert call(client, ALICE, "get", f"/audit?limit={limit}").status_code == 422

    def test_limit(self, client):
        for _ in range(5):
            call(client, CAROL, "post", "/agent/stop")
        assert len(call(client, ALICE, "get", "/audit?limit=2").json()["events"]) == 2

    def test_events_reach_the_file_too(self, client, tmp_path):
        path = tmp_path / "audit.log"
        audit.configure_file(str(path))
        call(client, CAROL, "post", "/agent/stop")
        recorded = [json.loads(line) for line in path.read_text().splitlines()]
        assert recorded[-1]["principal"] == "carol"
        assert recorded[-1]["path"] == "/agent/stop"


class TestAnnotate:
    def test_annotations_merge(self):
        from types import SimpleNamespace

        request = SimpleNamespace(state=SimpleNamespace())
        annotate(request, a=1)
        annotate(request, b=2)
        assert request.state.audit_detail == {"a": 1, "b": 2}


# ------------------------------------------------------------------- scheduler


class TestSchedulerInjections:
    def run_tick(self, injector):
        engine = ChaosEngine(lambda: None, injector=injector)
        settings = Settings(
            1,
            False,
            {"cpu": {"enabled": True, "probability": 1.0}},
        )
        engine.run_tick(settings, 1)
        engine.stop(timeout=5)

    def test_the_agents_own_injections_are_attributed_to_the_scheduler(self):
        self.run_tick(lambda name, cfg, dry: None)
        started, completed = (
            events("injection.started")[0],
            events("injection.completed")[0],
        )
        for e in (started, completed):
            assert (e["principal"], e["source"], e["failure_type"]) == (
                "scheduler",
                "scheduler",
                "cpu",
            )
        assert started["correlation_id"].startswith("iter-1-")

    def test_a_failed_scheduled_injection_is_recorded(self):
        def boom(name, cfg, dry):
            raise RuntimeError("no tc")

        self.run_tick(boom)
        (e,) = events("injection.failed")
        assert e["principal"] == "scheduler" and e["error"] == "no tc"
