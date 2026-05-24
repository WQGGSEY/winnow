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
from research_harness.memory.lesson_distillation import run_lesson_distillation
from research_harness.orchestrator.root_node_from_grilling import (
    attach_market_research_dossier,
    build_root_node_from_grilling,
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
        "root_node_path": str(root_node_path),
        "production_run_summary_path": str(production_dir / "production_run_summary.json"),
        "production_summary": production_summary,
    }
    (run_dir / "research_run_bundle.json").write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return bundle


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
        help="Take a grilling session, run market research, and chain into production.",
    )
    p_research.add_argument("--grilling-session", required=True, type=Path)
    p_research.add_argument("--run-dir", type=Path)
    p_research.add_argument("--max-papers", type=int, default=10)
    p_research.add_argument("--no-google-scholar", action="store_true")
    p_research.add_argument("--no-publish", action="store_true")

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
