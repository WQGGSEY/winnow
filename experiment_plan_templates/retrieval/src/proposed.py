"""Stand-in for the 'method under test'. Replace with your real ranker."""

from __future__ import annotations

from src.data import dataset_and_relevance


def proposed_ranker(query_id: str) -> list[str]:
    ground = dataset_and_relevance()[query_id]
    # near-perfect ranker that swaps the tail of the relevance order
    return ground[:7] + list(reversed(ground[7:]))
