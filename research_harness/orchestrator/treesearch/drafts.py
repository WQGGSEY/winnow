"""num_drafts — Sakana parity for "draft multiple initial attempts".

Sakana drafts `num_drafts` initial code attempts from the root task description
and pushes them all into the journal so ParallelAgent has multiple candidates
to work in parallel. We do not draft code; instead, we draft **claim-typed
sibling nodes** under the root so the 4 claim-typed stages each have
admissible candidates to process.

For a root with type `capability`, drafting fills:
    scope_pinning           ← {validity} draft
    mechanism_or_necessity  ← {mechanism, necessity} drafts
    boundary_ablation       ← {boundary} draft

This makes the staged loop substantive (each stage has work to do) rather
than the structural-only shell from before.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from research_harness.orchestrator.search_state import add_child_nodes
from research_harness.schemas.validator import validate_named_schema


# Per-stage default drafts. The number of drafts produced equals num_drafts;
# we round-robin through this list and dedupe.
STAGE_DRAFT_TYPES: dict[str, list[str]] = {
    "scope_pinning": ["validity", "operational", "taste"],
    "baseline_evidence": ["capability"],
    "mechanism_or_necessity": ["mechanism", "necessity"],
    "boundary_ablation": ["boundary", "constraint"],
}


def seed_drafts_from_root(
    search_state: dict[str, Any],
    *,
    num_drafts: int,
    max_depth: int,
    root_id: str | None = None,
) -> list[str]:
    """Seed up to num_drafts typed draft siblings under a root node.

    Only runs once per root — if the chosen root already has any children,
    it's a no-op. Returns the list of created child ids. When ``root_id``
    is omitted the first parent=None node is used (legacy single-root
    behaviour); pass an explicit ``root_id`` for multi-root tournaments.
    """
    if num_drafts < 1:
        return []
    if root_id is None:
        roots = [node for node in search_state["nodes"] if node.get("parent") is None]
        if not roots:
            return []
        root = roots[0]
    else:
        root = next(
            (n for n in search_state["nodes"] if n.get("id") == root_id),
            None,
        )
        if root is None:
            return []
    existing_children = [
        node for node in search_state["nodes"] if node.get("parent") == root["id"]
    ]
    if existing_children:
        return []

    drafts: list[dict[str, Any]] = []
    used_types: set[str] = set()
    for stage_name in (
        "scope_pinning",
        "mechanism_or_necessity",
        "boundary_ablation",
    ):
        for draft_type in STAGE_DRAFT_TYPES[stage_name]:
            if len(drafts) >= num_drafts:
                break
            if draft_type in used_types:
                continue
            # Don't draft a sibling of the same type as the root (it already covers it).
            if draft_type == root.get("type"):
                continue
            used_types.add(draft_type)
            drafts.append(_make_draft(root, draft_type, len(drafts) + 1))
        if len(drafts) >= num_drafts:
            break

    if not drafts:
        return []

    add_child_nodes(
        search_state,
        root["id"],
        drafts,
        reason="num_drafts seeded typed draft siblings from root",
    )
    return [draft["id"] for draft in drafts]


def _make_draft(root: dict[str, Any], draft_type: str, index: int) -> dict[str, Any]:
    child = deepcopy(root)
    child["id"] = f"{root['id']}_draft{index:02d}"
    child["type"] = draft_type
    child["status"] = "ready"
    child["stage"] = "experimentation"
    child["parent"] = root["id"]
    child["lineage"]["inherited_assumptions"] = list(
        root["lineage"]["inherited_assumptions"]
    )
    child["lineage"]["introduced_assumptions"] = [
        f"Drafted as a {draft_type} sibling of the root capability claim.",
    ]
    child["claim_contract"]["claim_under_test"] = (
        f"[{draft_type} draft] {root['claim_contract']['claim_under_test']}"
    )
    child["claim_contract"]["success_criteria"] = [
        f"Address the {draft_type} axis of the root claim explicitly.",
        *root["claim_contract"]["success_criteria"],
    ]
    child["claim_contract"]["disproof_conditions"] = [
        f"The {draft_type} draft does not address its axis.",
        *root["claim_contract"]["disproof_conditions"],
    ]
    child["failure_retrieval"]["query_tags"] = sorted(
        set([*root["failure_retrieval"]["query_tags"], draft_type, "num_drafts"])
    )
    child["outputs"] = {"artifacts": [], "verdict": None}
    validate_named_schema("node", child)
    return child
