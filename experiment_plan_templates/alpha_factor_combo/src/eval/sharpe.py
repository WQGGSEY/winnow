"""Annualized Sharpe ratio over a series of strategy returns.

Strategy return at period t is signal[t] * underlying_return[t+1]; the
final period drops because there is no next-period return.
"""

from __future__ import annotations

import math
from typing import Iterable


def strategy_returns(signal: list[int], underlying: list[float]) -> list[float]:
    return [signal[t] * underlying[t + 1] for t in range(len(signal) - 1)]


def sharpe(returns: Iterable[float], *, periods_per_year: int = 252) -> float:
    rs = list(returns)
    if len(rs) < 2:
        return 0.0
    mean = sum(rs) / len(rs)
    var = sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)
    if var <= 0:
        return 0.0
    return (mean / math.sqrt(var)) * math.sqrt(periods_per_year)
