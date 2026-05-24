"""Synthetic price-return data + two partially correlated alpha factors.

A real project would swap this for an actual returns loader (parquet,
S3, internal feed) and proper factor construction. Here we keep it
purely deterministic and standard-library-only.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


SEED = 42
N_PERIODS = 250


@dataclass(frozen=True)
class Series:
    returns: list[float]
    factor_a: list[float]
    factor_b: list[float]


def _gaussian(rng: random.Random, mu: float, sigma: float) -> float:
    return rng.gauss(mu, sigma)


def make_series(*, seed: int = SEED, n_periods: int = N_PERIODS) -> Series:
    """Deterministic synthetic returns + two noisy forward-looking signals.

    Both factors are noisy versions of the *next-period* return (positive
    predictive correlation), but they share only part of their noise.
    Equal-weight averaging of the two factors should therefore have
    higher signal-to-noise than either factor alone.
    """

    rng = random.Random(seed)
    # Daily log returns with small positive drift + 1% vol.
    returns = [_gaussian(rng, 0.0003, 0.01) for _ in range(n_periods)]

    factor_a: list[float] = []
    factor_b: list[float] = []
    for t in range(n_periods):
        forward = returns[t + 1] if t + 1 < n_periods else 0.0
        shared = _gaussian(rng, 0.0, 0.5)
        idio_a = _gaussian(rng, 0.0, 0.7)
        idio_b = _gaussian(rng, 0.0, 0.7)
        # Both factors load on next-period return plus shared + idiosyncratic noise.
        factor_a.append(80.0 * forward + shared + idio_a)
        factor_b.append(80.0 * forward + shared + idio_b)
    return Series(returns=returns, factor_a=factor_a, factor_b=factor_b)
