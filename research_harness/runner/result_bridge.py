from __future__ import annotations

from typing import Any


def runner_failure_worker_report(runner_result: dict[str, Any]) -> dict[str, Any] | None:
    candidate = runner_result.get("failure_record_candidate")
    if not candidate:
        return None

    status = (
        "timeout_or_turn_exhausted"
        if runner_result["status"] == "timeout"
        else "failed"
    )
    return {
        "node_id": runner_result["node_id"],
        "status": status,
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
            runner_result["stdout_path"],
            runner_result["stderr_path"],
        ],
        "unexpected_observations": [
            {
                "observation": "Deterministic runner did not complete successfully.",
                "evidence": str(candidate.get("reason") or runner_result["status"]),
                "suggested_branch_type": None,
                "scope_relation": "operational_blocker",
            }
        ],
        "failure_record_candidate": candidate,
    }
