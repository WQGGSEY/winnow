from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from research_harness.config import load_harness_config
from research_harness.schemas.validator import validate_named_schema


class SearchStateError(ValueError):
    """Raised when search state or node transitions violate policy."""


ALLOWED_TRANSITIONS = {
    "proposed": {"ready", "pruned"},
    "ready": {"running", "pruned", "blocked"},
    "running": {"ready", "completed_worker_report", "blocked", "failed"},
    "completed_worker_report": {"critic_reviewed", "blocked", "failed"},
    "critic_reviewed": {"orchestrator_reduced", "blocked", "failed"},
    "orchestrator_reduced": {"promoted", "needs_child_branch", "pruned", "blocked", "failed"},
    "needs_child_branch": {"pruned"},
}


TERMINAL_STATUSES = {"promoted", "pruned", "blocked", "failed"}


def search_policy_from_config(repo_root: Path) -> dict[str, Any]:
    """Load tree-search policy from configs/harness.yaml `search:` block.

    Every knob the harness uses to bound the search must live here so it
    can be adjusted without code changes. Last-resort fallbacks below are
    only hit when the YAML key is missing entirely; they are kept low
    rather than aspirational so a missing-config bug doesn't quietly let
    the tree explode.
    """
    config = load_harness_config(repo_root)
    search = config.get("search", {})
    return {
        "max_depth": int(search.get("max_depth", 5)),
        "max_debug_depth": int(search.get("max_debug_depth", 2)),
        "num_drafts": int(search.get("num_drafts", 3)),
        "debug_prob": float(search.get("debug_prob", 0.25)),
        "sunk_cost_policy": str(search.get("sunk_cost_policy", "progress_gated")),
        "scaleup_policy": str(search.get("scaleup_policy", "disallow_by_default")),
        "scaleup_requires": [
            str(item) for item in search.get("scaleup_requires", [])
        ],
    }


def make_frontier_item(
    node: dict[str, Any],
    *,
    depth: int,
    priority: float,
    reason: str,
) -> dict[str, Any]:
    item = {
        "node_id": node["id"],
        "parent": node.get("parent"),
        "depth": depth,
        "priority": priority,
        "stage": node["stage"],
        "status": "queued",
        "reason": reason,
    }
    validate_named_schema("frontier_item", item)
    return item


def initialize_search_state(
    *,
    search_id: str,
    root_node: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    state = {
        "search_id": search_id,
        "status": "initialized",
        "max_depth": int(policy["max_depth"]),
        "max_debug_depth": int(policy["max_debug_depth"]),
        "sunk_cost_policy": str(policy["sunk_cost_policy"]),
        "scaleup_policy": str(policy["scaleup_policy"]),
        "frontier": [
            make_frontier_item(root_node, depth=0, priority=1.0, reason="root")
        ],
        "nodes": [deepcopy(root_node)],
        "completed_node_ids": [],
        "promoted_node_ids": [],
        "pruned_node_ids": [],
        "transitions": [],
    }
    validate_search_state(state)
    return state


def validate_search_state(state: dict[str, Any]) -> None:
    validate_named_schema("search_state", state)
    for item in state["frontier"]:
        validate_named_schema("frontier_item", item)
    for node in state["nodes"]:
        validate_named_schema("node", node)
    for transition in state["transitions"]:
        validate_named_schema("node_transition", transition)


def transition_node(
    state: dict[str, Any],
    node_id: str,
    to_status: str,
    *,
    event: str,
    reason: str,
    created_child_ids: list[str] | None = None,
) -> dict[str, Any]:
    node = _node_by_id(state, node_id)
    from_status = node["status"]
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
        raise SearchStateError(
            f"invalid node transition: {node_id} {from_status} -> {to_status}"
        )
    node["status"] = to_status
    transition = {
        "node_id": node_id,
        "from_status": from_status,
        "to_status": to_status,
        "event": event,
        "reason": reason,
        "created_child_ids": created_child_ids or [],
    }
    validate_named_schema("node_transition", transition)
    state["transitions"].append(transition)
    if to_status in {"completed_worker_report", "critic_reviewed", "orchestrator_reduced"}:
        _append_unique(state["completed_node_ids"], node_id)
    elif to_status == "promoted":
        _append_unique(state["promoted_node_ids"], node_id)
    elif to_status in TERMINAL_STATUSES - {"promoted"}:
        _append_unique(state["pruned_node_ids"], node_id)
    _update_frontier_status(state, node_id, to_status)
    validate_search_state(state)
    return transition


def add_child_nodes(
    state: dict[str, Any],
    parent_id: str,
    child_nodes: list[dict[str, Any]],
    *,
    reason: str,
) -> None:
    existing_ids = {node["id"] for node in state["nodes"]}
    parent_depth = _frontier_depth(state, parent_id)
    for index, child in enumerate(child_nodes):
        if child["id"] in existing_ids:
            raise SearchStateError(f"duplicate node id: {child['id']}")
        validate_named_schema("node", child)
        state["nodes"].append(deepcopy(child))
        state["frontier"].append(
            make_frontier_item(
                child,
                depth=parent_depth + 1,
                priority=max(0.0, 0.8 - index * 0.05),
                reason=reason,
            )
        )
        existing_ids.add(child["id"])
    validate_search_state(state)


def _node_by_id(state: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in state["nodes"]:
        if node["id"] == node_id:
            return node
    raise SearchStateError(f"node not found: {node_id}")


def _frontier_depth(state: dict[str, Any], node_id: str) -> int:
    for item in state["frontier"]:
        if item["node_id"] == node_id:
            return int(item["depth"])
    raise SearchStateError(f"frontier item not found: {node_id}")


def _update_frontier_status(state: dict[str, Any], node_id: str, node_status: str) -> None:
    if node_status == "ready":
        frontier_status = "queued"
    elif node_status == "running":
        frontier_status = "running"
    elif node_status in TERMINAL_STATUSES or node_status in {
        "completed_worker_report",
        "critic_reviewed",
        "orchestrator_reduced",
        "needs_child_branch",
    }:
        frontier_status = "done"
    else:
        return
    for item in state["frontier"]:
        if item["node_id"] == node_id:
            item["status"] = frontier_status


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)
