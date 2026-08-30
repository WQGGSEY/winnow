from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_harness.agent_runtime import RuntimeAuthResult
from research_harness.orchestrator.demo import _demo_node
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.codex_invoker import CodexInvoker
from research_harness.workers.live_gate import (
    build_manual_live_node_plan,
    build_manual_live_smoke_plan,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
AUTH = RuntimeAuthResult(
    ok=True,
    mode="chatgpt_login",
    details={"status": "logged_in", "auth_method": "chatgpt"},
)


class LiveGateTests(unittest.TestCase):
    def test_ready_plan_uses_codex_worker_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            CodexInvoker, "auth_preflight", return_value=AUTH
        ):
            plan = build_manual_live_smoke_plan(
                REPO_ROOT,
                Path(tmp) / "live",
                codex_path="/usr/local/bin/codex",
                billing_ack=True,
            )

            validate_named_schema("manual_live_smoke_plan", plan)
            self.assertEqual(plan["status"], "ready_to_manually_run")
            self.assertEqual(plan["live_invocation_envelope"]["backend"], "codex_live")
            self.assertEqual(plan["auth_preflight"]["mode"], "chatgpt_login")
            self.assertTrue(plan["runtime_guard"]["requires_chatgpt_login"])
            self.assertEqual(plan["manual_command"][0], "/usr/local/bin/codex")
            self.assertEqual(
                plan["ingest_command"][3],
                "research_harness.workers.codex_stdout_ingest",
            )
            self.assertIn("--output-schema", plan["manual_command"])
            self.assertIn("--sandbox", plan["manual_command"])
            self.assertNotIn("--max-budget-usd", plan["manual_command"])

            plan_path = Path(tmp) / "live" / "manual_live_smoke_plan.json"
            self.assertEqual(json.loads(plan_path.read_text(encoding="utf-8")), plan)
            self.assertTrue(Path(plan["runbook_path"]).exists())

    def test_arbitrary_node_uses_its_own_workspace(self) -> None:
        node = _demo_node()
        node["id"] = "n_custom_live_002"
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            CodexInvoker, "auth_preflight", return_value=AUTH
        ):
            plan = build_manual_live_node_plan(
                REPO_ROOT,
                node,
                run_dir=Path(tmp) / "live_node",
                codex_path="/usr/local/bin/codex",
                billing_ack=True,
            )

            self.assertEqual(plan["live_invocation_envelope"]["node_id"], node["id"])
            self.assertIn(
                f"/nodes/{node['id']}/workspace/",
                plan["live_invocation_envelope"]["worker_task_path"],
            )

    def test_billing_ack_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            CodexInvoker, "auth_preflight", return_value=AUTH
        ):
            plan = build_manual_live_smoke_plan(
                REPO_ROOT,
                Path(tmp) / "live",
                codex_path="/usr/local/bin/codex",
                billing_ack=False,
            )

            self.assertEqual(plan["status"], "blocked_by_billing_guard")
            self.assertFalse(plan["billing_guard"]["ack_ok"])

    def test_missing_cli_is_reported_before_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "research_harness.workers.live_gate.shutil.which", return_value=None
        ):
            plan = build_manual_live_smoke_plan(
                REPO_ROOT,
                Path(tmp) / "live",
                billing_ack=True,
            )

            self.assertEqual(plan["status"], "blocked_by_missing_cli")
            self.assertFalse(plan["codex_cli"]["found"])

    def test_non_chatgpt_auth_is_blocked(self) -> None:
        auth = RuntimeAuthResult(
            ok=False,
            mode="non_chatgpt_auth",
            reason="codex login status is not using ChatGPT authentication.",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            CodexInvoker, "auth_preflight", return_value=auth
        ):
            plan = build_manual_live_smoke_plan(
                REPO_ROOT,
                Path(tmp) / "live",
                codex_path="/usr/local/bin/codex",
                billing_ack=True,
            )

            self.assertEqual(plan["status"], "blocked_by_auth")


if __name__ == "__main__":
    unittest.main()
