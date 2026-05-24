from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from research_harness.agents.grilling import (
    DEFAULT_MAX_ROUNDS,
    run_grilling_session,
)
from research_harness.agents.market_research import run_market_research
from research_harness.agents.research_refiner import run_research_refiner
from research_harness.memory.lesson_distillation import run_lesson_distillation
from research_harness.orchestrator.root_node_from_grilling import (
    attach_market_research_dossier,
    build_root_node_from_grilling,
    build_root_node_from_refined_plan,
    has_placeholder_baseline,
)
from research_harness.production_runner import run_production_pipeline
from research_harness.schemas.validator import validate_named_schema


class ResearchRunnerError(ValueError):
    """Raised when the research runner cannot proceed."""


def cmd_grill(
    repo_root: Path,
    *,
    user_goal: str,
    run_dir: Path | None,
    max_rounds: int,
    billing_ack: bool,
    execution_ack: bool,
) -> dict[str, Any]:
    session = run_grilling_session(
        repo_root,
        user_goal=user_goal,
        run_dir=run_dir,
        max_rounds=max_rounds,
        billing_ack=billing_ack,
        execution_ack=execution_ack,
    )
    return session


def cmd_research(
    repo_root: Path,
    *,
    grilling_session_path: Path,
    run_dir: Path | None,
    max_papers: int,
    enable_google_scholar: bool,
    publish: bool,
    skip_refine: bool = False,
    refiner_billing_ack: bool | None = None,
    refiner_execution_ack: bool | None = None,
) -> dict[str, Any]:
    grilling_session = _load_grilling_session(grilling_session_path)
    run_dir = (run_dir or repo_root / "runs" / "research" / grilling_session["session_id"]).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    market_dir = run_dir / "market_research"
    market_outcome = run_market_research(
        repo_root,
        grilling_session,
        run_dir=market_dir,
        max_papers=max_papers,
        enable_google_scholar=enable_google_scholar,
        write_dossier_to_memory=True,
    )

    refined_plan: dict[str, Any] | None = None
    refined_plan_path: str | None = None
    dataset_manifest_path: str | None = None
    if not skip_refine:
        refine_dir = run_dir / "refine"
        refined_plan = run_research_refiner(
            repo_root,
            grilling_session=grilling_session,
            market_research_brief=market_outcome.brief,
            run_dir=refine_dir,
            billing_ack=refiner_billing_ack,
            execution_ack=refiner_execution_ack,
        )
        refined_plan_path = refined_plan["plan_path"]
        if refined_plan["status"] in {"done", "max_rounds_reached"}:
            dataset_manifest_path = refined_plan["dataset_manifest_path"]

    if refined_plan is not None and refined_plan["status"] in {"done", "max_rounds_reached"}:
        candidate_ids = [c["id"] for c in market_outcome.dossier["candidates_index"]]
        root_node = build_root_node_from_refined_plan(
            refined_plan,
            grilling_session,
            baseline_dossier_id=market_outcome.dossier["id"],
            candidate_ids=candidate_ids,
            dataset_manifest_path=dataset_manifest_path,
        )
    else:
        root_node = build_root_node_from_grilling(grilling_session)
        if has_placeholder_baseline(root_node):
            candidate_ids = [c["id"] for c in market_outcome.dossier["candidates_index"]]
            root_node = attach_market_research_dossier(
                root_node,
                baseline_dossier_id=market_outcome.dossier["id"],
                candidate_ids=candidate_ids,
                baseline_analysis_md_path=market_outcome.brief.get(
                    "baseline_analysis_md_path"
                ),
            )
    validate_named_schema("node", root_node)
    root_node_path = run_dir / "root_node.json"
    root_node_path.write_text(
        json.dumps(root_node, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if dataset_manifest_path:
        node_workspace = run_dir / "production" / "tree" / "nodes" / root_node["id"] / "workspace"
        node_workspace.mkdir(parents=True, exist_ok=True)
        (node_workspace / "dataset_manifest.json").write_text(
            Path(dataset_manifest_path).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    production_dir = run_dir / "production"
    production_summary = run_production_pipeline(
        repo_root,
        production_dir,
        publish=publish,
        root_node=root_node,
    )

    bundle = {
        "type": "research_run_bundle",
        "run_dir": str(run_dir),
        "grilling_session_path": str(grilling_session_path),
        "grilling_session_id": grilling_session["session_id"],
        "market_research_brief_path": market_outcome.brief["brief_path"],
        "baseline_dossier_id": market_outcome.brief["baseline_dossier_id"],
        "refine_skipped": bool(skip_refine),
        "refined_plan_path": refined_plan_path,
        "refined_plan_status": refined_plan["status"] if refined_plan else None,
        "dataset_manifest_path": dataset_manifest_path,
        "root_node_path": str(root_node_path),
        "production_run_summary_path": str(production_dir / "production_run_summary.json"),
        "production_summary": production_summary,
    }
    (run_dir / "research_run_bundle.json").write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return bundle


def cmd_refine(
    repo_root: Path,
    *,
    grilling_session_path: Path,
    market_research_brief_path: Path,
    run_dir: Path | None,
    billing_ack: bool,
    execution_ack: bool,
) -> dict[str, Any]:
    grilling_session = _load_grilling_session(grilling_session_path)
    brief = json.loads(market_research_brief_path.read_text(encoding="utf-8"))
    validate_named_schema("market_research_brief", brief)
    return run_research_refiner(
        repo_root,
        grilling_session=grilling_session,
        market_research_brief=brief,
        run_dir=run_dir,
        billing_ack=billing_ack,
        execution_ack=execution_ack,
    )


def cmd_distill(
    repo_root: Path,
    *,
    summary_dir: Path | None,
    approve: bool,
    billing_ack: bool,
    execution_ack: bool,
) -> dict[str, Any]:
    return run_lesson_distillation(
        repo_root,
        summary_dir=summary_dir,
        approve=approve,
        billing_ack=billing_ack,
        execution_ack=execution_ack,
    )


def _load_grilling_session(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_named_schema("grilling_session", data)
    if data["status"] not in {"done", "max_rounds_reached"}:
        raise ResearchRunnerError(
            f"grilling session status {data['status']!r} is not usable for research"
        )
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="research_runner",
        description=(
            "High-level CLI: grill a user goal, run market research, generate "
            "a root node, chain into the production pipeline, and run "
            "lesson distillation."
        ),
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_grill = subparsers.add_parser(
        "grill", help="Run a multi-turn grilling session and save grilling_session.json"
    )
    p_grill.add_argument("--user-goal", required=True)
    p_grill.add_argument("--run-dir", type=Path)
    p_grill.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    p_grill.add_argument("--billing-ack", action="store_true")
    p_grill.add_argument("--execute-ack", action="store_true")

    p_research = subparsers.add_parser(
        "research",
        help="Take a grilling session, run market research, refiner, and production.",
    )
    p_research.add_argument("--grilling-session", required=True, type=Path)
    p_research.add_argument("--run-dir", type=Path)
    p_research.add_argument("--max-papers", type=int, default=10)
    p_research.add_argument("--no-google-scholar", action="store_true")
    p_research.add_argument("--no-publish", action="store_true")
    p_research.add_argument(
        "--skip-refine",
        action="store_true",
        help="Skip research_refiner (faster prototyping; production should not skip).",
    )
    p_research.add_argument("--refiner-billing-ack", action="store_true")
    p_research.add_argument("--refiner-execute-ack", action="store_true")

    p_refine = subparsers.add_parser(
        "refine",
        help="Run only the research_refiner against an existing grilling + market brief.",
    )
    p_refine.add_argument("--grilling-session", required=True, type=Path)
    p_refine.add_argument("--market-research-brief", required=True, type=Path)
    p_refine.add_argument("--run-dir", type=Path)
    p_refine.add_argument("--billing-ack", action="store_true")
    p_refine.add_argument("--execute-ack", action="store_true")

    p_distill = subparsers.add_parser(
        "distill", help="Run lesson distillation gate (deterministic trigger)."
    )
    p_distill.add_argument("--summary-dir", type=Path)
    p_distill.add_argument("--approve", action="store_true")
    p_distill.add_argument("--billing-ack", action="store_true")
    p_distill.add_argument("--execute-ack", action="store_true")

    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    if args.cmd == "grill":
        result = cmd_grill(
            repo_root,
            user_goal=args.user_goal,
            run_dir=args.run_dir,
            max_rounds=args.max_rounds,
            billing_ack=args.billing_ack,
            execution_ack=args.execute_ack,
        )
    elif args.cmd == "research":
        result = cmd_research(
            repo_root,
            grilling_session_path=args.grilling_session,
            run_dir=args.run_dir,
            max_papers=args.max_papers,
            enable_google_scholar=not args.no_google_scholar,
            publish=not args.no_publish,
            skip_refine=args.skip_refine,
            refiner_billing_ack=True if args.refiner_billing_ack else None,
            refiner_execution_ack=True if args.refiner_execute_ack else None,
        )
    elif args.cmd == "refine":
        result = cmd_refine(
            repo_root,
            grilling_session_path=args.grilling_session,
            market_research_brief_path=args.market_research_brief,
            run_dir=args.run_dir,
            billing_ack=args.billing_ack,
            execution_ack=args.execute_ack,
        )
    elif args.cmd == "distill":
        result = cmd_distill(
            repo_root,
            summary_dir=args.summary_dir,
            approve=args.approve,
            billing_ack=args.billing_ack,
            execution_ack=args.execute_ack,
        )
    else:
        parser.error(f"unknown subcommand: {args.cmd}")
        return
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
