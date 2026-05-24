from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from research_harness.config import load_settings
from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.memory.failure_memory import (
    FailureMemoryResult,
    record_failure_candidate,
    record_runner_failure_candidate,
)
from research_harness.orchestrator.branch_prior import build_failure_branch_prior
from research_harness.orchestrator.child_nodes import draft_child_nodes
from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.experiment_plan import (
    FALLBACK_TEMPLATE_ID,
    build_demo_experiment_plan,
    build_experiment_plan_for_node,
    build_job_manifest_from_experiment_plan,
    validate_experiment_plan,
)
from research_harness.orchestrator.reduction import reduce_node
from research_harness.orchestrator.search_state import (
    add_child_nodes,
    initialize_search_state,
    search_policy_from_config,
    transition_node,
    validate_search_state,
)
from research_harness.runner.evidence import build_worker_report_from_runner_evidence
from research_harness.runner.local_runner import LocalRunner
from research_harness.schemas.validator import validate_named_schema


ALLOWED_TREE_SEARCH_BACKENDS = {"mock"}
ExperimentPlanBuilder = Callable[[dict[str, Any], Path], dict[str, Any]]
CUSTOM_BUILDER_TEMPLATE_ID = "_custom_builder"


def run_mock_tree_search(
    repo_root: Path,
    run_dir: Path,
    *,
    backend_name: str = "mock",
    max_steps: int | None = None,
    experiment_plan_builder: ExperimentPlanBuilder | None = None,
    record_runner_failures: bool = True,
    root_node: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if backend_name not in ALLOWED_TREE_SEARCH_BACKENDS:
        raise ValueError(
            "tree search only accepts dry-run/local backends; live Claude remains behind live_gate"
        )
    settings = load_settings(repo_root)
    policy = search_policy_from_config(repo_root)
    root = root_node or _demo_node()
    state = initialize_search_state(
        search_id="s_demo_tree",
        root_node=root,
        policy=policy,
    )
    max_steps = max_steps or int(policy["max_depth"]) + 1
    run_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict[str, Any]] = []
    state["status"] = "running"
    steps = 0
    while steps < max_steps:
        item = _next_queued_item(state)
        if item is None:
            break
        steps += 1
        node = _node_by_id(state, item["node_id"])
        node_run_dir = run_dir / "nodes" / node["id"]
        node_run_dir.mkdir(parents=True, exist_ok=True)

        transition_node(
            state,
            node["id"],
            "running",
            event="dequeue",
            reason="frontier selected",
        )
        runner_summary = _execute_node_runner(
            repo_root,
            settings,
            run_dir,
            node_run_dir,
            node,
            experiment_plan_builder=experiment_plan_builder,
            record_failures=record_runner_failures,
        )
        node["outputs"]["template_used"] = runner_summary["template_used"]
        worker_report = runner_summary["worker_report"]
        validate_named_schema("worker_report", worker_report)
        _write_json(node_run_dir / "worker_report.json", worker_report)
        worker_failure_memory = None
        if (
            record_runner_failures
            and worker_report.get("failure_record_candidate")
            and runner_summary["runner_failure_memory"] is None
        ):
            worker_failure_memory = record_failure_candidate(
                repo_root,
                node,
                worker_report,
                source_artifact=_worker_failure_source(worker_report),
            )
        transition_node(
            state,
            node["id"],
            "completed_worker_report",
            event="worker_report",
            reason=f"worker status: {worker_report['status']}",
        )

        critics = select_critics(repo_root, node)
        reviews = run_critic_reviews(node, worker_report, critics)
        for review in reviews:
            validate_named_schema("critic_review", review)
        _write_json(node_run_dir / "critic_reviews.json", reviews)
        transition_node(
            state,
            node["id"],
            "critic_reviewed",
            event="critic_reviews",
            reason=f"{len(reviews)} critics reviewed node",
        )

        branch_prior = build_failure_branch_prior(repo_root, node, settings)
        reduction = reduce_node(node, worker_report, reviews, branch_prior=branch_prior)
        _write_json(node_run_dir / "orchestrator_reduction.json", reduction)
        transition_node(
            state,
            node["id"],
            "orchestrator_reduced",
            event="orchestrator_reduction",
            reason=reduction["final_verdict"],
        )

        if reduction["next_transition"] == "promoted":
            transition_node(
                state,
                node["id"],
                "promoted",
                event="promotion",
                reason="node promoted by reduction",
            )
        elif reduction["next_transition"] == "needs_child_branch":
            children = draft_child_nodes(
                node,
                reduction,
                parent_depth=int(item["depth"]),
                max_depth=int(policy["max_depth"]),
            )
            add_child_nodes(
                state,
                node["id"],
                children,
                reason="reduction requested child branch",
            )
            transition_node(
                state,
                node["id"],
                "needs_child_branch",
                event="branch",
                reason="node requires child branch",
                created_child_ids=[child["id"] for child in children],
            )
        else:
            transition_node(
                state,
                node["id"],
                "pruned",
                event="prune",
                reason=f"unhandled transition {reduction['next_transition']}",
            )
        artifacts.append(
            {
                "node_id": node["id"],
                "experiment_plan_path": runner_summary["experiment_plan_path"],
                "job_manifest_path": runner_summary["job_manifest_path"],
                "source_files": runner_summary["source_files"],
                "runner_result_path": runner_summary["runner_result_path"],
                "runner_status": runner_summary["runner_result"]["status"],
                "metrics_evidence_paths": runner_summary["metrics_evidence_paths"],
                "runner_failure_memory": runner_summary["runner_failure_memory"],
                "worker_failure_memory": _failure_memory_summary(worker_failure_memory),
                "worker_status": worker_report["status"],
                "next_transition": reduction["next_transition"],
                "template_used": runner_summary["template_used"],
            }
        )

    state["status"] = "completed" if _next_queued_item(state) is None else "blocked"
    validate_search_state(state)
    result = {"search_state": state, "artifacts": artifacts}
    _write_json(run_dir / "search_state.json", state)
    _write_json(run_dir / "tree_search_summary.json", result)
    return result


def _next_queued_item(state: dict[str, Any]) -> dict[str, Any] | None:
    queued = [item for item in state["frontier"] if item["status"] == "queued"]
    if not queued:
        return None
    queued.sort(key=lambda item: (-item["priority"], item["depth"], item["node_id"]))
    return queued[0]


def _node_by_id(state: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in state["nodes"]:
        if node["id"] == node_id:
            return node
    raise KeyError(node_id)


def _execute_node_runner(
    repo_root: Path,
    settings: dict[str, Any],
    run_dir: Path,
    node_run_dir: Path,
    node: dict[str, Any],
    *,
    experiment_plan_builder: ExperimentPlanBuilder | None,
    record_failures: bool,
) -> dict[str, Any]:
    if experiment_plan_builder is None:
        experiment_plan, template_used = build_experiment_plan_for_node(
            repo_root, node, run_dir, settings=settings
        )
    else:
        experiment_plan = experiment_plan_builder(node, run_dir)
        template_used = CUSTOM_BUILDER_TEMPLATE_ID
    validate_experiment_plan(node, experiment_plan, run_dir)
    experiment_plan_path = node_run_dir / "experiment_plan.json"
    _write_json(experiment_plan_path, experiment_plan)

    job_manifest = build_job_manifest_from_experiment_plan(
        node,
        experiment_plan,
        run_dir,
    )
    validate_named_schema("job_manifest", job_manifest)
    job_manifest_path = node_run_dir / "job_manifest.json"
    _write_json(job_manifest_path, job_manifest)

    runner = LocalRunner(run_dir, settings=settings)
    runner_result = runner.execute(job_manifest)
    validate_named_schema("runner_result", runner_result)
    evidence_report = build_worker_report_from_runner_evidence(
        node,
        job_manifest,
        runner_result,
        run_dir,
    )
    runner_failure_memory = (
        record_runner_failure_candidate(repo_root, node, runner_result)
        if record_failures
        else None
    )
    runner_result_path = Path(runner_result["workspace"]) / "runner_result.json"
    _attach_runner_outputs(
        node,
        run_dir,
        experiment_plan_path=experiment_plan_path,
        job_manifest_path=job_manifest_path,
        runner_result_path=runner_result_path,
        runner_result=runner_result,
        source_files=evidence_report.source_files,
        metrics_evidence_paths=evidence_report.metrics_evidence_paths,
    )
    return {
        "experiment_plan_path": _display_path(experiment_plan_path, run_dir),
        "job_manifest_path": _display_path(job_manifest_path, run_dir),
        "source_files": evidence_report.source_files,
        "runner_result_path": _display_path(runner_result_path, run_dir),
        "runner_result": runner_result,
        "metrics_evidence_paths": evidence_report.metrics_evidence_paths,
        "worker_report": evidence_report.worker_report,
        "runner_failure_memory": _failure_memory_summary(runner_failure_memory),
        "template_used": template_used,
    }


def _attach_runner_outputs(
    node: dict[str, Any],
    run_dir: Path,
    *,
    experiment_plan_path: Path,
    job_manifest_path: Path,
    runner_result_path: Path,
    runner_result: dict[str, Any],
    source_files: list[str],
    metrics_evidence_paths: list[str],
) -> None:
    artifacts = node["outputs"].setdefault("artifacts", [])
    for path in (experiment_plan_path, job_manifest_path, runner_result_path):
        artifact = _display_path(path, run_dir)
        if artifact not in artifacts:
            artifacts.append(artifact)
    for artifact in source_files:
        if artifact not in artifacts:
            artifacts.append(artifact)
    for artifact in metrics_evidence_paths:
        if artifact not in artifacts:
            artifacts.append(artifact)
    node["outputs"]["runner_status"] = runner_result["status"]
    node["outputs"]["experiment_plan_path"] = _display_path(
        experiment_plan_path,
        run_dir,
    )
    node["outputs"]["source_files"] = source_files
    node["outputs"]["runner_result_path"] = _display_path(runner_result_path, run_dir)
    node["outputs"]["metrics_evidence_paths"] = metrics_evidence_paths


def _failure_memory_summary(result: FailureMemoryResult | None) -> dict[str, str] | None:
    if result is None:
        return None
    return {
        "record_path": str(result.record_path),
        "index_path": str(result.index_path),
        "lesson": result.lesson,
    }


def _display_path(path: Path, run_dir: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(run_dir.resolve()))
    except ValueError:
        return str(resolved)


def _worker_failure_source(worker_report: dict[str, Any]) -> str | None:
    artifacts = worker_report.get("artifacts") or []
    if not artifacts:
        return None
    return str(artifacts[0])


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    run_dir = repo_root / "runs" / "mock_tree_search"
    result = run_mock_tree_search(repo_root, run_dir)
    summary = {
        "status": result["search_state"]["status"],
        "node_count": len(result["search_state"]["nodes"]),
        "promoted_node_ids": result["search_state"]["promoted_node_ids"],
        "search_state": str(run_dir / "search_state.json"),
        "tree_search_summary": str(run_dir / "tree_search_summary.json"),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
