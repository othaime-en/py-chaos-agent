"""Tests for API authentication and startup validation."""

import pytest
from fastapi.testclient import TestClient

from src import auth
from src.api import app, agent_state
from src.config import load_config

TOKEN = "a" * 40


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        auth.TOKEN_ENV,
        auth.TOKEN_FILE_ENV,
        auth.AUTH_DISABLED_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    agent_state.config = load_config()
    yield


@pytest.fixture
def client():
    return TestClient(app)


PROTECTED = [
    ("get", "/status"),
    ("get", "/config"),
    ("get", "/kill-switch/status"),
    ("get", "/metrics/summary"),
    ("post", "/agent/start"),
    ("post", "/agent/stop"),
    ("post", "/metrics/reset"),
    ("post", "/config/reload"),
    ("post", "/inject/manual"),
    ("patch", "/config"),
]


class TestPublicEndpoints:
    def test_health_is_public(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        assert client.get("/health").status_code == 200

    def test_root_is_public(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        assert client.get("/").status_code == 200

    def test_health_public_even_with_no_token_configured(self, client):
        assert client.get("/health").status_code == 200


class TestProtectedEndpoints:
    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_missing_token_is_401(self, client, monkeypatch, method, path):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        response = getattr(client, method)(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_wrong_token_is_401(self, client, monkeypatch, method, path):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        response = getattr(client, method)(
            path, headers={"Authorization": "Bearer " + "b" * 40}
        )
        assert response.status_code == 401

    def test_wrong_scheme_is_401(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        response = client.get("/status", headers={"Authorization": f"Basic {TOKEN}"})
        assert response.status_code == 401

    def test_empty_bearer_is_401(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        response = client.get("/status", headers={"Authorization": "Bearer "})
        assert response.status_code == 401

    def test_non_ascii_token_is_401_not_500(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        response = client.get(
            "/status", headers={"Authorization": "Bearer caf\u00e9".encode("utf-8")}
        )
        assert response.status_code == 401

    def test_valid_token_is_allowed(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        response = client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200

    def test_scheme_is_case_insensitive(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        response = client.get("/status", headers={"Authorization": f"bearer {TOKEN}"})
        assert response.status_code == 200

    def test_token_not_leaked_in_error_body(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        response = client.get("/status", headers={"Authorization": "Bearer nope"})
        assert TOKEN not in response.text


class TestFailClosed:
    def test_no_token_configured_is_503(self, client):
        assert client.get("/status").status_code == 503

    def test_short_token_is_503(self, client, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, "short")
        response = client.get("/status", headers={"Authorization": "Bearer short"})
        assert response.status_code == 503

    def test_unreadable_token_file_is_503(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv(auth.TOKEN_FILE_ENV, str(tmp_path / "missing"))
        assert client.get("/status").status_code == 503


class TestTokenSources:
    def test_token_file_is_used_and_stripped(self, client, monkeypatch, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text(TOKEN + "\n")
        monkeypatch.setenv(auth.TOKEN_FILE_ENV, str(token_file))
        response = client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200

    def test_token_file_wins_over_env(self, client, monkeypatch, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("f" * 40)
        monkeypatch.setenv(auth.TOKEN_FILE_ENV, str(token_file))
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        ok = client.get("/status", headers={"Authorization": "Bearer " + "f" * 40})
        bad = client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200
        assert bad.status_code == 401

    def test_rotation_takes_effect_without_restart(self, client, monkeypatch, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text(TOKEN)
        monkeypatch.setenv(auth.TOKEN_FILE_ENV, str(token_file))
        old = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/status", headers=old).status_code == 200

        token_file.write_text("r" * 40)
        assert client.get("/status", headers=old).status_code == 401
        new = {"Authorization": "Bearer " + "r" * 40}
        assert client.get("/status", headers=new).status_code == 200


class TestAuthDisabled:
    def test_explicit_disable_allows_requests(self, client, monkeypatch):
        monkeypatch.setenv(auth.AUTH_DISABLED_ENV, "true")
        assert client.get("/status").status_code == 200

    @pytest.mark.parametrize("value", ["", "false", "0", "no", "nope"])
    def test_non_truthy_values_do_not_disable(self, client, monkeypatch, value):
        monkeypatch.setenv(auth.AUTH_DISABLED_ENV, value)
        assert client.get("/status").status_code == 503


class TestDocsDisabledByDefault:
    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    def test_docs_not_served(self, client, path):
        assert client.get(path).status_code == 404


class TestValidateStartup:
    def test_requires_token(self):
        with pytest.raises(SystemExit, match="no API token"):
            auth.validate_startup("127.0.0.1", insecure_no_auth=False)

    def test_accepts_token_on_any_host(self, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        auth.validate_startup("0.0.0.0", insecure_no_auth=False)

    def test_rejects_short_token(self, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, "short")
        with pytest.raises(SystemExit, match="too short"):
            auth.validate_startup("127.0.0.1", insecure_no_auth=False)

    def test_insecure_allowed_on_loopback(self):
        auth.validate_startup("127.0.0.1", insecure_no_auth=True)

    def test_insecure_rejected_on_public_bind(self):
        with pytest.raises(SystemExit, match="non-loopback"):
            auth.validate_startup("0.0.0.0", insecure_no_auth=True)

    def test_env_disable_rejected_on_public_bind(self, monkeypatch):
        monkeypatch.setenv(auth.AUTH_DISABLED_ENV, "true")
        with pytest.raises(SystemExit, match="non-loopback"):
            auth.validate_startup("0.0.0.0", insecure_no_auth=False)


class TestLockout:
    """Repeated failed authentication locks the client out."""

    @pytest.fixture(autouse=True)
    def throttle_env(self, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "3")
        monkeypatch.setenv("CHAOS_AUTH_LOCKOUT_SECONDS", "300")

    def bad(self, client, n):
        return [
            client.get("/status", headers={"Authorization": "Bearer wrong"})
            for _ in range(n)
        ]

    def test_failures_below_the_limit_are_plain_401s(self, client):
        assert [r.status_code for r in self.bad(client, 3)] == [401, 401, 401]

    def test_the_next_request_is_locked_out(self, client):
        self.bad(client, 3)
        response = client.get("/status", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 429
        assert 1 <= int(response.headers["Retry-After"]) <= 300

    def test_a_correct_token_is_refused_during_the_lockout(self, client):
        """Otherwise a correct guess could be confirmed."""
        self.bad(client, 3)
        response = client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 429

    def test_missing_tokens_count_too(self, client):
        for _ in range(3):
            assert client.get("/status").status_code == 401
        assert client.get("/status").status_code == 429

    def test_a_good_login_resets_the_count(self, client):
        self.bad(client, 2)
        ok = client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200
        assert [r.status_code for r in self.bad(client, 2)] == [401, 401]

    def test_public_endpoints_stay_available_during_a_lockout(self, client):
        """Kubernetes probes must not be locked out."""
        self.bad(client, 3)
        assert client.get("/health").status_code == 200

    def test_it_can_be_turned_off(self, client, monkeypatch):
        monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "0")
        assert {r.status_code for r in self.bad(client, 20)} == {401}

    def test_lockout_ends(self, client, monkeypatch):
        from src import ratelimit

        now = [1000.0]
        monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
        self.bad(client, 3)
        assert self.bad(client, 1)[0].status_code == 429
        now[0] += 301
        ok = client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200

    def test_failures_and_lockouts_are_counted_in_metrics(self, client):
        from src.metrics import AUTH_FAILURES_TOTAL, AUTH_LOCKOUTS_TOTAL, metric_value

        before_invalid = metric_value(
            AUTH_FAILURES_TOTAL, surface="api", reason="invalid"
        )
        before_locked = metric_value(
            AUTH_FAILURES_TOTAL, surface="api", reason="locked_out"
        )
        before_lockouts = metric_value(AUTH_LOCKOUTS_TOTAL, surface="api")
        self.bad(client, 4)  # 3 invalid, then 1 refused
        assert (
            metric_value(AUTH_FAILURES_TOTAL, surface="api", reason="invalid")
            == before_invalid + 3
        )
        assert (
            metric_value(AUTH_FAILURES_TOTAL, surface="api", reason="locked_out")
            == before_locked + 1
        )
        assert metric_value(AUTH_LOCKOUTS_TOTAL, surface="api") == before_lockouts + 1

    def test_invalid_throttle_settings_stop_startup(self, monkeypatch):
        monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "lots")
        with pytest.raises(SystemExit, match="CHAOS_AUTH_MAX_FAILURES"):
            auth.validate_startup("127.0.0.1", insecure_no_auth=False)

    def test_invalid_throttle_settings_fail_closed_at_request_time(
        self, client, monkeypatch
    ):
        monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "lots")
        response = client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 503
