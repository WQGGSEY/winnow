from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from research_harness.agents.grilling import (
    DEFAULT_MAX_ROUNDS,
    run_grilling_session,
)
from research_harness.agents.research_refiner import run_research_refiner
from research_harness.connector.orchestrator import run_domain_connector
from research_harness.memory.lesson_distillation import run_lesson_distillation
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


def cmd_connector(
    repo_root: Path,
    *,
    grilling_session_path: Path,
    run_dir: Path | None,
    billing_ack: bool,
    execution_ack: bool,
    quota: int | None = None,
    max_fields_tried: int | None = None,
    field_seed: int | None = None,
) -> dict[str, Any]:
    """Turn a grilled problem into connector research inputs.

    Production freezes the problem and accepted success bar into one
    GoalContract;
    connector alternatives remain audit context and never become live roots.
    """
    grilling_session = _load_grilling_session(grilling_session_path)
    outcome = run_domain_connector(
        repo_root,
        grilling_session,
        run_dir=run_dir,
        billing_ack=billing_ack,
        execution_ack=execution_ack,
        quota=quota,
        max_fields_tried=max_fields_tried,
        field_seed=field_seed,
    )
    return outcome.session


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
            "High-level CLI for grilling, connector research, refinement, "
            "and lesson distillation."
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

    p_refine = subparsers.add_parser(
        "refine",
        help="Run only the research_refiner against an existing grilling + market brief.",
    )
    p_refine.add_argument("--grilling-session", required=True, type=Path)
    p_refine.add_argument("--market-research-brief", required=True, type=Path)
    p_refine.add_argument("--run-dir", type=Path)
    p_refine.add_argument("--billing-ack", action="store_true")
    p_refine.add_argument("--execute-ack", action="store_true")

    p_conn = subparsers.add_parser(
        "connector",
        help="ADR 0012: run the domain-connector over a grilling_session -> "
        "diverse far-framed claim_contracts (connector_session.json).",
    )
    p_conn.add_argument("--grilling-session", required=True, type=Path)
    p_conn.add_argument("--run-dir", type=Path,
                        help="Where connector_session.json is written; e.g. "
                        "runs/threads/<tid>/connector/.")
    p_conn.add_argument("--billing-ack", action="store_true")
    p_conn.add_argument("--execute-ack", action="store_true")
    p_conn.add_argument("--quota", type=int, default=None,
                        help="Target number of readings that must clear prune-1 "
                        "and reduction. Default from settings.")
    p_conn.add_argument("--max-fields-tried", type=int, default=None,
                        help="Compute cap on fields sampled. Default from settings.")
    p_conn.add_argument("--field-seed", type=int, default=None,
                        help="Override the field-draw seed (default: derived from "
                        "the grilling session id).")

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
    elif args.cmd == "refine":
        result = cmd_refine(
            repo_root,
            grilling_session_path=args.grilling_session,
            market_research_brief_path=args.market_research_brief,
            run_dir=args.run_dir,
            billing_ack=args.billing_ack,
            execution_ack=args.execute_ack,
        )
    elif args.cmd == "connector":
        result = cmd_connector(
            repo_root,
            grilling_session_path=args.grilling_session,
            run_dir=args.run_dir,
            billing_ack=args.billing_ack,
            execution_ack=args.execute_ack,
            quota=args.quota,
            max_fields_tried=args.max_fields_tried,
            field_seed=args.field_seed,
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
