from __future__ import annotations

from pathlib import Path
from typing import Any

from research_harness.schemas.validator import validate_named_schema


class LiveGateValidationError(ValueError):
    """Raised when live Codex output is not covered by an approved gate plan."""


def validate_live_ingest_gate(
    plan: dict[str, Any],
    envelope: dict[str, Any],
    *,
    stdout_path: Path,
    envelope_path: Path | None,
) -> None:
    validate_named_schema("manual_live_smoke_plan", plan)
    validate_named_schema("invocation_envelope", envelope)

    if envelope.get("backend") != "codex_live":
        raise LiveGateValidationError("live ingest gate only applies to codex_live")
    if plan["status"] != "ready_to_manually_run":
        raise LiveGateValidationError(f"live gate is not ready: {plan['status']}")
    if plan["execution_enabled"]:
        raise LiveGateValidationError("live gate must not enable automatic execution")
    if not plan["auth_preflight"]["ok"]:
        raise LiveGateValidationError("live gate auth preflight did not pass")
    if plan["auth_preflight"]["mode"] != "chatgpt_login":
        raise LiveGateValidationError("live gate auth mode must be chatgpt_login")
    if not plan["billing_guard"]["ack_ok"]:
        raise LiveGateValidationError("live gate billing acknowledgement is missing")
    runtime_guard = plan["runtime_guard"]
    if runtime_guard["execution_mode"] != "manual_only":
        raise LiveGateValidationError("live gate execution_mode must be manual_only")
    if runtime_guard["auto_execution"] != "forbidden":
        raise LiveGateValidationError("live gate auto_execution must be forbidden")
    if not runtime_guard["requires_chatgpt_login"]:
        raise LiveGateValidationError("live gate must require ChatGPT login")
    if runtime_guard["output_contract"] != "worker_task_result_then_harness_worker_report":
        raise LiveGateValidationError("live gate output contract is not worker_task_result")

    planned_stdout = Path(plan["manual_stdout_path"]).resolve()
    if stdout_path.resolve() != planned_stdout:
        raise LiveGateValidationError(
            f"stdout path does not match live gate plan: {stdout_path.resolve()}"
        )

    planned_envelope_path = Path(plan["live_invocation_envelope_path"]).resolve()
    if envelope_path is None:
        raise LiveGateValidationError("live ingest requires the envelope file path")
    if envelope_path.resolve() != planned_envelope_path:
        raise LiveGateValidationError(
            f"envelope path does not match live gate plan: {envelope_path.resolve()}"
        )
    if plan["live_invocation_envelope"] != envelope:
        raise LiveGateValidationError("envelope content does not match live gate plan")

    if envelope["output_schema"]["name"] != "worker_task_result":
        raise LiveGateValidationError("live envelope must request worker_task_result")
    if envelope["expected_output_path"] != plan["expected_output_path"]:
        raise LiveGateValidationError("expected_output_path does not match live gate plan")
    if plan["worker_report_path"] != str(
        Path(envelope["expected_output_path"]).with_name("worker_report.json")
    ):
        raise LiveGateValidationError("worker_report_path does not match envelope output")
