"""Deterministic sampling of scenario parameters from a seed."""

import random
from typing import Any

from chaos.model import Choice, IntRange, Parameter

MAX_OFFSET_SECONDS = 60


def sample(parameters: dict[str, Parameter], seed: int) -> dict[str, Any]:
    """Sample one value per parameter. The same seed gives the same values.

    Parameters are drawn in name order, so adding a parameter to a scenario
    does not change what the existing ones get for a given seed.
    """
    rng = random.Random(seed)  # noqa: S311 - reproducibility, not security
    values: dict[str, Any] = {}
    for name in sorted(parameters):
        spec = parameters[name]
        sub = random.Random(f"{seed}:{name}")  # noqa: S311
        if isinstance(spec, Choice):
            values[name] = sub.choice(spec.choice)
        elif isinstance(spec, IntRange):
            values[name] = sub.randint(*spec.int_range)
    # A random delay before injection, so timing cannot be memorized either.
    values["offset_seconds"] = rng.randint(0, MAX_OFFSET_SECONDS)
    return values
