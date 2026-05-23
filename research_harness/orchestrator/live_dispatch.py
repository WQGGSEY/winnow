from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from research_harness.config import load_settings
from research_harness.orchestrator.search_state import validate_search_state
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.live_gate import build_manual_live_node_plan
from research_harness.workers.live_smoke_runner import (
    DEFAULT_TIMEOUT_SECONDS,
    EXECUTION_ACK_ENV,
    EXECUTION_ACK_VALUE,
    CommandRunner,
    run_live_node_once,
)


class LiveDispatchError(ValueError):
    """Raised when an operator live dispatch target is not eligible."""


def load_search_state_file(search_state_path: Path) -> dict[str, Any]:
    state = json.loads(search_state_path.read_text(encoding="utf-8"))
    validate_search_state(state)
    return state


def select_live_dispatch_target(
    search_state: dict[str, Any],
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    """Select exactly one queued, ready node without mutating search state."""

    validate_search_state(search_state)
    if node_id is None:
        item = _next_queued_item(search_state)
        if item is None:
            raise LiveDispatchError("no queued frontier item is available for live dispatch")
    else:
        item = _frontier_item_by_node_id(search_state, node_id)
        if item["status"] != "queued":
            raise LiveDispatchError(f"frontier item is not queued: {node_id}")

    node = _node_by_id(search_state, item["node_id"])
    if node["status"] != "ready":
        raise LiveDispatchError(
            f"node is not ready for live dispatch: {node['id']} status={node['status']}"
        )
    validate_named_schema("node", node)
    validate_named_schema("frontier_item", item)
    return {
        "node": deepcopy(node),
        "frontier_item": deepcopy(item),
    }


def build_live_dispatch_plan(
    repo_root: Path,
    search_state_path: Path,
    *,
    run_dir: Path | None = None,
    node_id: str | None = None,
    claude_path: str | None = None,
    billing_ack: bool | None = None,
) -> dict[str, Any]:
    """Create a non-executing live dispatch plan for one queued search node."""

    repo_root = repo_root.resolve()
    search_state_path = search_state_path.resolve()
    search_state = load_search_state_file(search_state_path)
    target = select_live_dispatch_target(search_state, node_id=node_id)
    live_run_dir = _live_run_dir(repo_root, search_state, target["node"], run_dir)
    live_plan = build_manual_live_node_plan(
        repo_root,
        target["node"],
        run_dir=live_run_dir,
        claude_path=claude_path,
        billing_ack=billing_ack,
    )
    dispatch = _build_dispatch_record(
        repo_root,
        search_state_path,
        search_state,
        target,
        live_run_dir,
        live_plan,
        mode="plan",
        status=live_plan["status"],
        live_summary_path=None,
        live_summary_status=None,
        error=None if live_plan["status"] == "ready_to_manually_run" else live_plan["reason"],
    )
    _write_dispatch(live_run_dir, dispatch)
    return dispatch


def run_live_dispatch_once(
    repo_root: Path,
    search_state_path: Path,
    *,
    run_dir: Path | None = None,
    node_id: str | None = None,
    claude_path: str | None = None,
    billing_ack: bool | None = None,
    execution_ack: bool | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Execute one queued search node through the gated live-node runner."""

    repo_root = repo_root.resolve()
    search_state_path = search_state_path.resolve()
    search_state = load_search_state_file(search_state_path)
    target = select_live_dispatch_target(search_state, node_id=node_id)
    live_run_dir = _live_run_dir(repo_root, search_state, target["node"], run_dir)
    live_summary = run_live_node_once(
        repo_root,
        target["node"],
        run_dir=live_run_dir,
        claude_path=claude_path,
        billing_ack=billing_ack,
        execution_ack=execution_ack,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
    )
    dispatch = _build_dispatch_record(
        repo_root,
        search_state_path,
        search_state,
        target,
        live_run_dir,
        _live_plan_from_summary(live_summary),
        mode="execute_once",
        status=live_summary["status"],
        live_summary_path=live_summary["plan_path"]
        and str(Path(live_summary["plan_path"]).with_name("live_worker_run_summary.json")),
        live_summary_status=live_summary["status"],
        error=live_summary["error"],
    )
    _write_dispatch(live_run_dir, dispatch)
    return dispatch


def _next_queued_item(search_state: dict[str, Any]) -> dict[str, Any] | None:
    queued = [item for item in search_state["frontier"] if item["status"] == "queued"]
    if not queued:
        return None
    queued.sort(key=lambda item: (-item["priority"], item["depth"], item["node_id"]))
    return queued[0]


def _frontier_item_by_node_id(
    search_state: dict[str, Any],
    node_id: str,
) -> dict[str, Any]:
    for item in search_state["frontier"]:
        if item["node_id"] == node_id:
            return item
    raise LiveDispatchError(f"frontier item not found: {node_id}")


def _node_by_id(search_state: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in search_state["nodes"]:
        if node["id"] == node_id:
            return node
    raise LiveDispatchError(f"node not found: {node_id}")


def _live_run_dir(
    repo_root: Path,
    search_state: dict[str, Any],
    node: dict[str, Any],
    run_dir: Path | None,
) -> Path:
    if run_dir is not None:
        return run_dir
    return repo_root / "runs" / "live_dispatch" / search_state["search_id"] / node["id"]


def _build_dispatch_record(
    repo_root: Path,
    search_state_path: Path,
    search_state: dict[str, Any],
    target: dict[str, Any],
    live_run_dir: Path,
    live_plan: dict[str, Any],
    *,
    mode: str,
    status: str,
    live_summary_path: str | None,
    live_summary_status: str | None,
    error: str | None,
) -> dict[str, Any]:
    live_backend = (
        load_settings(repo_root)
        .get("runtime", {})
        .get("worker_backends", {})
        .get("claude_code_live", {})
    )
    dispatch = {
        "type": "live_node_dispatch",
        "mode": mode,
        "status": status,
        "search_state_path": str(search_state_path),
        "search_id": search_state["search_id"],
        "node_id": target["node"]["id"],
        "node_status": target["node"]["status"],
        "frontier_item": target["frontier_item"],
        "live_run_dir": str(live_run_dir),
        "live_plan_path": _live_plan_path(live_plan),
        "live_runbook_path": live_plan["runbook_path"],
        "live_summary_path": live_summary_path,
        "live_summary_status": live_summary_status,
        "tree_search_mutation": "forbidden",
        "operator_required_acks": {
            "billing_ack_env": live_backend.get("billing_ack_env"),
            "billing_ack_value": live_backend.get("billing_ack_value"),
            "execution_ack_env": EXECUTION_ACK_ENV,
            "execution_ack_value": EXECUTION_ACK_VALUE,
        },
        "error": error,
    }
    validate_named_schema("live_node_dispatch", dispatch)
    return dispatch


def _live_plan_from_summary(live_summary: dict[str, Any]) -> dict[str, Any]:
    plan_path = Path(live_summary["plan_path"])
    return {
        "plan_path": str(plan_path),
        "runbook_path": str(plan_path.with_name("manual_live_worker_runbook.md")),
    }


def _live_plan_path(live_plan: dict[str, Any]) -> str:
    if live_plan.get("plan_path"):
        return str(live_plan["plan_path"])
    return str(Path(live_plan["runbook_path"]).with_name("manual_live_worker_plan.json"))


def _write_dispatch(live_run_dir: Path, dispatch: dict[str, Any]) -> None:
    live_run_dir.mkdir(parents=True, exist_ok=True)
    (live_run_dir / "live_node_dispatch.json").write_text(
        json.dumps(dispatch, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan or execute one queued search-state node through the live gate."
    )
    parser.add_argument("--search-state", required=True, type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--node-id")
    parser.add_argument("--claude-path")
    parser.add_argument("--billing-ack", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--execute-ack", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    try:
        if args.execute:
            result = run_live_dispatch_once(
                repo_root,
                args.search_state,
                run_dir=args.run_dir,
                node_id=args.node_id,
                claude_path=args.claude_path,
                billing_ack=True if args.billing_ack else None,
                execution_ack=True if args.execute_ack else None,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            result = build_live_dispatch_plan(
                repo_root,
                args.search_state,
                run_dir=args.run_dir,
                node_id=args.node_id,
                claude_path=args.claude_path,
                billing_ack=True if args.billing_ack else None,
            )
    except LiveDispatchError as exc:
        print(
            json.dumps(
                {
                    "type": "live_node_dispatch_error",
                    "status": "blocked_no_dispatch_target",
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(2) from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
