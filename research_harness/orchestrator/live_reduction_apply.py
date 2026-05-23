from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from research_harness.orchestrator.child_nodes import draft_child_nodes
from research_harness.orchestrator.search_state import (
    add_child_nodes,
    transition_node,
    validate_search_state,
)
from research_harness.schemas.validator import validate_named_schema


class LiveReductionApplyError(ValueError):
    """Raised when a live reduction bundle cannot be applied safely."""


def apply_live_reduction(
    bundle_path: Path,
    *,
    approve: bool = False,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Apply an approved live reduction bundle to its search_state."""

    bundle_path = bundle_path.resolve()
    bundle = _load_json(bundle_path)
    validate_named_schema("live_reduction_bundle", bundle)

    search_state_path = Path(bundle["search_state_path"]).resolve()
    before_digest = _sha256_file(search_state_path)
    summary_path = (output_path or bundle_path.with_name("live_reduction_apply_summary.json")).resolve()
    base_summary = {
        "type": "live_reduction_apply_summary",
        "node_id": bundle["node_id"],
        "bundle_path": str(bundle_path),
        "search_state_path": str(search_state_path),
        "search_state_sha256_before": before_digest,
        "search_state_sha256_after": None,
        "approval_required": True,
        "approved": bool(approve),
        "memory_mutation": "forbidden",
        "applied_transition": None,
        "created_child_ids": [],
        "search_state_status": None,
        "summary_path": str(summary_path),
        "error": None,
    }
    if not approve:
        return _write_summary(
            summary_path,
            {
                **base_summary,
                "status": "blocked_by_missing_approval",
                "state_mutation": "forbidden",
                "error": "pass approve=True or --approve to mutate search_state",
            },
        )

    if before_digest != bundle["search_state_sha256"]:
        raise LiveReductionApplyError("search_state has changed since live reduction ingest")

    search_state = _load_json(search_state_path)
    validate_search_state(search_state)
    node = _node_by_id(search_state, bundle["node_id"])
    frontier_item = _frontier_item_by_node_id(search_state, bundle["node_id"])
    if node["status"] != "ready" or frontier_item["status"] != "queued":
        raise LiveReductionApplyError(
            "live reduction can only apply to a ready node with a queued frontier item"
        )

    reduction = bundle["orchestrator_reduction"]
    transition_node(
        search_state,
        node["id"],
        "running",
        event="live_dispatch_apply",
        reason="operator approved live worker report",
    )
    transition_node(
        search_state,
        node["id"],
        "completed_worker_report",
        event="worker_report",
        reason=f"live worker status: {bundle['worker_report']['status']}",
    )
    transition_node(
        search_state,
        node["id"],
        "critic_reviewed",
        event="critic_reviews",
        reason=f"{len(bundle['critic_reviews'])} live critics reviewed node",
    )
    transition_node(
        search_state,
        node["id"],
        "orchestrator_reduced",
        event="orchestrator_reduction",
        reason=reduction["final_verdict"],
    )

    created_child_ids: list[str] = []
    next_transition = reduction["next_transition"]
    if next_transition == "promoted":
        transition_node(
            search_state,
            node["id"],
            "promoted",
            event="promotion",
            reason="operator applied live reduction",
        )
    elif next_transition == "needs_child_branch":
        children = draft_child_nodes(
            node,
            reduction,
            parent_depth=int(frontier_item["depth"]),
            max_depth=int(search_state["max_depth"]),
        )
        created_child_ids = [child["id"] for child in children]
        add_child_nodes(
            search_state,
            node["id"],
            children,
            reason="operator applied live reduction",
        )
        transition_node(
            search_state,
            node["id"],
            "needs_child_branch",
            event="branch",
            reason="operator applied live reduction",
            created_child_ids=created_child_ids,
        )
    else:
        transition_node(
            search_state,
            node["id"],
            "pruned",
            event="prune",
            reason=f"operator applied live reduction transition {next_transition}",
        )

    search_state["status"] = "completed" if _next_queued_item(search_state) is None else "blocked"
    validate_search_state(search_state)
    _write_json(search_state_path, search_state)
    after_digest = _sha256_file(search_state_path)
    return _write_summary(
        summary_path,
        {
            **base_summary,
            "status": "applied",
            "state_mutation": "applied",
            "search_state_sha256_after": after_digest,
            "applied_transition": next_transition,
            "created_child_ids": created_child_ids,
            "search_state_status": search_state["status"],
        },
    )


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise LiveReductionApplyError(f"expected JSON object: {path}")
    return data


def _node_by_id(search_state: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in search_state["nodes"]:
        if node["id"] == node_id:
            validate_named_schema("node", node)
            return node
    raise LiveReductionApplyError(f"node not found in search_state: {node_id}")


def _frontier_item_by_node_id(search_state: dict[str, Any], node_id: str) -> dict[str, Any]:
    for item in search_state["frontier"]:
        if item["node_id"] == node_id:
            validate_named_schema("frontier_item", item)
            return item
    raise LiveReductionApplyError(f"frontier item not found in search_state: {node_id}")


def _next_queued_item(search_state: dict[str, Any]) -> dict[str, Any] | None:
    queued = [item for item in search_state["frontier"] if item["status"] == "queued"]
    if not queued:
        return None
    queued.sort(key=lambda item: (-item["priority"], item["depth"], item["node_id"]))
    return queued[0]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_summary(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    validate_named_schema("live_reduction_apply_summary", summary)
    _write_json(path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply an operator-approved live reduction bundle to search_state."
    )
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        summary = apply_live_reduction(
            args.bundle,
            approve=args.approve,
            output_path=args.output,
        )
    except LiveReductionApplyError as exc:
        print(
            json.dumps(
                {
                    "type": "live_reduction_apply_error",
                    "status": "blocked",
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(2) from exc
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
