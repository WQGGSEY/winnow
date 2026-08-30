from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_harness.agent_runtime import RuntimeAuthResult
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.codex_invoker import CodexInvoker
from research_harness.workers.live_smoke_runner import (
    run_live_node_once,
    run_manual_live_smoke,
)
from research_harness.orchestrator.demo import _demo_node


REPO_ROOT = Path(__file__).resolve().parents[1]
AUTH = RuntimeAuthResult(
    ok=True,
    mode="chatgpt_login",
    details={"status": "logged_in", "auth_method": "chatgpt"},
)


def _result(run_dir: Path, node_id: str = "n_demo_001") -> dict[str, object]:
    task = json.loads(
        (run_dir / "nodes" / node_id / "workspace" / "worker_task.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        "task_id": task["task_id"],
        "node_id": node_id,
        "status": "completed",
        "output_kind": "observed_result",
        "summary": "bounded smoke",
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


def _jsonl(message: str) -> str:
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "worker-thread"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": message},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 2,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 8,
                    "reasoning_output_tokens": 3,
                },
            },
        )
    ) + "\n"


class LiveSmokeRunnerTests(unittest.TestCase):
    def test_live_execution_requires_second_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            CodexInvoker, "auth_preflight", return_value=AUTH
        ):
            summary = run_manual_live_smoke(
                REPO_ROOT,
                run_dir=Path(tmp) / "live",
                codex_path="/usr/local/bin/codex",
                billing_ack=True,
                execution_ack=False,
                command_runner=lambda *_args, **_kwargs: self.fail("runner called"),
            )
            self.assertEqual(summary["status"], "blocked_by_execution_ack")

    def test_non_ready_gate_blocks_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            CodexInvoker, "auth_preflight", return_value=AUTH
        ):
            summary = run_manual_live_smoke(
                REPO_ROOT,
                run_dir=Path(tmp) / "live",
                codex_path="/usr/local/bin/codex",
                billing_ack=False,
                execution_ack=True,
                command_runner=lambda *_args, **_kwargs: self.fail("runner called"),
            )

            self.assertEqual(summary["status"], "blocked_by_gate")
            self.assertEqual(summary["gate_status"], "blocked_by_billing_guard")

    def test_successful_codex_jsonl_is_ingested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"

            def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertEqual(args[0], "/usr/local/bin/codex")
                self.assertIsInstance(kwargs["input"], str)
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=_jsonl(json.dumps(_result(run_dir))),
                    stderr="",
                )

            with patch.object(CodexInvoker, "auth_preflight", return_value=AUTH):
                summary = run_manual_live_smoke(
                    REPO_ROOT,
                    run_dir=run_dir,
                    codex_path="/usr/local/bin/codex",
                    billing_ack=True,
                    execution_ack=True,
                    command_runner=runner,
                )

            validate_named_schema("live_smoke_run_summary", summary)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["usage_estimate"]["reasoning_output_tokens"], 3)
            self.assertNotIn("reported_total_cost_usd", summary["usage_estimate"])

    def test_arbitrary_node_live_once_uses_node_workspace(self) -> None:
        node = _demo_node()
        node["id"] = "n_custom_live_002"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live_node"

            def runner(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=_jsonl(json.dumps(_result(run_dir, node["id"]))),
                    stderr="",
                )

            with patch.object(CodexInvoker, "auth_preflight", return_value=AUTH):
                summary = run_live_node_once(
                    REPO_ROOT,
                    node,
                    run_dir=run_dir,
                    codex_path="/usr/local/bin/codex",
                    billing_ack=True,
                    execution_ack=True,
                    command_runner=runner,
                )

            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["node_id"], node["id"])
            self.assertIn(f"/nodes/{node['id']}/workspace/", summary["stdout_path"])

    def test_timeout_is_non_promotable(self) -> None:
        def timeout(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(args, 1, output="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            CodexInvoker, "auth_preflight", return_value=AUTH
        ):
            summary = run_manual_live_smoke(
                REPO_ROOT,
                run_dir=Path(tmp) / "live",
                codex_path="/usr/local/bin/codex",
                billing_ack=True,
                execution_ack=True,
                timeout_seconds=1,
                command_runner=timeout,
            )
            self.assertEqual(summary["status"], "timeout")
            self.assertEqual(summary["worker_report_status"], "timeout_or_turn_exhausted")


if __name__ == "__main__":
    unittest.main()
