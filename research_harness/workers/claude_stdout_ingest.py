from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.output_repair import OutputRepairError, parse_or_repair_json
from research_harness.workers.workspace import WorkspaceGuardError, ensure_path_inside


class ClaudeStdoutIngestError(ValueError):
    """Raised when Claude CLI stdout cannot be interpreted safely."""


@dataclass(frozen=True)
class ClaudeStdoutIngestResult:
    worker_report: dict[str, Any]
    worker_report_path: Path
    ingest_metadata_path: Path
    repaired: bool
    note: str


def ingest_claude_cli_stdout(
    stdout_path: Path,
    envelope: dict[str, Any],
) -> ClaudeStdoutIngestResult:
    """Convert Claude CLI JSON stdout into a schema-valid worker report.

    Claude Code is an agent runtime, so the harness treats the CLI's outer JSON
    as runtime metadata. Only the nested `result` string may contain worker
    output, and only after the runtime metadata has no blocking state.
    """

    validate_named_schema("invocation_envelope", envelope)
    workspace = Path(envelope["workspace"]).resolve()
    stdout_path = stdout_path.resolve()
    expected_output_path = Path(envelope["expected_output_path"]).resolve()
    ensure_path_inside(stdout_path, workspace, "stdout_path")
    ensure_path_inside(expected_output_path, workspace, "expected_output_path")

    cli_result = _load_cli_result(stdout_path.read_text(encoding="utf-8"))
    report, repaired, note = _report_from_cli_result(cli_result, envelope)
    validate_named_schema("worker_report", report)

    expected_output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metadata_path = expected_output_path.with_name("worker_report_ingest.json")
    metadata = _ingest_metadata(cli_result, repaired, note)
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ClaudeStdoutIngestResult(
        worker_report=report,
        worker_report_path=expected_output_path,
        ingest_metadata_path=metadata_path,
        repaired=repaired,
        note=note,
    )


def _load_cli_result(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeStdoutIngestError("Claude CLI stdout was not JSON") from exc
    if not isinstance(data, dict):
        raise ClaudeStdoutIngestError("Claude CLI stdout must be a JSON object")
    if data.get("type") != "result":
        raise ClaudeStdoutIngestError("Claude CLI stdout is not a result object")
    return data


def _report_from_cli_result(
    cli_result: dict[str, Any],
    envelope: dict[str, Any],
) -> tuple[dict[str, Any], bool, str]:
    permission_denials = cli_result.get("permission_denials") or []
    if permission_denials:
        return (
            _blocked_report(
                envelope,
                status="blocked_permission",
                category="permission_denied",
                tags=["claude_cli", "permission_denied"],
                evidence=(
                    "Claude CLI reported permission denials before a trusted "
                    "worker_report could be accepted."
                ),
                cli_result=cli_result,
            ),
            False,
            "blocked_permission",
        )

    if cli_result.get("is_error"):
        status = "failed"
        tags = ["claude_cli", "runtime_error"]
        note = "runtime_error"
        if cli_result.get("subtype") == "error_max_budget_usd":
            status = "timeout_or_turn_exhausted"
            tags = ["claude_cli", "budget_exhausted"]
            note = "budget_exhausted"
        return (
            _blocked_report(
                envelope,
                status=status,
                category="runtime_blocker",
                tags=tags,
                evidence=_runtime_evidence(cli_result),
                cli_result=cli_result,
            ),
            False,
            note,
        )

    try:
        repair = parse_or_repair_json(str(cli_result.get("result") or ""), "worker_report")
    except OutputRepairError as exc:
        return (
            _blocked_report(
                envelope,
                status="invalid_worker_output",
                category="invalid_worker_output",
                tags=["claude_cli", "invalid_worker_output"],
                evidence=str(exc),
                cli_result=cli_result,
            ),
            False,
            "invalid_worker_output",
        )

    expected_node_id = envelope["node_id"]
    if repair.data.get("node_id") != expected_node_id:
        return (
            _blocked_report(
                envelope,
                status="invalid_worker_output",
                category="node_id_mismatch",
                tags=["claude_cli", "node_id_mismatch"],
                evidence=(
                    f"Worker report node_id {repair.data.get('node_id')!r} "
                    f"does not match envelope node_id {expected_node_id!r}."
                ),
                cli_result=cli_result,
            ),
            False,
            "node_id_mismatch",
        )
    return repair.data, repair.repaired, repair.note


def _blocked_report(
    envelope: dict[str, Any],
    *,
    status: str,
    category: str,
    tags: list[str],
    evidence: str,
    cli_result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "node_id": envelope["node_id"],
        "status": status,
        "claim_verdict_candidate": "not_evaluable",
        "metrics": {
            "claude_cli_subtype": cli_result.get("subtype"),
            "claude_cli_is_error": bool(cli_result.get("is_error")),
            "claude_cli_num_turns": cli_result.get("num_turns"),
            "claude_cli_reported_total_cost_usd": cli_result.get("total_cost_usd"),
        },
        "baselines": {},
        "disproof_conditions_hit": [],
        "artifacts": [],
        "unexpected_observations": [
            {
                "observation": "Claude CLI did not produce an acceptable worker_report.",
                "evidence": evidence,
                "suggested_branch_type": None,
                "scope_relation": "operational_blocker",
            }
        ],
        "failure_record_candidate": {
            "category": category,
            "tags": tags,
            "cli_subtype": cli_result.get("subtype"),
            "reason": evidence,
        },
    }


def _runtime_evidence(cli_result: dict[str, Any]) -> str:
    errors = cli_result.get("errors") or []
    if errors:
        return "; ".join(str(item) for item in errors)
    subtype = cli_result.get("subtype") or "unknown"
    return f"Claude CLI runtime did not complete successfully: {subtype}."


def _ingest_metadata(
    cli_result: dict[str, Any],
    repaired: bool,
    note: str,
) -> dict[str, Any]:
    return {
        "type": "claude_cli_stdout_ingest",
        "subtype": cli_result.get("subtype"),
        "is_error": bool(cli_result.get("is_error")),
        "num_turns": cli_result.get("num_turns"),
        "reported_total_cost_usd": cli_result.get("total_cost_usd"),
        "permission_denial_count": len(cli_result.get("permission_denials") or []),
        "repaired": repaired,
        "note": note,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest Claude CLI JSON stdout into a validated worker_report."
    )
    parser.add_argument("--stdout", required=True, type=Path)
    parser.add_argument("--envelope", required=True, type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    result = ingest_claude_cli_stdout(args.stdout, envelope)
    print(
        json.dumps(
            {
                "status": result.worker_report["status"],
                "claim_verdict_candidate": result.worker_report[
                    "claim_verdict_candidate"
                ],
                "worker_report_path": str(result.worker_report_path),
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
