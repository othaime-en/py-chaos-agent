"""
The Prometheus metrics endpoint, with the access controls the control API has.

Metrics are less sensitive than the control API but not harmless: they show when
chaos is running, what it targets, how the kill switch is doing and who is
failing to authenticate. They used to be served to every interface with no
credentials at all.

Rules, deliberately parallel to the API's (see src/auth.py):

* The default listen address is loopback (CHAOS_METRICS_HOST to change it).
* A scrape token (CHAOS_METRICS_TOKEN or CHAOS_METRICS_TOKEN_FILE, 32+ characters)
  is required whenever one is set, on any address.
* Listening on a non-loopback address WITHOUT a token is refused, unless
  CHAOS_METRICS_AUTH_DISABLED=true says so explicitly. Use that only when the
  network already limits who can reach the port (a Kubernetes NetworkPolicy, a
  private network), and it logs a warning.
* The scrape token must differ from the API token. A credential that sits in
  Prometheus's configuration must not also be able to start chaos.
* Repeated bad tokens lock a client out (the same throttle as the API).
* If TLS is configured (src/tls.py) it covers this port too, so Prometheus needs
  ``scheme: https``.

Prometheus supplies the token with:

    scrape_configs:
      - job_name: chaos-agent
        authorization:
          credentials_file: /etc/prometheus/chaos-metrics-token
        static_configs:
          - targets: ["chaos-agent:8000"]

Only GET /metrics is served.
"""

import errno
import hmac
import os
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Tuple

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from . import ratelimit, tls
from .logging_config import get_logger

logger = get_logger(__name__)

HOST_ENV = "CHAOS_METRICS_HOST"
TOKEN_ENV = "CHAOS_METRICS_TOKEN"
TOKEN_FILE_ENV = "CHAOS_METRICS_TOKEN_FILE"
AUTH_DISABLED_ENV = "CHAOS_METRICS_AUTH_DISABLED"
DEFAULT_HOST = "127.0.0.1"
MIN_TOKEN_LENGTH = 32

_TRUTHY = {"1", "true", "yes", "on"}


class MetricsAccessError(RuntimeError):
    """The metrics endpoint's access settings are unsafe or invalid."""


def host_from_env(default: str = DEFAULT_HOST) -> str:
    return os.environ.get(HOST_ENV, "").strip() or default


def read_token() -> Optional[str]:
    """
    The scrape token, or None if none is configured. The file wins over the
    variable. Read on every request, so rotating a mounted Secret needs no restart.

    Raises:
        ValueError: if the token is unreadable or too short.
    """
    path = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if path:
        try:
            with open(path) as f:
                token = f.read().strip()
        except OSError as e:
            raise ValueError(f"cannot read {TOKEN_FILE_ENV} {path}: {e.strerror or e}")
        source = f"the file {path}"
    else:
        token = os.environ.get(TOKEN_ENV, "").strip()
        source = TOKEN_ENV
    if not token:
        if path:
            raise ValueError(f"the metrics token file {path} is empty")
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"the metrics token in {source} is shorter than {MIN_TOKEN_LENGTH} "
            f"characters (generate one with: openssl rand -hex 32)"
        )
    return token


def check_access_policy(addr: str) -> Optional[str]:
    """
    Decide how the endpoint will be protected, before any port is opened.

    Returns the token if requests must carry one, or None if they need not.

    Raises:
        MetricsAccessError: for an unsafe or invalid configuration.
    """
    try:
        token = read_token()
    except ValueError as e:
        raise MetricsAccessError(str(e))

    if token is not None:
        _refuse_if_same_as_api_token(token)
        return token

    if tls.is_loopback(addr):
        return None

    if os.environ.get(AUTH_DISABLED_ENV, "").strip().lower() in _TRUTHY:
        logger.warning(
            "Metrics served on a non-loopback address WITHOUT authentication "
            f"({AUTH_DISABLED_ENV} is set). Make sure the network restricts who "
            "can reach it.",
            extra={"host": addr},
        )
        return None

    raise MetricsAccessError(
        f"refusing to serve metrics on '{addr}' without authentication. Set "
        f"{TOKEN_ENV} or {TOKEN_FILE_ENV} (generate one with: openssl rand -hex "
        f"32) and give it to Prometheus, or bind to 127.0.0.1 with "
        f"{HOST_ENV}=127.0.0.1. If a NetworkPolicy or private network already "
        f"limits access, set {AUTH_DISABLED_ENV}=true."
    )


def _refuse_if_same_as_api_token(token: str) -> None:
    from . import auth  # local import: auth imports the metrics module

    try:
        api_token = auth.get_expected_token()
    except ValueError:
        return  # the API's own startup check reports a bad API token
    if api_token is not None and hmac.compare_digest(
        token.encode(), api_token.encode()
    ):
        raise MetricsAccessError(
            f"{TOKEN_ENV} is the same as the API token. The scrape token sits in "
            f"Prometheus's configuration, so it must not also be able to start chaos. "
            f"Use a different value."
        )


@dataclass
class _Access:
    required: bool
    throttle: ratelimit.FailureThrottle


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    access: _Access

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A failed TLS handshake or a client that went away is routine noise.
        logger.debug("Metrics connection error", extra={"client": client_address[0]})


class _IPv6Server(_Server):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    timeout = (
        10  # seconds a client may take per read, so a stalled one cannot hang a thread
    )
    server_version = "chaos-metrics"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("metrics request", extra={"line": format % args})

    def _reply(self, code: int, body: bytes, content_type: str, **headers: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, message: str, **headers: str) -> None:
        self._reply(
            code, (message + "\n").encode(), "text/plain; charset=utf-8", **headers
        )

    def _authorized(self) -> bool:
        access = self.server.access
        if not access.required:
            return True

        from .audit import audit
        from .metrics import AUTH_FAILURES_TOTAL, AUTH_LOCKOUTS_TOTAL

        client = self.client_address[0]
        wait = access.throttle.retry_after(client)
        if wait is not None:
            AUTH_FAILURES_TOTAL.labels(surface="metrics", reason="locked_out").inc()
            self._text(
                429,
                "Too many failed authentication attempts. Try again later.",
                Retry_After=str(wait),
            )
            return False

        try:
            expected = read_token()
        except ValueError:
            logger.error("Metrics token became unreadable", exc_info=True)
            self._text(503, "Metrics authentication is misconfigured")
            return False
        if expected is None:  # the token was removed while running: fail closed
            self._text(503, "Metrics authentication is not configured")
            return False

        scheme, _, supplied = self.headers.get("Authorization", "").partition(" ")
        reason = None
        if scheme.lower() != "bearer" or not supplied.strip():
            reason = "missing"
        elif not hmac.compare_digest(supplied.strip().encode(), expected.encode()):
            reason = "invalid"

        if reason is None:
            access.throttle.record_success(client)
            return True

        AUTH_FAILURES_TOTAL.labels(surface="metrics", reason=reason).inc()
        locked = access.throttle.record_failure(client)
        if locked:
            AUTH_LOCKOUTS_TOTAL.labels(surface="metrics").inc()
            logger.warning(
                "Client locked out of the metrics endpoint",
                extra={"client": client},
            )
        audit.record(
            "auth.failed",
            principal="anonymous",
            surface="metrics",
            client=client,
            path=self.path.split("?")[0],
            detail={"reason": reason, "locked_out": locked},
        )
        self._text(
            401,
            "Missing bearer token" if reason == "missing" else "Invalid bearer token",
            WWW_Authenticate="Bearer",
        )
        return False

    def do_GET(self) -> None:
        if self.path.split("?")[0] != "/metrics":
            self._text(404, "Not found")
            return
        if not self._authorized():
            return
        self._reply(200, generate_latest(REGISTRY), CONTENT_TYPE_LATEST)

    def _not_allowed(self) -> None:
        self._text(405, "Only GET /metrics is served", Allow="GET")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _not_allowed


def serve(port: int, addr: str) -> Tuple[Any, threading.Thread]:
    """
    Bind the metrics server and start serving. Returns (server, thread) once the
    socket is bound and listening.

    Raises:
        MetricsAccessError: for unsafe or invalid access, TLS or throttle settings.
        OSError: if the address cannot be bound (the caller reports it).
    """
    token = check_access_policy(addr)

    try:
        throttle_settings = ratelimit.settings_from_env()
        tls_settings = tls.settings_from_env()
        context = tls.build_server_context(tls_settings) if tls_settings else None
    except (ValueError, tls.TlsError) as e:  # TlsError is a ValueError
        raise MetricsAccessError(str(e))

    server_class = _IPv6Server if ":" in addr else _Server
    server = server_class((addr, port), _Handler)
    server.access = _Access(
        required=token is not None,
        throttle=ratelimit.FailureThrottle(throttle_settings),
    )
    if context is not None:
        # Handshake in the request thread, not in accept(), so one slow client
        # cannot stall everyone else.
        server.socket = context.wrap_socket(
            server.socket, server_side=True, do_handshake_on_connect=False
        )

    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.1},  # how quickly shutdown() is noticed
        daemon=True,
        name="metrics",
    )
    thread.start()
    logger.info(
        "Metrics endpoint listening",
        extra={
            "address": addr,
            "port": server.server_address[1],
            "authentication": token is not None,
            "tls": context is not None,
        },
    )
    return server, thread


def describe_os_error(e: OSError) -> Tuple[str, bool]:
    """(message, is_port_conflict) for a bind failure."""
    return e.strerror or str(e), e.errno == errno.EADDRINUSE
