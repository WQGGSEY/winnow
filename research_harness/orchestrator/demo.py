from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.config import load_settings
from research_harness.orchestrator.branch_prior import build_failure_branch_prior
from research_harness.orchestrator.experiment_plan import (
    build_demo_experiment_plan,
    build_job_manifest_from_experiment_plan,
    validate_experiment_plan,
)
from research_harness.orchestrator.reduction import reduce_node
from research_harness.orchestrator.validation import validate_node_invariants
from research_harness.memory.baseline_dossier import build_baseline_resolution_report
from research_harness.memory.failure_memory import (
    FailureMemoryResult,
    record_failure_candidate,
    record_runner_failure_candidate,
)
from research_harness.publishing.ac import decide_acceptance
from research_harness.publishing.html import render_interactive_html
from research_harness.publishing.rebuttal import (
    build_orchestrator_rebuttal,
    build_rebuttal_packet,
)
from research_harness.runner.evidence import build_worker_report_from_runner_evidence
from research_harness.runner.local_runner import LocalRunner
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.codex_invoker import CodexInvoker
from research_harness.workers.workspace import prepare_node_workspace


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _settings(repo_root: Path) -> dict[str, Any]:
    return load_settings(repo_root)


def _demo_node() -> dict[str, Any]:
    return {
        "id": "n_demo_001",
        "type": "capability",
        "status": "ready",
        "domain": "agent_harness",
        "stage": "promotion",
        "parent": None,
        "lineage": {
            "root_goal_id": "rg_demo_001",
            "covers_goal_facets": [
                "subscription_only",
                "bounded_agent_runtime",
                "sakanav2_compatible_search_skeleton",
            ],
            "inherited_assumptions": [
                "Outer orchestrator owns search policy.",
                "Codex workers are bounded tools, not autonomous researchers.",
            ],
            "introduced_assumptions": [
                "Runtime envelopes are sufficient to make subscription workers usable in the tree.",
            ],
            "taste_constraints_applied": [
                "claim_first",
                "capability_then_mechanism",
                "necessity_against_current_best_naive_random",
            ],
        },
        "claim_contract": {
            "claim_under_test": (
                "A Sakana-v2-compatible harness can use bounded Codex "
                "subscription workers without corrupting the outer tree search."
            ),
            "mandatory_baselines": [
                "current_best_known: Sakana AI Scientist-v2 search skeleton",
                "naive: direct llm.generate to Codex port",
                "random_or_null: ad hoc Codex sessions without harness",
            ],
            "success_criteria": [
                "Worker writes only node-local artifacts.",
                "Critic selection is deterministic and independent of orchestrator choice.",
                "Publication gate emits rebuttal and AC artifacts from a state bundle.",
            ],
            "disproof_conditions": [
                "Worker expands scope or mutates global state.",
                "Critic routing can be bypassed by orchestrator choice.",
                "Missing baseline is treated as success.",
            ],
        },
        "baseline_refs": [
            {
                "baseline_dossier_id": "bd_agent_harness_20260523",
                "candidate_ids": [
                    "c1_sakana_ai_scientist_v2",
                    "c2_direct_api_port",
                    "c3_no_orchestrated_harness",
                ],
                "roles": ["current_best_known", "naive", "random_or_null"],
            }
        ],
        "runtime_profile": {
            "worker_type": "experiment_worker",
            "timeout_policy": "task_class_dependent",
            "turn_budget": 6,
        },
        "failure_retrieval": {
            "query_tags": ["agent_harness", "nested_agent_risk", "subscription_oauth"],
            "selected_fail_files": [],
        },
        "outputs": {
            "artifacts": [],
            "verdict": None,
        },
    }


def _validate_reviews(reviews: list[dict[str, Any]]) -> None:
    for review in reviews:
        validate_named_schema("critic_review", review)


def _failure_memory_summary(result: FailureMemoryResult | None) -> dict[str, str] | None:
    if result is None:
        return None
    return {
        "record_path": str(result.record_path),
        "index_path": str(result.index_path),
        "lesson": result.lesson,
    }


def _worker_failure_source(worker_report: dict[str, Any]) -> str | None:
    artifacts = worker_report.get("artifacts") or []
    if not artifacts:
        return None
    return str(artifacts[0])


def run_demo(repo_root: Path | None = None, run_dir: Path | None = None) -> Path:
    repo_root = repo_root or _repo_root()
    settings = _settings(repo_root)
    run_dir = run_dir or repo_root / "runs" / "demo_run"
    run_dir.mkdir(parents=True, exist_ok=True)

    node = _demo_node()
    validate_node_invariants(node)
    validate_named_schema("node", node)
    _write_json(run_dir / "node.json", node)

    experiment_plan = build_demo_experiment_plan(node, run_dir)
    validate_experiment_plan(node, experiment_plan, run_dir)
    validate_named_schema("experiment_plan", experiment_plan)
    _write_json(run_dir / "experiment_plan.json", experiment_plan)

    workspace_paths = prepare_node_workspace(run_dir, node["id"])
    invocation_envelope = CodexInvoker(
        workspace_paths["workspace"],
        settings,
        repo_root=repo_root,
    ).write_dry_run_artifacts(node, experiment_plan=experiment_plan)
    validate_named_schema("invocation_envelope", invocation_envelope)
    worker_task = json.loads(
        Path(invocation_envelope["worker_task_path"]).read_text(encoding="utf-8")
    )
    validate_named_schema("worker_task", worker_task)

    job_manifest = build_job_manifest_from_experiment_plan(node, experiment_plan, run_dir)
    validate_named_schema("job_manifest", job_manifest)
    runner = LocalRunner(run_dir, settings=settings)
    runner.validate_or_raise(job_manifest)
    _write_json(run_dir / "job_manifest.json", job_manifest)
    runner_result = runner.execute(job_manifest)
    validate_named_schema("runner_result", runner_result)
    runner_failure_memory = record_runner_failure_candidate(repo_root, node, runner_result)
    evidence_report = build_worker_report_from_runner_evidence(
        node,
        job_manifest,
        runner_result,
        run_dir,
    )

    baseline_resolution = build_baseline_resolution_report(
        repo_root,
        "bd_agent_harness_20260523",
        run_dir / "baseline_resolution.md",
    )

    worker_report = evidence_report.worker_report
    validate_named_schema("worker_report", worker_report)
    _write_json(run_dir / "worker_report.json", worker_report)
    worker_failure_memory = None
    if worker_report.get("failure_record_candidate") and runner_failure_memory is None:
        worker_failure_memory = record_failure_candidate(
            repo_root,
            node,
            worker_report,
            source_artifact=_worker_failure_source(worker_report),
        )

    critic_routing = select_critics(repo_root, node)
    critic_reviews = run_critic_reviews(node, worker_report, critic_routing)
    _validate_reviews(critic_reviews)
    _write_json(
        run_dir / "critic_review_bundle.json",
        {
            "routing": critic_routing,
            "reviews": critic_reviews,
        },
    )

    failure_branch_prior = build_failure_branch_prior(repo_root, node, settings)
    reduction = reduce_node(
        node,
        worker_report,
        critic_reviews,
        branch_prior=failure_branch_prior,
    )
    _write_json(run_dir / "orchestrator_reduction.json", reduction)

    state = {
        "node": node,
        "invocation_envelope": invocation_envelope,
        "worker_task": worker_task,
        "experiment_plan": experiment_plan,
        "job_manifest": job_manifest,
        "runner_result": runner_result,
        "source_files": evidence_report.source_files,
        "metrics_evidence_paths": evidence_report.metrics_evidence_paths,
        "runner_failure_memory": _failure_memory_summary(runner_failure_memory),
        "worker_failure_memory": _failure_memory_summary(worker_failure_memory),
        "baseline_resolution": baseline_resolution,
        "worker_report": worker_report,
        "failure_branch_prior": failure_branch_prior,
        "critic_routing": critic_routing,
        "critic_reviews": critic_reviews,
        "orchestrator_reduction": reduction,
    }

    build_rebuttal_packet(state, run_dir / "rebuttal_packet.md")
    build_orchestrator_rebuttal(state, run_dir / "orchestrator_rebuttal.md")

    rebuttal_node = dict(node)
    rebuttal_node["stage"] = "rebuttal"
    validate_named_schema("node", rebuttal_node)
    rebuttal_routing = select_critics(repo_root, rebuttal_node)
    rebuttal_reviews = run_critic_reviews(rebuttal_node, worker_report, rebuttal_routing)
    _validate_reviews(rebuttal_reviews)
    _write_json(
        run_dir / "rebuttal_critic_bundle.json",
        {
            "routing": rebuttal_routing,
            "reviews": rebuttal_reviews,
        },
    )

    ac_decision = decide_acceptance(rebuttal_reviews, settings)
    validate_named_schema("ac_decision", ac_decision)
    _write_json(run_dir / "ac_decision.json", ac_decision)

    state.update(
        {
            "rebuttal_critic_routing": rebuttal_routing,
            "rebuttal_critic_reviews": rebuttal_reviews,
            "ac_decision": ac_decision,
        }
    )
    _write_json(run_dir / "research_state_bundle.json", state)
    render_interactive_html(state, run_dir / "interactive_summary.html")

    return run_dir


def main() -> None:
    run_dir = run_demo()
    print(f"Demo run written to {run_dir}")
    print(f"Interactive summary: {run_dir / 'interactive_summary.html'}")


if __name__ == "__main__":
    main()
