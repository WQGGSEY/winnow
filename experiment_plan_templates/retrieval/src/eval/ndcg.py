"""nDCG@k computation. Replace with TREC-style evaluator for real work."""

from __future__ import annotations

import math
from typing import Callable


def ndcg_at_k(retrieved: list[str], ground_order: list[str], k: int = 10) -> float:
    rel_for = {doc: max(0, len(ground_order) - rank) for rank, doc in enumerate(ground_order)}
    dcg = 0.0
    for i, doc in enumerate(retrieved[:k]):
        gain = rel_for.get(doc, 0)
        dcg += (2 ** gain - 1) / math.log2(i + 2)
    ideal = sorted([rel_for[d] for d in ground_order], reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def mean_ndcg(
    ranker: Callable[[str], list[str]],
    relevance: dict[str, list[str]],
) -> float:
    return sum(ndcg_at_k(ranker(q), relevance[q]) for q in relevance) / len(relevance)
