"""Access control for the metrics endpoint."""

import http.client
import socket
import ssl
import urllib.error
import urllib.request

import pytest

from src import auth, metrics_http, tls
from src.audit import audit
from src.metrics import (
    AUTH_FAILURES_TOTAL,
    AUTH_LOCKOUTS_TOTAL,
    MetricsServerError,
    metric_value,
    start_metrics_server,
)

TOKEN = "m" * 40
API_TOKEN = "a" * 40

ENV = (
    metrics_http.HOST_ENV,
    metrics_http.TOKEN_ENV,
    metrics_http.TOKEN_FILE_ENV,
    metrics_http.AUTH_DISABLED_ENV,
    tls.CERT_ENV,
    tls.KEY_ENV,
    tls.CLIENT_CA_ENV,
    "CHAOS_AUTH_MAX_FAILURES",
    "CHAOS_AUTH_LOCKOUT_SECONDS",
    "CHAOS_METRICS_PORT",
    auth.TOKEN_ENV,
    auth.TOKEN_FILE_ENV,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ENV:
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)


@pytest.fixture
def servers():
    started = []

    def start(addr="127.0.0.1"):
        server, thread = start_metrics_server(0, addr=addr)
        started.append(server)
        return server.server_address[1]

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def get(port, path="/metrics", token=None, scheme="http", context=None, method="GET"):
    request = urllib.request.Request(
        f"{scheme}://127.0.0.1:{port}{path}", method=method
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(request, context=context, timeout=5)


def status_of(port, **kwargs):
    try:
        with get(port, **kwargs) as response:
            return response.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code


# ----------------------------------------------------------------- the policy


class TestToken:
    def test_none_configured(self):
        assert metrics_http.read_token() is None

    def test_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(metrics_http.TOKEN_ENV, f"  {TOKEN}\n")
        assert metrics_http.read_token() == TOKEN

    def test_from_a_file(self, monkeypatch, tmp_path):
        f = tmp_path / "t"
        f.write_text(TOKEN + "\n")
        monkeypatch.setenv(metrics_http.TOKEN_FILE_ENV, str(f))
        assert metrics_http.read_token() == TOKEN

    def test_the_file_wins_over_the_variable(self, monkeypatch, tmp_path):
        f = tmp_path / "t"
        f.write_text("f" * 40)
        monkeypatch.setenv(metrics_http.TOKEN_FILE_ENV, str(f))
        monkeypatch.setenv(metrics_http.TOKEN_ENV, TOKEN)
        assert metrics_http.read_token() == "f" * 40

    def test_a_short_token_is_refused(self, monkeypatch):
        monkeypatch.setenv(metrics_http.TOKEN_ENV, "short")
        with pytest.raises(ValueError, match="shorter than 32"):
            metrics_http.read_token()

    def test_an_empty_file_is_refused_not_treated_as_no_token(
        self, monkeypatch, tmp_path
    ):
        f = tmp_path / "t"
        f.write_text("\n")
        monkeypatch.setenv(metrics_http.TOKEN_FILE_ENV, str(f))
        with pytest.raises(ValueError, match="empty"):
            metrics_http.read_token()

    def test_a_missing_file_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setenv(metrics_http.TOKEN_FILE_ENV, str(tmp_path / "nope"))
        with pytest.raises(ValueError, match="cannot read"):
            metrics_http.read_token()


class TestPolicy:
    def test_loopback_needs_no_token(self):
        assert metrics_http.check_access_policy("127.0.0.1") is None
        assert metrics_http.check_access_policy("::1") is None

    def test_a_token_is_required_once_set_even_on_loopback(self, monkeypatch):
        monkeypatch.setenv(metrics_http.TOKEN_ENV, TOKEN)
        assert metrics_http.check_access_policy("127.0.0.1") == TOKEN

    def test_a_public_address_without_a_token_is_refused(self):
        with pytest.raises(metrics_http.MetricsAccessError) as exc:
            metrics_http.check_access_policy("0.0.0.0")
        message = str(exc.value)
        assert metrics_http.TOKEN_ENV in message
        assert metrics_http.AUTH_DISABLED_ENV in message

    def test_the_opt_out_allows_it_with_a_warning(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv(metrics_http.AUTH_DISABLED_ENV, "true")
        with caplog.at_level(logging.WARNING):
            assert metrics_http.check_access_policy("0.0.0.0") is None
        assert any("WITHOUT authentication" in r.getMessage() for r in caplog.records)

    def test_a_token_wins_over_the_opt_out(self, monkeypatch):
        monkeypatch.setenv(metrics_http.AUTH_DISABLED_ENV, "true")
        monkeypatch.setenv(metrics_http.TOKEN_ENV, TOKEN)
        assert metrics_http.check_access_policy("0.0.0.0") == TOKEN

    def test_the_scrape_token_must_differ_from_the_api_token(self, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, TOKEN)
        monkeypatch.setenv(metrics_http.TOKEN_ENV, TOKEN)
        with pytest.raises(
            metrics_http.MetricsAccessError, match="same as the API token"
        ):
            metrics_http.check_access_policy("0.0.0.0")

    def test_different_tokens_are_fine(self, monkeypatch):
        monkeypatch.setenv(auth.TOKEN_ENV, API_TOKEN)
        monkeypatch.setenv(metrics_http.TOKEN_ENV, TOKEN)
        assert metrics_http.check_access_policy("0.0.0.0") == TOKEN

    def test_host_from_the_environment(self, monkeypatch):
        assert metrics_http.host_from_env() == "127.0.0.1"
        monkeypatch.setenv(metrics_http.HOST_ENV, " 0.0.0.0 ")
        assert metrics_http.host_from_env() == "0.0.0.0"


class TestStartupRefusals:
    def test_a_public_bind_without_a_token_does_not_start(self):
        import threading

        before = threading.active_count()
        with pytest.raises(MetricsServerError, match="without authentication"):
            start_metrics_server(0, addr="0.0.0.0")
        assert threading.active_count() <= before

    def test_the_default_address_is_loopback_and_needs_nothing(self):
        server, _ = start_metrics_server(0)
        try:
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.shutdown()
            server.server_close()

    def test_the_host_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(metrics_http.HOST_ENV, "127.0.0.1")
        server, _ = start_metrics_server(0)
        try:
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.shutdown()
            server.server_close()

    def test_a_public_bind_with_a_token_starts(self, monkeypatch, servers):
        monkeypatch.setenv(metrics_http.TOKEN_ENV, TOKEN)
        port = servers("0.0.0.0")
        assert status_of(port, token=TOKEN) == 200

    def test_an_invalid_throttle_setting_stops_startup(self, monkeypatch):
        monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "lots")
        with pytest.raises(MetricsServerError, match="CHAOS_AUTH_MAX_FAILURES"):
            start_metrics_server(0, addr="127.0.0.1")

    def test_a_bad_token_stops_startup(self, monkeypatch):
        monkeypatch.setenv(metrics_http.TOKEN_ENV, "short")
        with pytest.raises(MetricsServerError, match="shorter than"):
            start_metrics_server(0, addr="127.0.0.1")

    def test_ipv6_loopback(self, servers):
        try:
            with socket.socket(socket.AF_INET6) as s:
                s.bind(("::1", 0))
        except OSError:
            pytest.skip("no IPv6 loopback here")
        server, _ = start_metrics_server(0, addr="::1")
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://[::1]:{port}/metrics", timeout=5) as r:
                assert r.status == 200
        finally:
            server.shutdown()
            server.server_close()


# ------------------------------------------------------------- the live server


class TestOpenOnLoopback:
    def test_serves_metrics_without_a_token(self, servers):
        port = servers()
        with get(port) as response:
            body = response.read().decode()
        assert response.status == 200
        assert "chaos_injections_total" in body
        assert response.headers["Content-Type"].startswith("text/plain")

    def test_only_the_metrics_path_is_served(self, servers):
        port = servers()
        assert status_of(port, path="/") == 404
        assert status_of(port, path="/status") == 404
        assert status_of(port, path="/metrics?x=1") == 200

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD"])
    def test_only_get_is_allowed(self, servers, method):
        port = servers()
        assert status_of(port, method=method) == 405

    def test_the_server_banner_does_not_name_the_python_version(self, servers):
        port = servers()
        with get(port) as response:
            assert "Python" not in response.headers.get("Server", "")


class TestWithToken:
    @pytest.fixture
    def port(self, monkeypatch, servers):
        monkeypatch.setenv(metrics_http.TOKEN_ENV, TOKEN)
        return servers()

    def test_no_token(self, port):
        assert status_of(port) == 401

    def test_the_challenge_names_the_scheme(self, port):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(port)
        assert exc.value.headers["WWW-Authenticate"] == "Bearer"
        exc.value.close()

    def test_wrong_token(self, port):
        assert status_of(port, token="x" * 40) == 401

    def test_the_right_token(self, port):
        assert status_of(port, token=TOKEN) == 200

    def test_another_scheme_is_not_accepted(self, port):
        request = urllib.request.Request(f"http://127.0.0.1:{port}/metrics")
        request.add_header("Authorization", f"Basic {TOKEN}")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 401
        exc.value.close()

    def test_unknown_paths_are_not_an_oracle(self, port):
        """404 for a bad path comes before authentication, so it reveals nothing
        about the token."""
        assert status_of(port, path="/nope") == 404

    def test_failures_are_counted_and_audited(self, port):
        before = metric_value(AUTH_FAILURES_TOTAL, surface="metrics", reason="invalid")
        status_of(port, token="x" * 40)
        after = metric_value(AUTH_FAILURES_TOTAL, surface="metrics", reason="invalid")
        assert after == before + 1
        (event,) = audit.recent(event="auth.failed")
        assert event["surface"] == "metrics" and event["principal"] == "anonymous"
        assert event["detail"]["reason"] == "invalid"

    def test_no_token_ever_reaches_the_audit_log(self, port):
        status_of(port, token="x" * 40 + "SECRET")
        status_of(port, token=TOKEN)
        import json

        dump = json.dumps(audit.recent(limit=100))
        assert "SECRET" not in dump and TOKEN not in dump


class TestLockout:
    @pytest.fixture
    def port(self, monkeypatch, servers):
        monkeypatch.setenv(metrics_http.TOKEN_ENV, TOKEN)
        monkeypatch.setenv("CHAOS_AUTH_MAX_FAILURES", "3")
        monkeypatch.setenv("CHAOS_AUTH_LOCKOUT_SECONDS", "300")
        return servers()

    def test_repeated_failures_lock_the_client_out(self, port):
        assert [status_of(port, token="x" * 40) for _ in range(3)] == [401] * 3
        assert status_of(port, token="x" * 40) == 429

    def test_a_correct_token_is_refused_during_the_lockout(self, port):
        for _ in range(3):
            status_of(port, token="x" * 40)
        assert status_of(port, token=TOKEN) == 429

    def test_retry_after_is_sent(self, port):
        for _ in range(3):
            status_of(port, token="x" * 40)
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(port, token=TOKEN)
        assert 1 <= int(exc.value.headers["Retry-After"]) <= 300
        exc.value.close()

    def test_a_good_scrape_clears_earlier_failures(self, port):
        status_of(port, token="x" * 40)
        status_of(port, token="x" * 40)
        assert status_of(port, token=TOKEN) == 200
        assert [status_of(port, token="x" * 40) for _ in range(2)] == [401, 401]

    def test_lockouts_are_counted(self, port):
        before = metric_value(AUTH_LOCKOUTS_TOTAL, surface="metrics")
        for _ in range(4):
            status_of(port, token="x" * 40)
        assert metric_value(AUTH_LOCKOUTS_TOTAL, surface="metrics") == before + 1

    def test_the_api_and_metrics_throttles_are_separate(self, port, monkeypatch):
        """A misconfigured Prometheus must not lock anyone out of the API."""
        for _ in range(4):
            status_of(port, token="x" * 40)
        assert auth.get_throttle().retry_after("127.0.0.1") is None


class TestRotation:
    def test_a_rotated_token_file_takes_effect_without_a_restart(
        self, monkeypatch, servers, tmp_path
    ):
        f = tmp_path / "t"
        f.write_text(TOKEN)
        monkeypatch.setenv(metrics_http.TOKEN_FILE_ENV, str(f))
        port = servers()
        assert status_of(port, token=TOKEN) == 200
        f.write_text("n" * 40)
        assert status_of(port, token=TOKEN) == 401
        assert status_of(port, token="n" * 40) == 200

    def test_a_token_file_that_disappears_fails_closed(
        self, monkeypatch, servers, tmp_path
    ):
        f = tmp_path / "t"
        f.write_text(TOKEN)
        monkeypatch.setenv(metrics_http.TOKEN_FILE_ENV, str(f))
        port = servers()
        f.unlink()
        assert status_of(port, token=TOKEN) == 503


class TestOptOut:
    def test_the_opt_out_serves_without_a_token(self, monkeypatch, servers):
        monkeypatch.setenv(metrics_http.AUTH_DISABLED_ENV, "true")
        port = servers("0.0.0.0")
        assert status_of(port) == 200


# -------------------------------------------------------------------------- TLS


def trusting(tls_certs, client_cert=False):
    context = ssl.create_default_context(cafile=tls_certs.ca)
    if client_cert:
        context.load_cert_chain(tls_certs.client_cert, tls_certs.client_key)
    return context


class TestTls:
    @pytest.fixture
    def secured(self, monkeypatch, servers, tls_certs):
        monkeypatch.setenv(metrics_http.TOKEN_ENV, TOKEN)
        monkeypatch.setenv(tls.CERT_ENV, tls_certs.cert)
        monkeypatch.setenv(tls.KEY_ENV, tls_certs.key)
        return servers()

    def test_https_with_a_token(self, secured, tls_certs):
        assert (
            status_of(secured, scheme="https", context=trusting(tls_certs), token=TOKEN)
            == 200
        )

    def test_the_token_is_still_required_over_tls(self, secured, tls_certs):
        assert status_of(secured, scheme="https", context=trusting(tls_certs)) == 401

    def test_an_untrusted_certificate_is_refused_by_the_client(self, secured):
        with pytest.raises(urllib.error.URLError) as exc:
            get(
                secured,
                scheme="https",
                context=ssl.create_default_context(),
                token=TOKEN,
            )
        assert isinstance(exc.value.reason, ssl.SSLCertVerificationError)

    def test_plain_http_to_the_tls_port_is_refused(self, secured):
        with pytest.raises((http.client.HTTPException, ConnectionError, OSError)):
            conn = http.client.HTTPConnection("127.0.0.1", secured, timeout=5)
            conn.request(
                "GET", "/metrics", headers={"Authorization": f"Bearer {TOKEN}"}
            )
            assert conn.getresponse().status != 200

    def test_a_slow_client_does_not_block_others(self, secured, tls_certs):
        """The handshake happens in the request thread, not in accept()."""
        stalled = socket.create_connection(("127.0.0.1", secured), timeout=5)
        try:
            assert (
                status_of(
                    secured, scheme="https", context=trusting(tls_certs), token=TOKEN
                )
                == 200
            )
        finally:
            stalled.close()

    def test_a_bad_certificate_stops_startup(self, monkeypatch, tls_certs):
        monkeypatch.setenv(tls.CERT_ENV, tls_certs.cert)
        monkeypatch.setenv(tls.KEY_ENV, tls_certs.other_key)
        with pytest.raises(MetricsServerError, match="belongs to the certificate"):
            start_metrics_server(0, addr="127.0.0.1")


class TestMutualTls:
    @pytest.fixture
    def secured(self, monkeypatch, servers, tls_certs):
        monkeypatch.setenv(metrics_http.TOKEN_ENV, TOKEN)
        monkeypatch.setenv(tls.CERT_ENV, tls_certs.cert)
        monkeypatch.setenv(tls.KEY_ENV, tls_certs.key)
        monkeypatch.setenv(tls.CLIENT_CA_ENV, tls_certs.ca)
        return servers()

    def test_a_client_certificate_and_token_get_in(self, secured, tls_certs):
        ctx = trusting(tls_certs, client_cert=True)
        assert status_of(secured, scheme="https", context=ctx, token=TOKEN) == 200

    def test_no_client_certificate_is_refused(self, secured, tls_certs):
        with pytest.raises((urllib.error.URLError, ssl.SSLError, OSError)):
            get(
                secured, scheme="https", context=trusting(tls_certs), token=TOKEN
            ).read()
