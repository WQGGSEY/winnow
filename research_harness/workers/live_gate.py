from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from research_harness.config import load_settings
from research_harness.local_preflight import run_preflight
from research_harness.orchestrator.demo import _demo_node
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.claude_code_invoker import ClaudeCodeInvoker
from research_harness.workers.workspace import prepare_node_workspace


def build_manual_live_smoke_plan(
    repo_root: Path,
    run_dir: Path | None = None,
    claude_path: str | None = None,
    billing_ack: bool | None = None,
) -> dict[str, Any]:
    """Build, but do not execute, the first live Claude Code smoke plan."""

    settings = load_settings(repo_root)
    run_dir = run_dir or repo_root / "runs" / "manual_live_smoke"
    run_dir.mkdir(parents=True, exist_ok=True)

    preflight_summary = run_preflight(repo_root)
    node = _demo_node()
    workspace_paths = prepare_node_workspace(run_dir, node["id"])
    invoker = ClaudeCodeInvoker(
        workspace_paths["workspace"],
        settings,
        repo_root=repo_root,
    )
    live_envelope = invoker.write_dry_run_artifacts(
        node,
        envelope_path=workspace_paths["workspace"] / "live_invocation_envelope.json",
    )
    live_envelope["backend"] = "claude_code_live"
    live_envelope["invocation_id"] = f"invoke_live_{node['id']}"
    invoker.validate_invocation_envelope(live_envelope)
    (workspace_paths["workspace"] / "live_invocation_envelope.json").write_text(
        json.dumps(live_envelope, indent=2) + "\n",
        encoding="utf-8",
    )

    detected_claude = claude_path or shutil.which("claude")
    auth = invoker.auth_preflight(
        claude_path=detected_claude or "claude",
        probe_cli_status=detected_claude is not None,
    )
    live_backend = (
        settings.get("runtime", {})
        .get("worker_backends", {})
        .get("claude_code_live", {})
    )
    ack_env = live_backend.get("billing_ack_env")
    ack_value = live_backend.get("billing_ack_value")
    requires_ack = bool(live_backend.get("requires_billing_ack", True))
    if billing_ack is None:
        billing_ack = bool(
            (not requires_ack)
            or (ack_env and os.environ.get(str(ack_env)) == str(ack_value))
        )
    api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))

    if preflight_summary["status"] != "passed":
        status = "blocked_by_local_preflight"
        reason = "local preflight did not pass"
    elif requires_ack and not billing_ack:
        status = "blocked_by_billing_guard"
        reason = (
            "live Claude Code may consume subscription quota or API/extra usage; "
            f"set {ack_env}={ack_value} only after accepting that risk"
        )
    elif not auth.ok:
        status = "blocked_by_auth"
        reason = auth.reason or "auth preflight failed"
    elif not detected_claude:
        status = "blocked_by_missing_cli"
        reason = "claude CLI was not found on PATH"
    else:
        status = "ready_to_manually_run"
        reason = "all local gates passed; live execution still requires manual operator action"

    manual_command = [
        detected_claude or "claude",
        *live_envelope["command_plan"]["args"],
    ]
    manual_stdout_path = workspace_paths["workspace"] / "claude_stdout.json"
    ingest_command = [
        sys.executable,
        "-B",
        "-m",
        "research_harness.workers.claude_stdout_ingest",
        "--stdout",
        str(manual_stdout_path),
        "--envelope",
        str(workspace_paths["workspace"] / "live_invocation_envelope.json"),
        "--plan",
        str(run_dir / "manual_live_smoke_plan.json"),
    ]
    worker_task_result_path = Path(live_envelope["expected_output_path"])
    worker_report_path = worker_task_result_path.with_name("worker_report.json")
    ingest_metadata_path = worker_report_path.with_name("worker_report_ingest.json")
    runbook_path = run_dir / "manual_live_smoke_runbook.md"
    plan = {
        "status": status,
        "execution_enabled": False,
        "reason": reason,
        "preflight_summary": preflight_summary,
        "auth_preflight": {
            "ok": auth.ok,
            "mode": auth.mode,
            "reason": auth.reason,
            "details": auth.details,
        },
        "claude_cli": {
            "found": detected_claude is not None,
            "path": detected_claude,
        },
        "billing_guard": {
            "requires_ack": requires_ack,
            "ack_env": ack_env,
            "ack_value": ack_value,
            "ack_ok": bool(billing_ack),
            "api_key_present": api_key_present,
        },
        "runtime_guard": {
            "execution_mode": "manual_only",
            "auto_execution": "forbidden",
            "requires_subscription_oauth": True,
            "requires_anthropic_api_key_unset": True,
            "output_contract": "worker_task_result_then_harness_worker_report",
        },
        "live_invocation_envelope_path": str(
            workspace_paths["workspace"] / "live_invocation_envelope.json"
        ),
        "live_invocation_envelope": live_envelope,
        "manual_command": manual_command,
        "manual_stdout_path": str(manual_stdout_path),
        "ingest_command": ingest_command,
        "expected_output_path": live_envelope["expected_output_path"],
        "worker_report_path": str(worker_report_path),
        "ingest_metadata_path": str(ingest_metadata_path),
        "runbook_path": str(runbook_path),
        "post_run_checks": [
            "Feed prompt_path to manual_command stdin; do not use interactive mode.",
            "Capture raw Claude CLI JSON stdout exactly at manual_stdout_path.",
            "Run ingest_command with --plan; live backend ingest is blocked without the matching manual_live_smoke_plan.",
            "Require Claude to emit worker_task_result only; let the harness derive worker_report.json after schema validation.",
            "Reject permission, timeout, or invalid output as non-promotable worker states.",
            "Run deterministic critic governance before orchestrator reduction.",
        ],
    }
    validate_named_schema("manual_live_smoke_plan", plan)
    (run_dir / "manual_live_smoke_plan.json").write_text(
        json.dumps(plan, indent=2) + "\n",
        encoding="utf-8",
    )
    runbook_path.write_text(_render_runbook(plan), encoding="utf-8")
    return plan


def _render_runbook(plan: dict[str, Any]) -> str:
    manual_command = " ".join(plan["manual_command"])
    ingest_command = " ".join(plan["ingest_command"])
    prompt_path = plan["live_invocation_envelope"]["prompt_path"]
    stdout_path = plan["manual_stdout_path"]
    return (
        "# Manual Live Smoke Runbook\n\n"
        "This runbook is generated by the harness. It does not execute Claude Code.\n\n"
        "## Gate Status\n\n"
        f"- Status: {plan['status']}\n"
        f"- Reason: {plan['reason']}\n"
        f"- Execution enabled by harness: {plan['execution_enabled']}\n"
        f"- Billing ack ok: {plan['billing_guard']['ack_ok']}\n"
        f"- API key present: {plan['billing_guard']['api_key_present']}\n"
        f"- Auth mode: {plan['auth_preflight']['mode']}\n\n"
        "## Manual Command\n\n"
        "Run only after the gate status is `ready_to_manually_run` and you accept "
        "the subscription quota/API-risk warning.\n\n"
        "```bash\n"
        f"{manual_command} < {prompt_path} > {stdout_path}\n"
        "```\n\n"
        "## Ingest Command\n\n"
        "```bash\n"
        f"{ingest_command}\n"
        "```\n\n"
        "## Expected Artifacts\n\n"
        f"- Worker task result: {plan['expected_output_path']}\n"
        f"- Worker report: {plan['worker_report_path']}\n"
        f"- Ingest metadata: {plan['ingest_metadata_path']}\n"
    )


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    plan = build_manual_live_smoke_plan(repo_root)
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
