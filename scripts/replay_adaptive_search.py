#!/usr/bin/env python3
"""Replay one recorded negative result through the active MCP search policy."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import research_harness.mcp_server as mcp
from research_harness.orchestrator.adaptive_search import build_research_goal

from evaluate_search_trace import evaluate_search_trace


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pick_negative_node(source: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    state = _read(source / "production" / "tree" / "search_state.json")
    by_id = {node["id"]: node for node in state["nodes"]}
    for decision_path in sorted(
        (source / "production" / "tree" / "nodes").glob(
            "*/mcp_professor_decision.json"
        )
    ):
        decision = _read(decision_path)
        if decision.get("next_transition") not in {"pruned", "needs_child_branch"}:
            continue
        worker_path = decision_path.parent / "worker_report.json"
        if not worker_path.exists():
            continue
        return deepcopy(by_id[decision["node_id"]]), _read(worker_path)
    raise RuntimeError("recorded thread has no replayable negative decision")


def _candidate(
    *,
    family: str,
    mechanism: str,
    intervention: str,
    bar_gap: str,
    capability: str | None,
    estimated_cost: float,
) -> dict[str, Any]:
    return {
        "type": "mechanism",
        "successor_claim": f"Test {family} under the unchanged frozen goal bar.",
        "rationale": f"The observed baseline failure is diagnostic of {mechanism}.",
        "strategy_family": family,
        "mechanism": mechanism,
        "intervention": intervention,
        "information_target": f"Whether {mechanism} is load-bearing.",
        "predicted_outcomes": [
            "The intervention closes the observed frozen-bar gap.",
            "The intervention leaves the observed frozen-bar gap unchanged.",
        ],
        "tests_bar_gaps": [bar_gap],
        "required_capabilities": [capability] if capability else [],
        "estimated_cost": estimated_cost,
    }


def replay(source: Path) -> dict[str, Any]:
    node, worker_report = _pick_negative_node(source)
    source_state = _read(source / "production" / "tree" / "search_state.json")
    thread = _read(source / "thread.json")
    grilling = _read(source / "grilling" / "grilling_session.json")
    envelope = _read(source / "production" / "feasibility_envelope.json")
    goal = build_research_goal(thread=thread, grilling=grilling, envelope=envelope)
    gaps = goal["bar"]["success_criteria"]
    first_gap = gaps[0]
    second_gap = gaps[1] if len(gaps) > 1 else gaps[0]
    data_ids = [
        source_info.get("id")
        for source_info in envelope.get("data_sources_available") or []
        if source_info.get("id")
    ]
    capability = f"data:{data_ids[0]}" if data_ids else None

    node["status"] = "critic_reviewed"
    replay_state = {
        "search_id": f"replay_{source_state['search_id']}",
        "status": "running",
        "max_depth": int(source_state.get("max_depth", 5)),
        "max_debug_depth": int(source_state.get("max_debug_depth", 2)),
        "sunk_cost_policy": source_state.get("sunk_cost_policy", "progress_gated"),
        "scaleup_policy": source_state.get("scaleup_policy", "disallow_by_default"),
        "frontier": [
            {
                "node_id": node["id"],
                "parent": node.get("parent"),
                "depth": 1,
                "priority": 1.0,
                "stage": node["stage"],
                "status": "done",
                "reason": "recorded negative replay",
            }
        ],
        "nodes": [node],
        "completed_node_ids": [node["id"]],
        "promoted_node_ids": [],
        "pruned_node_ids": [],
        "transitions": [],
    }

    with tempfile.TemporaryDirectory(prefix="research-harness-adaptive-replay-") as raw:
        root = Path(raw)
        tid = f"replay_{source.name}"
        target = root / tid
        node_dir = target / "production" / "tree" / "nodes" / node["id"]
        node_dir.mkdir(parents=True)
        (target / "grilling").mkdir()
        (target / "thread.json").write_text(json.dumps(thread), encoding="utf-8")
        (target / "grilling" / "grilling_session.json").write_text(
            json.dumps(grilling), encoding="utf-8"
        )
        (target / "production" / "feasibility_envelope.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
        (target / "production" / "tree" / "search_state.json").write_text(
            json.dumps(replay_state), encoding="utf-8"
        )
        (node_dir / "worker_report.json").write_text(
            json.dumps(worker_report), encoding="utf-8"
        )

        original_thread_dir = mcp._thread_dir
        original_failure_writer = mcp._write_failure_record
        mcp._thread_dir = lambda requested: root / requested
        mcp._write_failure_record = lambda **_kwargs: None
        try:
            decision = mcp.handle_submit_professor_decision(
                {
                    "thread_id": tid,
                    "node_id": node["id"],
                    "next_transition": "needs_child_branch",
                    "final_verdict": "recorded negative evidence did not clear the frozen bar",
                    "command_id": f"replay:{node['id']}",
                    "expected_revision": 0,
                    "follow_up_children": [
                        _candidate(
                            family="representation interaction recovery",
                            mechanism="independent features erase discriminative interactions",
                            intervention="add interaction-preserving features and compare on the same split",
                            bar_gap=first_gap,
                            capability=capability,
                            estimated_cost=0.2,
                        ),
                        _candidate(
                            family="minority support calibration",
                            mechanism="unequal class support dominates the failed comparison",
                            intervention="calibrate class support while preserving the same evidence contract",
                            bar_gap=second_gap,
                            capability=capability,
                            estimated_cost=0.4,
                        ),
                    ],
                },
                settings={},
            )
            selected = mcp.handle_get_next_admissible_node({"thread_id": tid})
        finally:
            mcp._thread_dir = original_thread_dir
            mcp._write_failure_record = original_failure_writer

        replay_scorecard = evaluate_search_trace(target)
        return {
            "type": "adaptive_search_replay",
            "source_thread": source.name,
            "source_scorecard": evaluate_search_trace(source),
            "decision": decision,
            "selected": selected,
            "replay_scorecard": replay_scorecard,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("thread_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.thread_dir.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
