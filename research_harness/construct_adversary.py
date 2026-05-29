"""ADR 0008 Axis 1 — the funded construct-adversary.

Construct validity (is the construction close to the QUESTION?) is the one
closeness axis that IS enforceable air-gapped, because the question is a
formal object that can be frozen and attacked without touching reality.

The verdict is earned ONLY by a funded adversary *failing*:

1. The adversary's referent is the FROZEN question (authored before the
   construction loop — the proposer did not write it, so it does not share
   the proposer's blind spot).
2. Every construction declares its ``pass_but_wrong`` region — the worlds in
   which the measurement passes yet the frozen question's true answer is NO.
   Empty/absent region → invalid (every non-trivial measurement has such
   worlds; claiming none is a lie).
3. The adversary *searches* that region for a breaking instance (measurement
   passes AND answer is NO). Found → ``broken`` (construct-invalid). A real
   funded search that finds none → ``survived`` (construct_valid).
4. Budget-0 / under-funded / empty search → ``invalid``. A certification that
   did not actually search is auto-invalid (the depth gate, aimed at the
   closeness adversary).

This module is pure: it RE-DERIVES the verdict from the adversary's enumerated
search rather than trusting a self-reported ``survived``. A lazy adversary
that enumerates a breaking world but claims survival is caught here.
"""

from __future__ import annotations

from typing import Any

PRODUCED_BY = "construct_adversary"
DEFAULT_MIN_BUDGET = 8
DEFAULT_MIN_DISTINCT_WORLDS = 3


def _is_break(world: dict[str, Any]) -> bool:
    """A world breaks the construction iff the measurement PASSES there but
    the frozen question's true answer is NO — i.e. a pass-but-wrong world."""
    return (
        world.get("measurement_passes") is True
        and str(world.get("frozen_answer", "")).strip().lower() == "no"
    )


def evaluate_construct_adversary_report(
    report: dict[str, Any],
    *,
    min_budget: int = DEFAULT_MIN_BUDGET,
    min_distinct_worlds: int = DEFAULT_MIN_DISTINCT_WORLDS,
) -> dict[str, Any]:
    """Re-derive the construct-validity verdict from a funded search.

    Returns {verdict: 'survived'|'broken'|'invalid', reasons: [...],
             distinct_worlds: int, break_world_ids: [...]}.
    'survived' (→ construct_valid) requires: harness-produced, funded
    (budget >= min), a real search (>= min distinct worlds in the
    pass_but_wrong region), and NO breaking world in the enumerated search.
    """
    reasons: list[str] = []

    if report.get("produced_by") != PRODUCED_BY:
        return {
            "verdict": "invalid",
            "reasons": [f"produced_by != {PRODUCED_BY!r} (provenance: the adversary, not the proposer, must author this)"],
            "distinct_worlds": 0,
            "break_world_ids": [],
        }

    worlds = report.get("worlds_tested") or []
    distinct_ids = {str(w.get("world_id")) for w in worlds if isinstance(w, dict) and w.get("world_id")}
    distinct_worlds = len(distinct_ids)
    break_world_ids = [
        str(w.get("world_id"))
        for w in worlds
        if isinstance(w, dict) and _is_break(w)
    ]

    budget = report.get("budget_total")
    if not isinstance(budget, int) or budget < min_budget:
        reasons.append(
            f"under-funded: budget_total={budget!r} < min_budget={min_budget}. "
            "A certification that did not actually search is auto-invalid."
        )
    if distinct_worlds < min_distinct_worlds:
        reasons.append(
            f"shallow search: {distinct_worlds} distinct worlds tested in the "
            f"pass_but_wrong region (need >= {min_distinct_worlds})."
        )

    # An explicit breaking_instance OR any enumerated breaking world = broken.
    explicit_break = report.get("breaking_instance") not in (None, {}, [])
    if explicit_break or break_world_ids:
        return {
            "verdict": "broken",
            "reasons": (
                ["adversary found a pass-but-wrong instance — the construction "
                 "passes its measurement in a world where the frozen question's "
                 "answer is NO; construct-invalid."]
                + ([f"breaking worlds: {break_world_ids}"] if break_world_ids else [])
            ),
            "distinct_worlds": distinct_worlds,
            "break_world_ids": break_world_ids,
        }

    if reasons:
        # Funded-failure not established → cannot certify survival.
        return {
            "verdict": "invalid",
            "reasons": reasons,
            "distinct_worlds": distinct_worlds,
            "break_world_ids": [],
        }

    return {
        "verdict": "survived",
        "reasons": [
            f"funded failure: adversary tested {distinct_worlds} distinct "
            f"pass_but_wrong worlds on budget {budget} and found no break — "
            "construct_valid (necessary, NOT sufficient; says nothing about reality)."
        ],
        "distinct_worlds": distinct_worlds,
        "break_world_ids": [],
    }
