"""Stand-in 'current best known' baseline."""

from __future__ import annotations

from src.data import dataset_and_relevance


def current_best_ranker(query_id: str) -> list[str]:
    ground = dataset_and_relevance()[query_id]
    return ground[:5] + ground[5:][::-1]
