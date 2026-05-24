"""Toy retrieval evaluation entrypoint.

The harness materializes this file (and its sibling modules) into the node
workspace before running. We deliberately use plain Python so the example
runs without external dependencies; swap in real model/data/eval modules
when you adapt this template to your project.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.data import dataset_and_relevance
from src.proposed import proposed_ranker
from src.baselines.current_best import current_best_ranker
from src.baselines.naive import naive_ranker
from src.baselines.random_baseline import random_ranker
from src.eval.ndcg import mean_ndcg


def main() -> None:
    artifacts = Path("artifacts")
    artifacts.mkdir(exist_ok=True)

    relevance = dataset_and_relevance()
    proposed = mean_ndcg(proposed_ranker, relevance)
    current_best = mean_ndcg(current_best_ranker, relevance)
    naive = mean_ndcg(naive_ranker, relevance)
    random_score = mean_ndcg(random_ranker, relevance)

    beats_best = proposed > current_best
    beats_naive = proposed > naive
    beats_random = proposed > random_score
    if beats_best and beats_naive and beats_random:
        verdict = "supported"
    elif not (beats_naive and beats_random):
        verdict = "contradicted"
    else:
        verdict = "inconclusive"

    payload = {
        "metrics": {"ndcg_at_10": proposed},
        "baselines": {
            "current_best_known": current_best,
            "naive": naive,
            "random_or_null": random_score,
        },
        "claim_verdict_candidate": verdict,
        "disproof_conditions_hit": (
            [] if beats_naive else ["nDCG@10 within noise of naive baseline"]
        ),
        "unexpected_observations": [],
    }
    (artifacts / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n")
    (artifacts / "run.log").write_text(
        f"proposed={proposed:.4f} current_best={current_best:.4f} "
        f"naive={naive:.4f} random={random_score:.4f}\n"
    )
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
