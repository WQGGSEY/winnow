from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from research_harness.config import load_yaml
from research_harness.schemas.validator import validate_named_schema


class BaselineDossierError(ValueError):
    """Raised when a baseline dossier is incomplete or unsafe to consume."""


REQUIRED_DECISIONS = {
    "selected",
    "selected_as_naive",
    "selected_as_random_or_null",
}

PACKAGED_DOSSIER_DIR = Path(__file__).with_name("baseline_dossiers")


def dossier_path(repo_root: Path, dossier_id: str) -> Path:
    operator_path = (
        repo_root / "memory" / "baseline_dossiers" / f"{dossier_id}.yaml"
    )
    if operator_path.exists():
        return operator_path
    return PACKAGED_DOSSIER_DIR / f"{dossier_id}.yaml"


def load_baseline_dossier(repo_root: Path, dossier_id: str) -> dict[str, Any]:
    path = dossier_path(repo_root, dossier_id)
    if not path.exists():
        raise BaselineDossierError(f"baseline dossier not found: {dossier_id}")
    dossier = load_yaml(path)
    if not isinstance(dossier, dict):
        raise BaselineDossierError("baseline dossier must be a map")
    validate_baseline_dossier(repo_root, dossier, base_dir=path.parent)
    return dossier


def validate_baseline_dossier(
    repo_root: Path,
    dossier: dict[str, Any],
    *,
    base_dir: Path | None = None,
) -> None:
    validate_named_schema("baseline_dossier", dossier)

    try:
        date.fromisoformat(str(dossier["created_at"]))
    except ValueError as exc:
        raise BaselineDossierError("created_at must be ISO date YYYY-MM-DD") from exc

    base_dir = base_dir or dossier_path(repo_root, str(dossier["id"])).parent
    candidate_ids = {candidate["id"] for candidate in dossier["candidates_index"]}
    selected = dossier["selected"]
    decisions = {candidate["decision"] for candidate in dossier["candidates_index"]}
    if selected is None:
        if decisions != {"unqualified"}:
            raise BaselineDossierError(
                "an unselected dossier may contain only unqualified candidates"
            )
    else:
        selected_id = selected["candidate_id"]
        if selected_id not in candidate_ids:
            raise BaselineDossierError(
                f"selected candidate_id not in candidates_index: {selected_id}"
            )
        missing_decisions = sorted(REQUIRED_DECISIONS - decisions)
        if missing_decisions:
            raise BaselineDossierError(
                "baseline dossier missing required candidate decisions: "
                + ", ".join(missing_decisions)
            )

    for candidate in dossier["candidates_index"]:
        detail_file = candidate["detail_file"]
        if Path(detail_file).is_absolute() or ".." in Path(detail_file).parts:
            raise BaselineDossierError(f"candidate detail_file must be relative: {detail_file}")
        if not (base_dir / detail_file).exists():
            raise BaselineDossierError(f"candidate detail_file missing: {detail_file}")

    for source in dossier["source_index"]:
        if not str(source["url"]).startswith(("https://", "http://")):
            raise BaselineDossierError(f"source url must be http(s): {source['id']}")
        try:
            date.fromisoformat(str(source["accessed_at"]))
        except ValueError as exc:
            raise BaselineDossierError(
                f"source accessed_at must be ISO date: {source['id']}"
            ) from exc


def build_baseline_resolution_report(
    repo_root: Path,
    dossier_id: str,
    output_path: Path,
) -> dict[str, Any]:
    dossier = load_baseline_dossier(repo_root, dossier_id)
    if dossier["selected"] is None:
        raise BaselineDossierError(
            "baseline candidates are unqualified; validate a qualification record first"
        )
    candidates_by_decision = {
        candidate["decision"]: candidate["id"] for candidate in dossier["candidates_index"]
    }
    report = {
        "dossier_id": dossier["id"],
        "query": dossier["query"],
        "selected_current_best_known": dossier["selected"]["candidate_id"],
        "naive": candidates_by_decision["selected_as_naive"],
        "random_or_null": candidates_by_decision["selected_as_random_or_null"],
        "evidence_tags": dossier["selected"]["evidence_tags"],
        "risk_tags": dossier["selected"]["risk_tags"],
        "source_count": len(dossier["source_index"]),
        "refresh_required_before": dossier["refresh_policy"]["required_before"],
        "webfetch_executed": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Baseline Resolution Dry Run",
        "",
        f"- Dossier: {report['dossier_id']}",
        f"- Current best-known: {report['selected_current_best_known']}",
        f"- Naive: {report['naive']}",
        f"- Random/null: {report['random_or_null']}",
        f"- Webfetch executed: {report['webfetch_executed']}",
        "",
        "## Evidence Tags",
    ]
    lines.extend(f"- {tag}" for tag in report["evidence_tags"])
    lines.extend(["", "## Risk Tags"])
    lines.extend(f"- {tag}" for tag in report["risk_tags"])
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def validate_baseline_selection(
    repo_root: Path,
    dossier: dict[str, Any],
    qualification: dict[str, Any],
    *,
    artifact_root: Path,
    dossier_base_dir: Path | None = None,
    runner_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate comparable, executed baseline assignments.

    This proves that all required roles use the same declared comparison and
    that the canonical runner verifier can rebuild each result. It does not
    prove that a method is scientifically suitable for the role.

    Baseline preflight does not need an active research direction. Create one
    node directory per baseline under ``production/tree/baseline_preflight``,
    derive its job manifest with ``derive_job_manifest_from_experiment_plan``,
    execute it with ``LocalRunner.execute``, and build ``worker_report.json``
    with ``build_worker_report_from_runner_evidence``. The qualification then
    names those files relative to ``production/tree``. This function runs
    ``verify_strong_execution_evidence`` again before contract compilation.
    """
    validate_baseline_dossier(repo_root, dossier, base_dir=dossier_base_dir)
    validate_named_schema("baseline_qualification", qualification)
    if qualification["dossier_id"] != dossier["id"]:
        raise BaselineDossierError("qualification dossier_id does not match dossier")

    assignments = qualification["assignments"]
    required_roles = {"current_best_known", "naive", "random_or_null"}
    roles = [assignment["role"] for assignment in assignments]
    if len(assignments) != len(required_roles) or set(roles) != required_roles:
        raise BaselineDossierError(
            "qualification must assign each required baseline role exactly once"
        )
    assigned_candidates = [assignment["candidate_id"] for assignment in assignments]
    if len(set(assigned_candidates)) != len(assigned_candidates):
        raise BaselineDossierError(
            "qualification must use a distinct candidate for each baseline role"
        )

    candidate_ids = {candidate["id"] for candidate in dossier["candidates_index"]}
    source_support = {
        source["id"]: set(source["supports"]) for source in dossier["source_index"]
    }
    comparison = assignments[0]["comparison"]
    if any(assignment["comparison"] != comparison for assignment in assignments[1:]):
        raise BaselineDossierError(
            "baseline comparisons must use the same task, data, split, budget, and metric"
        )
    artifact_root = artifact_root.resolve()
    selected: dict[str, str] = {}
    execution_bindings: dict[str, dict[str, str]] = {}
    used_measurements: set[tuple[str, str]] = set()

    def execution_path(raw: str, label: str) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            path = artifact_root / path
        path = path.resolve()
        try:
            path.relative_to(artifact_root)
        except ValueError as exc:
            raise BaselineDossierError(f"{label} is outside artifact_root: {path}") from exc
        return path

    for assignment in assignments:
        candidate_id = assignment["candidate_id"]
        if assignment["role"] == "current_best_known" and not assignment["source_ids"]:
            raise BaselineDossierError("current_best_known requires literature provenance")
        if candidate_id not in candidate_ids:
            raise BaselineDossierError(
                f"qualified candidate is not in dossier: {candidate_id}"
            )
        for source_id in assignment["source_ids"]:
            if source_id not in source_support:
                raise BaselineDossierError(f"unknown qualification source_id: {source_id}")
            if candidate_id not in source_support[source_id]:
                raise BaselineDossierError(
                    f"source {source_id} does not support candidate {candidate_id}"
                )

        receipt = assignment["reproducibility_receipt"]
        if receipt["metric_id"] != comparison["metric_id"]:
            raise BaselineDossierError(
                f"receipt metric does not match comparison for candidate {candidate_id}"
            )
        try:
            node = json.loads(execution_path(receipt["node_path"], "node_path").read_text())
            experiment_plan = json.loads(
                execution_path(receipt["experiment_plan_path"], "experiment_plan_path").read_text()
            )
            worker_report = json.loads(
                execution_path(receipt["worker_report_path"], "worker_report_path").read_text()
            )
            from research_harness.orchestrator.strong_result import (
                verify_strong_execution_evidence,
            )
            execution_evidence = verify_strong_execution_evidence(
                node=node,
                experiment_plan=experiment_plan,
                worker_report=worker_report,
                node_dir=execution_path(receipt["node_dir"], "node_dir"),
                tree_dir=execution_path(receipt["tree_dir"], "tree_dir"),
                settings=runner_settings,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise BaselineDossierError(
                f"runner evidence is invalid for candidate {candidate_id}: {exc}"
            ) from exc
        implementation = assignment["implementation"]
        matching_sources = [
            source
            for source in experiment_plan.get("source_files", [])
            if source.get("path") == implementation["identifier"]
        ]
        if len(matching_sources) != 1:
            raise BaselineDossierError(
                f"implementation source is not unique in plan for candidate {candidate_id}"
            )
        source_content = matching_sources[0].get("content")
        if not isinstance(source_content, str):
            raise BaselineDossierError(
                f"implementation source content is missing for candidate {candidate_id}"
            )
        source_sha256 = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
        if implementation["version"] != source_sha256:
            raise BaselineDossierError(
                f"implementation version does not match executed source for candidate {candidate_id}"
            )
        baselines = worker_report.get("baselines") if isinstance(worker_report, dict) else None
        baseline_key = receipt["baseline_key"]
        if not isinstance(baselines, dict) or baselines.get(baseline_key) != receipt["metric_value"]:
            raise BaselineDossierError(
                f"execution artifact does not contain the receipted metric for candidate {candidate_id}"
            )
        measurement_identity = (execution_evidence["runner_result_sha256"], baseline_key)
        if measurement_identity in used_measurements:
            raise BaselineDossierError(
                "the same executed baseline measurement cannot qualify multiple roles"
            )
        used_measurements.add(measurement_identity)
        selected[assignment["role"]] = candidate_id
        execution_bindings[assignment["role"]] = {
            "job_manifest_sha256": execution_evidence["job_manifest_sha256"],
            "runner_result_sha256": execution_evidence["runner_result_sha256"],
            "source_sha256": source_sha256,
            "baseline_key": baseline_key,
        }

    return {
        "dossier_id": dossier["id"],
        "comparison": comparison,
        "assignments": selected,
        "execution_bindings": execution_bindings,
        "qualification_limit": (
            "structural comparability and artifact integrity verified; "
            "scientific role suitability requires review"
        ),
    }
