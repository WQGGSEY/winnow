"""Field source for [[reading (field-forced)]].

The diversity front-end forces the LLM to read the one vague abstraction
through an *externally chosen* field. The field must never be picked by the
LLM ("be diverse" mode-collapses) nor selected for relevance ("best" also
collapses) — it is drawn by **uniform random sampling** from a large frozen
namespace. We use the frozen arXiv category taxonomy (155 leaf categories).

Crucially, unlike the retired atlas (ADR 0011), we measure NO distance between
fields: there is no "near"/"far" table to author, hence no operator
blind-spot ceiling on distances. The namespace only needs to be big and broad;
spread comes from uniform sampling and the production gate decides which
forced readings earned their standing.

Sampling is **without replacement** (a deterministic permutation under a
harness-fixed seed): the connector iterates the permutation, running
reading + prune-1 on each field until a quota of readings clears, so a field
is never re-tried and the namespace exhausts naturally. Determinism (explicit
seed, never the global RNG) keeps a thread's field draw reproducible.

Uniform over the 155 leaves is intentional — NOT stratified by archive.
Archive-stratification would re-introduce a weighting/curation knob (which
fields matter more), exactly the authored-distance move the atlas died on.
"""

from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA_PATH = Path(__file__).resolve().parent / "data" / "arxiv_categories.json"


@lru_cache(maxsize=4)
def load_categories(path: str | None = None) -> tuple[dict[str, Any], ...]:
    """Load the frozen field namespace as an immutable tuple of category dicts.

    Each entry is ``{"code", "name", "archive"}``. Cached; the result is a
    tuple so callers cannot mutate the shared frozen list.
    """
    src = Path(path) if path is not None else _DATA_PATH
    payload = json.loads(src.read_text(encoding="utf-8"))
    categories = payload["categories"]
    if not isinstance(categories, list) or not categories:
        raise ValueError(f"field namespace at {src} has no categories")
    return tuple(dict(c) for c in categories)


def field_permutation(*, seed: int, path: str | None = None) -> list[dict[str, Any]]:
    """Return a deterministic uniform-random permutation of the whole namespace.

    The connector consumes this in order — running reading + prune-1 on each
    field until its quota of passing readings is met (resample-to-quota) — so a
    field is drawn at most once and the namespace exhausts naturally. Same seed
    → same order (reproducible field draw per thread).
    """
    cats = [dict(c) for c in load_categories(path)]
    random.Random(seed).shuffle(cats)
    return cats


def sample_fields(n: int, *, seed: int, path: str | None = None) -> list[dict[str, Any]]:
    """Draw up to ``n`` distinct fields uniformly at random (no replacement).

    Convenience over :func:`field_permutation` — the first ``n`` of the
    permutation. Returns fewer than ``n`` only when the namespace is smaller
    than ``n``; ``n <= 0`` returns ``[]``.
    """
    if n <= 0:
        return []
    return field_permutation(seed=seed, path=path)[:n]
