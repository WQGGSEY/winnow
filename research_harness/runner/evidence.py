from __future__ import annotations

import json
import math
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


def _finite_json_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('non-finite number in measurement evidence: ' + value)
    return number


@dataclass(frozen=True)
class EvidenceReport:
    worker_report: dict[str, Any]
    source_files: list[str]
    metrics_evidence_paths: list[str]


def build_worker_report_from_runner_evidence(
    node: dict[str, Any],
    manifest: dict[str, Any],
    runner_result: dict[str, Any],
    run_dir: Path,
) -> EvidenceReport:
    report = _build_worker_report_from_runner_evidence(
        node, manifest, runner_result, run_dir
    )
    input_evidence = runner_result.get("input_evidence")
    if input_evidence is not None:
        report.worker_report["input_evidence"] = dict(input_evidence)
        manifest_artifact = _display_path(
            Path(runner_result["workspace"]) / input_evidence["manifest_path"],
            run_dir,
        )
        if manifest_artifact not in report.worker_report["artifacts"]:
            report.worker_report["artifacts"].append(manifest_artifact)
    return report


def _build_worker_report_from_runner_evidence(
    node: dict[str, Any],
    manifest: dict[str, Any],
    runner_result: dict[str, Any],
    run_dir: Path,
) -> EvidenceReport:
    runner_failure = runner_failure_worker_report(runner_result)
    source_files = _source_files(runner_result, run_dir)
    if runner_failure is not None:
        runner_failure["artifacts"] = [
            *source_files,
            _display_path(Path(runner_result["stdout_path"]), run_dir),
            _display_path(Path(runner_result["stderr_path"]), run_dir),
        ]
        return EvidenceReport(
            worker_report=runner_failure,
            source_files=source_files,
            metrics_evidence_paths=[],
        )

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
            source_files=source_files,
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
                source_files=source_files,
                metrics_evidence_paths=evidence_paths,
            )
        try:
            parsed = json.loads(metric_path.read_text(encoding="utf-8"),
                                parse_float=_finite_json_number, parse_constant=_finite_json_number)
        except ValueError as exc:
            return _invalid_report(
                node,
                manifest,
                runner_result,
                run_dir,
                reason=f"declared metrics file is not valid JSON: {raw_path}: {exc}",
                tags=["invalid_metrics_json"],
                source_files=source_files,
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
                source_files=source_files,
                metrics_evidence_paths=evidence_paths,
            )
        evidence.append((metric_path, parsed))

    return _evidence_to_worker_report(
        node,
        manifest,
        runner_result,
        run_dir,
        evidence,
        source_files,
        evidence_paths,
    )


def _evidence_to_worker_report(
    node: dict[str, Any],
    manifest: dict[str, Any],
    runner_result: dict[str, Any],
    run_dir: Path,
    evidence: list[tuple[Path, dict[str, Any]]],
    source_files: list[str],
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
                source_files=source_files,
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
                source_files=source_files,
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
                source_files=source_files,
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
                source_files=source_files,
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
                    source_files=source_files,
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
                    source_files=source_files,
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
                source_files=source_files,
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
                source_files=source_files,
                metrics_evidence_paths=evidence_paths,
            )
        unexpected_observations.extend(raw_observations)

    baseline_status = _evaluate_baseline_evidence_requirements(
        manifest,
        metrics,
        baselines,
    )
    if baseline_status['overall'] == 'not_required':
        verdict = 'inconclusive'
    if baseline_status["overall"] == "not_evaluable":
        return _baseline_confounded_report(
            node,
            manifest,
            runner_result,
            run_dir,
            metrics=metrics,
            baselines=baselines,
            baseline_status=baseline_status,
            source_files=source_files,
            metrics_evidence_paths=evidence_paths,
        )

    failure_record_candidate = None
    if baseline_status["overall"] == "failed":
        verdict = "contradicted"
        disproof_conditions_hit.extend(
            [
                "mandatory baseline evidence requirement failed: "
                f"{result['role']} via {result['baseline_key']}"
                for result in baseline_status["results"]
                if result["status"] == "failed"
            ]
        )
        unexpected_observations.append(
            {
                "observation": "Claim looked supported by raw metrics but failed mandatory baseline comparison.",
                "evidence": _baseline_failure_reason(baseline_status),
                "suggested_branch_type": "necessity",
                "scope_relation": "directly_refutes_claim",
            }
        )
        failure_record_candidate = {
            "category": "negative_result",
            "tags": [
                "baseline_dominated_success",
                "mandatory_baseline",
                manifest["task_class"],
                *_baseline_failure_tags(baseline_status),
                *manifest.get("failure_index_hints", {}).get("risk_tags", []),
            ],
            "reason": _baseline_failure_reason(baseline_status),
        }

    worker_report = {
        "node_id": node["id"],
        "status": "completed",
        "claim_verdict_candidate": verdict,
        "metrics": metrics,
        "baselines": baselines,
        "baseline_evidence_status": baseline_status,
        "disproof_conditions_hit": disproof_conditions_hit,
        "artifacts": [
            *source_files,
            *evidence_paths,
            _display_path(Path(runner_result["stdout_path"]), run_dir),
            _display_path(Path(runner_result["stderr_path"]), run_dir),
        ],
        "unexpected_observations": unexpected_observations,
        "failure_record_candidate": failure_record_candidate,
    }
    return EvidenceReport(
        worker_report=worker_report,
        source_files=source_files,
        metrics_evidence_paths=evidence_paths,
    )


def _invalid_report(
    node: dict[str, Any],
    manifest: dict[str, Any],
    runner_result: dict[str, Any],
    run_dir: Path,
    *,
    reason: str,
    tags: list[str],
    source_files: list[str] | None = None,
    metrics_evidence_paths: list[str] | None = None,
) -> EvidenceReport:
    source_artifacts = source_files or []
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
            *source_artifacts,
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
    return EvidenceReport(
        worker_report=worker_report,
        source_files=source_artifacts,
        metrics_evidence_paths=evidence_paths,
    )


def _baseline_confounded_report(
    node: dict[str, Any],
    manifest: dict[str, Any],
    runner_result: dict[str, Any],
    run_dir: Path,
    *,
    metrics: dict[str, Any],
    baselines: dict[str, Any],
    baseline_status: dict[str, Any],
    source_files: list[str],
    metrics_evidence_paths: list[str],
) -> EvidenceReport:
    reason = _baseline_failure_reason(baseline_status)
    worker_report = {
        "node_id": node["id"],
        "status": "completed",
        "claim_verdict_candidate": "not_evaluable",
        "metrics": metrics,
        "baselines": baselines,
        "baseline_evidence_status": baseline_status,
        "disproof_conditions_hit": [],
        "artifacts": [
            *source_files,
            *metrics_evidence_paths,
            _display_path(Path(runner_result["stdout_path"]), run_dir),
            _display_path(Path(runner_result["stderr_path"]), run_dir),
        ],
        "unexpected_observations": [
            {
                "observation": "Mandatory baseline evidence could not be evaluated.",
                "evidence": reason,
                "suggested_branch_type": "validity",
                "scope_relation": "operational_blocker",
            }
        ],
        "failure_record_candidate": {
            "category": "confounded_result",
            "tags": [
                "baseline_evidence_missing",
                "mandatory_baseline",
                manifest["task_class"],
                *_baseline_failure_tags(baseline_status),
                *manifest.get("failure_index_hints", {}).get("risk_tags", []),
            ],
            "reason": reason,
        },
    }
    return EvidenceReport(
        worker_report=worker_report,
        source_files=source_files,
        metrics_evidence_paths=metrics_evidence_paths,
    )


def _evaluate_baseline_evidence_requirements(
    manifest: dict[str, Any],
    metrics: dict[str, Any],
    baselines: dict[str, Any],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    overall = "passed" if manifest.get("baseline_evidence_requirements") else "not_required"
    for requirement in manifest.get("baseline_evidence_requirements", []):
        metric_key = requirement["metric_key"]
        baseline_key = requirement["baseline_key"]
        metric_value = metrics.get(metric_key)
        baseline_value = baselines.get(baseline_key)
        result = {
            "role": requirement["role"],
            "metric_key": metric_key,
            "baseline_key": baseline_key,
            "operator": requirement["operator"],
            "margin": requirement["margin"],
            "required": requirement["required"],
            "metric_value": metric_value,
            "baseline_value": baseline_value,
            "status": "passed",
            "reason": "requirement satisfied",
        }
        if metric_key not in metrics:
            result["status"] = "not_evaluable"
            result["reason"] = f"metric key is missing: {metric_key}"
        elif baseline_key not in baselines:
            result["status"] = "not_evaluable"
            result["reason"] = f"baseline key is missing: {baseline_key}"
        elif not _is_number(metric_value) or not _is_number(baseline_value):
            result["status"] = "not_evaluable"
            result["reason"] = (
                "metric and baseline values must be numeric for baseline comparison"
            )
        elif not _passes_requirement(
            float(metric_value),
            float(baseline_value),
            requirement["operator"],
            float(requirement["margin"]),
        ):
            result["status"] = "failed"
            result["reason"] = (
                f"{metric_key}={metric_value} does not satisfy "
                f"{requirement['operator']} {baseline_key}={baseline_value} "
                f"with margin={requirement['margin']}"
            )
        results.append(result)

    required_results = [result for result in results if result["required"]]
    if any(result["status"] == "not_evaluable" for result in required_results):
        overall = "not_evaluable"
    elif any(result["status"] == "failed" for result in required_results):
        overall = "failed"
    return {
        "overall": overall,
        "results": results,
    }


def _passes_requirement(
    metric_value: float,
    baseline_value: float,
    operator: str,
    margin: float,
) -> bool:
    if operator == "greater_than":
        return metric_value > baseline_value + margin
    if operator == "greater_equal":
        return metric_value >= baseline_value + margin
    if operator == "less_than":
        return metric_value < baseline_value - margin
    if operator == "less_equal":
        return metric_value <= baseline_value - margin
    return False


def _baseline_failure_reason(baseline_status: dict[str, Any]) -> str:
    failures = [
        result
        for result in baseline_status.get("results", [])
        if result.get("status") != "passed"
    ]
    if not failures:
        return "All mandatory baseline evidence requirements passed."
    return "; ".join(str(result["reason"]) for result in failures)


def _baseline_failure_tags(baseline_status: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for result in baseline_status.get("results", []):
        if result.get("status") == "passed":
            continue
        tags.extend(
            [
                str(result.get("role")),
                str(result.get("metric_key")),
                str(result.get("baseline_key")),
                str(result.get("status")),
            ]
        )
    return sorted(set(tags))


def _source_files(runner_result: dict[str, Any], run_dir: Path) -> list[str]:
    return [
        _display_path(Path(raw_path), run_dir)
        for raw_path in runner_result.get("source_files", [])
    ]


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


def _is_number(value: Any) -> bool:
    return (isinstance(value, int) or isinstance(value, float)) and not isinstance(
        value,
        bool,
    )


def _display_path(path: Path, run_dir: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(run_dir.resolve()))
    except ValueError:
        return str(resolved)
