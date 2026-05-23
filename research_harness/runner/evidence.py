from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_harness.runner.result_bridge import runner_failure_worker_report
from research_harness.workers.workspace import ensure_path_inside


VALID_VERDICTS = {"supported", "contradicted", "inconclusive", "not_evaluable"}
RESERVED_EVIDENCE_KEYS = {
    "metrics",
    "baselines",
    "claim_verdict_candidate",
    "disproof_conditions_hit",
    "unexpected_observations",
}


@dataclass(frozen=True)
class EvidenceReport:
    worker_report: dict[str, Any]
    metrics_evidence_paths: list[str]


def build_worker_report_from_runner_evidence(
    node: dict[str, Any],
    manifest: dict[str, Any],
    runner_result: dict[str, Any],
    run_dir: Path,
) -> EvidenceReport:
    runner_failure = runner_failure_worker_report(runner_result)
    if runner_failure is not None:
        return EvidenceReport(worker_report=runner_failure, metrics_evidence_paths=[])

    workspace = Path(runner_result["workspace"]).resolve()
    metrics_files = manifest.get("outputs", {}).get("metrics_files", [])
    if not metrics_files:
        return _invalid_report(
            node,
            manifest,
            runner_result,
            run_dir,
            reason="job manifest declares no metrics_files",
            tags=["missing_metrics_contract"],
        )

    evidence: list[tuple[Path, dict[str, Any]]] = []
    evidence_paths: list[str] = []
    for raw_path in metrics_files:
        metric_path = (workspace / Path(raw_path)).resolve()
        ensure_path_inside(metric_path, workspace, "metrics_evidence")
        display_path = _display_path(metric_path, run_dir)
        evidence_paths.append(display_path)
        if not metric_path.exists():
            return _invalid_report(
                node,
                manifest,
                runner_result,
                run_dir,
                reason=f"declared metrics file is missing: {raw_path}",
                tags=["missing_metrics_file"],
                metrics_evidence_paths=evidence_paths,
            )
        try:
            parsed = json.loads(metric_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return _invalid_report(
                node,
                manifest,
                runner_result,
                run_dir,
                reason=f"declared metrics file is not valid JSON: {raw_path}: {exc.msg}",
                tags=["invalid_metrics_json"],
                metrics_evidence_paths=evidence_paths,
            )
        if not isinstance(parsed, dict):
            return _invalid_report(
                node,
                manifest,
                runner_result,
                run_dir,
                reason=f"declared metrics file must contain a JSON object: {raw_path}",
                tags=["invalid_metrics_shape"],
                metrics_evidence_paths=evidence_paths,
            )
        evidence.append((metric_path, parsed))

    return _evidence_to_worker_report(
        node,
        manifest,
        runner_result,
        run_dir,
        evidence,
        evidence_paths,
    )


def _evidence_to_worker_report(
    node: dict[str, Any],
    manifest: dict[str, Any],
    runner_result: dict[str, Any],
    run_dir: Path,
    evidence: list[tuple[Path, dict[str, Any]]],
    evidence_paths: list[str],
) -> EvidenceReport:
    metrics: dict[str, Any] = {}
    baselines: dict[str, Any] = {}
    disproof_conditions_hit: list[str] = []
    unexpected_observations: list[dict[str, Any]] = []
    verdict = "inconclusive"

    for metric_path, parsed in evidence:
        raw_metrics = parsed.get("metrics")
        if raw_metrics is None:
            raw_metrics = {
                key: value
                for key, value in parsed.items()
                if key not in RESERVED_EVIDENCE_KEYS
            }
        if not isinstance(raw_metrics, dict) or not raw_metrics:
            return _invalid_report(
                node,
                manifest,
                runner_result,
                run_dir,
                reason=f"metrics payload must be a non-empty object: {metric_path.name}",
                tags=["invalid_metrics_payload"],
                metrics_evidence_paths=evidence_paths,
            )
        duplicate_metrics = sorted(set(metrics).intersection(raw_metrics))
        if duplicate_metrics:
            return _invalid_report(
                node,
                manifest,
                runner_result,
                run_dir,
                reason="duplicate metric keys across evidence files: "
                + ", ".join(duplicate_metrics),
                tags=["duplicate_metric_keys"],
                metrics_evidence_paths=evidence_paths,
            )
        metrics.update(raw_metrics)

        raw_baselines = parsed.get("baselines", {})
        if not isinstance(raw_baselines, dict):
            return _invalid_report(
                node,
                manifest,
                runner_result,
                run_dir,
                reason=f"baselines payload must be an object: {metric_path.name}",
                tags=["invalid_baselines_payload"],
                metrics_evidence_paths=evidence_paths,
            )
        duplicate_baselines = sorted(set(baselines).intersection(raw_baselines))
        if duplicate_baselines:
            return _invalid_report(
                node,
                manifest,
                runner_result,
                run_dir,
                reason="duplicate baseline keys across evidence files: "
                + ", ".join(duplicate_baselines),
                tags=["duplicate_baseline_keys"],
                metrics_evidence_paths=evidence_paths,
            )
        baselines.update(raw_baselines)

        raw_verdict = parsed.get("claim_verdict_candidate")
        if raw_verdict is not None:
            if raw_verdict not in VALID_VERDICTS:
                return _invalid_report(
                    node,
                    manifest,
                    runner_result,
                    run_dir,
                    reason=f"invalid claim_verdict_candidate: {raw_verdict}",
                    tags=["invalid_verdict_payload"],
                    metrics_evidence_paths=evidence_paths,
                )
            if verdict != "inconclusive" and verdict != raw_verdict:
                return _invalid_report(
                    node,
                    manifest,
                    runner_result,
                    run_dir,
                    reason="conflicting claim_verdict_candidate values across evidence files",
                    tags=["conflicting_verdict_payload"],
                    metrics_evidence_paths=evidence_paths,
                )
            verdict = str(raw_verdict)

        raw_disproof = parsed.get("disproof_conditions_hit", [])
        if not _is_string_list(raw_disproof):
            return _invalid_report(
                node,
                manifest,
                runner_result,
                run_dir,
                reason=f"disproof_conditions_hit must be a list of strings: {metric_path.name}",
                tags=["invalid_disproof_payload"],
                metrics_evidence_paths=evidence_paths,
            )
        disproof_conditions_hit.extend(raw_disproof)

        raw_observations = parsed.get("unexpected_observations", [])
        if not _is_observation_list(raw_observations):
            return _invalid_report(
                node,
                manifest,
                runner_result,
                run_dir,
                reason=f"unexpected_observations payload is invalid: {metric_path.name}",
                tags=["invalid_observation_payload"],
                metrics_evidence_paths=evidence_paths,
            )
        unexpected_observations.extend(raw_observations)

    worker_report = {
        "node_id": node["id"],
        "status": "completed",
        "claim_verdict_candidate": verdict,
        "metrics": metrics,
        "baselines": baselines,
        "disproof_conditions_hit": disproof_conditions_hit,
        "artifacts": [
            *evidence_paths,
            _display_path(Path(runner_result["stdout_path"]), run_dir),
            _display_path(Path(runner_result["stderr_path"]), run_dir),
        ],
        "unexpected_observations": unexpected_observations,
        "failure_record_candidate": None,
    }
    return EvidenceReport(worker_report=worker_report, metrics_evidence_paths=evidence_paths)


def _invalid_report(
    node: dict[str, Any],
    manifest: dict[str, Any],
    runner_result: dict[str, Any],
    run_dir: Path,
    *,
    reason: str,
    tags: list[str],
    metrics_evidence_paths: list[str] | None = None,
) -> EvidenceReport:
    evidence_paths = metrics_evidence_paths or []
    failure_record_candidate = {
        "category": "invalid_experiment",
        "tags": [
            "runner",
            "metrics_evidence",
            manifest["task_class"],
            *tags,
            *manifest.get("failure_index_hints", {}).get("risk_tags", []),
        ],
        "reason": reason,
    }
    worker_report = {
        "node_id": node["id"],
        "status": "failed",
        "claim_verdict_candidate": "not_evaluable",
        "metrics": {
            "runner_status": runner_result["status"],
            "runner_exit_code": runner_result["exit_code"],
            "runner_elapsed_sec": runner_result["elapsed_sec"],
            "runner_timeout_sec": runner_result["timeout_sec"],
        },
        "baselines": {},
        "disproof_conditions_hit": [],
        "artifacts": [
            *evidence_paths,
            _display_path(Path(runner_result["stdout_path"]), run_dir),
            _display_path(Path(runner_result["stderr_path"]), run_dir),
        ],
        "unexpected_observations": [
            {
                "observation": "Runner completed but declared metrics evidence was invalid.",
                "evidence": reason,
                "suggested_branch_type": None,
                "scope_relation": "operational_blocker",
            }
        ],
        "failure_record_candidate": failure_record_candidate,
    }
    return EvidenceReport(worker_report=worker_report, metrics_evidence_paths=evidence_paths)


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_observation_list(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        required = {"observation", "evidence", "scope_relation"}
        allowed = {*required, "suggested_branch_type"}
        if set(item) - allowed:
            return False
        if not required.issubset(item):
            return False
        if not all(isinstance(item[key], str) for key in required):
            return False
        if "suggested_branch_type" in item and not (
            item["suggested_branch_type"] is None
            or isinstance(item["suggested_branch_type"], str)
        ):
            return False
    return True


def _display_path(path: Path, run_dir: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(run_dir.resolve()))
    except ValueError:
        return str(resolved)
