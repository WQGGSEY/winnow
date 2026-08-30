from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.workspace import ensure_path_inside


class WorkerTaskError(ValueError):
    """Raised when a worker task can grant research-policy authority."""


VALID_OUTPUT_KINDS = {"source_patch", "observed_result"}
SCOPE_VIOLATION_KEYS = {
    "claim_changed",
    "baselines_changed",
    "shared_memory_write_attempted",
    "branch_created",
    "files_written_outside_workspace",
}


def build_worker_task(
    node: dict[str, Any],
    workspace: Path,
    *,
    experiment_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    role = node["runtime_profile"]["worker_type"]
    if role == "runner_job":
        raise WorkerTaskError("runner_job is not a Codex worker task role")
    workspace = workspace.resolve()
    output_kinds = _output_kinds_for_role(role)
    task = {
        "task_id": f"task_{node['id']}_{role}",
        "node_id": node["id"],
        "role": role,
        "status": "ready",
        "objective": _objective_for_role(role),
        "scope_locks": {
            "claim_under_test": node["claim_contract"]["claim_under_test"],
            "mandatory_baselines": list(node["claim_contract"]["mandatory_baselines"]),
            "success_criteria": list(node["claim_contract"]["success_criteria"]),
            "disproof_conditions": list(node["claim_contract"]["disproof_conditions"]),
        },
        "input_artifacts": [
            {
                "name": "worker_task",
                "path": "worker_task.json",
                "mode": "read_only",
            },
            {
                "name": "claim_contract",
                "path": "inline:scope_locks",
                "mode": "inline",
            },
        ],
        "allowed_output_kinds": output_kinds,
        "result_contract": {
            "schema_name": "worker_task_result",
            "expected_output_path": str(workspace / "worker_task_result.json"),
        },
        "write_policy": {
            "allowed_write_roots": [str(workspace)],
            "shared_memory_write": "forbidden",
            "repo_write": "forbidden",
        },
        "branch_policy": {
            "may_suggest_branches": True,
            "may_create_branches": False,
        },
        "forbidden_actions": [
            "change_claim_contract",
            "choose_or_replace_baselines",
            "mutate_shared_memory",
            "create_search_branch",
            "change_critic_routing",
            "change_publication_gate",
            "write_outside_workspace",
        ],
        "experiment_plan_summary": _experiment_plan_summary(experiment_plan),
    }
    validate_worker_task(node, task, workspace)
    return task


def write_worker_task(
    node: dict[str, Any],
    workspace: Path,
    *,
    experiment_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = build_worker_task(node, workspace, experiment_plan=experiment_plan)
    path = workspace.resolve() / "worker_task.json"
    ensure_path_inside(path, workspace.resolve(), "worker_task")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return task


def validate_worker_task(
    node: dict[str, Any],
    worker_task: dict[str, Any],
    workspace: Path,
) -> None:
    validate_named_schema("worker_task", worker_task)
    if worker_task["node_id"] != node["id"]:
        raise WorkerTaskError("worker_task node_id does not match node")
    if worker_task["role"] != node["runtime_profile"]["worker_type"]:
        raise WorkerTaskError("worker_task role does not match node runtime_profile")
    if worker_task["branch_policy"]["may_create_branches"]:
        raise WorkerTaskError("worker_task may not grant branch creation authority")

    contract = node["claim_contract"]
    locks = worker_task["scope_locks"]
    for key in (
        "claim_under_test",
        "mandatory_baselines",
        "success_criteria",
        "disproof_conditions",
    ):
        if locks[key] != contract[key]:
            raise WorkerTaskError(f"worker_task scope_locks cannot override {key}")

    output_kinds = set(worker_task["allowed_output_kinds"])
    if not output_kinds.issubset(VALID_OUTPUT_KINDS):
        raise WorkerTaskError("worker_task grants unsupported output kind")

    workspace = workspace.resolve()
    expected_output = Path(worker_task["result_contract"]["expected_output_path"])
    ensure_path_inside(expected_output, workspace, "worker_task expected_output_path")
    for write_root in worker_task["write_policy"]["allowed_write_roots"]:
        ensure_path_inside(Path(write_root), workspace, "worker_task allowed_write_roots")


def worker_report_from_task_result(
    worker_task_result: dict[str, Any],
    worker_task: dict[str, Any],
    cli_result: dict[str, Any],
) -> dict[str, Any]:
    validate_named_schema("worker_task", worker_task)
    validate_named_schema("worker_task_result", worker_task_result)
    if worker_task_result["task_id"] != worker_task["task_id"]:
        return _blocked_scope_report(
            worker_task,
            cli_result,
            f"Worker task_result task_id {worker_task_result['task_id']!r} "
            f"does not match task {worker_task['task_id']!r}.",
            ["task_id_mismatch"],
        )
    if worker_task_result["node_id"] != worker_task["node_id"]:
        return _blocked_scope_report(
            worker_task,
            cli_result,
            f"Worker task_result node_id {worker_task_result['node_id']!r} "
            f"does not match node {worker_task['node_id']!r}.",
            ["node_id_mismatch"],
        )
    if worker_task_result["output_kind"] not in worker_task["allowed_output_kinds"]:
        return _blocked_scope_report(
            worker_task,
            cli_result,
            f"Worker emitted output_kind {worker_task_result['output_kind']!r}, "
            "which is not allowed by worker_task.",
            ["unsupported_output_kind"],
        )
    scope_check = worker_task_result["scope_check"]
    violated = [key for key in sorted(SCOPE_VIOLATION_KEYS) if scope_check.get(key)]
    if violated:
        return _blocked_scope_report(
            worker_task,
            cli_result,
            "Worker reported scope violations: " + ", ".join(violated),
            violated,
        )

    if worker_task_result["output_kind"] == "observed_result":
        return _observed_result_report(worker_task_result, worker_task)
    return _source_patch_report(worker_task_result, worker_task)


def _output_kinds_for_role(role: str) -> list[str]:
    if role == "experiment_worker":
        return ["source_patch", "observed_result"]
    return ["observed_result"]


def _objective_for_role(role: str) -> str:
    if role == "experiment_worker":
        return (
            "Produce only a source_patch for the planned experiment or report only "
            "observed_result evidence from the assigned task."
        )
    if role == "critic_worker":
        return "Report observed critique evidence without changing branch structure."
    return "Report observed result evidence without changing the research plan."


def _experiment_plan_summary(experiment_plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if experiment_plan is None:
        return None
    return {
        "plan_id": experiment_plan["plan_id"],
        "task_class": experiment_plan["task_class"],
        "source_paths": [
            source_file["path"]
            for source_file in experiment_plan.get("source_files", [])
        ],
        "metrics_files": experiment_plan.get("expected_outputs", {}).get(
            "metrics_files",
            [],
        ),
        "baseline_evidence_requirements": experiment_plan.get(
            "baseline_evidence_requirements",
            [],
        ),
    }


def _observed_result_report(
    worker_task_result: dict[str, Any],
    worker_task: dict[str, Any],
) -> dict[str, Any]:
    observed = worker_task_result.get("observed_result") or {}
    observations = list(observed.get("unexpected_observations", []))
    observations.extend(_branch_suggestions_as_observations(worker_task_result))
    return {
        "node_id": worker_task["node_id"],
        "status": "completed"
        if worker_task_result["status"] == "completed"
        else "invalid_worker_output",
        "claim_verdict_candidate": observed.get(
            "claim_verdict_candidate",
            "not_evaluable",
        ),
        "metrics": observed.get("metrics", {}),
        "baselines": observed.get("baselines", {}),
        "disproof_conditions_hit": observed.get("disproof_conditions_hit", []),
        "artifacts": observed.get("artifacts", []),
        "unexpected_observations": observations,
        "failure_record_candidate": None,
    }


def _source_patch_report(
    worker_task_result: dict[str, Any],
    worker_task: dict[str, Any],
) -> dict[str, Any]:
    patch = worker_task_result.get("source_patch") or {}
    file_count = len(patch.get("files", []) or [])
    return {
        "node_id": worker_task["node_id"],
        "status": "completed",
        "claim_verdict_candidate": "not_evaluable",
        "metrics": {
            "source_patch_file_count": file_count,
            "source_patch_only": 1,
        },
        "baselines": {},
        "disproof_conditions_hit": [],
        "artifacts": [],
        "unexpected_observations": [
            {
                "observation": "Worker emitted a source patch, not experiment evidence.",
                "evidence": worker_task_result["summary"],
                "suggested_branch_type": None,
                "scope_relation": "requires_runner_evaluation",
            }
        ],
        "failure_record_candidate": None,
    }


def _blocked_scope_report(
    worker_task: dict[str, Any],
    cli_result: dict[str, Any],
    evidence: str,
    tags: list[str],
) -> dict[str, Any]:
    return {
        "node_id": worker_task["node_id"],
        "status": "invalid_worker_output",
        "claim_verdict_candidate": "not_evaluable",
        "metrics": {
            "codex_input_tokens": cli_result.get("input_tokens", 0),
            "codex_cached_input_tokens": cli_result.get("cached_input_tokens", 0),
            "codex_cache_write_input_tokens": cli_result.get(
                "cache_write_input_tokens", 0
            ),
            "codex_output_tokens": cli_result.get("output_tokens", 0),
            "codex_reasoning_output_tokens": cli_result.get(
                "reasoning_output_tokens", 0
            ),
        },
        "baselines": {},
        "disproof_conditions_hit": [],
        "artifacts": [],
        "unexpected_observations": [
            {
                "observation": "Worker task result violated the worker_task contract.",
                "evidence": evidence,
                "suggested_branch_type": None,
                "scope_relation": "scope_violation",
            }
        ],
        "failure_record_candidate": {
            "category": "scope_violation",
            "tags": ["codex_cli", *tags],
            "reason": evidence,
        },
    }


def _branch_suggestions_as_observations(
    worker_task_result: dict[str, Any],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for suggestion in worker_task_result.get("branch_suggestions", []):
        observations.append(
            {
                "observation": "Worker suggested a branch but did not create it.",
                "evidence": suggestion["description"] + " Evidence: " + suggestion["evidence"],
                "suggested_branch_type": suggestion["suggested_branch_type"],
                "scope_relation": "branch_suggestion_only",
            }
        )
    return observations
