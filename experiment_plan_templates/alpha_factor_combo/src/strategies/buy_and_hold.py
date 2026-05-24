"""Naive baseline: always long."""

from __future__ import annotations


def buy_and_hold_signal(n_periods: int) -> list[int]:
    return [1] * n_periods
