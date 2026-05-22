from __future__ import annotations

from copy import deepcopy
from typing import Any

from research_harness.schemas.validator import validate_named_schema


VALID_CHILD_TYPES = {
    "capability",
    "validity",
    "necessity",
    "boundary",
    "mechanism",
    "constraint",
    "taste",
    "operational",
}


def draft_child_nodes(
    parent: dict[str, Any],
    reduction: dict[str, Any],
    *,
    parent_depth: int,
    max_depth: int,
) -> list[dict[str, Any]]:
    if reduction.get("next_transition") != "needs_child_branch":
        return []
    if parent_depth >= max_depth:
        return []

    children: list[dict[str, Any]] = []
    for index, suggestion in enumerate(reduction.get("child_branch_suggestions", []), start=1):
        child = _draft_child(parent, suggestion, index)
        validate_named_schema("node", child)
        children.append(child)
    return children


def _draft_child(
    parent: dict[str, Any],
    suggestion: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    child = deepcopy(parent)
    child_type = str(suggestion.get("type") or "validity")
    if child_type not in VALID_CHILD_TYPES:
        child_type = "validity"
    reason = str(suggestion.get("reason") or "Investigate parent blocker.")
    source = str(suggestion.get("source") or "orchestrator_reduction")

    child["id"] = f"{parent['id']}_b{index:02d}"
    child["type"] = child_type
    child["status"] = "ready"
    child["stage"] = "experimentation"
    child["parent"] = parent["id"]
    child["lineage"]["inherited_assumptions"] = [
        *parent["lineage"]["inherited_assumptions"],
        *parent["lineage"]["introduced_assumptions"],
    ]
    child["lineage"]["introduced_assumptions"] = [
        f"Child branch opened from {source}: {reason}"
    ]
    child["claim_contract"]["claim_under_test"] = (
        f"Child branch for {parent['id']} can resolve: {reason}"
    )
    child["claim_contract"]["success_criteria"] = [
        f"Directly address branch reason: {reason}",
        *parent["claim_contract"]["success_criteria"],
    ]
    child["claim_contract"]["disproof_conditions"] = [
        "The branch does not address its parent blocker.",
        *parent["claim_contract"]["disproof_conditions"],
    ]
    child["failure_retrieval"]["query_tags"] = _dedupe(
        [
            *parent["failure_retrieval"]["query_tags"],
            child_type,
            _source_tag(source),
        ]
    )
    if source.startswith("failure_memory:"):
        failure_file = source.split(":", 1)[1]
        child["failure_retrieval"]["selected_fail_files"] = _dedupe(
            [*parent["failure_retrieval"]["selected_fail_files"], failure_file]
        )
    return child


def _source_tag(source: str) -> str:
    if source.startswith("failure_memory:"):
        return "failure_memory"
    return source.replace(".", "_").replace(":", "_")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
