"""The probability rule: exact at the edges, and one rule for both loops."""

import random

import pytest

from src.chance import should_inject


class TestEdges:
    def test_probability_zero_never_fires_even_for_a_roll_of_zero(self):
        """`roll > probability` let a roll of exactly 0.0 through at 0.0."""
        assert should_inject(0.0, 0.0) is False

    def test_probability_zero_never_fires_for_any_roll(self):
        rng = random.Random(1234)
        assert not any(should_inject(0.0, rng.random()) for _ in range(100_000))

    def test_probability_one_always_fires_for_any_possible_roll(self):
        largest_roll = 1 - 2**-53  # the biggest value random.random() can return
        assert should_inject(1.0, 0.0) is True
        assert should_inject(1.0, largest_roll) is True

    def test_probability_one_always_fires(self):
        rng = random.Random(99)
        assert all(should_inject(1.0, rng.random()) for _ in range(100_000))

    def test_a_roll_equal_to_the_probability_does_not_fire(self):
        assert should_inject(0.5, 0.5) is False

    @pytest.mark.parametrize(
        "probability,roll,expected",
        [
            (0.3, 0.0, True),
            (0.3, 0.2999, True),
            (0.3, 0.3, False),
            (0.3, 0.3001, False),
            (0.3, 0.9999, False),
        ],
    )
    def test_threshold(self, probability, roll, expected):
        assert should_inject(probability, roll) is expected


class TestDistribution:
    @pytest.mark.parametrize("probability", [0.1, 0.25, 0.5, 0.9])
    def test_fires_about_probability_of_the_time(self, probability):
        rng = random.Random(2024)
        n = 40_000
        fired = sum(should_inject(probability, rng.random()) for _ in range(n))
        # ~5 standard deviations of slack: stable for a fixed seed
        assert abs(fired / n - probability) < 0.012


class TestBothLoopsUseIt:
    """The standalone agent and the API loop must share the rule."""

    def test_agent_uses_the_shared_rule(self):
        import inspect

        from src import agent

        source = inspect.getsource(agent.main)
        assert "should_inject(" in source
        assert "roll > probability" not in source

    def test_api_loop_uses_the_shared_rule(self):
        import inspect

        from src import api

        source = inspect.getsource(api.run_agent_loop)
        assert "should_inject(" in source
        assert "random.random() > probability" not in source
