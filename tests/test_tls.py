"""TLS for the control API."""

import http.client
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from src import api_server, auth, tls
from src.api import agent_state, app
from src.config import load_config
from src.tls import TlsError, TlsSettings

TOKEN = "t" * 40
TLS_ENV = (tls.CERT_ENV, tls.KEY_ENV, tls.CLIENT_CA_ENV, tls.REQUIRE_TLS_ENV)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in TLS_ENV + (
        auth.TOKEN_ENV,
        auth.TOKEN_FILE_ENV,
        auth.TOKENS_FILE_ENV,
        auth.AUTH_DISABLED_ENV,
        "CHAOS_API_HOST",
    ):
        # set-then-delete so the original value is restored afterwards, even for
        # variables that api_server.main() sets directly in os.environ
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "0")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------- settings


class TestSettings:
    def test_nothing_configured_means_no_tls(self):
        assert tls.settings_from_env() is None

    def test_cert_and_key(self, monkeypatch):
        monkeypatch.setenv(tls.CERT_ENV, "/c.pem")
        monkeypatch.setenv(tls.KEY_ENV, "/k.pem")
        assert tls.settings_from_env() == TlsSettings("/c.pem", "/k.pem", None)

    def test_client_ca_makes_it_mutual(self, monkeypatch):
        monkeypatch.setenv(tls.CERT_ENV, "/c.pem")
        monkeypatch.setenv(tls.KEY_ENV, "/k.pem")
        monkeypatch.setenv(tls.CLIENT_CA_ENV, "/ca.pem")
        s = tls.settings_from_env()
        assert s.client_ca_file == "/ca.pem" and s.mutual

    def test_flags_override_the_environment(self, monkeypatch):
        monkeypatch.setenv(tls.CERT_ENV, "/env.pem")
        monkeypatch.setenv(tls.KEY_ENV, "/env.key")
        s = tls.settings_from_env("/flag.pem", "/flag.key")
        assert (s.cert_file, s.key_file) == ("/flag.pem", "/flag.key")

    def test_a_flag_can_complete_the_environment(self, monkeypatch):
        monkeypatch.setenv(tls.CERT_ENV, "/c.pem")
        assert tls.settings_from_env(key="/k.pem") == TlsSettings("/c.pem", "/k.pem")

    def test_whitespace_is_ignored(self, monkeypatch):
        monkeypatch.setenv(tls.CERT_ENV, "  /c.pem \n")
        monkeypatch.setenv(tls.KEY_ENV, " /k.pem ")
        assert tls.settings_from_env().cert_file == "/c.pem"

    def test_a_certificate_without_a_key_is_an_error(self, monkeypatch):
        monkeypatch.setenv(tls.CERT_ENV, "/c.pem")
        with pytest.raises(TlsError, match=tls.KEY_ENV):
            tls.settings_from_env()

    def test_a_key_without_a_certificate_is_an_error(self, monkeypatch):
        monkeypatch.setenv(tls.KEY_ENV, "/k.pem")
        with pytest.raises(TlsError, match=tls.CERT_ENV):
            tls.settings_from_env()

    def test_a_client_ca_without_tls_is_an_error(self, monkeypatch):
        """Silently ignoring it would leave the API open to any client."""
        monkeypatch.setenv(tls.CLIENT_CA_ENV, "/ca.pem")
        with pytest.raises(TlsError, match="TLS is not"):
            tls.settings_from_env()


class TestContext:
    def test_a_good_pair_loads(self, tls_certs):
        context = tls.build_server_context(TlsSettings(tls_certs.cert, tls_certs.key))
        assert context.verify_mode == ssl.CERT_NONE

    def test_the_minimum_version_is_tls_1_2(self, tls_certs):
        context = tls.build_server_context(TlsSettings(tls_certs.cert, tls_certs.key))
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2

    def test_a_client_ca_requires_client_certificates(self, tls_certs):
        context = tls.build_server_context(
            TlsSettings(tls_certs.cert, tls_certs.key, tls_certs.ca)
        )
        assert context.verify_mode == ssl.CERT_REQUIRED

    def test_a_key_that_does_not_match_is_a_readable_error(self, tls_certs):
        with pytest.raises(TlsError, match="belongs to the certificate"):
            tls.build_server_context(TlsSettings(tls_certs.cert, tls_certs.other_key))

    def test_a_missing_certificate_names_the_file(self, tls_certs, tmp_path):
        missing = str(tmp_path / "nope.pem")
        with pytest.raises(TlsError, match="nope.pem"):
            tls.build_server_context(TlsSettings(missing, tls_certs.key))

    def test_garbage_is_a_readable_error(self, tls_certs, tmp_path):
        junk = tmp_path / "junk.pem"
        junk.write_text("not a certificate")
        with pytest.raises(TlsError, match="PEM"):
            tls.build_server_context(TlsSettings(str(junk), tls_certs.key))

    def test_a_missing_client_ca_names_the_file(self, tls_certs, tmp_path):
        missing = str(tmp_path / "no-ca.pem")
        with pytest.raises(TlsError, match="no-ca.pem"):
            tls.build_server_context(
                TlsSettings(tls_certs.cert, tls_certs.key, missing)
            )

    def test_a_bad_client_ca_is_a_readable_error(self, tls_certs, tmp_path):
        junk = tmp_path / "ca.pem"
        junk.write_text("nope")
        with pytest.raises(TlsError, match="client CA"):
            tls.build_server_context(
                TlsSettings(tls_certs.cert, tls_certs.key, str(junk))
            )


class TestUvicornOptions:
    def test_none_means_no_options(self):
        assert tls.uvicorn_options(None) == {}

    def test_cert_and_key(self):
        assert tls.uvicorn_options(TlsSettings("/c", "/k")) == {
            "ssl_certfile": "/c",
            "ssl_keyfile": "/k",
        }

    def test_mutual_tls_demands_a_client_certificate(self):
        options = tls.uvicorn_options(TlsSettings("/c", "/k", "/ca"))
        assert options["ssl_ca_certs"] == "/ca"
        assert options["ssl_cert_reqs"] == ssl.CERT_REQUIRED


class TestStartupPolicy:
    def test_loopback_without_tls_is_fine_and_quiet(self, caplog):
        tls.validate_startup("127.0.0.1", None)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_non_loopback_without_tls_warns(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            tls.validate_startup("0.0.0.0", None)
        assert any("WITHOUT TLS" in r.getMessage() for r in caplog.records)

    def test_require_tls_refuses_non_loopback_without_tls(self, monkeypatch):
        monkeypatch.setenv(tls.REQUIRE_TLS_ENV, "true")
        with pytest.raises(SystemExit, match="without TLS"):
            tls.validate_startup("0.0.0.0", None)

    def test_require_tls_is_satisfied_by_tls(self, monkeypatch, tls_certs):
        monkeypatch.setenv(tls.REQUIRE_TLS_ENV, "true")
        tls.validate_startup("0.0.0.0", TlsSettings(tls_certs.cert, tls_certs.key))

    def test_require_tls_does_not_apply_on_loopback(self, monkeypatch):
        monkeypatch.setenv(tls.REQUIRE_TLS_ENV, "true")
        tls.validate_startup("127.0.0.1", None)

    def test_an_unusable_certificate_stops_startup(self, tls_certs):
        with pytest.raises(SystemExit, match="Refusing to start"):
            tls.validate_startup(
                "127.0.0.1", TlsSettings(tls_certs.cert, tls_certs.other_key)
            )


# ----------------------------------------------------------- the entrypoint


@pytest.fixture
def captured(monkeypatch):
    calls = {}
    monkeypatch.setattr(api_server, "start_metrics_server", lambda port: None)

    def fake_run(self):
        c = self.config
        calls.update(
            host=c.host,
            certfile=c.ssl_certfile,
            keyfile=c.ssl_keyfile,
            ca=c.ssl_ca_certs,
            reqs=c.ssl_cert_reqs,
        )

    monkeypatch.setattr(api_server.ChaosServer, "run", fake_run)
    return calls


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["api_server", *argv])
    api_server.main()


class TestEntrypoint:
    def test_flags_turn_on_https(self, monkeypatch, captured, tls_certs):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        run(monkeypatch, "--tls-cert", tls_certs.cert, "--tls-key", tls_certs.key)
        assert captured["certfile"] == tls_certs.cert
        assert captured["keyfile"] == tls_certs.key
        assert captured["ca"] is None

    def test_the_environment_turns_on_https(self, monkeypatch, captured, tls_certs):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        monkeypatch.setenv(tls.CERT_ENV, tls_certs.cert)
        monkeypatch.setenv(tls.KEY_ENV, tls_certs.key)
        run(monkeypatch)
        assert captured["certfile"] == tls_certs.cert

    def test_a_client_ca_demands_client_certificates(
        self, monkeypatch, captured, tls_certs
    ):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        run(
            monkeypatch,
            "--tls-cert", tls_certs.cert,
            "--tls-key", tls_certs.key,
            "--tls-client-ca", tls_certs.ca,
        )  # fmt: skip
        assert captured["ca"] == tls_certs.ca
        assert captured["reqs"] == ssl.CERT_REQUIRED

    def test_flags_are_passed_on_for_the_metrics_server(
        self, monkeypatch, captured, tls_certs
    ):
        import os

        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        run(monkeypatch, "--tls-cert", tls_certs.cert, "--tls-key", tls_certs.key)
        assert os.environ[tls.CERT_ENV] == tls_certs.cert
        assert os.environ[tls.KEY_ENV] == tls_certs.key

    def test_without_tls_nothing_is_set(self, monkeypatch, captured):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        run(monkeypatch)
        assert captured["certfile"] is None

    def test_a_half_configured_tls_refuses_to_start(
        self, monkeypatch, captured, tls_certs
    ):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        with pytest.raises(SystemExit, match="needs both"):
            run(monkeypatch, "--tls-cert", tls_certs.cert)

    def test_a_bad_certificate_refuses_to_start(self, monkeypatch, captured, tls_certs):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        with pytest.raises(SystemExit, match="Refusing to start"):
            run(
                monkeypatch,
                "--tls-cert",
                tls_certs.cert,
                "--tls-key",
                tls_certs.other_key,
            )
        assert captured == {}

    def test_require_tls_refuses_a_plain_public_listener(self, monkeypatch, captured):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        monkeypatch.setenv(tls.REQUIRE_TLS_ENV, "true")
        with pytest.raises(SystemExit, match="without TLS"):
            run(monkeypatch, "--host", "0.0.0.0")
        assert captured == {}


# ----------------------------------------------------------- a live handshake


class LiveServer:
    def __init__(self, settings):
        self.port = free_port()
        agent_state.config = load_config()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="off",  # no kill switch, no auto-started chaos loop
            timeout_graceful_shutdown=1,  # a test may leave a connection open
            timeout_keep_alive=1,
            **tls.uvicorn_options(settings),
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.server.started, "server did not start"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def get(self, path, context, token=None):
        request = urllib.request.Request(f"https://127.0.0.1:{self.port}{path}")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        return urllib.request.urlopen(request, context=context, timeout=5)


def trusting(tls_certs, client_cert=False):
    context = ssl.create_default_context(cafile=tls_certs.ca)
    if client_cert:
        context.load_cert_chain(tls_certs.client_cert, tls_certs.client_key)
    return context


class TestLiveTls:
    @pytest.fixture
    def secured(self, monkeypatch, tls_certs):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        with LiveServer(TlsSettings(tls_certs.cert, tls_certs.key)) as server:
            yield server

    def test_https_works_for_a_client_that_trusts_the_ca(self, secured, tls_certs):
        assert secured.get("/health", trusting(tls_certs)).status == 200

    def test_authentication_still_applies_over_tls(self, secured, tls_certs):
        ctx = trusting(tls_certs)
        with pytest.raises(urllib.error.HTTPError) as exc:
            secured.get("/status", ctx)
        assert exc.value.code == 401
        assert secured.get("/status", ctx, token=TOKEN).status == 200

    def test_an_untrusted_certificate_is_refused_by_the_client(self, secured):
        with pytest.raises(urllib.error.URLError) as exc:
            secured.get("/health", ssl.create_default_context())
        assert isinstance(exc.value.reason, ssl.SSLCertVerificationError)

    def test_plain_http_to_the_tls_port_gets_nothing_useful(self, secured):
        with pytest.raises((http.client.HTTPException, ConnectionError, OSError)):
            conn = http.client.HTTPConnection("127.0.0.1", secured.port, timeout=5)
            conn.request("GET", "/health")
            response = conn.getresponse()
            assert response.status != 200

    @pytest.mark.filterwarnings("ignore::DeprecationWarning")
    def test_old_tls_versions_are_refused(self, secured):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1
            context.maximum_version = ssl.TLSVersion.TLSv1_1
        except (ValueError, ssl.SSLError):
            pytest.skip("this OpenSSL cannot offer TLS 1.1")
        with pytest.raises((ssl.SSLError, OSError)):
            with socket.create_connection(
                ("127.0.0.1", secured.port), timeout=5
            ) as raw:
                context.wrap_socket(raw)

    def test_tls_1_2_or_newer_is_negotiated(self, secured, tls_certs):
        context = trusting(tls_certs)
        with socket.create_connection(("127.0.0.1", secured.port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as conn:
                assert conn.version() in ("TLSv1.2", "TLSv1.3")


class TestLiveMutualTls:
    @pytest.fixture
    def secured(self, monkeypatch, tls_certs):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        settings = TlsSettings(tls_certs.cert, tls_certs.key, tls_certs.ca)
        with LiveServer(settings) as server:
            yield server

    def test_a_client_with_a_certificate_gets_in(self, secured, tls_certs):
        ctx = trusting(tls_certs, client_cert=True)
        assert secured.get("/health", ctx).status == 200

    def test_a_client_without_one_is_refused_before_any_request(
        self, secured, tls_certs
    ):
        with pytest.raises((urllib.error.URLError, ssl.SSLError, OSError)):
            secured.get("/health", trusting(tls_certs)).read()

    def test_a_certificate_from_another_ca_is_refused(self, secured, tls_certs):
        context = ssl.create_default_context(cafile=tls_certs.ca)
        context.load_cert_chain(tls_certs.other_cert, tls_certs.other_key)
        with pytest.raises((urllib.error.URLError, ssl.SSLError, OSError)):
            secured.get("/health", context).read()

    def test_a_valid_certificate_still_needs_a_token_for_protected_routes(
        self, secured, tls_certs
    ):
        ctx = trusting(tls_certs, client_cert=True)
        with pytest.raises(urllib.error.HTTPError) as exc:
            secured.get("/status", ctx)
        assert exc.value.code == 401
        assert secured.get("/status", ctx, token=TOKEN).status == 200
