"""Signal handlers wake and clean up in-flight injections before exiting."""

import logging
import signal

import pytest
import uvicorn

from src import agent, api_server
from src.lifecycle import lifecycle


class TestAgentSignalHandler:
    @pytest.fixture(autouse=True)
    def logger(self, monkeypatch):
        monkeypatch.setattr(
            agent, "logger", logging.getLogger("test-agent"), raising=False
        )

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
    def test_cleans_up_then_exits_zero(self, sig, monkeypatch):
        ran = []
        monkeypatch.setattr(lifecycle, "run_cleanups", lambda: ran.append(1))
        ticket, _ = lifecycle.begin("network")

        with pytest.raises(SystemExit) as exc:
            agent.signal_handler(sig, None)

        assert exc.value.code == 0
        assert ran, "cleanups were not run"
        assert lifecycle.is_shutting_down()
        assert ticket.interrupted

    def test_cleanup_on_exit_runs_registered_cleanups(self, monkeypatch):
        ran = []
        monkeypatch.setattr(lifecycle, "run_cleanups", lambda: ran.append(1))
        agent.cleanup_on_exit()
        assert ran == [1]


class TestApiServerSignalHandling:
    def make_server(self):
        return api_server.ChaosServer(uvicorn.Config("src.api:app"))

    def test_handle_exit_cleans_up_before_uvicorn_proceeds(self, monkeypatch):
        order = []
        monkeypatch.setattr(lifecycle, "run_cleanups", lambda: order.append("cleanup"))
        server = self.make_server()
        original = uvicorn.Server.handle_exit

        def spy(self_, sig, frame):
            order.append("uvicorn")
            return original(self_, sig, frame)

        monkeypatch.setattr(uvicorn.Server, "handle_exit", spy)
        ticket, _ = lifecycle.begin("network")

        server.handle_exit(signal.SIGTERM, None)

        assert order == ["cleanup", "uvicorn"]
        assert server.should_exit is True
        assert ticket.interrupted

    def test_second_ctrl_c_still_force_exits(self):
        """Our handler must not break uvicorn's own escalation on repeat SIGINT."""
        server = self.make_server()
        server.handle_exit(signal.SIGINT, None)
        server.handle_exit(signal.SIGINT, None)
        assert server.should_exit is True
        assert server.force_exit is True

    def test_main_uses_chaos_server_and_installs_atexit(self, monkeypatch):
        installed = []
        started = []
        monkeypatch.setattr(lifecycle, "install_atexit", lambda: installed.append(1))
        monkeypatch.setattr(api_server, "start_metrics_server", lambda port: None)
        monkeypatch.setattr(
            api_server.ChaosServer, "run", lambda self: started.append(type(self))
        )
        monkeypatch.setattr("sys.argv", ["api_server", "--insecure-no-auth"])
        monkeypatch.delenv("CHAOS_API_TOKEN", raising=False)
        api_server.main()
        monkeypatch.delenv("CHAOS_API_AUTH_DISABLED", raising=False)
        assert installed == [1]
        assert started == [api_server.ChaosServer]

    def test_reload_mode_falls_back_to_plain_uvicorn(self, monkeypatch):
        calls = []
        monkeypatch.setattr(api_server, "start_metrics_server", lambda port: None)
        monkeypatch.setattr(
            api_server.uvicorn, "run", lambda app, **kw: calls.append(kw)
        )
        monkeypatch.setattr(
            "sys.argv", ["api_server", "--insecure-no-auth", "--reload"]
        )
        api_server.main()
        monkeypatch.delenv("CHAOS_API_AUTH_DISABLED", raising=False)
        assert calls and calls[0]["reload"] is True
