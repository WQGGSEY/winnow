from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.claude_stdout_ingest import ingest_claude_cli_stdout
from research_harness.workers.live_gate import build_manual_live_smoke_plan


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


class ClaudeStdoutIngestTests(unittest.TestCase):
    def _ready_plan(self, tmp: str) -> tuple[dict[str, object], dict[str, object], Path]:
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "research_harness.workers.claude_code_invoker.subprocess.run",
                return_value=_auth_status(),
            ):
                plan = build_manual_live_smoke_plan(
                    REPO_ROOT,
                    Path(tmp) / "live",
                    claude_path="/usr/local/bin/claude",
                    billing_ack=True,
                )
        envelope = plan["live_invocation_envelope"]
        envelope_path = Path(plan["live_invocation_envelope_path"])
        return plan, envelope, envelope_path

    def _worker_task_result(self, envelope: dict[str, object]) -> dict[str, object]:
        worker_task = json.loads(
            Path(str(envelope["worker_task_path"])).read_text(encoding="utf-8")
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

    def _write_stdout(
        self,
        plan: dict[str, object],
        payload: dict[str, object],
    ) -> Path:
        stdout_path = Path(str(plan["manual_stdout_path"]))
        stdout_path.write_text(json.dumps(payload), encoding="utf-8")
        return stdout_path

    def test_success_result_is_repaired_and_written_to_expected_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            stdout_path = self._write_stdout(
                plan,
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "result": "```json\n"
                    + json.dumps(self._worker_task_result(envelope))
                    + "\n```",
                    "total_cost_usd": 0.01,
                    "permission_denials": [],
                },
            )

            result = ingest_claude_cli_stdout(
                stdout_path,
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            validate_named_schema("worker_report", result.worker_report)
            self.assertTrue(result.repaired)
            self.assertEqual(result.worker_report["status"], "completed")
            self.assertEqual(
                result.worker_report["metrics"]["live_smoke_json_contract"],
                1,
            )
            self.assertTrue(Path(envelope["expected_output_path"]).exists())
            validate_named_schema(
                "worker_task_result",
                json.loads(
                    Path(envelope["expected_output_path"]).read_text(
                        encoding="utf-8"
                    )
                ),
            )
            self.assertTrue(result.worker_report_path.exists())
            self.assertTrue(result.ingest_metadata_path.exists())

    def test_live_backend_without_plan_is_blocked_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, _ = self._ready_plan(tmp)
            stdout_path = Path(str(plan["manual_stdout_path"]))

            result = ingest_claude_cli_stdout(stdout_path, envelope)

            self.assertEqual(result.worker_report["status"], "blocked_preflight")
            self.assertIn(
                "manual_live_smoke_plan",
                result.worker_report["failure_record_candidate"]["reason"],
            )

    def test_stdout_path_mismatch_is_blocked_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            wrong_stdout = Path(envelope["workspace"]) / "wrong_stdout.json"

            result = ingest_claude_cli_stdout(
                wrong_stdout,
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            self.assertEqual(result.worker_report["status"], "blocked_preflight")
            self.assertIn(
                "stdout path",
                result.worker_report["failure_record_candidate"]["reason"],
            )

    def test_missing_envelope_path_is_blocked_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, _ = self._ready_plan(tmp)

            result = ingest_claude_cli_stdout(
                Path(str(plan["manual_stdout_path"])),
                envelope,
                live_plan=plan,
            )

            self.assertEqual(result.worker_report["status"], "blocked_preflight")
            self.assertIn(
                "envelope file path",
                result.worker_report["failure_record_candidate"]["reason"],
            )

    def test_non_subscription_runtime_guard_is_blocked_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            plan["runtime_guard"]["requires_subscription_oauth"] = False

            result = ingest_claude_cli_stdout(
                Path(str(plan["manual_stdout_path"])),
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            self.assertEqual(result.worker_report["status"], "blocked_preflight")
            self.assertIn(
                "subscription OAuth",
                result.worker_report["failure_record_candidate"]["reason"],
            )

    def test_blocked_billing_plan_is_blocked_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "research_harness.workers.claude_code_invoker.subprocess.run",
                    return_value=_auth_status(),
                ):
                    plan = build_manual_live_smoke_plan(
                        REPO_ROOT,
                        Path(tmp) / "live",
                        claude_path="/usr/local/bin/claude",
                        billing_ack=False,
                    )
            envelope = plan["live_invocation_envelope"]

            result = ingest_claude_cli_stdout(
                Path(str(plan["manual_stdout_path"])),
                envelope,
                live_plan=plan,
                envelope_path=Path(str(plan["live_invocation_envelope_path"])),
            )

            self.assertEqual(result.worker_report["status"], "blocked_preflight")
            self.assertIn(
                "blocked_by_billing_guard",
                result.worker_report["failure_record_candidate"]["reason"],
            )

    def test_api_key_present_plan_is_blocked_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret"}, clear=False):
                plan = build_manual_live_smoke_plan(
                    REPO_ROOT,
                    Path(tmp) / "live",
                    claude_path="/usr/local/bin/claude",
                    billing_ack=True,
                )
            envelope = plan["live_invocation_envelope"]

            result = ingest_claude_cli_stdout(
                Path(str(plan["manual_stdout_path"])),
                envelope,
                live_plan=plan,
                envelope_path=Path(str(plan["live_invocation_envelope_path"])),
            )

            self.assertEqual(result.worker_report["status"], "blocked_preflight")
            self.assertIn(
                "blocked_by_auth",
                result.worker_report["failure_record_candidate"]["reason"],
            )

    def test_permission_denials_force_blocked_permission_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            stdout_path = self._write_stdout(
                plan,
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 2,
                    "result": json.dumps(self._worker_task_result(envelope)),
                    "total_cost_usd": 0.02,
                    "permission_denials": [{"tool_name": "Read"}],
                },
            )

            result = ingest_claude_cli_stdout(
                stdout_path,
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            self.assertEqual(result.worker_report["status"], "blocked_permission")
            self.assertEqual(
                result.worker_report["failure_record_candidate"]["category"],
                "invalid_experiment",
            )

    def test_budget_error_becomes_non_promotable_worker_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            stdout_path = self._write_stdout(
                plan,
                {
                    "type": "result",
                    "subtype": "error_max_budget_usd",
                    "is_error": True,
                    "num_turns": 1,
                    "result": "",
                    "total_cost_usd": 0.11899725,
                    "permission_denials": [],
                    "errors": ["Reached maximum budget ($0.05)"],
                },
            )

            result = ingest_claude_cli_stdout(
                stdout_path,
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            self.assertEqual(result.worker_report["status"], "timeout_or_turn_exhausted")
            self.assertIn(
                "budget_exhausted",
                result.worker_report["failure_record_candidate"]["tags"],
            )

    def test_process_timeout_becomes_non_promotable_worker_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            stdout_path = self._write_stdout(
                plan,
                {
                    "type": "result",
                    "subtype": "error_process_timeout",
                    "is_error": True,
                    "num_turns": 0,
                    "result": "",
                    "total_cost_usd": None,
                    "permission_denials": [],
                    "errors": ["Claude CLI process timed out after 1 seconds."],
                },
            )

            result = ingest_claude_cli_stdout(
                stdout_path,
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            self.assertEqual(result.worker_report["status"], "timeout_or_turn_exhausted")
            self.assertIn(
                "process_timeout",
                result.worker_report["failure_record_candidate"]["tags"],
            )

    def test_invalid_inner_result_becomes_invalid_worker_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            stdout_path = self._write_stdout(
                plan,
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "result": "{\"node_id\": \"n_demo_001\"}",
                    "total_cost_usd": 0.01,
                    "permission_denials": [],
                },
            )

            result = ingest_claude_cli_stdout(
                stdout_path,
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            self.assertEqual(result.worker_report["status"], "invalid_worker_output")

    def test_scope_violation_becomes_invalid_worker_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            task_result = self._worker_task_result(envelope)
            task_result["scope_check"]["branch_created"] = True
            stdout_path = self._write_stdout(
                plan,
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "result": json.dumps(task_result),
                    "total_cost_usd": 0.01,
                    "permission_denials": [],
                },
            )

            result = ingest_claude_cli_stdout(
                stdout_path,
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            self.assertEqual(result.worker_report["status"], "invalid_worker_output")
            self.assertEqual(
                result.worker_report["failure_record_candidate"]["category"],
                "scope_violation",
            )
            self.assertIn(
                "branch_created",
                result.worker_report["failure_record_candidate"]["tags"],
            )

    def test_ingest_module_cli_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            stdout_path = self._write_stdout(
                plan,
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "result": json.dumps(self._worker_task_result(envelope)),
                    "total_cost_usd": 0.01,
                    "permission_denials": [],
                },
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "research_harness.workers.claude_stdout_ingest",
                    "--stdout",
                    str(stdout_path),
                    "--envelope",
                    str(envelope_path),
                    "--plan",
                    str(Path(tmp) / "live" / "manual_live_smoke_plan.json"),
                ],
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertTrue(Path(summary["worker_report_path"]).exists())


if __name__ == "__main__":
    unittest.main()
