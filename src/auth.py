"""
Authentication for the Py-Chaos-Agent control API.

Design:
- Secure by default. Every route requires a bearer token except the
  unauthenticated liveness endpoints listed in PUBLIC_PATHS.
- Fail closed. If no token is configured (and auth has not been explicitly
  disabled), protected routes return 503 instead of letting requests through.
- The token is read on every request, so rotating a mounted Kubernetes
  Secret takes effect without restarting the agent.
- Repeated failures lock the client out for a while (see src.ratelimit).

Credential sources (either or both may be configured):
    CHAOS_API_TOKENS_FILE   per-user tokens with roles (see src.tokens)
    CHAOS_API_TOKEN_FILE    path to a file containing one shared token
    CHAOS_API_TOKEN         the shared token itself
(for the shared token, the file wins over the variable)

The shared token is a full-access "admin". Per-user tokens carry a role (viewer,
operator or admin); ROLE_POLICY says which role each route needs.

Explicit opt-out (loopback-only; enforced by src.api_server at startup):
    CHAOS_API_AUTH_DISABLED=true
"""

import hmac
import os
from pathlib import Path
from typing import Dict, NoReturn, Optional, Tuple

from fastapi import HTTPException, Request, status

from . import ratelimit, tokens
from .logging_config import get_logger
from .metrics import AUTH_FAILURES_TOTAL, AUTH_LOCKOUTS_TOTAL
from .tokens import ADMIN, OPERATOR, VIEWER, Principal, TokenFileError, TokenStore

logger = get_logger(__name__)

TOKEN_ENV = "CHAOS_API_TOKEN"
TOKEN_FILE_ENV = "CHAOS_API_TOKEN_FILE"
AUTH_DISABLED_ENV = "CHAOS_API_AUTH_DISABLED"
TOKENS_FILE_ENV = tokens.TOKENS_FILE_ENV

# 32 chars is 128 bits of entropy when generated with `openssl rand -hex 16`.
# The documented generator (`openssl rand -hex 32`) yields 64 chars.
MIN_TOKEN_LENGTH = 32

# Unauthenticated endpoints. Kept deliberately tiny and side-effect free:
# they exist for container healthchecks and Kubernetes probes.
PUBLIC_PATHS = frozenset({"/", "/health"})

# The role each route needs: (HTTP method, route path) -> role. Anything not
# listed needs "admin", so a route added without a decision is locked down, not
# open (tests fail if a route is missing here).
ROLE_POLICY: Dict[Tuple[str, str], str] = {
    # Read-only
    ("GET", "/status"): VIEWER,
    ("GET", "/kill-switch/status"): VIEWER,
    ("GET", "/limits"): VIEWER,
    ("GET", "/config"): VIEWER,
    ("GET", "/config/failures/{failure_type}"): VIEWER,
    ("GET", "/metrics/summary"): VIEWER,
    ("GET", "/docs"): VIEWER,
    ("GET", "/redoc"): VIEWER,
    ("GET", "/openapi.json"): VIEWER,
    ("GET", "/docs/oauth2-redirect"): VIEWER,
    # Run chaos
    ("POST", "/agent/start"): OPERATOR,
    ("POST", "/agent/stop"): OPERATOR,
    ("POST", "/agent/restart"): OPERATOR,
    ("POST", "/inject/manual"): OPERATOR,
    ("POST", "/inject/abort"): OPERATOR,
    # Change the rules: configuration and safety overrides
    ("PATCH", "/config"): ADMIN,
    ("PATCH", "/config/failures/{failure_type}"): ADMIN,
    ("POST", "/config/reload"): ADMIN,
    ("POST", "/kill-switch/reset"): ADMIN,
    ("POST", "/metrics/reset"): ADMIN,
}

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


def required_role(method: str, route_path: str) -> str:
    """The role needed for a request. Unknown routes need admin."""
    return ROLE_POLICY.get((method.upper(), route_path), ADMIN)


_store: Optional[TokenStore] = None


def get_token_store() -> Optional[TokenStore]:
    """The per-user token store, or None if CHAOS_API_TOKENS_FILE is not set."""
    global _store
    path = os.environ.get(TOKENS_FILE_ENV, "").strip()
    if not path:
        _store = None
        return None
    if _store is None or _store.path != path:
        _store = TokenStore(path)
    return _store


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


_throttle: Optional[ratelimit.FailureThrottle] = None


def get_throttle() -> ratelimit.FailureThrottle:
    """
    The API's failed-login throttle, built from the environment.

    Rebuilt if the settings change, so a test (or an operator restarting with new
    values) never keeps an old limit. Raises ValueError for invalid settings.
    """
    global _throttle
    settings = ratelimit.settings_from_env()
    if _throttle is None or _throttle.settings != settings:
        _throttle = ratelimit.FailureThrottle(settings)
    return _throttle


def reset_throttle() -> None:
    """Forget all failure counts and lockouts."""
    if _throttle is not None:
        _throttle.reset()


def _reject(
    request: Request,
    throttle: ratelimit.FailureThrottle,
    reason: str,
    owner: Optional[str] = None,
) -> NoReturn:
    """Count a failed attempt, maybe start a lockout, and raise the 401."""
    client = _client_ip(request)
    AUTH_FAILURES_TOTAL.labels(surface="api", reason=reason).inc()
    request.state.auth_failure = reason
    if throttle.record_failure(client):
        AUTH_LOCKOUTS_TOTAL.labels(surface="api").inc()
        logger.warning(
            "Client locked out after repeated failed authentication",
            extra={
                "client": client,
                "lockout_seconds": throttle.settings.lockout_seconds,
            },
        )
    # The caller is told only "missing" or "invalid". The log says more.
    detail = "Missing bearer token" if reason == "missing" else "Invalid bearer token"
    extra = {"path": request.url.path, "client": client, "reason": reason}
    if owner:
        extra["token_owner"] = owner
    logger.warning(f"Request rejected: {detail.lower()}", extra=extra)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_auth(request: Request) -> None:
    """
    FastAPI dependency applied app-wide. Allows PUBLIC_PATHS through, requires
    `Authorization: Bearer <token>` for everything else, and checks that the
    caller's role is enough for the route. The caller is left in
    ``request.state.principal`` for the audit log.
    """
    if request.url.path in PUBLIC_PATHS:
        return

    if auth_disabled():
        request.state.principal = Principal("anonymous", ADMIN, "auth-disabled")
        return

    try:
        shared = get_expected_token()
        store = get_token_store()
        if store is not None:
            store.entries()  # load now: a bad file must fail closed, not later
    except ValueError as e:  # includes TokenFileError
        logger.error("Invalid API token configuration", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is misconfigured",
        )

    if shared is None and store is None:
        # Fail closed: no token configured means nobody gets in.
        logger.error(
            "Request rejected: no API token configured",
            extra={"path": request.url.path, "client": _client_ip(request)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured",
        )

    try:
        throttle = get_throttle()
    except ValueError as e:
        logger.error(
            "Invalid authentication throttle settings", extra={"error": str(e)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is misconfigured",
        )

    # A locked-out client is refused before its credential is even looked at, so
    # a correct guess cannot be confirmed.
    client = _client_ip(request)
    wait = throttle.retry_after(client)
    if wait is not None:
        AUTH_FAILURES_TOTAL.labels(surface="api", reason="locked_out").inc()
        request.state.auth_failure = "locked_out"
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts. Try again later.",
            headers={"Retry-After": str(wait)},
        )

    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")

    if scheme.lower() != "bearer" or not supplied:
        _reject(request, throttle, "missing")

    supplied = supplied.strip()
    principal: Optional[Principal] = None
    failure = "invalid"
    owner: Optional[str] = None

    if store is not None:
        principal, reason, owner = store.lookup(supplied)
        if reason is not None:
            failure = reason
    if principal is None and shared is not None:
        # Constant-time comparison; encode so non-ASCII input can't raise TypeError.
        if hmac.compare_digest(supplied.encode(), shared.encode()):
            principal = Principal("shared-token", ADMIN, "shared-token")

    if principal is None:
        _reject(request, throttle, failure, owner=owner)

    throttle.record_success(client)
    request.state.principal = principal

    route = request.scope.get("route")
    if route is not None:
        needed = required_role(request.method, getattr(route, "path", request.url.path))
        if not principal.allows(needed):
            logger.warning(
                "Request refused: role too low",
                extra={
                    "principal": principal.name,
                    "role": principal.role,
                    "required_role": needed,
                    "path": request.url.path,
                    "method": request.method,
                    "client": client,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action needs the '{needed}' role",
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
        ratelimit.settings_from_env()
    except ValueError as e:
        raise SystemExit(f"Refusing to start: {e}")

    try:
        token = get_expected_token()
    except ValueError as e:
        raise SystemExit(f"Refusing to start: {e}")

    tokens_file = os.environ.get(TOKENS_FILE_ENV, "").strip()
    if tokens_file:
        try:
            users = tokens.load_tokens(tokens_file)
        except TokenFileError as e:
            raise SystemExit(f"Refusing to start: {e}")
        logger.info(
            "Per-user API tokens loaded",
            extra={"count": len(users), "roles": sorted({u.role for u in users})},
        )

    if token is None and not tokens_file:
        raise SystemExit(
            f"Refusing to start: no API token configured. Set {TOKENS_FILE_ENV} "
            f"(per-user tokens with roles; see python -m src.tokens), or "
            f"{TOKEN_ENV} / {TOKEN_FILE_ENV} (one shared token; generate it with: "
            f"openssl rand -hex 32), or pass --insecure-no-auth for loopback-only "
            f"local development."
        )
