"""Per-user tokens and roles on the API."""

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src import auth
from src.api import agent_state, app
from src.config import load_config
from src.tokens import generate_token, hash_token

ALICE, BOB, CAROL = generate_token(), generate_token(), generate_token()
SHARED = "s" * 40


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
    monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "0")  # not testing lockout here
    agent_state.config = load_config()
    return path


@pytest.fixture
def client():
    return TestClient(app)


def call(client, token, method, path, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.request(method, path, headers=headers, **kwargs)


# (method, path, body, minimum role)
READ = [
    ("get", "/status", None),
    ("get", "/kill-switch/status", None),
    ("get", "/limits", None),
    ("get", "/config", None),
    ("get", "/config/failures/cpu", None),
    ("get", "/metrics/summary", None),
]
OPERATE = [
    ("post", "/agent/stop", None),
    ("post", "/agent/restart", None),
    ("post", "/inject/abort", None),
    ("post", "/inject/manual", {"failure_type": "cpu", "dry_run": True}),
]
ADMIN_ONLY = [
    ("patch", "/config", {"dry_run": True}),
    ("patch", "/config/failures/cpu", {"probability": 0.1}),
    ("post", "/config/reload", None),
    ("post", "/kill-switch/reset", None),
    ("post", "/metrics/reset", None),
]


def ok_or_other_error(response):
    """Allowed: got past authentication and authorization (any other status)."""
    return response.status_code not in (401, 403)


class TestViewer:
    @pytest.mark.parametrize("method, path, body", READ)
    def test_can_read(self, client, method, path, body):
        assert ok_or_other_error(call(client, BOB, method, path, json=body))

    @pytest.mark.parametrize("method, path, body", OPERATE + ADMIN_ONLY)
    def test_cannot_change_anything(self, client, method, path, body):
        assert call(client, BOB, method, path, json=body).status_code == 403


class TestOperator:
    @pytest.mark.parametrize("method, path, body", READ + OPERATE)
    def test_can_read_and_run_chaos(self, client, method, path, body):
        assert ok_or_other_error(call(client, CAROL, method, path, json=body))

    @pytest.mark.parametrize("method, path, body", ADMIN_ONLY)
    def test_cannot_change_configuration_or_safety(self, client, method, path, body):
        assert call(client, CAROL, method, path, json=body).status_code == 403


class TestAdmin:
    @pytest.mark.parametrize("method, path, body", READ + OPERATE + ADMIN_ONLY)
    def test_can_do_everything(self, client, method, path, body):
        assert ok_or_other_error(call(client, ALICE, method, path, json=body))


class TestForbiddenIsNotUnauthenticated:
    def test_403_says_which_role_is_needed(self, client):
        response = call(client, BOB, "post", "/agent/stop")
        assert response.status_code == 403
        assert "'operator'" in response.json()["detail"]

    def test_403_is_not_counted_as_a_failed_login(self, client, monkeypatch):
        """A valid user without permission is not an attacker."""
        monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "3")
        codes = {
            call(client, BOB, "post", "/agent/stop").status_code for _ in range(10)
        }
        assert codes == {403}
        assert call(client, BOB, "get", "/status").status_code == 200


class TestAuthentication:
    def test_unknown_token(self, client):
        assert call(client, generate_token(), "get", "/status").status_code == 401

    def test_no_token(self, client):
        assert call(client, None, "get", "/status").status_code == 401

    def test_the_hash_from_the_file_is_not_a_credential(self, client):
        assert call(client, hash_token(ALICE), "get", "/status").status_code == 401

    def test_health_stays_public(self, client):
        assert client.get("/health").status_code == 200

    def test_an_expired_token_is_refused(self, client, env):
        write_tokens(env, [("old", "admin", ALICE, "2020-01-01")])
        response = call(client, ALICE, "get", "/status")
        assert response.status_code == 401
        assert "expired" not in response.text  # the caller is not told why

    def test_expired_tokens_are_counted_separately_in_metrics(self, client, env):
        from src.metrics import AUTH_FAILURES_TOTAL, metric_value

        write_tokens(env, [("old", "admin", ALICE, "2020-01-01")])
        before = metric_value(AUTH_FAILURES_TOTAL, surface="api", reason="expired")
        call(client, ALICE, "get", "/status")
        after = metric_value(AUTH_FAILURES_TOTAL, surface="api", reason="expired")
        assert after == before + 1

    def test_revoking_a_token_takes_effect_without_a_restart(self, client, env):
        import os
        import time

        assert call(client, BOB, "get", "/status").status_code == 200
        write_tokens(env, [("alice", "admin", ALICE)])
        os.utime(env, (time.time() + 10, time.time() + 10))
        assert call(client, BOB, "get", "/status").status_code == 401
        assert call(client, ALICE, "get", "/status").status_code == 200

    def test_a_broken_file_fails_closed(self, client, env):
        import os
        import time

        assert call(client, ALICE, "get", "/status").status_code == 200
        env.write_text("tokens: [")
        os.utime(env, (time.time() + 10, time.time() + 10))
        assert call(client, ALICE, "get", "/status").status_code == 503

    def test_a_missing_file_fails_closed(self, client, env):
        env.unlink()
        assert call(client, ALICE, "get", "/status").status_code == 503


class TestSharedTokenStillWorks:
    def test_shared_token_alone_is_admin(self, client, monkeypatch):
        monkeypatch.delenv(auth.TOKENS_FILE_ENV)
        monkeypatch.setenv(auth.TOKEN_ENV, SHARED)
        for method, path, body in READ + OPERATE + ADMIN_ONLY:
            assert ok_or_other_error(call(client, SHARED, method, path, json=body))

    def test_both_sources_can_be_used_together(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, SHARED)
        assert call(client, SHARED, "patch", "/config", json={}).status_code != 403
        assert call(client, BOB, "get", "/status").status_code == 200
        assert call(client, BOB, "patch", "/config", json={}).status_code == 403

    def test_a_wrong_token_is_refused_when_both_are_configured(
        self, client, monkeypatch
    ):
        monkeypatch.setenv(auth.TOKEN_ENV, SHARED)
        assert call(client, "x" * 40, "get", "/status").status_code == 401


class TestRolePolicy:
    def test_every_route_has_an_explicit_role(self):
        """A route added without a decision must fail this test, not just be
        quietly locked to admin."""
        missing = []
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if path in auth.PUBLIC_PATHS:
                continue
            for method in methods - {"HEAD", "OPTIONS"}:
                if (method, path) not in auth.ROLE_POLICY:
                    missing.append((method, path))
        assert missing == []

    def test_the_policy_names_only_real_routes(self):
        real = {
            (m, r.path)
            for r in app.routes
            for m in (getattr(r, "methods", None) or set())
        }
        stale = [
            key
            for key in auth.ROLE_POLICY
            if key not in real
            and not key[1].startswith(("/docs", "/redoc", "/openapi"))
        ]
        assert stale == []

    def test_unlisted_routes_need_admin(self):
        assert auth.required_role("GET", "/brand-new") == "admin"
        assert auth.required_role("DELETE", "/status") == "admin"

    def test_methods_are_case_insensitive(self):
        assert auth.required_role("get", "/status") == "viewer"

    def test_the_write_endpoints_are_never_viewer_accessible(self):
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
                if route.path in auth.PUBLIC_PATHS:
                    continue
                assert auth.required_role(method, route.path) != "viewer", route.path


class TestStartup:
    def test_a_tokens_file_alone_is_enough(self, monkeypatch):
        auth.validate_startup("0.0.0.0", insecure_no_auth=False)

    def test_a_bad_tokens_file_stops_startup(self, env):
        env.write_text("tokens: []")
        with pytest.raises(SystemExit, match="empty"):
            auth.validate_startup("0.0.0.0", insecure_no_auth=False)

    def test_a_missing_tokens_file_stops_startup(self, env):
        env.unlink()
        with pytest.raises(SystemExit, match="cannot read"):
            auth.validate_startup("0.0.0.0", insecure_no_auth=False)

    def test_no_credentials_at_all_stops_startup_and_names_the_options(
        self, monkeypatch
    ):
        monkeypatch.delenv(auth.TOKENS_FILE_ENV)
        with pytest.raises(SystemExit) as exc:
            auth.validate_startup("0.0.0.0", insecure_no_auth=False)
        assert auth.TOKENS_FILE_ENV in str(exc.value)
        assert auth.TOKEN_ENV in str(exc.value)

    def test_disabled_auth_gives_everyone_admin_on_loopback(self, client, monkeypatch):
        monkeypatch.setenv(auth.AUTH_DISABLED_ENV, "true")
        response = client.patch("/config", json={})
        assert response.status_code not in (401, 403)
