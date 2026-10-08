"""
Authentication for the Py-Chaos-Agent control API.

Design:
- Secure by default. Every route requires a bearer token except the
  unauthenticated liveness endpoints listed in PUBLIC_PATHS.
- Fail closed. If no token is configured (and auth has not been explicitly
  disabled), protected routes return 503 instead of letting requests through.
- The token is read on every request, so rotating a mounted Kubernetes
  Secret takes effect without restarting the agent.

Token sources, in priority order:
    1. CHAOS_API_TOKEN_FILE  (path to a file containing the token)
    2. CHAOS_API_TOKEN       (the token itself)

Explicit opt-out (loopback-only; enforced by src.api_server at startup):
    CHAOS_API_AUTH_DISABLED=true
"""

import hmac
import os
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request, status

from .logging_config import get_logger

logger = get_logger(__name__)

TOKEN_ENV = "CHAOS_API_TOKEN"
TOKEN_FILE_ENV = "CHAOS_API_TOKEN_FILE"
AUTH_DISABLED_ENV = "CHAOS_API_AUTH_DISABLED"

# 32 chars is 128 bits of entropy when generated with `openssl rand -hex 16`.
# The documented generator (`openssl rand -hex 32`) yields 64 chars.
MIN_TOKEN_LENGTH = 32

# Unauthenticated endpoints. Kept deliberately tiny and side-effect free:
# they exist for container healthchecks and Kubernetes probes.
PUBLIC_PATHS = frozenset({"/", "/health"})

_TRUTHY = {"1", "true", "yes", "on"}


def auth_disabled() -> bool:
    """True only if auth was explicitly disabled via environment."""
    return os.environ.get(AUTH_DISABLED_ENV, "").strip().lower() in _TRUTHY


def get_expected_token() -> Optional[str]:
    """
    Resolve the configured API token, or None if none is configured.

    Raises:
        ValueError: if a token is configured but too short (misconfiguration
        should be loud, not silently accepted).
    """
    token: Optional[str] = None

    token_file = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError as e:
            logger.error(
                "Could not read API token file",
                extra={"path": token_file, "error": str(e)},
            )
            return None
    else:
        token = os.environ.get(TOKEN_ENV, "").strip() or None

    if token is None or token == "":
        return None

    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"API token is too short ({len(token)} chars); "
            f"minimum is {MIN_TOKEN_LENGTH}. Generate one with: openssl rand -hex 32"
        )

    return token


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def require_auth(request: Request) -> None:
    """
    FastAPI dependency applied app-wide. Allows PUBLIC_PATHS through and
    requires `Authorization: Bearer <token>` for everything else.
    """
    if request.url.path in PUBLIC_PATHS:
        return

    if auth_disabled():
        return

    try:
        expected = get_expected_token()
    except ValueError as e:
        logger.error("Invalid API token configuration", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is misconfigured",
        )

    if expected is None:
        # Fail closed: no token configured means nobody gets in.
        logger.error(
            "Request rejected: no API token configured",
            extra={"path": request.url.path, "client": _client_ip(request)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured",
        )

    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")

    if scheme.lower() != "bearer" or not supplied:
        logger.warning(
            "Request rejected: missing bearer token",
            extra={"path": request.url.path, "client": _client_ip(request)},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time comparison; encode so non-ASCII input can't raise TypeError.
    if not hmac.compare_digest(supplied.strip().encode(), expected.encode()):
        logger.warning(
            "Request rejected: invalid bearer token",
            extra={"path": request.url.path, "client": _client_ip(request)},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def validate_startup(host: str, insecure_no_auth: bool) -> None:
    """
    Refuse to start the API in an unsafe configuration.

    Rules:
      * A token must be configured, unless auth is explicitly disabled.
      * Disabling auth is only allowed when bound to a loopback address.

    Raises:
        SystemExit: with a human-readable message on any violation.
    """
    loopback = host in {"127.0.0.1", "::1", "localhost"}

    if insecure_no_auth or auth_disabled():
        if not loopback:
            raise SystemExit(
                f"Refusing to start: authentication is disabled but the API is "
                f"bound to non-loopback address '{host}'. Set {TOKEN_ENV} (or "
                f"{TOKEN_FILE_ENV}) or bind to 127.0.0.1."
            )
        logger.warning(
            "API authentication is DISABLED (loopback only). "
            "Do not use this outside local development."
        )
        return

    try:
        token = get_expected_token()
    except ValueError as e:
        raise SystemExit(f"Refusing to start: {e}")

    if token is None:
        raise SystemExit(
            f"Refusing to start: no API token configured. Set {TOKEN_ENV} or "
            f"{TOKEN_FILE_ENV} (generate one with: openssl rand -hex 32), or pass "
            f"--insecure-no-auth for loopback-only local development."
        )
