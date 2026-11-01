"""
Audit log: who did what to the chaos agent, and when.

This agent can kill processes and degrade a service, so "who triggered that?" has
to have an answer. Every event is one JSON object with a timestamp, the person or
system responsible (``principal`` and ``role``), where the request came from, and
what happened.

What is recorded:

* every request that changes something (anything other than GET), with its
  outcome and details such as the failure type or the configuration changed
* every refused request: wrong, missing or expired token, a lockout, or a role
  that is too low
* every read of the audit log itself
* every injection, with ``source`` ``manual`` (and who) or ``scheduler`` (the
  agent's own loop), and how it ended

Plain reads (status, config, ...) are not recorded; that would bury the events
that matter under health-check noise.

Where it goes, all at once:

* the ``chaos.audit`` logger, so it reaches stdout and your log pipeline
* a file of one JSON object per line, if CHAOS_AUDIT_LOG_FILE is set. It is
  created owner-only (0600) and opened for append on each event, so log rotation
  works. Put it on a volume that outlives the container.
* an in-memory ring of the latest 500 events, readable by an admin at GET /audit

Tokens and other secrets are never recorded: request headers are not logged, and
any detail key that looks like a secret is masked.

An audit log kept only on the host that is being audited is evidence, not proof.
Ship it somewhere the agent cannot write to.
"""

import datetime as dt
import json
import os
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from .logging_config import get_logger
from .metrics import AUDIT_WRITE_FAILURES_TOTAL

AUDIT_LOG_FILE_ENV = "CHAOS_AUDIT_LOG_FILE"
RING_SIZE = 500

logger = get_logger("chaos.audit")

# Methods that do not change anything. Their requests are recorded only when they
# are refused or read the audit log.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SECRET_WORDS = ("token", "secret", "password", "authorization", "credential")


class AuditError(RuntimeError):
    """The audit log file cannot be written."""


def _scrub(value: Any, depth: int = 0) -> Any:
    """Mask anything that looks like a secret, and keep the result JSON-safe."""
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        return {
            str(k): (
                "***"
                if any(w in str(k).lower() for w in _SECRET_WORDS)
                else _scrub(v, depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_scrub(v, depth + 1) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class AuditLog:
    def __init__(self, ring_size: int = RING_SIZE) -> None:
        self._ring: Deque[Dict[str, Any]] = deque(maxlen=ring_size)
        self._lock = threading.Lock()
        self._path: Optional[str] = None

    # -- file -----------------------------------------------------------------

    @property
    def path(self) -> Optional[str]:
        return self._path

    def configure_file(self, path: Optional[str]) -> None:
        """
        Write events to ``path`` (None for no file).

        Raises:
            AuditError: if the file cannot be created or opened for append, so a
            misconfigured audit log stops startup instead of silently recording
            nothing.
        """
        if not path:
            self._path = None
            return
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.close(fd)
        except OSError as e:
            raise AuditError(
                f"cannot write the audit log {path}: {e.strerror or e}"
            ) from e
        self._path = path

    def configure_from_env(self) -> None:
        self.configure_file(os.environ.get(AUDIT_LOG_FILE_ENV, "").strip() or None)

    def _write(self, event: Dict[str, Any]) -> None:
        if self._path is None:
            return
        line = json.dumps(event, sort_keys=True, default=str) + "\n"
        try:
            # Opened per event so rotation (rename or truncate) is respected.
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)
        except OSError as e:
            # An unwritable audit file must not take the API down, but it must
            # not be quiet either.
            AUDIT_WRITE_FAILURES_TOTAL.inc()
            logger.error(
                "Audit log write failed",
                extra={"path": self._path, "error": str(e)},
            )

    # -- events ----------------------------------------------------------------

    def record(
        self,
        event: str,
        *,
        principal: str = "unknown",
        role: Optional[str] = None,
        **fields: Any,
    ) -> Dict[str, Any]:
        """Record one event and return it."""
        entry: Dict[str, Any] = {
            "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            "principal": principal,
            "role": role,
        }
        entry.update(_scrub(fields))
        with self._lock:
            self._ring.append(entry)
        self._write(entry)
        logger.info(f"audit {event}", extra={"audit": entry})
        return entry

    def recent(
        self,
        limit: int = 100,
        principal: Optional[str] = None,
        event: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """The latest events, newest first, optionally filtered."""
        with self._lock:
            items = list(self._ring)
        items.reverse()
        if principal is not None:
            items = [e for e in items if e.get("principal") == principal]
        if event is not None:
            items = [e for e in items if e.get("event") == event]
        return items[: max(limit, 0)]

    def reset(self) -> None:
        """Forget the in-memory events (not the file). For tests."""
        with self._lock:
            self._ring.clear()


audit = AuditLog()


def annotate(request: Any, **detail: Any) -> None:
    """Add detail to the audit event for this request (``request`` is the
    Starlette Request)."""
    current = getattr(request.state, "audit_detail", None)
    if current is None:
        current = request.state.audit_detail = {}
    current.update(detail)


def _outcome(status: int) -> str:
    if status in (401, 403, 429):
        return "denied"
    return "ok" if status < 400 else "error"


class AuditMiddleware:
    """
    Pure ASGI middleware that records the requests described in the module
    docstring once their response is known.

    It reads what the auth dependency left in ``scope["state"]`` (who the caller
    is, or why they were refused), so it never sees a token.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        scope.setdefault("state", {})
        status_holder = {"status": 500}

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                self._record(scope, status_holder["status"])
            except Exception:  # auditing must never break a response
                logger.error("Audit middleware failed", exc_info=True)

    @staticmethod
    def _record(scope: Any, status: int) -> None:
        method = scope["method"].upper()
        path = scope["path"]
        state = scope.get("state", {})
        denied = status in (401, 403, 429)
        if method in SAFE_METHODS and not denied and path != "/audit":
            return

        principal = state.get("principal")
        client = scope.get("client")
        route = scope.get("route")
        fields: Dict[str, Any] = {
            "client": client[0] if client else "unknown",
            "method": method,
            "path": path,
            "status": status,
            "outcome": _outcome(status),
        }
        if route is not None and getattr(route, "path", path) != path:
            fields["route"] = route.path
        detail = dict(state.get("audit_detail") or {})

        if state.get("auth_forbidden"):
            # The role check in src.auth refused it. (A 403 from a handler, such
            # as a safety refusal, is an ordinary api.request with outcome denied.)
            event = "auth.forbidden"
        elif state.get("auth_failure") or status in (401, 429):
            event = "auth.failed"
            detail["reason"] = state.get("auth_failure", "unknown")
            if state.get("auth_failure_owner"):
                detail["owner"] = state["auth_failure_owner"]
        elif path == "/audit":
            event = "audit.read"
        else:
            event = "api.request"

        if detail:
            fields["detail"] = detail
        if principal is not None:
            audit.record(
                event,
                principal=principal.name,
                role=principal.role,
                auth=principal.method,
                **fields,
            )
        else:
            audit.record(event, principal="anonymous", role=None, **fields)
