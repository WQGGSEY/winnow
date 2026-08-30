from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_harness.agent_runtime import RuntimeAuthResult
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.codex_invoker import CodexInvoker
from research_harness.workers.codex_stdout_ingest import ingest_codex_cli_stdout
from research_harness.workers.live_gate import build_manual_live_smoke_plan


REPO_ROOT = Path(__file__).resolve().parents[1]


def _jsonl(message: str) -> str:
    events = [
        {"type": "thread.started", "thread_id": "thread-worker"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": message},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 12,
                "cached_input_tokens": 3,
                "cache_write_input_tokens": 1,
                "output_tokens": 20,
                "reasoning_output_tokens": 4,
            },
        },
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class CodexStdoutIngestTests(unittest.TestCase):
    def _ready_plan(self, tmp: str) -> tuple[dict[str, object], dict[str, object], Path]:
        with patch.object(
            CodexInvoker,
            "auth_preflight",
            return_value=RuntimeAuthResult(
                ok=True,
                mode="chatgpt_login",
                details={"status": "logged_in", "auth_method": "chatgpt"},
            ),
        ):
            plan = build_manual_live_smoke_plan(
                REPO_ROOT,
                Path(tmp) / "live",
                codex_path="/usr/local/bin/codex",
                billing_ack=True,
            )
        envelope = plan["live_invocation_envelope"]
        return plan, envelope, Path(plan["live_invocation_envelope_path"])

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

    def test_final_message_is_repaired_and_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            stdout_path = Path(plan["manual_stdout_path"])
            stdout_path.write_text(
                _jsonl(
                    "```json\n"
                    + json.dumps(self._worker_task_result(envelope))
                    + "\n```"
                ),
                encoding="utf-8",
            )

            result = ingest_codex_cli_stdout(
                stdout_path,
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            validate_named_schema("worker_report", result.worker_report)
            self.assertTrue(result.repaired)
            self.assertEqual(result.worker_report["status"], "completed")
            metadata = json.loads(result.ingest_metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["usage"]["cached_input_tokens"], 3)
            self.assertNotIn("total_cost_usd", metadata)

    def test_live_backend_without_matching_plan_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, _ = self._ready_plan(tmp)
            result = ingest_codex_cli_stdout(Path(plan["manual_stdout_path"]), envelope)
            self.assertEqual(result.worker_report["status"], "blocked_preflight")

    def test_stdout_path_mismatch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            wrong_stdout = Path(plan["manual_stdout_path"]).with_name("other.jsonl")

            result = ingest_codex_cli_stdout(
                wrong_stdout,
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            self.assertEqual(result.worker_report["status"], "blocked_preflight")
            self.assertIn(
                "stdout path does not match",
                result.worker_report["failure_record_candidate"]["reason"],
            )

    def test_missing_envelope_path_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, _ = self._ready_plan(tmp)

            result = ingest_codex_cli_stdout(
                Path(plan["manual_stdout_path"]),
                envelope,
                live_plan=plan,
            )

            self.assertEqual(result.worker_report["status"], "blocked_preflight")
            self.assertIn(
                "requires the envelope file path",
                result.worker_report["failure_record_candidate"]["reason"],
            )

    def test_blocked_billing_plan_cannot_authorize_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            plan["status"] = "blocked_by_billing_guard"
            plan["billing_guard"]["ack_ok"] = False

            result = ingest_codex_cli_stdout(
                Path(plan["manual_stdout_path"]),
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            self.assertEqual(result.worker_report["status"], "blocked_preflight")
            self.assertIn(
                "live gate is not ready",
                result.worker_report["failure_record_candidate"]["reason"],
            )

    def test_scope_violation_is_not_promotable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            task_result = self._worker_task_result(envelope)
            task_result["scope_check"]["branch_created"] = True
            stdout_path = Path(plan["manual_stdout_path"])
            stdout_path.write_text(_jsonl(json.dumps(task_result)), encoding="utf-8")

            result = ingest_codex_cli_stdout(
                stdout_path,
                envelope,
                live_plan=plan,
                envelope_path=envelope_path,
            )

            self.assertEqual(result.worker_report["status"], "invalid_worker_output")
            self.assertIn(
                "branch_created",
                result.worker_report["failure_record_candidate"]["tags"],
            )

    def test_ingest_module_cli_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, envelope, envelope_path = self._ready_plan(tmp)
            stdout_path = Path(plan["manual_stdout_path"])
            stdout_path.write_text(
                _jsonl(json.dumps(self._worker_task_result(envelope))),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "research_harness.workers.codex_stdout_ingest",
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
            self.assertEqual(json.loads(completed.stdout)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
