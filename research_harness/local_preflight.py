from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_harness.config import (
    load_harness_config,
    load_lessons,
    load_research_profile,
    load_settings,
)
from research_harness.critics.governance import select_critics
from research_harness.memory.baseline_dossier import load_baseline_dossier
from research_harness.orchestrator.demo import _demo_node, run_demo
from research_harness.orchestrator.tree_search import run_mock_tree_search
from research_harness.orchestrator.validation import validate_node_invariants
from research_harness.schemas.validator import validate_named_schema


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run_preflight(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    settings = load_settings(root)
    harness_config = load_harness_config(root)
    research_profile = load_research_profile(root)
    lessons = load_lessons(root)
    baseline_dossier = load_baseline_dossier(root, "bd_agent_harness_20260523")

    node = _demo_node()
    validate_node_invariants(node)
    promotion_routing = select_critics(root, node)
    rebuttal_node = dict(node)
    rebuttal_node["stage"] = "rebuttal"
    rebuttal_routing = select_critics(root, rebuttal_node)

    run_dir = run_demo(root, root / "runs" / "prelive_preflight")
    state = json.loads((run_dir / "research_state_bundle.json").read_text(encoding="utf-8"))
    validate_named_schema("node", state["node"])
    validate_named_schema("worker_report", state["worker_report"])
    validate_named_schema("invocation_envelope", state["invocation_envelope"])
    validate_named_schema("job_manifest", state["job_manifest"])
    validate_named_schema("baseline_dossier", baseline_dossier)
    validate_named_schema("ac_decision", state["ac_decision"])
    tree_result = run_mock_tree_search(root, root / "runs" / "prelive_tree_search")
    validate_named_schema("search_state", tree_result["search_state"])

    return {
        "status": "passed",
        "settings_backend": settings["runtime"]["default_backend"],
        "harness_semantics": harness_config["harness_semantics"],
        "research_profile_id": research_profile["profile_id"],
        "active_lesson_count": len(lessons.get("active_lessons", [])),
        "baseline_dossier_id": baseline_dossier["id"],
        "promotion_critic_count": len(promotion_routing["applied_critics"]),
        "rebuttal_critic_count": len(rebuttal_routing["applied_critics"]),
        "run_dir": str(run_dir),
        "tree_search_state": str(root / "runs" / "prelive_tree_search" / "search_state.json"),
        "interactive_summary": str(run_dir / "interactive_summary.html"),
    }


def main() -> None:
    result = run_preflight()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
