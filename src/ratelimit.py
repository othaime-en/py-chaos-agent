"""
Lockout for repeated failed authentication.

An API token is long and random, so guessing one is not realistic. The throttle
is there for the cases that are: a leaked or mistyped credential being retried in
a loop, a scanner probing the port, and noise in the logs. It also gives
operators a signal (a lockout, and the ``chaos_auth_lockouts_total`` metric)
that someone is hitting the API with bad credentials.

Behaviour, per client address:

* each failed attempt is remembered for ``window_seconds``
* ``max_failures`` failures inside the window start a lockout of
  ``lockout_seconds``
* during a lockout every request is refused (HTTP 429 with ``Retry-After``)
  without looking at the credential, so a correct guess cannot be confirmed
* a successful login clears the failure count; it does not lift a lockout
* when the lockout ends the client starts again with a clean slate

Memory is bounded: at most ``max_clients`` addresses are tracked, and an
attacker rotating addresses cannot grow it past that.

Configuration, read from the environment:

    CHAOS_AUTH_MAX_FAILURES            default 5     (0 turns the throttle off)
    CHAOS_AUTH_FAILURE_WINDOW_SECONDS  default 60
    CHAOS_AUTH_LOCKOUT_SECONDS         default 300

Behind a reverse proxy every request arrives from the proxy's address, so
without help one bad client would lock everyone out. uvicorn rewrites the client
address from X-Forwarded-For when the connection comes from an address listed in
FORWARDED_ALLOW_IPS (default 127.0.0.1); set it to your proxy.
"""

import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

MAX_FAILURES_ENV = "CHAOS_AUTH_MAX_FAILURES"
WINDOW_ENV = "CHAOS_AUTH_FAILURE_WINDOW_SECONDS"
LOCKOUT_ENV = "CHAOS_AUTH_LOCKOUT_SECONDS"

DEFAULT_MAX_FAILURES = 5
DEFAULT_WINDOW_SECONDS = 60
DEFAULT_LOCKOUT_SECONDS = 300
DEFAULT_MAX_CLIENTS = 10_000


@dataclass(frozen=True)
class ThrottleSettings:
    max_failures: int = DEFAULT_MAX_FAILURES
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    lockout_seconds: float = DEFAULT_LOCKOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0


def _read_number(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum:g}, got {raw}")
    return value


def settings_from_env() -> ThrottleSettings:
    """
    Read the throttle settings.

    Raises:
        ValueError: for a value that is not a number or is out of range, so a
        typo stops startup instead of silently disabling the protection.
    """
    max_failures = int(_read_number(MAX_FAILURES_ENV, DEFAULT_MAX_FAILURES, 0))
    return ThrottleSettings(
        max_failures=max_failures,
        window_seconds=_read_number(WINDOW_ENV, DEFAULT_WINDOW_SECONDS, 1),
        lockout_seconds=_read_number(LOCKOUT_ENV, DEFAULT_LOCKOUT_SECONDS, 1),
    )


class _Client:
    __slots__ = ("failures", "locked_until")

    def __init__(self) -> None:
        self.failures: Deque[float] = deque()
        self.locked_until: float = 0.0


class FailureThrottle:
    """Tracks failed authentication per client and locks out repeat offenders."""

    def __init__(
        self,
        settings: Optional[ThrottleSettings] = None,
        max_clients: int = DEFAULT_MAX_CLIENTS,
    ) -> None:
        self.settings = settings or ThrottleSettings()
        self._max_clients = max_clients
        self._clients: "OrderedDict[str, _Client]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> float:
        # Looked up at call time so tests can patch time.monotonic.
        return time.monotonic()

    def retry_after(self, client: str) -> Optional[int]:
        """Whole seconds until ``client`` may try again, or None if not locked."""
        if not self.settings.enabled:
            return None
        with self._lock:
            entry = self._clients.get(client)
            if entry is None:
                return None
            remaining = entry.locked_until - self._now()
            if remaining <= 0:
                return None
            return max(1, int(remaining + 0.999))

    def record_failure(self, client: str) -> bool:
        """
        Note a failed attempt. Returns True if it started a lockout.
        """
        if not self.settings.enabled:
            return False
        with self._lock:
            now = self._now()
            entry = self._clients.get(client)
            if entry is None:
                entry = self._clients[client] = _Client()
                self._evict(now)
            self._clients.move_to_end(client)

            if entry.locked_until > now:
                return False  # already locked; do not extend it

            if entry.locked_until:  # a lockout that has ended: start fresh
                entry.locked_until = 0.0
                entry.failures.clear()

            cutoff = now - self.settings.window_seconds
            while entry.failures and entry.failures[0] <= cutoff:
                entry.failures.popleft()
            entry.failures.append(now)

            if len(entry.failures) >= self.settings.max_failures:
                entry.failures.clear()
                entry.locked_until = now + self.settings.lockout_seconds
                return True
            return False

    def record_success(self, client: str) -> None:
        """A good login clears the failure count (but not an active lockout)."""
        if not self.settings.enabled:
            return
        with self._lock:
            entry = self._clients.get(client)
            if entry is None:
                return
            if entry.locked_until > self._now():
                return
            del self._clients[client]

    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._clients)

    def reset(self) -> None:
        with self._lock:
            self._clients.clear()

    def _evict(self, now: float) -> None:
        """Keep memory bounded. Called with the lock held."""
        if len(self._clients) <= self._max_clients:
            return
        # Prefer dropping clients that are not locked out, oldest first.
        stale: Dict[str, None] = {
            key: None for key, c in self._clients.items() if c.locked_until <= now
        }
        victims = list(stale) or list(self._clients)
        for key in victims:
            if len(self._clients) <= self._max_clients:
                break
            del self._clients[key]
