"""The one place that decides whether an injection fires this tick."""


def should_inject(probability: float, roll: float) -> bool:
    """
    True if an injection with this ``probability`` should fire for ``roll``.

    ``roll`` comes from ``random.random()``, which is in [0.0, 1.0). Firing when
    ``roll < probability`` makes the edges exact:

    * probability 0.0 never fires, even for a roll of exactly 0.0
      (``roll > probability`` skipped only rolls ABOVE it, so 0.0 could fire)
    * probability 1.0 always fires, since no roll reaches 1.0
    * a roll equal to the probability does not fire
    """
    return roll < probability
