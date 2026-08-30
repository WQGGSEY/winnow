from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_harness.adapters.codex_cli import CodexCliAdapter, CodexCliError
from research_harness.agent_runtime import AgentUsage
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.live_gate_validation import (
    LiveGateValidationError,
    validate_live_ingest_gate,
)
from research_harness.workers.output_repair import OutputRepairError, parse_or_repair_json
from research_harness.workers.worker_task import worker_report_from_task_result
from research_harness.workers.workspace import ensure_path_inside


@dataclass(frozen=True)
class CodexStdoutIngestResult:
    worker_report: dict[str, Any]
    worker_report_path: Path
    worker_task_result_path: Path | None
    ingest_metadata_path: Path
    repaired: bool
    note: str


def ingest_codex_cli_stdout(
    stdout_path: Path,
    envelope: dict[str, Any],
    *,
    live_plan: dict[str, Any] | None = None,
    envelope_path: Path | None = None,
) -> CodexStdoutIngestResult:
    """Validate a Codex JSONL stream and derive the bounded worker report."""

    validate_named_schema("invocation_envelope", envelope)
    workspace = Path(envelope["workspace"]).resolve()
    stdout_path = stdout_path.resolve()
    expected_output_path = Path(envelope["expected_output_path"]).resolve()
    worker_task_path = Path(envelope["worker_task_path"]).resolve()
    ensure_path_inside(stdout_path, workspace, "stdout_path")
    ensure_path_inside(expected_output_path, workspace, "expected_output_path")
    ensure_path_inside(worker_task_path, workspace, "worker_task_path")

    if envelope["backend"] == "codex_live":
        if live_plan is None:
            return _blocked_preflight_result(
                envelope,
                expected_output_path,
                evidence="live Codex ingest requires manual_live_smoke_plan.json",
            )
        try:
            validate_live_ingest_gate(
                live_plan,
                envelope,
                stdout_path=stdout_path,
                envelope_path=envelope_path,
            )
        except LiveGateValidationError as exc:
            return _blocked_preflight_result(
                envelope,
                expected_output_path,
                evidence=str(exc),
            )

    raw = stdout_path.read_text(encoding="utf-8")
    try:
        completion = CodexCliAdapter().parse_completion(raw, label="bounded worker")
    except CodexCliError as exc:
        timeout = "timed out" in raw.lower() or "timeout" in raw.lower()
        report = _blocked_report(
            envelope,
            status="timeout_or_turn_exhausted" if timeout else "failed",
            category="invalid_experiment",
            tags=["codex_cli", "process_timeout" if timeout else "runtime_error"],
            evidence=str(exc),
            usage=AgentUsage(),
        )
        return _write_result(
            report,
            expected_output_path,
            usage=AgentUsage(),
            task_result=None,
            repaired=False,
            note="process_timeout" if timeout else "runtime_error",
        )

    worker_task = json.loads(worker_task_path.read_text(encoding="utf-8"))
    validate_named_schema("worker_task", worker_task)
    try:
        repair = parse_or_repair_json(completion.text, "worker_task_result")
    except OutputRepairError as exc:
        report = _blocked_report(
            envelope,
            status="invalid_worker_output",
            category="implementation_failure",
            tags=["codex_cli", "invalid_worker_output"],
            evidence=str(exc),
            usage=completion.usage,
        )
        return _write_result(
            report,
            expected_output_path,
            usage=completion.usage,
            task_result=None,
            repaired=False,
            note="invalid_worker_output",
        )

    usage = completion.usage.as_dict()
    report = worker_report_from_task_result(repair.data, worker_task, usage)
    return _write_result(
        report,
        expected_output_path,
        usage=completion.usage,
        task_result=repair.data,
        repaired=repair.repaired,
        note=repair.note,
    )


def _blocked_report(
    envelope: dict[str, Any],
    *,
    status: str,
    category: str,
    tags: list[str],
    evidence: str,
    usage: AgentUsage,
) -> dict[str, Any]:
    return {
        "node_id": envelope["node_id"],
        "status": status,
        "claim_verdict_candidate": "not_evaluable",
        "metrics": {f"codex_{key}": value for key, value in usage.as_dict().items()},
        "baselines": {},
        "disproof_conditions_hit": [],
        "artifacts": [],
        "unexpected_observations": [
            {
                "observation": "Codex did not produce an acceptable worker_task_result.",
                "evidence": evidence,
                "suggested_branch_type": None,
                "scope_relation": "operational_blocker",
            }
        ],
        "failure_record_candidate": {
            "category": category,
            "tags": tags,
            "reason": evidence,
        },
    }


def _blocked_preflight_result(
    envelope: dict[str, Any],
    expected_output_path: Path,
    *,
    evidence: str,
) -> CodexStdoutIngestResult:
    report = _blocked_report(
        envelope,
        status="blocked_preflight",
        category="invalid_experiment",
        tags=["live_gate", "blocked_preflight"],
        evidence=evidence,
        usage=AgentUsage(),
    )
    return _write_result(
        report,
        expected_output_path,
        usage=AgentUsage(),
        task_result=None,
        repaired=False,
        note="blocked_preflight",
        live_gate_reason=evidence,
    )


def _write_result(
    report: dict[str, Any],
    expected_output_path: Path,
    *,
    usage: AgentUsage,
    task_result: dict[str, Any] | None,
    repaired: bool,
    note: str,
    live_gate_reason: str | None = None,
) -> CodexStdoutIngestResult:
    validate_named_schema("worker_report", report)
    if task_result is not None:
        validate_named_schema("worker_task_result", task_result)
        expected_output_path.write_text(
            json.dumps(task_result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    worker_report_path = expected_output_path.with_name("worker_report.json")
    worker_report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metadata_path = worker_report_path.with_name("worker_report_ingest.json")
    metadata: dict[str, Any] = {
        "type": "codex_cli_stdout_ingest",
        "usage": usage.as_dict(),
        "repaired": repaired,
        "note": note,
    }
    if live_gate_reason is not None:
        metadata["live_gate_blocked"] = True
        metadata["live_gate_reason"] = live_gate_reason
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return CodexStdoutIngestResult(
        worker_report=report,
        worker_report_path=worker_report_path,
        worker_task_result_path=expected_output_path if task_result is not None else None,
        ingest_metadata_path=metadata_path,
        repaired=repaired,
        note=note,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest Codex CLI JSONL stdout into a validated worker_report."
    )
    parser.add_argument("--stdout", required=True, type=Path)
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    live_plan = json.loads(args.plan.read_text(encoding="utf-8")) if args.plan else None
    result = ingest_codex_cli_stdout(
        args.stdout,
        envelope,
        live_plan=live_plan,
        envelope_path=args.envelope,
    )
    print(
        json.dumps(
            {
                "status": result.worker_report["status"],
                "claim_verdict_candidate": result.worker_report[
                    "claim_verdict_candidate"
                ],
                "worker_report_path": str(result.worker_report_path),
                "worker_task_result_path": str(result.worker_task_result_path)
                if result.worker_task_result_path
                else None,
                "ingest_metadata_path": str(result.ingest_metadata_path),
                "repaired": result.repaired,
                "note": result.note,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
