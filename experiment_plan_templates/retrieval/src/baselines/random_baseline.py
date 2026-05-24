"""Seeded random baseline so the demo is deterministic."""

from __future__ import annotations

import random

from src.data import dataset_and_relevance


def random_ranker(query_id: str) -> list[str]:
    rng = random.Random(hash(query_id) & 0xFFFFFFFF)
    items = list(dataset_and_relevance()[query_id])
    rng.shuffle(items)
    return items
