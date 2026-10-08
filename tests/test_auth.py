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
