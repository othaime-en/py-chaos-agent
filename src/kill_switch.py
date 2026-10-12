"""
Kill switch (circuit breaker) for Py-Chaos-Agent.

Stops chaos automatically when the target application becomes unhealthy, and
lets it resume once the target has recovered.

How it works
------------
The kill switch polls the target's health endpoint, but only while chaos is
*armed* (the agent loop is running or an injection is in flight). Idle, it does
not count failures, so a target that is down for its own reasons does not trip
it. States:

* CLOSED: chaos allowed. ``consecutive_failures_threshold`` failed checks in a
  row trip the breaker, unless a grace period applies.
* OPEN: tripped. Chaos is refused and in-flight injections are aborted. After
  ``recovery_test_interval_seconds`` it moves to HALF_OPEN.
* HALF_OPEN: chaos is still off. ``recovery_success_threshold`` healthy checks
  in a row close the breaker. A single failed check re-opens it at once.

Chaos does not stop being *possible* to run; it is *paused*. The agent loop
keeps running, skips injections while the breaker is not CLOSED, and carries on
by itself after recovery. With ``resume_after_recovery: false`` the breaker
instead latches: chaos stays off after recovery until an operator calls
``reset()``.

Grace periods. A process kill makes the target unavailable on purpose, so health
checks are skipped for ``grace_after_process_kill_seconds`` after one. A grace
period of ``grace_after_agent_start_seconds`` also applies each time chaos
becomes armed, so a target that is still starting up does not trip it.

Everything an injection does goes through :meth:`KillSwitch.injection`, which
refuses when the breaker is not CLOSED and records the injection in the failure
context. The agent loop, the standalone agent, and ``POST /inject/manual`` all
use it.
"""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Iterator, Optional, Set, Tuple

import requests

from .metrics import KILL_SWITCH_HEALTH_CHECKS, KILL_SWITCH_STATE, KILL_SWITCH_TRIPS

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation, chaos allowed
    OPEN = "open"  # Tripped, chaos stopped
    HALF_OPEN = "half_open"  # Testing whether the target recovered


_STATE_GAUGE = {
    CircuitState.CLOSED: 0,
    CircuitState.OPEN: 1,
    CircuitState.HALF_OPEN: 2,
}


class KillSwitchOpen(Exception):
    """Raised by :meth:`KillSwitch.injection` when chaos is not allowed."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class KillSwitchConfig:
    """Configuration for kill switch behavior."""

    enabled: bool = True

    # Health check settings
    health_check_interval_seconds: int = 10
    consecutive_failures_threshold: int = 3

    # Grace periods
    grace_after_process_kill_seconds: int = 60
    grace_after_agent_start_seconds: int = 30

    # Recovery settings
    recovery_test_interval_seconds: int = 120
    recovery_success_threshold: int = 3
    resume_after_recovery: bool = True

    # Target health endpoint
    target_health_url: str = "http://localhost:8080/health"
    health_check_timeout_seconds: int = 2

    # Whether the API may turn the kill switch off
    allow_api_disable: bool = False


class FailureContext:
    """
    Track active chaos injections to differentiate expected vs unexpected failures.

    When a process kill is injected, the app is expected to be down for a while.
    This prevents false positives in the kill switch.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._active_injections: Set[str] = set()
        self._lock = threading.Lock()
        self._last_process_kill_time: Optional[float] = None

    def mark_injection_start(self, failure_type: str) -> None:
        """Record that a failure injection has started."""
        with self._lock:
            self._active_injections.add(failure_type)
            if failure_type == "process":
                self._last_process_kill_time = self._clock()
                logger.info("Process kill injection started, entering grace period")

    def mark_injection_end(self, failure_type: str) -> None:
        """Record that a failure injection has completed."""
        with self._lock:
            self._active_injections.discard(failure_type)

    def is_in_process_kill_grace_period(self, grace_seconds: int) -> bool:
        """Check if we're within grace period after a process kill."""
        with self._lock:
            if self._last_process_kill_time is None:
                return False
            return (self._clock() - self._last_process_kill_time) < grace_seconds

    def get_active_injections(self) -> Set[str]:
        """Get copy of currently active injections."""
        with self._lock:
            return self._active_injections.copy()


class KillSwitch:
    """
    Circuit breaker that pauses chaos when the target becomes unhealthy.

    Args:
        config: Settings (usually ``Config.kill_switch``).
        is_armed: Returns True while chaos is active. While it returns False
            and the breaker is CLOSED, health is not checked. Defaults to
            always armed.
        health_check: Returns True if the target is healthy. Defaults to an HTTP
            GET of ``config.target_health_url``. Injectable for tests.
        clock: Time source. Injectable for tests.
    """

    def __init__(
        self,
        config: KillSwitchConfig,
        is_armed: Optional[Callable[[], bool]] = None,
        health_check: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self._clock = clock
        self._is_armed = is_armed or (lambda: True)
        self._health_check = health_check or self._check_target_health
        self.failure_context = FailureContext(clock)

        # All mutable state below is guarded by this lock. It is re-entrant so
        # a callback may safely call back into the kill switch.
        self._lock = threading.RLock()
        self.circuit_state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.latched = False
        self.trip_count = 0
        self.last_trip_reason: Optional[str] = None
        self.last_trip_time: Optional[float] = None
        self._opened_at: Optional[float] = None
        self._armed_since: Optional[float] = None
        self._last_health: Optional[Dict[str, Any]] = None

        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._on_trip: Optional[Callable[[], Any]] = None

        KILL_SWITCH_STATE.set(_STATE_GAUGE[self.circuit_state])

    # ------------------------------------------------------------------
    # Gating
    # ------------------------------------------------------------------

    def allows_injection(self) -> Tuple[bool, str]:
        """Whether chaos may run right now, and if not, why."""
        if not self.config.enabled:
            return True, ""
        with self._lock:
            if self.circuit_state == CircuitState.OPEN:
                return False, (
                    "kill switch is open: the target was unhealthy "
                    f"({self.last_trip_reason or 'health checks failed'})"
                )
            if self.circuit_state == CircuitState.HALF_OPEN:
                return False, "kill switch is half-open: testing target recovery"
            if self.latched:
                return False, (
                    "kill switch is latched after a trip "
                    "(resume_after_recovery is false); reset it to resume chaos"
                )
            return True, ""

    @property
    def paused(self) -> bool:
        """True if chaos is currently being held back by the kill switch."""
        return not self.allows_injection()[0]

    @contextmanager
    def injection(self, failure_type: str, dry_run: bool = False) -> Iterator[None]:
        """
        Gate and track one injection.

        Raises :class:`KillSwitchOpen` if chaos is not allowed. Otherwise records
        the injection in the failure context (so a process kill earns its grace
        period) for the duration of the ``with`` block. Dry runs change nothing,
        so they are neither gated nor recorded.
        """
        if dry_run:
            yield
            return

        allowed, reason = self.allows_injection()
        if not allowed:
            raise KillSwitchOpen(reason)

        self.failure_context.mark_injection_start(failure_type)
        try:
            yield
        finally:
            self.failure_context.mark_injection_end(failure_type)

    # ------------------------------------------------------------------
    # Monitoring thread
    # ------------------------------------------------------------------

    def start_monitoring(
        self, should_stop_agent_callback: Optional[Callable[[], Any]] = None
    ) -> None:
        """
        Start the background health monitor. Idempotent.

        Args:
            should_stop_agent_callback: Called (once per trip, from the monitor
                thread) after the breaker opens, to abort in-flight injections.
        """
        if not self.config.enabled:
            logger.info("Kill switch is disabled")
            return

        with self._lock:
            if should_stop_agent_callback is not None:
                self._on_trip = should_stop_agent_callback
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="kill-switch-monitor"
            )
            self._monitor_thread.start()

        logger.info(
            "Kill switch monitoring started",
            extra={
                "check_interval": self.config.health_check_interval_seconds,
                "failure_threshold": self.config.consecutive_failures_threshold,
                "process_kill_grace": self.config.grace_after_process_kill_seconds,
                "target_health_url": self.config.target_health_url,
            },
        )

    def stop_monitoring(self) -> None:
        """Stop the health monitor and wait for it to exit."""
        self._stop_event.set()
        thread = self._monitor_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        logger.info("Kill switch monitoring stopped")

    def update_config(self, config: KillSwitchConfig) -> None:
        """
        Apply new settings (after a config reload) WITHOUT touching breaker
        state, so reloading the file cannot be used to clear a trip.
        """
        with self._lock:
            self.config = config

    def _monitor_loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"Error in kill switch monitor: {e}", exc_info=True)
            if self._stop_event.wait(self.config.health_check_interval_seconds):
                return

    # ------------------------------------------------------------------
    # One evaluation step (driven by the monitor thread; callable in tests)
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Evaluate once and move the state machine."""
        if not self.config.enabled:
            return

        with self._lock:
            state = self.circuit_state

        if state == CircuitState.CLOSED:
            self._tick_closed()
        elif state == CircuitState.OPEN:
            self._tick_open()
        else:
            self._tick_half_open()

    def _tick_closed(self) -> None:
        now = self._clock()

        # Idle: nothing to protect, so do not count failures.
        if not self._is_armed():
            with self._lock:
                self._armed_since = None
                self.consecutive_failures = 0
            return

        with self._lock:
            if self._armed_since is None:
                self._armed_since = now
            armed_for = now - self._armed_since

        if armed_for < self.config.grace_after_agent_start_seconds:
            logger.debug(f"In startup grace period ({armed_for:.0f}s)")
            return

        if self.failure_context.is_in_process_kill_grace_period(
            self.config.grace_after_process_kill_seconds
        ):
            logger.debug("In process kill grace period, skipping health check")
            return

        if self._probe():
            with self._lock:
                if self.consecutive_failures > 0:
                    logger.info(
                        "Target health check passed, resetting failure count",
                        extra={"previous_failures": self.consecutive_failures},
                    )
                self.consecutive_failures = 0
            return

        with self._lock:
            self.consecutive_failures += 1
            failures = self.consecutive_failures
        logger.warning(
            f"Target health check failed "
            f"({failures}/{self.config.consecutive_failures_threshold})",
            extra={
                "consecutive_failures": failures,
                "active_injections": sorted(
                    self.failure_context.get_active_injections()
                ),
            },
        )
        if failures >= self.config.consecutive_failures_threshold:
            self._trip(f"{failures} consecutive failed health checks")

    def _tick_open(self) -> None:
        with self._lock:
            opened_at = self._opened_at or self._clock()
            if self._clock() - opened_at < self.config.recovery_test_interval_seconds:
                return
            self._set_state(CircuitState.HALF_OPEN)
            self.consecutive_successes = 0
        logger.info("Circuit entering half-open state, testing recovery")

    def _tick_half_open(self) -> None:
        if self._probe():
            with self._lock:
                self.consecutive_successes += 1
                successes = self.consecutive_successes
                if successes < self.config.recovery_success_threshold:
                    return
                self._set_state(CircuitState.CLOSED)
                self.consecutive_failures = 0
                self.consecutive_successes = 0
                self._armed_since = None  # fresh startup grace when chaos resumes
                latched = self.latched = not self.config.resume_after_recovery
            if latched:
                logger.warning(
                    "Target recovered but chaos stays off (resume_after_recovery "
                    "is false). Call POST /kill-switch/reset to resume."
                )
            else:
                logger.info("Target recovered, closing circuit; chaos may resume")
        else:
            # One failure while testing recovery is enough: the target is not
            # ready. Re-open and restart the wait.
            with self._lock:
                self._set_state(CircuitState.OPEN)
                self._opened_at = self._clock()
                self.consecutive_successes = 0
            logger.warning("Recovery test failed, circuit re-opened")

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def _set_state(self, state: CircuitState) -> None:
        with self._lock:
            self.circuit_state = state
            KILL_SWITCH_STATE.set(_STATE_GAUGE[state])

    def _trip(self, reason: str) -> None:
        """Open the breaker, then notify. State changes first, so a failing
        callback cannot leave chaos running."""
        with self._lock:
            self._set_state(CircuitState.OPEN)
            self._opened_at = self._clock()
            self.trip_count += 1
            self.last_trip_reason = reason
            self.last_trip_time = self._clock()
            self.consecutive_successes = 0
            self.latched = not self.config.resume_after_recovery
            callback = self._on_trip
        KILL_SWITCH_TRIPS.inc()

        logger.critical(
            "KILL SWITCH TRIGGERED! Pausing chaos.",
            extra={
                "reason": reason,
                "trip_count": self.trip_count,
                "will_resume": self.config.resume_after_recovery,
            },
        )
        if callback is not None:
            try:
                callback()
            except Exception as e:
                logger.error(f"Kill switch callback failed: {e}", exc_info=True)

    def reset(self, verify: bool = True) -> Tuple[bool, str]:
        """
        Operator override: close the breaker and clear any latch.

        With ``verify`` (the default) the target must pass a health check right
        now, so a reset cannot be used to resume chaos against a target that is
        still down. Returns ``(ok, message)``.
        """
        if verify and not self._probe():
            detail = (self._last_health or {}).get("detail", "health check failed")
            return False, f"target is not healthy ({detail}); not resetting"
        with self._lock:
            self._set_state(CircuitState.CLOSED)
            self.consecutive_failures = 0
            self.consecutive_successes = 0
            self.latched = False
            self._armed_since = None
        logger.info("Kill switch reset by operator")
        return True, "kill switch reset"

    # ------------------------------------------------------------------
    # Health checking
    # ------------------------------------------------------------------

    def _probe(self) -> bool:
        """Run one health check, record it, and count it."""
        healthy = bool(self._health_check())
        KILL_SWITCH_HEALTH_CHECKS.labels(
            result="healthy" if healthy else "unhealthy"
        ).inc()
        return healthy

    def _record_health(self, healthy: bool, detail: str) -> None:
        with self._lock:
            self._last_health = {
                "healthy": healthy,
                "detail": detail,
                "checked_at": self._clock(),
            }

    def _check_target_health(self) -> bool:
        """
        Check whether the target is healthy: any 2xx response is healthy.

        Fails closed. A network error, a timeout, or an unexpected exception
        all count as unhealthy, because a broken safety check must not leave
        chaos running unprotected.
        """
        url = self.config.target_health_url
        try:
            response = requests.get(
                url,
                timeout=self.config.health_check_timeout_seconds,
                allow_redirects=False,
            )
            healthy = 200 <= response.status_code < 300
            self._record_health(healthy, f"HTTP {response.status_code}")
            logger.debug(
                f"Health check: {url} -> {response.status_code}",
                extra={"is_healthy": healthy},
            )
            return healthy
        except requests.exceptions.RequestException as e:
            self._record_health(False, f"{type(e).__name__}")
            logger.debug(f"Health check failed: {e}")
            return False
        except Exception as e:
            self._record_health(False, f"unexpected {type(e).__name__}")
            logger.error(f"Unexpected error in health check: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Current kill switch status."""
        allowed, reason = self.allows_injection()
        with self._lock:
            seconds_until_recovery_test: Optional[float] = None
            if self.circuit_state == CircuitState.OPEN and self._opened_at:
                remaining = self.config.recovery_test_interval_seconds - (
                    self._clock() - self._opened_at
                )
                seconds_until_recovery_test = round(max(remaining, 0), 1)
            return {
                "enabled": self.config.enabled,
                "circuit_state": self.circuit_state.value,
                "allows_injection": allowed,
                "paused_reason": reason or None,
                "latched": self.latched,
                "consecutive_failures": self.consecutive_failures,
                "consecutive_successes": self.consecutive_successes,
                "trip_count": self.trip_count,
                "last_trip_reason": self.last_trip_reason,
                "last_trip_time": self.last_trip_time,
                "seconds_until_recovery_test": seconds_until_recovery_test,
                "resume_after_recovery": self.config.resume_after_recovery,
                "target_health_url": self.config.target_health_url,
                "last_health_check": self._last_health,
                "active_injections": sorted(
                    self.failure_context.get_active_injections()
                ),
                "monitoring_active": self._monitor_thread is not None
                and self._monitor_thread.is_alive(),
            }


# ============================================================================
# Integration Helpers
# ============================================================================


def wrap_injection_with_context(
    failure_type: str, injection_func, failure_context: FailureContext, *args, **kwargs
):
    """
    Wrap a failure injection function to track it in the failure context.

    Kept for compatibility. Prefer :meth:`KillSwitch.injection`, which also
    refuses to run while the breaker is not CLOSED.
    """
    failure_context.mark_injection_start(failure_type)
    try:
        return injection_func(*args, **kwargs)
    finally:
        failure_context.mark_injection_end(failure_type)


# ============================================================================
# Example Configurations
# ============================================================================
# Reference values only. The running agent reads the `kill_switch` section of
# config.yaml (see Config.kill_switch); these are not used at runtime.

# Conservative kill switch (triggers quickly)
CONSERVATIVE_CONFIG = KillSwitchConfig(
    enabled=True,
    consecutive_failures_threshold=2,
    grace_after_process_kill_seconds=30,
    health_check_interval_seconds=5,
)

# Aggressive kill switch (allows more failures)
AGGRESSIVE_CONFIG = KillSwitchConfig(
    enabled=True,
    consecutive_failures_threshold=5,
    grace_after_process_kill_seconds=90,
    health_check_interval_seconds=15,
)

# Production recommended
PRODUCTION_CONFIG = KillSwitchConfig(
    enabled=True,
    consecutive_failures_threshold=3,
    grace_after_process_kill_seconds=60,
    grace_after_agent_start_seconds=30,
    health_check_interval_seconds=10,
    recovery_test_interval_seconds=120,
    recovery_success_threshold=3,
)
