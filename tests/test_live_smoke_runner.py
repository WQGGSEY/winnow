from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.live_smoke_runner import (
    EXECUTION_ACK_ENV,
    EXECUTION_ACK_VALUE,
    run_manual_live_smoke,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _auth_status(subscription_type: str = "max") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["claude", "auth", "status", "--json"],
        returncode=0,
        stdout=json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "email": "redacted@example.com",
                "orgId": "redacted",
                "subscriptionType": subscription_type,
            }
        ),
        stderr="",
    )


def _worker_task_result(run_dir: Path) -> dict[str, object]:
    worker_task = json.loads(
        (
            run_dir
            / "nodes"
            / "n_demo_001"
            / "workspace"
            / "worker_task.json"
        ).read_text(encoding="utf-8")
    )
    return {
        "task_id": worker_task["task_id"],
        "node_id": "n_demo_001",
        "status": "completed",
        "output_kind": "observed_result",
        "summary": "live smoke only; no experiment was executed",
        "source_patch": None,
        "observed_result": {
            "claim_verdict_candidate": "not_evaluable",
            "metrics": {"live_smoke_json_contract": 1},
            "baselines": {},
            "disproof_conditions_hit": [],
            "artifacts": [],
            "unexpected_observations": [],
        },
        "branch_suggestions": [],
        "scope_check": {
            "claim_changed": False,
            "baselines_changed": False,
            "shared_memory_write_attempted": False,
            "branch_created": False,
            "files_written_outside_workspace": False,
        },
    }


class LiveSmokeRunnerTests(unittest.TestCase):
    def test_ready_gate_without_execution_ack_does_not_run_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"
            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "research_harness.workers.claude_code_invoker.subprocess.run",
                    return_value=_auth_status(),
                ):
                    summary = run_manual_live_smoke(
                        REPO_ROOT,
                        run_dir=run_dir,
                        claude_path="/usr/local/bin/claude",
                        billing_ack=True,
                        execution_ack=False,
                        command_runner=self._unexpected_runner,
                    )

            validate_named_schema("live_smoke_run_summary", summary)
            self.assertEqual(summary["status"], "blocked_by_execution_ack")
            self.assertEqual(summary["gate_status"], "ready_to_manually_run")
            self.assertFalse(summary["execution_enabled_by_runner"])
            self.assertTrue((run_dir / "live_smoke_run_summary.json").exists())

    def test_non_ready_gate_blocks_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"
            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "research_harness.workers.claude_code_invoker.subprocess.run",
                    return_value=_auth_status(),
                ):
                    summary = run_manual_live_smoke(
                        REPO_ROOT,
                        run_dir=run_dir,
                        claude_path="/usr/local/bin/claude",
                        billing_ack=False,
                        execution_ack=True,
                        command_runner=self._unexpected_runner,
                    )

            self.assertEqual(summary["status"], "blocked_by_gate")
            self.assertEqual(summary["gate_status"], "blocked_by_billing_guard")
            self.assertFalse(summary["execution_enabled_by_runner"])

    def test_successful_live_smoke_is_ingested_and_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"

            def fake_runner(
                args: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
                cli_result = {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "result": json.dumps(_worker_task_result(run_dir)),
                    "total_cost_usd": 0.009,
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 100,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 40,
                    },
                    "modelUsage": {"claude-sonnet-test": {"costUSD": 0.009}},
                    "permission_denials": [],
                }
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=json.dumps(cli_result),
                    stderr="",
                )

            with patch.dict(
                os.environ,
                {EXECUTION_ACK_ENV: EXECUTION_ACK_VALUE},
                clear=True,
            ):
                with patch(
                    "research_harness.workers.claude_code_invoker.subprocess.run",
                    return_value=_auth_status(),
                ):
                    summary = run_manual_live_smoke(
                        REPO_ROOT,
                        run_dir=run_dir,
                        claude_path="/usr/local/bin/claude",
                        billing_ack=True,
                        command_runner=fake_runner,
                    )

            validate_named_schema("live_smoke_run_summary", summary)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["worker_report_status"], "completed")
            self.assertEqual(summary["ingest_note"], "valid_json")
            self.assertEqual(
                summary["usage_estimate"]["cache_creation_input_tokens"],
                100,
            )
            self.assertTrue(Path(summary["worker_report_path"]).exists())
            self.assertTrue(Path(summary["worker_task_result_path"]).exists())

    def test_timeout_is_converted_to_non_promotable_worker_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"

            def timeout_runner(
                args: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess[str]:
                raise subprocess.TimeoutExpired(
                    cmd=args,
                    timeout=1,
                    output="",
                    stderr="",
                )

            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "research_harness.workers.claude_code_invoker.subprocess.run",
                    return_value=_auth_status(),
                ):
                    summary = run_manual_live_smoke(
                        REPO_ROOT,
                        run_dir=run_dir,
                        claude_path="/usr/local/bin/claude",
                        billing_ack=True,
                        execution_ack=True,
                        timeout_seconds=1,
                        command_runner=timeout_runner,
                    )

            self.assertEqual(summary["status"], "timeout")
            self.assertEqual(summary["worker_report_status"], "timeout_or_turn_exhausted")
            self.assertIn("timed out", summary["error"])
            self.assertTrue(Path(summary["raw_stdout_path"]).exists())

    def _unexpected_runner(
        self,
        args: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("live command runner should not be called")


if __name__ == "__main__":
    unittest.main()
