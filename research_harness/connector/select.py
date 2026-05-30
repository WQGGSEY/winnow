"""[[research forest + single output]] — select the single strongest survivor.

The forest produces N gate-judged roots; the output is ONE. This pure ranking
picks it: by [[verdict_strength]] (the ADR 0008 earned-strength ladder), then by
[[investigation_depth]] (distinct_attempts) as the tie-break, then node_id for
determinism. Strength is PRIMARY on purpose — a lucky-shallow survivor scores
low on earned strength (it never reached construct_/transfer_valid), so depth
cannot rescue it past a genuinely stronger claim. This introduces no
multiple-comparison problem: every candidate already cleared the full per-claim
gate (earned, not lucky).

Input candidates are SURVIVORS only (roots that earned a terminal verdict); the
mcp_server glue enumerates them and reads each root's harness-stamped
verdict_strength + investigation_depth. Empty input -> no winner; the caller
renders honest-failure (this function does not).
"""

from __future__ import annotations

from typing import Any

from research_harness.verdict_strength import strength_rank


def select_strongest(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Return ``{"winner": <candidate|None>, "ranking": [candidates sorted]}``.

    Each candidate is ``{node_id, verdict_strength, distinct_attempts}``.
    Ranking key: verdict_strength DESC (canonical ladder; unknown tokens rank
    below every reachable verdict), then distinct_attempts DESC, then node_id
    ASC (deterministic tie-break).
    """
    ranked = sorted(
        candidates,
        key=lambda c: (
            -strength_rank(str(c.get("verdict_strength") or "")),
            -int(c.get("distinct_attempts") or 0),
            str(c.get("node_id") or ""),
        ),
    )
    return {"winner": ranked[0] if ranked else None, "ranking": ranked}
