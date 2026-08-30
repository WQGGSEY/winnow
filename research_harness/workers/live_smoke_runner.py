from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from research_harness.adapters.codex_cli import CodexCliAdapter, CodexCliError
from research_harness.orchestrator.demo import _demo_node
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.codex_stdout_ingest import ingest_codex_cli_stdout
from research_harness.workers.live_gate import build_manual_live_node_plan


EXECUTION_ACK_ENV = "RESEARCH_HARNESS_EXECUTE_CODEX_LIVE"
EXECUTION_ACK_VALUE = "live_smoke_ack"
DEFAULT_TIMEOUT_SECONDS = 300

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def run_manual_live_smoke(
    repo_root: Path,
    *,
    run_dir: Path | None = None,
    codex_path: str | None = None,
    billing_ack: bool | None = None,
    execution_ack: bool | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Execute the single live smoke path behind explicit operator gates.

    This is intentionally not a general live worker backend. It only runs the
    generated manual smoke command, captures raw stdout, and hands that stdout
    back to the normal live gate + ingest path.
    """

    return run_live_node_once(
        repo_root,
        _demo_node(),
        run_dir=run_dir,
        codex_path=codex_path,
        billing_ack=billing_ack,
        execution_ack=execution_ack,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        plan_filename="manual_live_smoke_plan.json",
        runbook_filename="manual_live_smoke_runbook.md",
        summary_filename="live_smoke_run_summary.json",
    )


def run_live_node_once(
    repo_root: Path,
    node: dict[str, Any],
    *,
    run_dir: Path | None = None,
    codex_path: str | None = None,
    billing_ack: bool | None = None,
    execution_ack: bool | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    command_runner: CommandRunner | None = None,
    plan_filename: str = "manual_live_worker_plan.json",
    runbook_filename: str = "manual_live_worker_runbook.md",
    summary_filename: str = "live_worker_run_summary.json",
) -> dict[str, Any]:
    """Execute exactly one node through the live gate and stdout ingest path."""

    repo_root = repo_root.resolve()
    validate_named_schema("node", node)
    plan = build_manual_live_node_plan(
        repo_root,
        node,
        run_dir=run_dir,
        codex_path=codex_path,
        billing_ack=billing_ack,
        plan_filename=plan_filename,
        runbook_filename=runbook_filename,
    )
    plan_path = Path(plan["runbook_path"]).with_name(plan_filename)
    summary_path = Path(plan["runbook_path"]).with_name(summary_filename)
    stdout_path = Path(plan["manual_stdout_path"])
    stderr_path = stdout_path.with_name("codex_stderr.txt")
    raw_stdout_path = stdout_path.with_name("codex_stdout.raw.jsonl")
    prompt_path = Path(plan["live_invocation_envelope"]["prompt_path"])
    prompt_text = prompt_path.read_text(encoding="utf-8")

    base_summary = {
        "type": "live_smoke_run_summary",
        "node_id": node["id"],
        "invocation_id": plan["live_invocation_envelope"]["invocation_id"],
        "gate_status": plan["status"],
        "plan_path": str(plan_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "raw_stdout_path": None,
        "worker_report_path": None,
        "worker_task_result_path": None,
        "ingest_metadata_path": None,
        "command_returncode": None,
        "timeout_seconds": int(timeout_seconds),
        "worker_report_status": None,
        "ingest_note": None,
        "prompt_bytes": len(prompt_text.encode("utf-8")),
        "prompt_words": len(prompt_text.split()),
        "usage_estimate": {},
        "error": None,
    }

    if plan["status"] != "ready_to_manually_run":
        return _write_summary(
            summary_path,
            {
                **base_summary,
                "status": "blocked_by_gate",
                "execution_enabled_by_runner": False,
                "error": plan["reason"],
            },
        )

    if not _execution_ack_ok(execution_ack):
        return _write_summary(
            summary_path,
            {
                **base_summary,
                "status": "blocked_by_execution_ack",
                "execution_enabled_by_runner": False,
                "error": (
                    f"set {EXECUTION_ACK_ENV}={EXECUTION_ACK_VALUE} or pass "
                    "--execute-ack to run the live node command"
                ),
            },
        )

    runner = command_runner or subprocess.run
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()

    timed_out = False
    returncode: int | None = None
    try:
        completed = runner(
            plan["manual_command"],
            input=prompt_text,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
        returncode = completed.returncode
        stderr_path.write_text(_to_text(completed.stderr), encoding="utf-8")
        raw_stdout = _to_text(completed.stdout)
        raw_path = _write_ingestable_stdout(
            stdout_path,
            raw_stdout_path,
            raw_stdout,
            returncode=returncode,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stderr_path.write_text(_to_text(exc.stderr), encoding="utf-8")
        raw_stdout = _to_text(exc.stdout)
        raw_path = _write_ingestable_stdout(
            stdout_path,
            raw_stdout_path,
            raw_stdout,
            returncode=None,
            timeout_seconds=timeout_seconds,
            forced_subtype="error_process_timeout",
            forced_error=f"Codex CLI process timed out after {timeout_seconds} seconds.",
        )

    envelope_path = Path(plan["live_invocation_envelope_path"])
    ingest = ingest_codex_cli_stdout(
        stdout_path,
        plan["live_invocation_envelope"],
        live_plan=plan,
        envelope_path=envelope_path,
    )
    worker_status = ingest.worker_report["status"]
    summary_status = _summary_status(
        worker_status,
        command_returncode=returncode,
        timed_out=timed_out,
    )
    summary = {
        **base_summary,
        "status": summary_status,
        "execution_enabled_by_runner": True,
        "raw_stdout_path": str(raw_path) if raw_path else None,
        "worker_report_path": str(ingest.worker_report_path),
        "worker_task_result_path": str(ingest.worker_task_result_path)
        if ingest.worker_task_result_path
        else None,
        "ingest_metadata_path": str(ingest.ingest_metadata_path),
        "command_returncode": returncode,
        "worker_report_status": worker_status,
        "ingest_note": ingest.note,
        "usage_estimate": _usage_estimate(stdout_path),
        "error": None if worker_status == "completed" else _worker_error(ingest.worker_report),
    }
    return _write_summary(summary_path, summary)


def _execution_ack_ok(execution_ack: bool | None) -> bool:
    if execution_ack is not None:
        return execution_ack
    return os.environ.get(EXECUTION_ACK_ENV) == EXECUTION_ACK_VALUE


def _write_ingestable_stdout(
    stdout_path: Path,
    raw_stdout_path: Path,
    raw_stdout: str,
    *,
    returncode: int | None,
    timeout_seconds: int,
    forced_subtype: str | None = None,
    forced_error: str | None = None,
) -> Path | None:
    if forced_subtype is None and _is_usable_cli_result(raw_stdout, returncode):
        stdout_path.write_text(raw_stdout, encoding="utf-8")
        return None

    raw_stdout_path.write_text(raw_stdout, encoding="utf-8")
    subtype = forced_subtype or "error_process_failed"
    error = forced_error or (
        "Codex CLI did not return a usable JSONL result"
        if returncode == 0
        else f"Codex CLI exited with return code {returncode}"
    )
    stdout_path.write_text(
        json.dumps({"type": "turn.failed", "error": error, "subtype": subtype})
        + "\n",
        encoding="utf-8",
    )
    return raw_stdout_path


def _is_usable_cli_result(raw: str, returncode: int | None) -> bool:
    try:
        CodexCliAdapter().parse_completion(raw, label="live worker")
    except CodexCliError:
        return False
    return returncode in {0, None}


def _usage_estimate(stdout_path: Path) -> dict[str, Any]:
    try:
        result = CodexCliAdapter().parse_completion(
            stdout_path.read_text(encoding="utf-8"),
            label="live worker usage",
        )
    except CodexCliError:
        return {}
    return result.usage.as_dict()


def _summary_status(
    worker_status: str,
    *,
    command_returncode: int | None,
    timed_out: bool,
) -> str:
    if timed_out:
        return "timeout"
    if worker_status == "completed":
        return "completed"
    if command_returncode not in {0, None}:
        return "runtime_failed"
    return "worker_blocked"


def _worker_error(worker_report: dict[str, Any]) -> str | None:
    candidate = worker_report.get("failure_record_candidate")
    if isinstance(candidate, dict) and candidate.get("reason"):
        return str(candidate["reason"])
    observations = worker_report.get("unexpected_observations") or []
    if observations and isinstance(observations[0], dict):
        return str(observations[0].get("evidence") or observations[0].get("observation"))
    return None


def _write_summary(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    validate_named_schema("live_smoke_run_summary", summary)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def _to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the single gated Codex live smoke and ingest stdout."
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--codex-path")
    parser.add_argument("--billing-ack", action="store_true")
    parser.add_argument("--execute-ack", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    summary = run_manual_live_smoke(
        repo_root,
        run_dir=args.run_dir,
        codex_path=args.codex_path,
        billing_ack=True if args.billing_ack else None,
        execution_ack=True if args.execute_ack else None,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
