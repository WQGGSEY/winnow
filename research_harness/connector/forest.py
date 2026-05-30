"""[[research forest + single output]] seeding — connector claims → N roots.

Turns the connector's N kept ``claim_contract``s into ONE production
search_state holding N **coexisting** roots, each draft-seeded so the existing
typed-stage gate has work under every root. The multi-root forest is
operationally NEW (real threads have only ever been single-root); the scaffold
(``seed_drafts_from_root`` ``root_id``, the append-root pattern) already exists
and is reused here rather than reimplemented.

This is the connector→production handoff core. It does NOT run the gate or
select a winner — the existing per-node gate runs each tree, and select-strongest
(Option A: per-survivor terminal → rank by verdict_strength then
investigation_depth) happens at production's end.

Baselines stay the placeholder dossier (``bd_pending_market_research``): each
far-claim's real current_best / naive / random baselines are resolved later,
P-aware, in the production validity stage — exactly as the legacy single-root
path defers them.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from research_harness.orchestrator.root_node_from_grilling import (
    build_root_node_from_grilling,
)
from research_harness.orchestrator.search_state import (
    initialize_search_state,
    make_frontier_item,
    validate_search_state,
)
from research_harness.orchestrator.treesearch.drafts import seed_drafts_from_root
from research_harness.orchestrator.validation import validate_node_invariants
from research_harness.schemas.validator import validate_named_schema


def _slug(code: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(code).lower()).strip("_") or "field"


def _build_forest_root(
    grilling_session: dict[str, Any], claim: dict[str, Any], index: int
) -> dict[str, Any]:
    field = claim.get("field") or {}
    suffix = f"c{index:02d}_{_slug(field.get('code') or 'field')}"
    root = build_root_node_from_grilling(grilling_session, node_id_suffix=suffix)
    cc = claim["claim_contract"]
    # The reduced far-claim's contract REPLACES grilling's (claim + all three
    # baseline/success/disproof lists are far-framing specific).
    root["claim_contract"] = {
        "claim_under_test": cc["claim_under_test"],
        "mandatory_baselines": list(cc["mandatory_baselines"]),
        "success_criteria": list(cc["success_criteria"]),
        "disproof_conditions": list(cc["disproof_conditions"]),
    }
    root["lineage"]["introduced_assumptions"].append(
        f"connector far-framing via field {field.get('code')} ({field.get('name')})"
    )
    validate_node_invariants(root)
    validate_named_schema("node", root)
    return root


def build_forest_search_state(
    grilling_session: dict[str, Any],
    claims: list[dict[str, Any]],
    *,
    search_id: str,
    policy: dict[str, Any],
    num_drafts: int | None = None,
    max_depth: int | None = None,
) -> dict[str, Any]:
    """Build a multi-root production search_state from connector claims.

    Each claim becomes a coexisting root (parent=None), draft-seeded. Raises if
    ``claims`` is empty (0 claims → honest-failure, handled by the caller, not a
    forest).
    """
    if not claims:
        raise ValueError("build_forest_search_state requires at least one claim")
    validate_named_schema("grilling_session", grilling_session)
    nd = int(num_drafts if num_drafts is not None else policy["num_drafts"])
    md = int(max_depth if max_depth is not None else policy["max_depth"])

    roots = [_build_forest_root(grilling_session, c, i) for i, c in enumerate(claims)]
    ids = [r["id"] for r in roots]
    if len(set(ids)) != len(ids):
        raise ValueError(f"forest roots have duplicate ids: {ids}")

    # Root 0 bootstraps the state; the rest are appended (mirrors the
    # seed_alternative_root_formulation append pattern). Always pass an explicit
    # root_id to seed_drafts_from_root so a later root is never confused with
    # roots[0].
    state = initialize_search_state(search_id=search_id, root_node=roots[0], policy=policy)
    seed_drafts_from_root(state, num_drafts=nd, max_depth=md, root_id=roots[0]["id"])
    for root in roots[1:]:
        state["nodes"].append(deepcopy(root))
        state["frontier"].append(
            make_frontier_item(root, depth=0, priority=1.0, reason="connector forest root")
        )
        state["transitions"].append(
            {
                "node_id": root["id"],
                "from_status": "ready",
                "to_status": "ready",
                "event": "seed_forest_root",
                "reason": "connector far-framing root",
                "created_child_ids": [],
            }
        )
        seed_drafts_from_root(state, num_drafts=nd, max_depth=md, root_id=root["id"])

    validate_search_state(state)
    return state
