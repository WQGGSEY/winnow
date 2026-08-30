#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate_search_trace(thread_dir: Path) -> dict[str, Any]:
    tree_dir = thread_dir / "production" / "tree"
    state = _read_json(tree_dir / "search_state.json")
    nodes = state.get("nodes") or []
    transitions = state.get("transitions") or []
    adaptive = state.get("adaptive") or {}

    report_paths = sorted(tree_dir.glob("nodes/*/worker_report.json"))
    evidence_digests: list[str] = []
    source_digests: list[str] = []
    outcome_counts: Counter[str] = Counter()
    for report_path in report_paths:
        report = _read_json(report_path)
        evidence_digests.append(
            _stable_digest(
                {
                    "metrics": report.get("metrics"),
                    "baselines": report.get("baselines"),
                    "baseline_evidence_status": report.get(
                        "baseline_evidence_status"
                    ),
                }
            )
        )
        outcome_counts[str(report.get("claim_verdict_candidate") or "missing")] += 1
        source_path = report_path.parent / "workspace" / "src" / "experiment.py"
        if source_path.exists():
            source_digests.append(hashlib.sha256(source_path.read_bytes()).hexdigest())

    decision_paths = sorted(tree_dir.glob("nodes/*/mcp_professor_decision.json"))
    decisions = [_read_json(path) for path in decision_paths]
    negative_decisions = [
        decision
        for decision in decisions
        if decision.get("next_transition") in {"needs_child_branch", "pruned"}
    ]
    adaptive_child_ids = {
        child_id
        for transition in transitions
        if transition.get("event") not in {"seed_forest_root", "seed_drafts"}
        for child_id in transition.get("created_child_ids") or []
    }
    strategy_families = {
        str((node.get("strategy") or {}).get("family"))
        for node in nodes
        if (node.get("strategy") or {}).get("family")
    }
    adaptive_strategies = [
        strategy
        for strategy in adaptive.get("strategies") or []
        if isinstance(strategy, dict)
    ]
    priority_entries = [
        item
        for item in state.get("frontier") or []
        if (item.get("priority_components") or {}).get("evidence_basis")
    ]

    report_count = len(report_paths)
    unique_evidence = len(set(evidence_digests))
    return {
        "type": "search_trace_scorecard",
        "thread_id": thread_dir.name,
        "node_count": len(nodes),
        "root_count": sum(node.get("parent") in {None, ""} for node in nodes),
        "worker_report_count": report_count,
        "unique_experiment_source_count": len(set(source_digests)),
        "unique_evidence_count": unique_evidence,
        "duplicate_evidence_rate": (
            round(1 - unique_evidence / report_count, 6) if report_count else 0.0
        ),
        "negative_decision_count": len(negative_decisions),
        "negative_decisions_with_follow_ups": sum(
            bool(decision.get("follow_up_children")) for decision in negative_decisions
        ),
        "adaptive_child_count": len(adaptive_child_ids),
        "strategy_family_count": len(strategy_families),
        "adaptive_strategy_count": len(adaptive_strategies),
        "adaptive_strategy_id_count": len(
            {strategy.get("id") for strategy in adaptive_strategies}
        ),
        "adaptive_observation_count": len(adaptive.get("observations") or []),
        "adaptive_experiment_count": len(adaptive.get("experiments") or []),
        "duplicate_rejection_count": len(
            adaptive.get("duplicate_rejections") or []
        ),
        "evidence_prioritized_frontier_count": len(priority_entries),
        "search_disposition": adaptive.get("disposition"),
        "pause_reason": (adaptive.get("pause") or {}).get("reason"),
        "strong_result_verified": bool(
            (adaptive.get("strong_result_receipt") or {}).get("verified")
        ),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "node_status_counts": dict(
            sorted(Counter(str(node.get("status")) for node in nodes).items())
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("thread_dir", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            evaluate_search_trace(args.thread_dir.resolve()),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
