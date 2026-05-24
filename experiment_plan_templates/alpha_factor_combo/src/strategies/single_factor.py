"""Best-single-factor baseline: pick the factor with higher in-sample IC and trade it."""

from __future__ import annotations


def _ic(factor: list[float], returns: list[float]) -> float:
    """Spearman-ish rank correlation proxy (cheap, deterministic, stdlib)."""

    if len(factor) < 2:
        return 0.0
    n = len(factor)
    # Use covariance / (std * std) on the raw values; cheap enough for the
    # tiny synthetic series and good enough to pick the better factor.
    mean_f = sum(factor) / n
    mean_r = sum(returns) / n
    cov = sum((f - mean_f) * (r - mean_r) for f, r in zip(factor, returns)) / n
    var_f = sum((f - mean_f) ** 2 for f in factor) / n
    var_r = sum((r - mean_r) ** 2 for r in returns) / n
    if var_f <= 0 or var_r <= 0:
        return 0.0
    return cov / (var_f**0.5 * var_r**0.5)


def best_single_factor_signal(
    factor_a: list[float],
    factor_b: list[float],
    returns_for_ic: list[float],
) -> list[int]:
    # Use a leading-period IC (computed on the held series) just to pick which
    # factor to trade. The chosen factor's own sign decides each period.
    ic_a = _ic(factor_a, returns_for_ic)
    ic_b = _ic(factor_b, returns_for_ic)
    chosen = factor_a if ic_a >= ic_b else factor_b
    return [1 if v > 0 else -1 for v in chosen]
