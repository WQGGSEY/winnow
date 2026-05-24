"""Null baseline: random long/short, seeded for reproducibility."""

from __future__ import annotations

import random


def random_signal(n_periods: int, *, seed: int) -> list[int]:
    rng = random.Random(seed)
    return [1 if rng.random() > 0.5 else -1 for _ in range(n_periods)]
