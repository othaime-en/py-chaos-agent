"""Lockout for repeated failed authentication."""

import pytest

from src import ratelimit
from src.ratelimit import FailureThrottle, ThrottleSettings


class Clock:
    """A controllable replacement for time.monotonic."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(ratelimit.time, "monotonic", c)
    return c


def throttle(max_failures=3, window=60, lockout=300, **kwargs):
    return FailureThrottle(ThrottleSettings(max_failures, window, lockout), **kwargs)


class TestLockout:
    def test_failures_below_the_limit_do_not_lock(self, clock):
        t = throttle()
        assert [t.record_failure("a"), t.record_failure("a")] == [False, False]
        assert t.retry_after("a") is None

    def test_the_failure_that_reaches_the_limit_starts_a_lockout(self, clock):
        t = throttle()
        results = [t.record_failure("a") for _ in range(3)]
        assert results == [False, False, True]
        assert t.retry_after("a") == 300

    def test_retry_after_counts_down_and_rounds_up(self, clock):
        t = throttle(lockout=300)
        for _ in range(3):
            t.record_failure("a")
        clock.advance(100.2)
        assert t.retry_after("a") == 200

    def test_the_lockout_ends(self, clock):
        t = throttle(lockout=300)
        for _ in range(3):
            t.record_failure("a")
        clock.advance(300)
        assert t.retry_after("a") is None

    def test_after_a_lockout_the_client_starts_clean(self, clock):
        t = throttle()
        for _ in range(3):
            t.record_failure("a")
        clock.advance(301)
        assert [t.record_failure("a"), t.record_failure("a")] == [False, False]
        assert t.record_failure("a") is True

    def test_failures_outside_the_window_are_forgotten(self, clock):
        t = throttle(window=60)
        t.record_failure("a")
        t.record_failure("a")
        clock.advance(61)
        assert t.record_failure("a") is False  # only 1 recent failure
        assert t.retry_after("a") is None

    def test_clients_are_tracked_separately(self, clock):
        t = throttle()
        for _ in range(3):
            t.record_failure("attacker")
        assert t.retry_after("attacker") is not None
        assert t.retry_after("someone-else") is None

    def test_failures_during_a_lockout_do_not_extend_it(self, clock):
        t = throttle(lockout=300)
        for _ in range(3):
            t.record_failure("a")
        clock.advance(200)
        assert t.record_failure("a") is False
        assert t.retry_after("a") == 100

    def test_a_success_clears_the_failure_count(self, clock):
        t = throttle()
        t.record_failure("a")
        t.record_failure("a")
        t.record_success("a")
        assert t.record_failure("a") is False
        assert t.tracked_clients() == 1

    def test_a_success_does_not_lift_a_lockout(self, clock):
        t = throttle()
        for _ in range(3):
            t.record_failure("a")
        t.record_success("a")
        assert t.retry_after("a") is not None

    def test_a_clean_client_leaves_no_state(self, clock):
        t = throttle()
        t.record_failure("a")
        t.record_success("a")
        assert t.tracked_clients() == 0

    def test_reset_forgets_everyone(self, clock):
        t = throttle()
        for _ in range(3):
            t.record_failure("a")
        t.reset()
        assert t.retry_after("a") is None and t.tracked_clients() == 0


class TestDisabled:
    def test_zero_failures_turns_it_off(self, clock):
        t = throttle(max_failures=0)
        assert not t.settings.enabled
        assert [t.record_failure("a") for _ in range(50)] == [False] * 50
        assert t.retry_after("a") is None
        assert t.tracked_clients() == 0


class TestMemoryIsBounded:
    def test_many_addresses_cannot_grow_it_without_limit(self, clock):
        t = throttle(max_clients=100)
        for i in range(1000):
            t.record_failure(f"10.0.{i // 256}.{i % 256}")
        assert t.tracked_clients() <= 100

    def test_locked_clients_are_kept_in_preference_to_idle_ones(self, clock):
        t = throttle(max_clients=10)
        for _ in range(3):
            t.record_failure("locked")
        for i in range(50):
            t.record_failure(f"noise-{i}")
        assert t.retry_after("locked") is not None


class TestSettingsFromEnvironment:
    ENV = (
        ratelimit.MAX_FAILURES_ENV,
        ratelimit.WINDOW_ENV,
        ratelimit.LOCKOUT_ENV,
    )

    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        for name in self.ENV:
            monkeypatch.delenv(name, raising=False)

    def test_defaults(self):
        s = ratelimit.settings_from_env()
        assert (s.max_failures, s.window_seconds, s.lockout_seconds) == (5, 60, 300)
        assert s.enabled

    def test_values_are_read(self, monkeypatch):
        monkeypatch.setenv(ratelimit.MAX_FAILURES_ENV, "10")
        monkeypatch.setenv(ratelimit.WINDOW_ENV, "30")
        monkeypatch.setenv(ratelimit.LOCKOUT_ENV, "900")
        s = ratelimit.settings_from_env()
        assert (s.max_failures, s.window_seconds, s.lockout_seconds) == (10, 30, 900)

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv(ratelimit.MAX_FAILURES_ENV, "0")
        assert not ratelimit.settings_from_env().enabled

    @pytest.mark.parametrize(
        "name, value",
        [
            (ratelimit.MAX_FAILURES_ENV, "many"),
            (ratelimit.MAX_FAILURES_ENV, "-1"),
            (ratelimit.WINDOW_ENV, "0"),
            (ratelimit.LOCKOUT_ENV, "soon"),
            (ratelimit.LOCKOUT_ENV, "0"),
        ],
    )
    def test_bad_values_are_an_error_not_a_silent_default(
        self, monkeypatch, name, value
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            ratelimit.settings_from_env()
