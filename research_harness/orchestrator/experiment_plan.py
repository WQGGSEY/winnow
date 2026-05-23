from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.workspace import ensure_path_inside


class ExperimentPlanError(ValueError):
    """Raised when a plan attempts to drift away from the node contract."""


def build_demo_experiment_plan(node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    workspace = run_dir / "nodes" / node["id"] / "workspace"
    return {
        "plan_id": f"plan_{node['id']}_smoke",
        "node_id": node["id"],
        "claim_under_test": node["claim_contract"]["claim_under_test"],
        "objective": (
            "Generate a deterministic smoke experiment that measures whether "
            "bounded runner evidence can support the node claim without giving "
            "the worker search-policy authority."
        ),
        "task_class": "smoke_test",
        "workspace": str(workspace.resolve()),
        "source_files": [
            {
                "path": "experiment.py",
                "purpose": "Write deterministic metrics for runner evidence ingestion.",
                "content": _demo_experiment_source(),
            }
        ],
        "entrypoint": {
            "command": [sys.executable],
            "args": ["experiment.py"],
        },
        "resources": {
            "timeout_sec": 60,
            "gpu": None,
            "cpu": 1,
            "memory_gb": 1,
        },
        "inputs": {
            "datasets": [],
            "snapshots": [],
        },
        "expected_outputs": {
            "metrics_files": ["artifacts/metrics.json"],
            "logs": ["artifacts/run.log"],
            "artifact_dirs": ["artifacts/"],
        },
        "mandatory_baselines": list(node["claim_contract"]["mandatory_baselines"]),
        "success_criteria": list(node["claim_contract"]["success_criteria"]),
        "disproof_conditions": list(node["claim_contract"]["disproof_conditions"]),
        "guardrails": {
            "allowed_write_roots": ["workspace"],
            "forbidden_actions": [
                "scope_expansion",
                "baseline_changes",
                "shared_memory_write",
                "critic_routing_changes",
                "publication_gate_changes",
            ],
            "scope_policy": "orchestrator_owned_search_policy",
        },
        "failure_index_hints": {
            "domain_tags": [node["domain"]],
            "method_tags": [
                "experiment_plan",
                "bounded_worker",
                "deterministic_runner",
            ],
            "risk_tags": list(node["failure_retrieval"]["query_tags"]),
        },
        "reproducibility": {
            "seed": 0,
            "code_snapshot": "local_pre_live_scaffold",
            "data_snapshot": "none",
        },
    }


def validate_experiment_plan(
    node: dict[str, Any],
    experiment_plan: dict[str, Any],
    run_dir: Path,
) -> None:
    validate_named_schema("experiment_plan", experiment_plan)
    if experiment_plan["node_id"] != node["id"]:
        raise ExperimentPlanError("experiment plan node_id does not match node")

    contract = node["claim_contract"]
    expected_pairs = {
        "claim_under_test": contract["claim_under_test"],
        "mandatory_baselines": contract["mandatory_baselines"],
        "success_criteria": contract["success_criteria"],
        "disproof_conditions": contract["disproof_conditions"],
    }
    for key, expected in expected_pairs.items():
        if experiment_plan[key] != expected:
            raise ExperimentPlanError(
                f"experiment plan cannot override node claim_contract.{key}"
            )

    workspace = Path(experiment_plan["workspace"]).resolve()
    ensure_path_inside(workspace, run_dir.resolve(), "experiment plan workspace")

    seen_source_paths: set[str] = set()
    for source_file in experiment_plan["source_files"]:
        source_path = _relative_workspace_path(source_file["path"], "source_files")
        normalized = source_path.as_posix()
        if normalized in seen_source_paths:
            raise ExperimentPlanError(f"duplicate source file path: {normalized}")
        seen_source_paths.add(normalized)
        ensure_path_inside(workspace / source_path, workspace, "source_files")

    for key in ("metrics_files", "logs", "artifact_dirs"):
        for raw_path in experiment_plan["expected_outputs"].get(key, []):
            output_path = _relative_workspace_path(raw_path, key)
            ensure_path_inside(workspace / output_path, workspace, key)

    if "workspace" not in experiment_plan["guardrails"]["allowed_write_roots"]:
        raise ExperimentPlanError("experiment plan must restrict writes to workspace")


def materialize_experiment_plan(
    node: dict[str, Any],
    experiment_plan: dict[str, Any],
    run_dir: Path,
) -> list[str]:
    validate_experiment_plan(node, experiment_plan, run_dir)
    workspace = Path(experiment_plan["workspace"]).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for source_file in experiment_plan["source_files"]:
        relative_path = _relative_workspace_path(source_file["path"], "source_files")
        target = (workspace / relative_path).resolve()
        ensure_path_inside(target, workspace, "source_files")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source_file["content"], encoding="utf-8")
        written.append(relative_path.as_posix())
    return written


def build_job_manifest_from_experiment_plan(
    node: dict[str, Any],
    experiment_plan: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    source_files = materialize_experiment_plan(node, experiment_plan, run_dir)
    return {
        "job_id": f"job_{node['id']}_{experiment_plan['task_class']}",
        "experiment_plan_id": experiment_plan["plan_id"],
        "node_id": node["id"],
        "task_class": experiment_plan["task_class"],
        "workspace": experiment_plan["workspace"],
        "source_files": source_files,
        "entrypoint": experiment_plan["entrypoint"],
        "resources": experiment_plan["resources"],
        "inputs": experiment_plan["inputs"],
        "outputs": experiment_plan["expected_outputs"],
        "claim_contract": node["claim_contract"],
        "failure_index_hints": experiment_plan["failure_index_hints"],
        "reproducibility": experiment_plan["reproducibility"],
    }


def _relative_workspace_path(raw_path: str, field_name: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise ExperimentPlanError(f"{field_name} must use relative workspace paths")
    return path


def _demo_experiment_source() -> str:
    return "\n".join(
        [
            "import json",
            "from pathlib import Path",
            "",
            "artifacts = Path('artifacts')",
            "artifacts.mkdir(exist_ok=True)",
            "payload = {",
            "    'metrics': {",
            "        'bounded_worker_success_rate': 0.92,",
            "        'schema_validity': 1.0,",
            "    },",
            "    'baselines': {",
            "        'current_best_known': 0.80,",
            "        'naive_direct_port': 0.45,",
            "        'random_or_null': 0.05,",
            "    },",
            "    'claim_verdict_candidate': 'supported',",
            "    'disproof_conditions_hit': [],",
            "    'unexpected_observations': [",
            "        {",
            "            'observation': 'The runtime envelope is the main differentiator from a direct API port.',",
            "            'evidence': 'Naive direct port baseline lacks scope, permission, and output-schema controls.',",
            "            'suggested_branch_type': 'validity',",
            "            'scope_relation': 'directly_explains_success',",
            "        }",
            "    ],",
            "}",
            "(artifacts / 'metrics.json').write_text(json.dumps(payload, indent=2) + '\\n')",
            "print('runner smoke metrics written')",
            "",
        ]
    )
