"""Tiny in-memory retrieval dataset used by the template.

Replace this with your real loader (e.g. BEIR, LegalBench, etc.).
"""

from __future__ import annotations


def dataset_and_relevance() -> dict[str, list[str]]:
    """Map of query_id -> doc_ids ordered by descending ground-truth relevance."""

    return {
        "q1": ["d3", "d7", "d1", "d5", "d9", "d2", "d8", "d4", "d6", "d10"],
        "q2": ["d6", "d2", "d9", "d4", "d1", "d3", "d8", "d7", "d10", "d5"],
        "q3": ["d8", "d4", "d2", "d9", "d6", "d1", "d3", "d10", "d5", "d7"],
    }
