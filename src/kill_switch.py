"""
Kill Switch / Circuit Breaker for Py-Chaos-Agent.

Automatically stops chaos when target application experiences unexpected failures.
Accounts for expected failures from chaos injections (especially process kills).
"""

import time
import logging
import threading
from typing import Optional, Set
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation, chaos running
    OPEN = "open"          # Circuit tripped, chaos stopped
    HALF_OPEN = "half_open"  # Testing if system recovered


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
    
    # Target health endpoint
    target_health_url: str = "http://localhost:8080/health"
    health_check_timeout_seconds: int = 2


class FailureContext:
    """
    Track active chaos injections to differentiate expected vs unexpected failures.
    
    When process injection is active, we expect the app to be down temporarily.
    This prevents false positives in the kill switch.
    """
    
    def __init__(self):
        self._active_injections: Set[str] = set()
        self._lock = threading.Lock()
        self._last_process_kill_time: Optional[float] = None
    
    def mark_injection_start(self, failure_type: str) -> None:
        """Record that a failure injection has started."""
        with self._lock:
            self._active_injections.add(failure_type)
            if failure_type == "process":
                self._last_process_kill_time = time.time()
                logger.info("Process kill injection started, entering grace period")
    
    def mark_injection_end(self, failure_type: str) -> None:
        """Record that a failure injection has completed."""
        with self._lock:
            self._active_injections.discard(failure_type)
    
    def is_in_process_kill_grace_period(self, grace_seconds: int) -> bool:
        """Check if we're within grace period after a process kill."""
        with self._lock:
            if not self._last_process_kill_time:
                return False
            
            elapsed = time.time() - self._last_process_kill_time
            return elapsed < grace_seconds
    
    def get_active_injections(self) -> Set[str]:
        """Get copy of currently active injections."""
        with self._lock:
            return self._active_injections.copy()


class KillSwitch:
    """
    Automatic kill switch that stops chaos when unexpected failures occur.
    
    Features:
    - Consecutive failure detection
    - Grace period after process kills
    - Circuit breaker pattern with recovery testing
    - Differentiates expected (chaos-induced) from unexpected failures
    """
    
    def __init__(self, config: KillSwitchConfig):
        self.config = config
        self.failure_context = FailureContext()
        
        # State tracking
        self.circuit_state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.agent_start_time: Optional[float] = None
        
        # Thread control
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._should_stop_agent_callback = None
    
    def start_monitoring(self, should_stop_agent_callback) -> None:
        """
        Start the health monitoring loop.
        
        Args:
            should_stop_agent_callback: Function to call to stop the chaos agent
        """
        if not self.config.enabled:
            logger.info("Kill switch is disabled")
            return
        
        self._should_stop_agent_callback = should_stop_agent_callback
        self.agent_start_time = time.time()
        self._stop_event.clear()
        
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="kill-switch-monitor"
        )
        self._monitor_thread.start()
        
        logger.info(
            "Kill switch monitoring started",
            extra={
                "check_interval": self.config.health_check_interval_seconds,
                "failure_threshold": self.config.consecutive_failures_threshold,
                "process_kill_grace": self.config.grace_after_process_kill_seconds,
            }
        )
    
    def stop_monitoring(self) -> None:
        """Stop the health monitoring loop."""
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("Kill switch monitoring stopped")
    
    def _monitor_loop(self) -> None:
        """Main monitoring loop that runs in background thread."""
        while not self._stop_event.is_set():
            try:
                # Check if we should trip the kill switch
                if self._should_trip_kill_switch():
                    logger.critical(
                        "KILL SWITCH TRIGGERED! Stopping chaos agent.",
                        extra={
                            "consecutive_failures": self.consecutive_failures,
                            "circuit_state": self.circuit_state.value,
                        }
                    )
                    
                    # Trigger the callback to stop the agent
                    if self._should_stop_agent_callback:
                        self._should_stop_agent_callback()
                    
                    # Open the circuit
                    self.circuit_state = CircuitState.OPEN
                    
                    # Wait before testing recovery
                    self._stop_event.wait(self.config.recovery_test_interval_seconds)
                    
                    # Try half-open state
                    if not self._stop_event.is_set():
                        self.circuit_state = CircuitState.HALF_OPEN
                        logger.info("Circuit entering half-open state, testing recovery")
                
                # Sleep until next check
                self._stop_event.wait(self.config.health_check_interval_seconds)
                
            except Exception as e:
                logger.error(f"Error in kill switch monitor loop: {e}", exc_info=True)
                self._stop_event.wait(self.config.health_check_interval_seconds)
    
    def _should_trip_kill_switch(self) -> bool:
        """
        Determine if kill switch should trigger.
        
        Returns:
            True if chaos should be stopped, False otherwise
        """
        # Don't trip if circuit is already open
        if self.circuit_state == CircuitState.OPEN:
            return False
        
        # Grace period after agent start
        if self.agent_start_time:
            elapsed = time.time() - self.agent_start_time
            if elapsed < self.config.grace_after_agent_start_seconds:
                logger.debug(f"In startup grace period ({elapsed:.0f}s)")
                return False
        
        # Grace period after process kill
        if self.failure_context.is_in_process_kill_grace_period(
            self.config.grace_after_process_kill_seconds
        ):
            logger.debug("In process kill grace period, skipping health check")
            return False
        
        # Check target health
        is_healthy = self._check_target_health()
        
        if not is_healthy:
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            
            logger.warning(
                f"Target health check failed ({self.consecutive_failures}/{self.config.consecutive_failures_threshold})",
                extra={
                    "consecutive_failures": self.consecutive_failures,
                    "active_injections": list(self.failure_context.get_active_injections()),
                }
            )
            
            # Trip if threshold exceeded
            if self.consecutive_failures >= self.config.consecutive_failures_threshold:
                return True
        else:
            # Health check passed
            if self.consecutive_failures > 0:
                logger.info(
                    "Target health check passed, resetting failure count",
                    extra={"previous_failures": self.consecutive_failures}
                )
            
            self.consecutive_failures = 0
            self.consecutive_successes += 1
            
            # If in half-open state, check if we can close the circuit
            if self.circuit_state == CircuitState.HALF_OPEN:
                if self.consecutive_successes >= self.config.recovery_success_threshold:
                    logger.info("System recovered, closing circuit")
                    self.circuit_state = CircuitState.CLOSED
                    self.consecutive_successes = 0
        
        return False
    
    def _check_target_health(self) -> bool:
        """
        Check if target application is healthy.
        
        Returns:
            True if healthy, False otherwise
        """
        try:
            import requests
            
            response = requests.get(
                self.config.target_health_url,
                timeout=self.config.health_check_timeout_seconds
            )
            
            is_healthy = response.status_code == 200
            
            logger.debug(
                f"Health check: {self.config.target_health_url} -> {response.status_code}",
                extra={"is_healthy": is_healthy}
            )
            
            return is_healthy
            
        except requests.exceptions.RequestException as e:
            logger.debug(f"Health check failed: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error in health check: {e}", exc_info=True)
            # Fail open - don't stop chaos on monitoring errors
            return True
    
    def get_status(self) -> dict:
        """Get current kill switch status."""
        return {
            "enabled": self.config.enabled,
            "circuit_state": self.circuit_state.value,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "active_injections": list(self.failure_context.get_active_injections()),
            "monitoring_active": self._monitor_thread is not None and self._monitor_thread.is_alive(),
        }


# ============================================================================
# Integration Helpers
# ============================================================================

def wrap_injection_with_context(
    failure_type: str,
    injection_func,
    failure_context: FailureContext,
    *args,
    **kwargs
):
    """
    Wrap a failure injection function to track it in the failure context.
    
    Usage:
        wrap_injection_with_context(
            "cpu",
            inject_cpu,
            failure_context,
            config,
            dry_run=False
        )
    """
    failure_context.mark_injection_start(failure_type)
    try:
        return injection_func(*args, **kwargs)
    finally:
        failure_context.mark_injection_end(failure_type)


# ============================================================================
# Example Configuration
# ============================================================================

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