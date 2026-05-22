from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.live_gate import build_manual_live_smoke_plan


REPO_ROOT = Path(__file__).resolve().parents[1]


class LiveGateTests(unittest.TestCase):
    def test_manual_live_plan_is_ready_when_auth_ok_and_cli_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=True):
                plan = build_manual_live_smoke_plan(
                    REPO_ROOT,
                    Path(tmp) / "live",
                    claude_path="/usr/local/bin/claude",
                    billing_ack=True,
                )

            validate_named_schema("manual_live_smoke_plan", plan)
            validate_named_schema("invocation_envelope", plan["live_invocation_envelope"])
            self.assertEqual(plan["status"], "ready_to_manually_run")
            self.assertFalse(plan["execution_enabled"])
            self.assertEqual(plan["live_invocation_envelope"]["backend"], "claude_code_live")
            self.assertFalse(
                plan["live_invocation_envelope"]["command_plan"]["executes_in_dry_run"]
            )
            self.assertIn("--model", plan["manual_command"])
            self.assertIn("--max-budget-usd", plan["manual_command"])
            self.assertIn("--tools", plan["manual_command"])
            self.assertTrue((Path(tmp) / "live" / "manual_live_smoke_plan.json").exists())
            self.assertEqual(
                plan["live_invocation_envelope"]["command_plan"]["stdin_path"],
                plan["live_invocation_envelope"]["prompt_path"],
            )

    def test_manual_live_plan_blocks_when_api_key_would_override_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret"}, clear=False):
                plan = build_manual_live_smoke_plan(
                    REPO_ROOT,
                    Path(tmp) / "live",
                    claude_path="/usr/local/bin/claude",
                    billing_ack=True,
                )

            self.assertEqual(plan["status"], "blocked_by_auth")
            self.assertFalse(plan["auth_preflight"]["ok"])
            self.assertIn("ANTHROPIC_API_KEY", plan["auth_preflight"]["reason"])

    def test_manual_live_plan_blocks_when_claude_cli_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=True):
                with patch("research_harness.workers.live_gate.shutil.which", return_value=None):
                    plan = build_manual_live_smoke_plan(
                        REPO_ROOT,
                        Path(tmp) / "live",
                        billing_ack=True,
                    )

            self.assertEqual(plan["status"], "blocked_by_missing_cli")
            self.assertFalse(plan["claude_cli"]["found"])
            self.assertFalse(plan["execution_enabled"])

    def test_written_manual_live_plan_is_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=True):
                plan = build_manual_live_smoke_plan(
                    REPO_ROOT,
                    Path(tmp) / "live",
                    claude_path="/usr/local/bin/claude",
                    billing_ack=True,
                )
            written = json.loads(
                (Path(tmp) / "live" / "manual_live_smoke_plan.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(written["status"], plan["status"])
            self.assertEqual(written["manual_command"][0], "/usr/local/bin/claude")
            self.assertIn("-p", written["manual_command"])

    def test_manual_live_plan_blocks_without_billing_ack_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=True):
                plan = build_manual_live_smoke_plan(
                    REPO_ROOT,
                    Path(tmp) / "live",
                    claude_path="/usr/local/bin/claude",
                )

            self.assertEqual(plan["status"], "blocked_by_billing_guard")
            self.assertFalse(plan["billing_guard"]["ack_ok"])
            self.assertFalse(plan["execution_enabled"])


if __name__ == "__main__":
    unittest.main()
