from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from research_harness.config import load_settings
from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.publishing.ac import decide_acceptance
from research_harness.publishing.rebuttal import (
    build_orchestrator_rebuttal,
    build_rebuttal_packet,
)
from research_harness.schemas.validator import validate_named_schema


class LiveRebuttalSessionError(ValueError):
    """Raised when a live rebuttal session input is invalid."""


def build_live_rebuttal_session(
    repo_root: Path,
    bundle_path: Path,
    *,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Build rebuttal, rebuttal critics, and AC decision for a promoted live node."""

    repo_root = repo_root.resolve()
    bundle_path = bundle_path.resolve()
    bundle = _load_json(bundle_path)
    validate_named_schema("live_reduction_bundle", bundle)
    output_dir = (output_dir or bundle_path.parent).resolve()
    session_path = output_dir / "live_rebuttal_session.json"
    node_id = bundle["node_id"]
    reduction = bundle["orchestrator_reduction"]

    if reduction["next_transition"] != "promoted":
        session = {
            "type": "live_rebuttal_session",
            "status": "blocked_not_promoted",
            "node_id": node_id,
            "bundle_path": str(bundle_path),
            "rebuttal_packet_path": None,
            "orchestrator_rebuttal_path": None,
            "rebuttal_critic_bundle_path": None,
            "ac_decision_path": None,
            "research_state_bundle_path": None,
            "session_path": str(session_path),
            "state_mutation": "forbidden",
            "memory_mutation": "forbidden",
            "ac_decision": None,
            "publication_ready": False,
            "error": "live rebuttal session requires a promoted reduction",
        }
        return _write_session(session_path, session)

    settings = load_settings(repo_root)
    state = {
        "node": bundle["node"],
        "worker_report": bundle["worker_report"],
        "critic_routing": bundle["critic_routing"],
        "critic_reviews": bundle["critic_reviews"],
        "failure_branch_prior": bundle["failure_branch_prior"],
        "orchestrator_reduction": reduction,
        "live_reduction_bundle_path": str(bundle_path),
    }
    rebuttal_packet_path = output_dir / "live_rebuttal_packet.md"
    orchestrator_rebuttal_path = output_dir / "live_orchestrator_rebuttal.md"
    build_rebuttal_packet(state, rebuttal_packet_path)
    build_orchestrator_rebuttal(state, orchestrator_rebuttal_path)

    rebuttal_node = dict(bundle["node"])
    rebuttal_node["stage"] = "rebuttal"
    validate_named_schema("node", rebuttal_node)
    rebuttal_routing = select_critics(repo_root, rebuttal_node)
    rebuttal_reviews = run_critic_reviews(
        rebuttal_node,
        bundle["worker_report"],
        rebuttal_routing,
    )
    for review in rebuttal_reviews:
        validate_named_schema("critic_review", review)
    ac_decision = decide_acceptance(rebuttal_reviews, settings)
    validate_named_schema("ac_decision", ac_decision)

    rebuttal_critic_bundle_path = output_dir / "live_rebuttal_critic_bundle.json"
    ac_decision_path = output_dir / "live_ac_decision.json"
    research_state_bundle_path = output_dir / "live_research_state_bundle.json"
    _write_json(
        rebuttal_critic_bundle_path,
        {
            "routing": rebuttal_routing,
            "reviews": rebuttal_reviews,
        },
    )
    _write_json(ac_decision_path, ac_decision)
    state.update(
        {
            "rebuttal_packet_path": str(rebuttal_packet_path),
            "orchestrator_rebuttal_path": str(orchestrator_rebuttal_path),
            "rebuttal_critic_routing": rebuttal_routing,
            "rebuttal_critic_reviews": rebuttal_reviews,
            "ac_decision": ac_decision,
        }
    )
    _write_json(research_state_bundle_path, state)

    session = {
        "type": "live_rebuttal_session",
        "status": "completed",
        "node_id": node_id,
        "bundle_path": str(bundle_path),
        "rebuttal_packet_path": str(rebuttal_packet_path),
        "orchestrator_rebuttal_path": str(orchestrator_rebuttal_path),
        "rebuttal_critic_bundle_path": str(rebuttal_critic_bundle_path),
        "ac_decision_path": str(ac_decision_path),
        "research_state_bundle_path": str(research_state_bundle_path),
        "session_path": str(session_path),
        "state_mutation": "forbidden",
        "memory_mutation": "forbidden",
        "ac_decision": ac_decision,
        "publication_ready": ac_decision["decision"] == "accept",
        "error": None,
    }
    return _write_session(session_path, session)


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise LiveRebuttalSessionError(f"expected JSON object: {path}")
    return data


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_session(path: Path, session: dict[str, Any]) -> dict[str, Any]:
    validate_named_schema("live_rebuttal_session", session)
    _write_json(path, session)
    return session


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a live rebuttal session from a promoted live reduction bundle."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    try:
        session = build_live_rebuttal_session(
            args.repo_root,
            args.bundle,
            output_dir=args.output_dir,
        )
    except LiveRebuttalSessionError as exc:
        print(
            json.dumps(
                {
                    "type": "live_rebuttal_session_error",
                    "status": "blocked",
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(2) from exc
    print(json.dumps(session, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
