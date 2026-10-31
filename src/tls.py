"""
TLS for the control API (and the metrics endpoint).

Without TLS, API tokens cross the network in clear text. Binding to a loopback
address avoids that; anything else needs either this module or a TLS-terminating
reverse proxy in front.

Configuration (command-line flags on src.api_server override the environment):

    CHAOS_TLS_CERT_FILE       PEM certificate (chain), e.g. fullchain.pem
    CHAOS_TLS_KEY_FILE        PEM private key
    CHAOS_TLS_CLIENT_CA_FILE  optional: require clients to present a certificate
                              signed by this CA (mutual TLS)
    CHAOS_REQUIRE_TLS         true: refuse to listen on a non-loopback address
                              without TLS

TLS 1.2 is the minimum version. The certificate and key are loaded once at
startup, so rotate them by restarting (a rolling restart in Kubernetes). When TLS
is on it covers both the API port and the metrics port, so Prometheus must scrape
with ``scheme: https``.

Without TLS configured, listening on a non-loopback address logs a warning. With
a reverse proxy in front that already terminates TLS, that warning is expected;
set CHAOS_REQUIRE_TLS only where the agent itself must be reached over TLS.
"""

import os
import ssl
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .logging_config import get_logger

logger = get_logger(__name__)

CERT_ENV = "CHAOS_TLS_CERT_FILE"
KEY_ENV = "CHAOS_TLS_KEY_FILE"
CLIENT_CA_ENV = "CHAOS_TLS_CLIENT_CA_FILE"
REQUIRE_TLS_ENV = "CHAOS_REQUIRE_TLS"

_TRUTHY = {"1", "true", "yes", "on"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class TlsError(ValueError):
    """The TLS configuration is incomplete or unusable."""


@dataclass(frozen=True)
class TlsSettings:
    cert_file: str
    key_file: str
    client_ca_file: Optional[str] = None

    @property
    def mutual(self) -> bool:
        """True if clients must present a certificate."""
        return self.client_ca_file is not None


def is_loopback(host: str) -> bool:
    return host in _LOOPBACK


def require_tls() -> bool:
    return os.environ.get(REQUIRE_TLS_ENV, "").strip().lower() in _TRUTHY


def settings_from_env(
    cert: Optional[str] = None,
    key: Optional[str] = None,
    client_ca: Optional[str] = None,
) -> Optional[TlsSettings]:
    """
    The TLS settings, or None if TLS is not configured.

    Arguments (from command-line flags) win over the environment.

    Raises:
        TlsError: if only one of certificate and key is given, or a client CA is
        given without a certificate.
    """
    cert = (cert or os.environ.get(CERT_ENV, "")).strip() or None
    key = (key or os.environ.get(KEY_ENV, "")).strip() or None
    client_ca = (client_ca or os.environ.get(CLIENT_CA_ENV, "")).strip() or None

    if cert is None and key is None:
        if client_ca is not None:
            raise TlsError(
                f"{CLIENT_CA_ENV} is set but TLS is not: set {CERT_ENV} and "
                f"{KEY_ENV} too"
            )
        return None
    if cert is None or key is None:
        missing = KEY_ENV if key is None else CERT_ENV
        raise TlsError(f"TLS needs both a certificate and a key: {missing} is not set")
    return TlsSettings(cert, key, client_ca)


def _require_file(path: str, what: str) -> None:
    """ssl's own errors do not name the file, so check each one first."""
    if not os.path.isfile(path):
        raise TlsError(f"TLS {what} not found: {path}")
    if not os.access(path, os.R_OK):
        raise TlsError(f"TLS {what} is not readable: {path}")


def build_server_context(settings: TlsSettings) -> ssl.SSLContext:
    """
    An SSL context for a server, with TLS 1.2 as the minimum.

    Raises:
        TlsError: for an unreadable file, a malformed PEM, or a key that does not
        match the certificate, with the file named, so a typo does not become a
        stack trace.
    """
    _require_file(settings.cert_file, "certificate")
    _require_file(settings.key_file, "private key")
    if settings.client_ca_file is not None:
        _require_file(settings.client_ca_file, "client CA file")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(settings.cert_file, settings.key_file)
    except (ssl.SSLError, OSError) as e:
        raise TlsError(
            f"cannot load the TLS certificate {settings.cert_file} and key "
            f"{settings.key_file}: {getattr(e, 'strerror', None) or e}. Check that "
            f"both are PEM files and that the key belongs to the certificate."
        ) from e
    if settings.client_ca_file is not None:
        try:
            context.load_verify_locations(settings.client_ca_file)
        except (ssl.SSLError, OSError) as e:
            raise TlsError(
                f"cannot load the client CA {settings.client_ca_file}: "
                f"{getattr(e, 'strerror', None) or e}"
            ) from e
        context.verify_mode = ssl.CERT_REQUIRED
    return context


def uvicorn_options(settings: Optional[TlsSettings]) -> Dict[str, Any]:
    """The uvicorn.Config keyword arguments for ``settings`` (empty for none)."""
    if settings is None:
        return {}
    options: Dict[str, Any] = {
        "ssl_certfile": settings.cert_file,
        "ssl_keyfile": settings.key_file,
    }
    if settings.client_ca_file is not None:
        options["ssl_ca_certs"] = settings.client_ca_file
        options["ssl_cert_reqs"] = ssl.CERT_REQUIRED
    return options


def validate_startup(host: str, settings: Optional[TlsSettings]) -> None:
    """
    Check the TLS setup before any port is opened.

    * a certificate that cannot be loaded stops startup
    * a non-loopback listener without TLS is refused if CHAOS_REQUIRE_TLS is set,
      and otherwise warned about

    Raises:
        SystemExit: with a human-readable message.
    """
    if settings is not None:
        try:
            build_server_context(settings)
        except TlsError as e:
            raise SystemExit(f"Refusing to start: {e}")
        logger.info(
            "TLS enabled",
            extra={
                "certificate": settings.cert_file,
                "client_certificates_required": settings.mutual,
            },
        )
        return

    if is_loopback(host):
        return
    if require_tls():
        raise SystemExit(
            f"Refusing to start: {REQUIRE_TLS_ENV} is set but the API would listen "
            f"on '{host}' without TLS. Set {CERT_ENV} and {KEY_ENV} (or --tls-cert "
            f"and --tls-key), or bind to 127.0.0.1."
        )
    logger.warning(
        "API listening on a non-loopback address WITHOUT TLS: tokens cross the "
        "network in clear text. Set CHAOS_TLS_CERT_FILE and CHAOS_TLS_KEY_FILE, or "
        "put a TLS-terminating proxy in front.",
        extra={"host": host},
    )
