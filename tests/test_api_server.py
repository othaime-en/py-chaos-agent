"""Tests for the API server entrypoint's safe-by-default behavior."""

import os

import pytest

from src import api_server, auth

TOKEN = "s" * 40


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        auth.TOKEN_ENV,
        auth.TOKEN_FILE_ENV,
        auth.AUTH_DISABLED_ENV,
        "CHAOS_API_HOST",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def captured(monkeypatch):
    """Stub out the network side effects and capture uvicorn's arguments."""
    calls = {}
    monkeypatch.setattr(api_server, "start_metrics_server", lambda port: None)

    def fake_run(self):
        calls.update(host=self.config.host, port=self.config.port)

    monkeypatch.setattr(api_server.ChaosServer, "run", fake_run)
    return calls


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["api_server", *argv])
    api_server.main()


def test_default_host_is_loopback(monkeypatch, captured):
    monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
    run(monkeypatch)
    assert captured["host"] == "127.0.0.1"


def test_host_can_come_from_env(monkeypatch, captured):
    monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("CHAOS_API_HOST", "0.0.0.0")
    run(monkeypatch)
    assert captured["host"] == "0.0.0.0"


def test_refuses_to_start_without_token(monkeypatch, captured):
    with pytest.raises(SystemExit, match="no API token"):
        run(monkeypatch)
    assert captured == {}


def test_refuses_public_bind_without_token(monkeypatch, captured):
    with pytest.raises(SystemExit):
        run(monkeypatch, "--host", "0.0.0.0")
    assert captured == {}


def test_insecure_flag_allowed_on_loopback(monkeypatch, captured):
    run(monkeypatch, "--insecure-no-auth")
    assert captured["host"] == "127.0.0.1"
    assert os.environ[auth.AUTH_DISABLED_ENV] == "true"
    monkeypatch.delenv(auth.AUTH_DISABLED_ENV)


def test_insecure_flag_rejected_on_public_bind(monkeypatch, captured):
    with pytest.raises(SystemExit, match="non-loopback"):
        run(monkeypatch, "--host", "0.0.0.0", "--insecure-no-auth")
    assert captured == {}
