from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from research_harness.config import load_settings
from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.local_preflight import run_preflight
from research_harness.orchestrator.treesearch.agent_manager import (
    run_agent_manager_search,
)
from research_harness.publishing.ac import decide_acceptance
from research_harness.publishing.publish import publish_state_bundle
from research_harness.publishing.rebuttal import (
    build_orchestrator_rebuttal,
    build_rebuttal_packet,
)
from research_harness.schemas.validator import validate_named_schema


class ProductionRunError(ValueError):
    """Raised when the production runner cannot proceed safely."""


_PLACEHOLDER_BASELINE_PREFIXES = ("TBD", "tbd", "placeholder")


def _is_placeholder_contract(contract: dict[str, Any]) -> bool:
    """True if the grilling intake left placeholders for the Professor to fill."""
    baselines = contract.get("mandatory_baselines") or []
    for b in baselines:
        if not isinstance(b, str):
            continue
        # placeholder pattern: anything containing 'TBD (Professor will design)'
        if "TBD" in b or "Professor will design" in b or "Professor must" in b:
            return True
    success = contract.get("success_criteria") or []
    for s in success:
        if isinstance(s, str) and "Professor must" in s:
            return True
    disproof = contract.get("disproof_conditions") or []
    for d in disproof:
        if isinstance(d, str) and "Professor must" in d:
            return True
    return False


def _professor_design_claim_if_placeholder(
    root_node: dict[str, Any],
    settings: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """If the root_node has placeholder fields, get the Professor to design
    the real claim contract before the tree starts. Records the hand-off
    dialog under run_dir/intake_to_claim_dialog.json so the operator can
    audit the conversion.
    """
    contract = root_node.get("claim_contract", {}) or {}
    if not _is_placeholder_contract(contract):
        return root_node

    llm_cfg = (settings.get("runtime", {}) or {}).get("llm_orchestrator", {}) or {}
    if not llm_cfg.get("enabled", False):
        # LLM orchestrator disabled — leave placeholders. Downstream validation
        # will reject them, surfacing the misconfiguration loudly.
        return root_node

    from research_harness.orchestrator.llm_orchestrator import (
        MockLLMClient,
        Professor,
        build_llm_client,
    )
    from research_harness.orchestrator.llm_orchestrator.mock_handlers import (
        register_default_mock_handlers,
    )

    client = build_llm_client(settings, role="professor")
    if isinstance(client, MockLLMClient):
        register_default_mock_handlers(client)
    professor = Professor(client)
    new_contract, dialog = professor.problem_to_initial_claim(
        problem_statement=contract.get("claim_under_test", ""),
        domain=str(root_node.get("domain", "")),
        node_type=str(root_node.get("type", "validity")),
        goal_facets=list(
            root_node.get("lineage", {}).get("covers_goal_facets", []) or []
        ),
    )
    revised = dict(root_node)
    revised["claim_contract"] = {**contract, **new_contract}
    # Validate; if Professor's contract somehow fails, abort loudly so the
    # operator sees the upstream problem rather than a silent fallback.
    from research_harness.orchestrator.validation import validate_node_invariants

    validate_node_invariants(revised)
    # Persist the hand-off dialog so it shows up in the run audit trail.
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "intake_to_claim_dialog.json").write_text(
        json.dumps(
            {
                "original_contract": contract,
                "new_contract": new_contract,
                "dialog": [
                    {
                        "speaker": e.speaker,
                        "intent": e.intent,
                        "text": e.text,
                        "metadata": e.metadata,
                    }
                    for e in dialog
                ],
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    return revised


def run_production_pipeline(
    repo_root: Path,
    run_dir: Path,
    *,
    backend_name: str = "mock",
    publish: bool = True,
    root_node: dict[str, Any] | None = None,
    max_ac_revisions: int = 1,
) -> dict[str, Any]:
    """Chain preflight -> mock tree search -> rebuttal/AC -> publish in one call.

    Live Claude execution remains outside this chain. Operators run live
    nodes through research_harness.orchestrator.live_dispatch, then apply
    results via the existing approval-gated modules.
    """

    repo_root = repo_root.resolve()
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    settings = load_settings(repo_root)
    preflight = run_preflight(repo_root)
    if preflight["status"] != "passed":
        raise ProductionRunError(
            f"local preflight did not pass: status={preflight['status']}"
        )

    # If the root_node came in with placeholder baselines/success/disproof
    # (the new grilling intake fills these as TBD), the Professor designs the
    # real claim_contract here BEFORE the tree starts. This is the
    # 정출연→교수님 hand-off: the user owns the problem, the lab owns the
    # research design.
    if root_node is not None:
        root_node = _professor_design_claim_if_placeholder(root_node, settings, run_dir)

    tree_dir = run_dir / "tree"
    tree_result = run_agent_manager_search(
        repo_root,
        tree_dir,
        backend_name=backend_name,
        root_node=root_node,
    )
    search_state = tree_result["search_state"]
    promoted_ids = list(search_state["promoted_node_ids"])

    templates_used = {
        node["id"]: (node.get("outputs", {}) or {}).get("template_used")
        for node in search_state["nodes"]
    }
    fallback_node_ids = [
        node_id
        for node_id, template in templates_used.items()
        if template == "_fallback_demo"
    ]

    rebuttal_summary: dict[str, Any] | None = None
    publication_dispatch: dict[str, Any] | None = None
    research_state_bundle_path: str | None = None

    if promoted_ids:
        # Prefer the promoted root (parent is None) so the paper title and
        # claim reflect the user's actual question, not a typed draft sibling
        # introduced by num_drafts. Fall back to the first promoted node.
        promoted_id = next(
            (
                pid
                for pid in promoted_ids
                if _node_by_id(search_state, pid).get("parent") is None
            ),
            promoted_ids[0],
        )
        node = _node_by_id(search_state, promoted_id)
        node_dir = tree_dir / "nodes" / promoted_id
        worker_report = _read_json(node_dir / "worker_report.json")
        critic_reviews = _read_json(node_dir / "critic_reviews.json")
        reduction = _read_json(node_dir / "orchestrator_reduction.json")
        validate_named_schema("worker_report", worker_report)
        for review in critic_reviews:
            validate_named_schema("critic_review", review)

        critic_routing = select_critics(repo_root, node)
        rebuttal_dir = run_dir / "rebuttal"
        rebuttal_dir.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {
            "node": node,
            "worker_report": worker_report,
            "critic_routing": critic_routing,
            "critic_reviews": critic_reviews,
            "orchestrator_reduction": reduction,
        }
        rebuttal_packet_path = rebuttal_dir / "rebuttal_packet.md"
        orchestrator_rebuttal_path = rebuttal_dir / "orchestrator_rebuttal.md"
        build_rebuttal_packet(state, rebuttal_packet_path)
        build_orchestrator_rebuttal(state, orchestrator_rebuttal_path)

        rebuttal_node = dict(node)
        rebuttal_node["stage"] = "rebuttal"
        validate_named_schema("node", rebuttal_node)
        rebuttal_routing = select_critics(repo_root, rebuttal_node)
        rebuttal_reviews = run_critic_reviews(
            rebuttal_node,
            worker_report,
            rebuttal_routing,
        )
        for review in rebuttal_reviews:
            validate_named_schema("critic_review", review)

        ac_decision = decide_acceptance(rebuttal_reviews, settings)
        validate_named_schema("ac_decision", ac_decision)

        state.update(
            {
                "rebuttal_packet_path": str(rebuttal_packet_path),
                "orchestrator_rebuttal_path": str(orchestrator_rebuttal_path),
                "rebuttal_critic_routing": rebuttal_routing,
                "rebuttal_critic_reviews": rebuttal_reviews,
                "ac_decision": ac_decision,
                "evidence_is_fake": bool(fallback_node_ids),
            }
        )
        rebuttal_critic_bundle_path = rebuttal_dir / "rebuttal_critic_bundle.json"
        ac_decision_path = rebuttal_dir / "ac_decision.json"
        state_bundle_path = rebuttal_dir / "research_state_bundle.json"
        _write_json(
            rebuttal_critic_bundle_path,
            {"routing": rebuttal_routing, "reviews": rebuttal_reviews},
        )
        _write_json(ac_decision_path, ac_decision)
        _write_json(state_bundle_path, state)
        research_state_bundle_path = str(state_bundle_path)

        rebuttal_summary = {
            "promoted_node_id": promoted_id,
            "rebuttal_packet_path": str(rebuttal_packet_path),
            "orchestrator_rebuttal_path": str(orchestrator_rebuttal_path),
            "rebuttal_critic_bundle_path": str(rebuttal_critic_bundle_path),
            "ac_decision_path": str(ac_decision_path),
            "research_state_bundle_path": str(state_bundle_path),
            "ac_decision": ac_decision,
        }

        # AC-reject revision loop: if the area chair rejected the paper,
        # ask the Professor to propose a NEW root claim (honest + strong,
        # not lazy) and rerun tree search with that claim. Capped by
        # max_ac_revisions to avoid loops.
        ac_revision_history: list[dict[str, Any]] = []
        revision_index = 0
        while (
            ac_decision.get("decision") == "reject"
            and revision_index < max_ac_revisions
        ):
            revision_index += 1
            from research_harness.orchestrator.llm_orchestrator import (
                MockLLMClient,
                Professor,
                build_llm_client,
            )
            from research_harness.orchestrator.llm_orchestrator.mock_handlers import (
                register_default_mock_handlers,
            )

            llm_cfg = (settings.get("runtime", {}) or {}).get("llm_orchestrator", {}) or {}
            if not llm_cfg.get("enabled", False):
                break
            client = build_llm_client(settings, role="professor")
            if isinstance(client, MockLLMClient):
                register_default_mock_handlers(client)
            professor = Professor(client)
            new_claim, entries = professor.revise_after_ac_reject(
                root_claim=node["claim_contract"]["claim_under_test"],
                ac_decision=ac_decision,
                promoted_nodes=[
                    _node_by_id(search_state, pid) for pid in promoted_ids
                ],
            )
            ac_revision_history.append(
                {
                    "revision": revision_index,
                    "previous_claim": node["claim_contract"]["claim_under_test"],
                    "new_claim": new_claim,
                    "dialog": [
                        {
                            "speaker": e.speaker,
                            "intent": e.intent,
                            "text": e.text,
                            "metadata": e.metadata,
                        }
                        for e in entries
                    ],
                    "previous_ac_decision": dict(ac_decision),
                }
            )
            # Build a new root node with the revised claim and recurse.
            new_root = copy.deepcopy(root_node or _node_by_id(search_state, promoted_ids[0]))
            new_root["claim_contract"]["claim_under_test"] = new_claim
            new_root["id"] = f"{new_root['id']}_rev{revision_index}"
            new_root["parent"] = None
            new_root["status"] = "ready"
            new_root["lineage"]["introduced_assumptions"] = [
                f"AC rejected the previous paper (revision {revision_index}); "
                f"Professor proposed honest+strong successor claim.",
            ]
            new_run_dir = run_dir.parent / f"{run_dir.name}_rev{revision_index}"
            rerun = run_production_pipeline(
                repo_root,
                new_run_dir,
                backend_name=backend_name,
                publish=publish,
                root_node=new_root,
                max_ac_revisions=max_ac_revisions - revision_index,
            )
            rerun["ac_revision_history"] = (
                ac_revision_history + (rerun.get("ac_revision_history") or [])
            )
            return rerun

        if publish:
            publication_dir = run_dir / "publication"
            publication_dispatch = publish_state_bundle(
                state,
                settings,
                publication_dir,
            )

    summary = {
        "type": "production_run_summary",
        "repo_root": str(repo_root),
        "run_dir": str(run_dir),
        "backend": backend_name,
        "preflight_status": preflight["status"],
        "tree_search_status": search_state["status"],
        "node_count": len(search_state["nodes"]),
        "promoted_node_ids": promoted_ids,
        "templates_used": templates_used,
        "fallback_node_ids": fallback_node_ids,
        "evidence_is_fake": bool(fallback_node_ids),
        "tree_search_state_path": str(tree_dir / "search_state.json"),
        "tree_search_summary_path": str(tree_dir / "tree_search_summary.json"),
        "research_state_bundle_path": research_state_bundle_path,
        "rebuttal_summary": rebuttal_summary,
        "publication_dispatch": publication_dispatch,
        "live_execution": (
            "Live Claude workers are not chained here. Use "
            "research_harness.orchestrator.live_dispatch and the existing "
            "approval-gated modules for live nodes."
        ),
    }
    _write_json(run_dir / "production_run_summary.json", summary)
    return summary


def _node_by_id(search_state: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in search_state["nodes"]:
        if node["id"] == node_id:
            return node
    raise ProductionRunError(f"promoted node not found in search state: {node_id}")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the production pipeline end-to-end on the mock backend: "
            "preflight, tree search, rebuttal, AC decision, and gated publish."
        )
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip the renderer dispatch; rebuttal/AC artifacts still written.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    run_dir = args.run_dir or repo_root / "runs" / "production_run"
    summary = run_production_pipeline(
        repo_root,
        run_dir,
        publish=not args.no_publish,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
