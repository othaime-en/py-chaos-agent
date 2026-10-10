"""
Injection lifecycle: overlap guard, abort, shutdown, and cleanup.

Three problems this module solves:

1. **Overlap.** Only one injection per failure type may run at a time. A second
   request (a loop tick landing on a manual API call, say) is refused instead of
   stacking on top of the first and corrupting its cleanup.

2. **Interruptible holds.** Injections hold their effect for ``duration``
   seconds. They hold it through :func:`sleep`, which returns early when the
   injection is aborted (``POST /agent/stop``, the kill switch) or the process
   is shutting down. The injection's own ``finally`` then removes its effect, so
   an abort takes effect in milliseconds rather than at the end of the duration.

3. **Shutdown cleanup.** Anything that leaves state behind (a ``tc`` rule)
   registers a cleanup function here. :meth:`Lifecycle.shutdown` wakes every
   in-flight injection and runs the cleanups. Entry points call it from signal
   handlers, from the API lifespan, and from ``atexit``, so cleanup does not
   depend on which path the process takes out.

What this cannot do: run code after SIGKILL or an OOM kill. For that, the
network module repairs stale state at the next startup.

Locks are re-entrant (``RLock``) because :meth:`shutdown` is called from signal
handlers, which run on the main thread, possibly while that same thread is
inside one of these critical sections.
"""

import atexit
import contextvars
import threading
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from .logging_config import get_logger
from .metrics import INJECTIONS_TOTAL

logger = get_logger(__name__)

# The ticket of the injection running in the current thread/context, so that
# deeply nested code (a worker loop, a memory hold) can call sleep() without
# threading a parameter through every signature.
_current_ticket: "contextvars.ContextVar[Optional[InjectionTicket]]" = (
    contextvars.ContextVar("chaos_current_ticket", default=None)
)


class InjectionTicket:
    """Proof that this injection holds the guard for its failure type."""

    __slots__ = ("failure_type", "generation", "_owner")

    def __init__(self, owner: "Lifecycle", failure_type: str, generation: int):
        self._owner = owner
        self.failure_type = failure_type
        self.generation = generation

    def sleep(self, seconds: float) -> bool:
        """Wait up to ``seconds``. True if cut short by an abort or shutdown."""
        return self._owner._sleep(seconds, self.generation)

    @property
    def interrupted(self) -> bool:
        return self._owner._interrupted(self.generation)


class Lifecycle:
    def __init__(self) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._generation = 0
        self._shutting_down = False
        self._active: Dict[str, InjectionTicket] = {}
        self._cleanups: Dict[str, Callable[[], None]] = {}
        self._cleanup_lock = threading.RLock()
        self._atexit_installed = False

    # -- overlap guard -----------------------------------------------------

    def begin(self, failure_type: str) -> Tuple[Optional[InjectionTicket], str]:
        """
        Try to start an injection of ``failure_type``.

        Returns ``(ticket, "")`` on success, or ``(None, reason)`` if one of
        that type is already running or the process is shutting down.
        """
        with self._cond:
            if self._shutting_down:
                return None, "the agent is shutting down"
            if failure_type in self._active:
                return None, f"a {failure_type} injection is already running"
            ticket = InjectionTicket(self, failure_type, self._generation)
            self._active[failure_type] = ticket
            return ticket, ""

    def end(self, ticket: InjectionTicket) -> None:
        with self._cond:
            if self._active.get(ticket.failure_type) is ticket:
                del self._active[ticket.failure_type]
            self._cond.notify_all()

    def begin_or_skip(self, failure_type: str) -> Optional[InjectionTicket]:
        """
        Like :meth:`begin`, but on refusal logs the reason and counts the
        request as ``skipped`` so callers can simply return.
        """
        ticket, reason = self.begin(failure_type)
        if ticket is None:
            logger.warning(
                "Injection skipped",
                extra={"failure_type": failure_type, "reason": reason},
            )
            INJECTIONS_TOTAL.labels(failure_type=failure_type, status="skipped").inc()
        return ticket

    @contextmanager
    def injection(
        self, failure_type: str, enabled: bool = True
    ) -> Iterator[Optional[InjectionTicket]]:
        """
        Hold the guard for the duration of the ``with`` block.

        Yields the ticket, or ``None`` if the request was refused (already
        logged and counted). With ``enabled=False`` (dry runs) nothing is held
        and ``None`` is yielded without any refusal being recorded.
        """
        ticket = self.begin_or_skip(failure_type) if enabled else None
        token = _current_ticket.set(ticket) if ticket is not None else None
        try:
            yield ticket
        finally:
            if token is not None:
                _current_ticket.reset(token)
            if ticket is not None:
                self.end(ticket)

    @contextmanager
    def bound(self, ticket: Optional[InjectionTicket]) -> Iterator[None]:
        """Make ``ticket`` current in this thread (for injections that hand
        their ticket to a worker thread)."""
        token = _current_ticket.set(ticket)
        try:
            yield
        finally:
            _current_ticket.reset(token)

    def is_active(self, failure_type: str) -> bool:
        with self._cond:
            return failure_type in self._active

    def active_types(self) -> List[str]:
        with self._cond:
            return sorted(self._active)

    # -- abort and shutdown ------------------------------------------------

    def abort_active(self, reason: str = "") -> int:
        """
        Cut short every injection that is running right now. Injections that
        start afterwards are unaffected. Returns how many were running.
        """
        with self._cond:
            count = len(self._active)
            self._generation += 1
            self._cond.notify_all()
        if count:
            logger.warning(
                "Aborting in-flight injections",
                extra={"count": count, "reason": reason},
            )
        return count

    def shutdown(self, reason: str = "") -> None:
        """
        Begin process shutdown: refuse new injections, wake and abort running
        ones, and run all registered cleanups. Safe to call repeatedly and from
        signal handlers.
        """
        with self._cond:
            first = not self._shutting_down
            self._shutting_down = True
            self._generation += 1
            self._cond.notify_all()
        if first:
            logger.info("Shutdown initiated", extra={"reason": reason})
        self.run_cleanups()

    def is_shutting_down(self) -> bool:
        with self._cond:
            return self._shutting_down

    # -- interruptible waiting --------------------------------------------

    def _interrupted(self, generation: int) -> bool:
        with self._cond:
            return self._shutting_down or self._generation != generation

    def _sleep(self, seconds: float, generation: int) -> bool:
        with self._cond:
            return bool(
                self._cond.wait_for(
                    lambda: self._shutting_down or self._generation != generation,
                    timeout=max(seconds, 0),
                )
            )

    def sleep(self, seconds: float) -> bool:
        """
        Interruptible sleep for the current injection. Returns True if it was
        cut short. Outside an injection it still wakes on shutdown.
        """
        ticket = _current_ticket.get()
        if ticket is not None:
            return ticket.sleep(seconds)
        with self._cond:
            generation = self._generation
        return self._sleep(seconds, generation)

    # -- cleanup registry --------------------------------------------------

    def register_cleanup(self, name: str, fn: Callable[[], None]) -> None:
        """Register an idempotent function that removes state left behind."""
        with self._cleanup_lock:
            self._cleanups[name] = fn

    def run_cleanups(self) -> None:
        """Run every registered cleanup. One failing does not stop the rest."""
        with self._cleanup_lock:
            cleanups = list(self._cleanups.items())
        for name, fn in cleanups:
            try:
                fn()
            except Exception as e:  # never let one cleanup block the others
                logger.error(
                    "Cleanup failed",
                    exc_info=True,
                    extra={"cleanup": name, "error": str(e)},
                )

    def install_atexit(self) -> None:
        """Run cleanups on normal interpreter exit. Idempotent."""
        with self._cleanup_lock:
            if self._atexit_installed:
                return
            atexit.register(self.run_cleanups)
            self._atexit_installed = True

    # -- testing -----------------------------------------------------------

    def reset(self) -> None:
        """Forget active injections and shutdown state (registered cleanups
        are kept). For tests."""
        with self._cond:
            self._generation = 0
            self._shutting_down = False
            self._active.clear()
            self._cond.notify_all()


# Process-wide instance used by the injectors and entry points.
lifecycle = Lifecycle()
