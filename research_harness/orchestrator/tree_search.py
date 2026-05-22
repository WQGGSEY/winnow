from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_harness.config import load_settings
from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.orchestrator.branch_prior import build_failure_branch_prior
from research_harness.orchestrator.child_nodes import draft_child_nodes
from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.reduction import reduce_node
from research_harness.orchestrator.search_state import (
    add_child_nodes,
    initialize_search_state,
    search_policy_from_config,
    transition_node,
    validate_search_state,
)
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.mock_backend import MockWorkerBackend


ALLOWED_TREE_SEARCH_BACKENDS = {"mock"}


class MockTreeSearchBackend:
    def run(self, node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
        return MockWorkerBackend().run(node, run_dir)


def run_mock_tree_search(
    repo_root: Path,
    run_dir: Path,
    *,
    backend: Any | None = None,
    backend_name: str = "mock",
    max_steps: int | None = None,
) -> dict[str, Any]:
    if backend_name not in ALLOWED_TREE_SEARCH_BACKENDS:
        raise ValueError(
            "tree search only accepts dry-run/local backends; live Claude remains behind live_gate"
        )
    settings = load_settings(repo_root)
    policy = search_policy_from_config(repo_root)
    root = _demo_node()
    state = initialize_search_state(
        search_id="s_demo_tree",
        root_node=root,
        policy=policy,
    )
    backend = backend or MockTreeSearchBackend()
    max_steps = max_steps or int(policy["max_depth"]) + 1
    run_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict[str, Any]] = []
    state["status"] = "running"
    steps = 0
    while steps < max_steps:
        item = _next_queued_item(state)
        if item is None:
            break
        steps += 1
        node = _node_by_id(state, item["node_id"])
        node_run_dir = run_dir / "nodes" / node["id"]
        node_run_dir.mkdir(parents=True, exist_ok=True)

        transition_node(
            state,
            node["id"],
            "running",
            event="dequeue",
            reason="frontier selected",
        )
        worker_report = backend.run(node, node_run_dir)
        validate_named_schema("worker_report", worker_report)
        _write_json(node_run_dir / "worker_report.json", worker_report)
        transition_node(
            state,
            node["id"],
            "completed_worker_report",
            event="worker_report",
            reason=f"worker status: {worker_report['status']}",
        )

        critics = select_critics(repo_root, node)
        reviews = run_critic_reviews(node, worker_report, critics)
        for review in reviews:
            validate_named_schema("critic_review", review)
        _write_json(node_run_dir / "critic_reviews.json", reviews)
        transition_node(
            state,
            node["id"],
            "critic_reviewed",
            event="critic_reviews",
            reason=f"{len(reviews)} critics reviewed node",
        )

        branch_prior = build_failure_branch_prior(repo_root, node, settings)
        reduction = reduce_node(node, worker_report, reviews, branch_prior=branch_prior)
        _write_json(node_run_dir / "orchestrator_reduction.json", reduction)
        transition_node(
            state,
            node["id"],
            "orchestrator_reduced",
            event="orchestrator_reduction",
            reason=reduction["final_verdict"],
        )

        if reduction["next_transition"] == "promoted":
            transition_node(
                state,
                node["id"],
                "promoted",
                event="promotion",
                reason="node promoted by reduction",
            )
        elif reduction["next_transition"] == "needs_child_branch":
            children = draft_child_nodes(
                node,
                reduction,
                parent_depth=int(item["depth"]),
                max_depth=int(policy["max_depth"]),
            )
            add_child_nodes(
                state,
                node["id"],
                children,
                reason="reduction requested child branch",
            )
            transition_node(
                state,
                node["id"],
                "needs_child_branch",
                event="branch",
                reason="node requires child branch",
                created_child_ids=[child["id"] for child in children],
            )
        else:
            transition_node(
                state,
                node["id"],
                "pruned",
                event="prune",
                reason=f"unhandled transition {reduction['next_transition']}",
            )
        artifacts.append(
            {
                "node_id": node["id"],
                "worker_status": worker_report["status"],
                "next_transition": reduction["next_transition"],
            }
        )

    state["status"] = "completed" if _next_queued_item(state) is None else "blocked"
    validate_search_state(state)
    result = {"search_state": state, "artifacts": artifacts}
    _write_json(run_dir / "search_state.json", state)
    _write_json(run_dir / "tree_search_summary.json", result)
    return result


def _next_queued_item(state: dict[str, Any]) -> dict[str, Any] | None:
    queued = [item for item in state["frontier"] if item["status"] == "queued"]
    if not queued:
        return None
    queued.sort(key=lambda item: (-item["priority"], item["depth"], item["node_id"]))
    return queued[0]


def _node_by_id(state: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in state["nodes"]:
        if node["id"] == node_id:
            return node
    raise KeyError(node_id)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    run_dir = repo_root / "runs" / "mock_tree_search"
    result = run_mock_tree_search(repo_root, run_dir)
    summary = {
        "status": result["search_state"]["status"],
        "node_count": len(result["search_state"]["nodes"]),
        "promoted_node_ids": result["search_state"]["promoted_node_ids"],
        "search_state": str(run_dir / "search_state.json"),
        "tree_search_summary": str(run_dir / "tree_search_summary.json"),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
