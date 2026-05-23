from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from research_harness.config import load_settings
from research_harness.orchestrator.demo import _demo_node
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.claude_code_invoker import ClaudeCodeInvoker
from research_harness.workers.claude_stdout_ingest import ingest_claude_cli_stdout


REPO_ROOT = Path(__file__).resolve().parents[1]


class ClaudeStdoutIngestTests(unittest.TestCase):
    def _envelope(self, tmp: str) -> dict[str, object]:
        settings = load_settings(REPO_ROOT)
        workspace = Path(tmp) / "node" / "workspace"
        invoker = ClaudeCodeInvoker(workspace, settings, repo_root=REPO_ROOT)
        envelope = invoker.write_dry_run_artifacts(_demo_node())
        envelope["backend"] = "claude_code_live"
        envelope["invocation_id"] = "invoke_live_n_demo_001"
        invoker.validate_invocation_envelope(envelope)
        return envelope

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

    def test_success_result_is_repaired_and_written_to_expected_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = self._envelope(tmp)
            workspace = Path(envelope["workspace"])
            stdout_path = workspace / "claude_stdout.json"
            cli_result = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "result": "```json\n"
                + json.dumps(self._worker_task_result(envelope))
                + "\n```",
                "total_cost_usd": 0.01,
                "permission_denials": [],
            }
            stdout_path.write_text(json.dumps(cli_result), encoding="utf-8")

            result = ingest_claude_cli_stdout(stdout_path, envelope)

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

    def test_permission_denials_force_blocked_permission_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = self._envelope(tmp)
            workspace = Path(envelope["workspace"])
            stdout_path = workspace / "claude_stdout.json"
            cli_result = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 2,
                "result": json.dumps(self._worker_task_result(envelope)),
                "total_cost_usd": 0.02,
                "permission_denials": [{"tool_name": "Read"}],
            }
            stdout_path.write_text(json.dumps(cli_result), encoding="utf-8")

            result = ingest_claude_cli_stdout(stdout_path, envelope)

            self.assertEqual(result.worker_report["status"], "blocked_permission")
            self.assertEqual(
                result.worker_report["failure_record_candidate"]["category"],
                "invalid_experiment",
            )

    def test_budget_error_becomes_non_promotable_worker_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = self._envelope(tmp)
            workspace = Path(envelope["workspace"])
            stdout_path = workspace / "claude_stdout.json"
            cli_result = {
                "type": "result",
                "subtype": "error_max_budget_usd",
                "is_error": True,
                "num_turns": 1,
                "result": "",
                "total_cost_usd": 0.11899725,
                "permission_denials": [],
                "errors": ["Reached maximum budget ($0.05)"],
            }
            stdout_path.write_text(json.dumps(cli_result), encoding="utf-8")

            result = ingest_claude_cli_stdout(stdout_path, envelope)

            self.assertEqual(result.worker_report["status"], "timeout_or_turn_exhausted")
            self.assertIn(
                "budget_exhausted",
                result.worker_report["failure_record_candidate"]["tags"],
            )

    def test_invalid_inner_result_becomes_invalid_worker_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = self._envelope(tmp)
            workspace = Path(envelope["workspace"])
            stdout_path = workspace / "claude_stdout.json"
            cli_result = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "result": "{\"node_id\": \"n_demo_001\"}",
                "total_cost_usd": 0.01,
                "permission_denials": [],
            }
            stdout_path.write_text(json.dumps(cli_result), encoding="utf-8")

            result = ingest_claude_cli_stdout(stdout_path, envelope)

            self.assertEqual(result.worker_report["status"], "invalid_worker_output")

    def test_scope_violation_becomes_invalid_worker_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = self._envelope(tmp)
            workspace = Path(envelope["workspace"])
            stdout_path = workspace / "claude_stdout.json"
            task_result = self._worker_task_result(envelope)
            task_result["scope_check"]["branch_created"] = True
            cli_result = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "result": json.dumps(task_result),
                "total_cost_usd": 0.01,
                "permission_denials": [],
            }
            stdout_path.write_text(json.dumps(cli_result), encoding="utf-8")

            result = ingest_claude_cli_stdout(stdout_path, envelope)

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
            envelope = self._envelope(tmp)
            workspace = Path(envelope["workspace"])
            envelope_path = workspace / "live_invocation_envelope.json"
            envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
            stdout_path = workspace / "claude_stdout.json"
            cli_result = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "result": json.dumps(self._worker_task_result(envelope)),
                "total_cost_usd": 0.01,
                "permission_denials": [],
            }
            stdout_path.write_text(json.dumps(cli_result), encoding="utf-8")

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
