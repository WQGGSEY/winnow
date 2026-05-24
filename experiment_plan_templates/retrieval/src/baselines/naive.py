"""Naive baseline: lexicographic doc id ordering."""

from __future__ import annotations

from src.data import dataset_and_relevance


def naive_ranker(query_id: str) -> list[str]:
    return sorted(dataset_and_relevance()[query_id])
