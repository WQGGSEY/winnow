"""Deterministic execution evidence required for a strong research result."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from research_harness.orchestrator.experiment_plan import (
    derive_job_manifest_from_experiment_plan,
)
from research_harness.runner.evidence import build_worker_report_from_runner_evidence
from research_harness.runner.local_runner import (
    resolve_runner_command,
    resolve_runner_environment,
)
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.workspace import ensure_path_inside


class StrongExecutionEvidenceError(ValueError):
    """Raised when persisted artifacts do not prove one successful execution."""


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrongExecutionEvidenceError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise StrongExecutionEvidenceError(f"{label} must contain a JSON object")
    return value


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True).encode("utf-8")
    ).hexdigest()


def verify_strong_execution_evidence(
    *,
    node: dict[str, Any],
    experiment_plan: dict[str, Any],
    worker_report: dict[str, Any],
    node_dir: Path,
    tree_dir: Path,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild worker evidence from the declared job and measured JSON files.

    A worker-authored ``supported`` label is not proof. This verifier binds the
    plan to the job manifest, checks the runner's concrete files, reloads the
    declared metrics, and requires the deterministic evidence bridge to
    reproduce the persisted worker report exactly.
    """

    expected_workspace = (node_dir / "workspace").resolve()
    if Path(str(experiment_plan.get("workspace") or "")).resolve() != expected_workspace:
        raise StrongExecutionEvidenceError(
            "experiment workspace is not the promoted node workspace"
        )

    job_manifest = _read_json_object(node_dir / "job_manifest.json", "job manifest")
    runner_result = _read_json_object(
        expected_workspace / "runner_result.json",
        "runner result",
    )
    try:
        validate_named_schema("job_manifest", job_manifest)
        validate_named_schema("runner_result", runner_result)
    except ValueError as exc:
        raise StrongExecutionEvidenceError(str(exc)) from exc

    expected_manifest = derive_job_manifest_from_experiment_plan(
        node,
        experiment_plan,
    )
    if job_manifest != expected_manifest:
        raise StrongExecutionEvidenceError(
            "job manifest does not derive exactly from the experiment plan"
        )

    expected_source_paths: list[str] = []
    for source in experiment_plan.get("source_files") or []:
        relative_path = Path(str(source.get("path") or ""))
        source_path = (expected_workspace / relative_path).resolve()
        try:
            ensure_path_inside(source_path, expected_workspace, "strong-result source")
            source_text = source_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise StrongExecutionEvidenceError(
                f"experiment source is unavailable: {relative_path}"
            ) from exc
        if source_text != source.get("content"):
            raise StrongExecutionEvidenceError(
                f"executed source differs from the experiment plan: {relative_path}"
            )
        expected_source_paths.append(str(source_path))

    try:
        expected_command = resolve_runner_command(job_manifest, settings)
        expected_environment = resolve_runner_environment(settings)
    except ValueError as exc:
        raise StrongExecutionEvidenceError(str(exc)) from exc
    if (
        runner_result.get("status") != "completed"
        or runner_result.get("exit_code") != 0
        or runner_result.get("experiment_plan_id") != experiment_plan.get("plan_id")
        or runner_result.get("node_id") != node.get("id")
        or Path(str(runner_result.get("workspace") or "")).resolve()
        != expected_workspace
        or runner_result.get("source_files") != expected_source_paths
        or runner_result.get("command") != expected_command
        or (runner_result.get("environment_overrides") or {})
        != expected_environment
    ):
        raise StrongExecutionEvidenceError(
            "runner result is not a successful execution of the promoted plan"
        )

    for key in ("stdout_path", "stderr_path"):
        output_path = Path(str(runner_result.get(key) or "")).resolve()
        try:
            ensure_path_inside(output_path, expected_workspace, key)
        except ValueError as exc:
            raise StrongExecutionEvidenceError(
                f"runner {key} escapes the experiment workspace"
            ) from exc
        if not output_path.is_file():
            raise StrongExecutionEvidenceError(f"runner {key} is missing")

    try:
        rebuilt = build_worker_report_from_runner_evidence(
            node,
            job_manifest,
            runner_result,
            tree_dir,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise StrongExecutionEvidenceError(
            f"runner evidence cannot be rebuilt: {exc}"
        ) from exc
    if rebuilt.worker_report != worker_report:
        raise StrongExecutionEvidenceError(
            "worker report does not match deterministic runner evidence"
        )

    metrics_bundle: list[dict[str, Any]] = []
    for artifact in rebuilt.metrics_evidence_paths:
        artifact_path = Path(artifact)
        if not artifact_path.is_absolute():
            artifact_path = tree_dir / artifact_path
        artifact_path = artifact_path.resolve()
        try:
            ensure_path_inside(
                artifact_path,
                expected_workspace,
                "strong-result metrics evidence",
            )
        except ValueError as exc:
            raise StrongExecutionEvidenceError(
                "metrics evidence escapes the experiment workspace"
            ) from exc
        metrics_bundle.append(
            {
                "path": str(artifact_path.relative_to(tree_dir.resolve())),
                "payload": _read_json_object(artifact_path, "metrics evidence"),
            }
        )
    if not metrics_bundle:
        raise StrongExecutionEvidenceError("no declared metrics evidence was executed")

    return {
        "job_manifest": job_manifest,
        "runner_result": runner_result,
        "metrics_evidence": metrics_bundle,
        "job_manifest_sha256": _sha256(job_manifest),
        "runner_result_sha256": _sha256(runner_result),
        "metrics_evidence_sha256": _sha256(metrics_bundle),
    }
