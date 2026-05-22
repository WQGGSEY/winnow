from __future__ import annotations

import json
import shutil
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

    auth = invoker.auth_preflight()
    detected_claude = claude_path or shutil.which("claude")

    if preflight_summary["status"] != "passed":
        status = "blocked_by_local_preflight"
        reason = "local preflight did not pass"
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
    plan = {
        "status": status,
        "execution_enabled": False,
        "reason": reason,
        "preflight_summary": preflight_summary,
        "auth_preflight": {
            "ok": auth.ok,
            "mode": auth.mode,
            "reason": auth.reason,
        },
        "claude_cli": {
            "found": detected_claude is not None,
            "path": detected_claude,
        },
        "live_invocation_envelope": live_envelope,
        "manual_command": manual_command,
        "expected_output_path": live_envelope["expected_output_path"],
        "post_run_checks": [
            "Parse output through output_repair.parse_or_repair_json(schema_name='worker_report').",
            "Validate worker_report schema before critic review.",
            "Reject permission, timeout, or invalid output as non-promotable worker states.",
            "Run deterministic critic governance before orchestrator reduction.",
        ],
    }
    validate_named_schema("manual_live_smoke_plan", plan)
    (run_dir / "manual_live_smoke_plan.json").write_text(
        json.dumps(plan, indent=2) + "\n",
        encoding="utf-8",
    )
    return plan


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    plan = build_manual_live_smoke_plan(repo_root)
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()

