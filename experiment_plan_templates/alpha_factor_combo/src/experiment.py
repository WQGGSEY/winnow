"""Backtest entrypoint for the alpha_factor_combo template."""

from __future__ import annotations

import json
from pathlib import Path

from src.data import SEED, make_series
from src.eval.sharpe import sharpe, strategy_returns
from src.strategies.buy_and_hold import buy_and_hold_signal
from src.strategies.ensemble import ensemble_signal
from src.strategies.random_signal import random_signal
from src.strategies.single_factor import best_single_factor_signal


def main() -> None:
    artifacts = Path("artifacts")
    artifacts.mkdir(exist_ok=True)

    series = make_series()
    n = len(series.returns)

    ensemble = ensemble_signal(series.factor_a, series.factor_b)
    single = best_single_factor_signal(
        series.factor_a, series.factor_b, series.returns
    )
    hold = buy_and_hold_signal(n)
    rand = random_signal(n, seed=SEED + 7)

    sharpe_ensemble = sharpe(strategy_returns(ensemble, series.returns))
    sharpe_single = sharpe(strategy_returns(single, series.returns))
    sharpe_hold = sharpe(strategy_returns(hold, series.returns))
    sharpe_random = sharpe(strategy_returns(rand, series.returns))

    beats_single = sharpe_ensemble > sharpe_single
    beats_naive = sharpe_ensemble > sharpe_hold
    beats_random = sharpe_ensemble > sharpe_random
    if beats_single and beats_naive and beats_random:
        verdict = "supported"
    elif not (beats_naive and beats_random):
        verdict = "contradicted"
    else:
        verdict = "inconclusive"

    disproof: list[str] = []
    if not beats_naive:
        disproof.append("ensemble Sharpe did not exceed buy-and-hold")
    if not beats_random:
        disproof.append("ensemble Sharpe did not exceed random signal")

    payload = {
        "metrics": {"sharpe_ratio": sharpe_ensemble},
        "baselines": {
            "current_best_known": sharpe_single,
            "naive": sharpe_hold,
            "random_or_null": sharpe_random,
        },
        "claim_verdict_candidate": verdict,
        "disproof_conditions_hit": disproof,
        "unexpected_observations": [
            {
                "observation": (
                    "Ensemble Sharpe is bounded by the residual correlation between "
                    "the two factors; averaging buys signal-to-noise only when the "
                    "idiosyncratic components dominate the shared noise."
                ),
                "evidence": (
                    f"ensemble={sharpe_ensemble:.3f} single={sharpe_single:.3f} "
                    f"buy_and_hold={sharpe_hold:.3f} random={sharpe_random:.3f}"
                ),
                "scope_relation": "within_claim",
                "suggested_branch_type": "boundary",
            }
        ]
        if beats_single
        else [],
    }
    (artifacts / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n")
    (artifacts / "run.log").write_text(
        f"ensemble={sharpe_ensemble:.4f} single={sharpe_single:.4f} "
        f"buy_and_hold={sharpe_hold:.4f} random={sharpe_random:.4f}\n"
    )
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
