"""Proposed strategy: equal-weight ensemble of two alpha factors."""

from __future__ import annotations


def ensemble_signal(factor_a: list[float], factor_b: list[float]) -> list[int]:
    return [1 if (a + b) / 2.0 > 0 else -1 for a, b in zip(factor_a, factor_b)]
