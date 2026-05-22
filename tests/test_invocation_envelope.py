from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from research_harness.config import load_settings
from research_harness.orchestrator.demo import _demo_node
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.claude_code_invoker import ClaudeCodeInvoker
from research_harness.workers.workspace import WorkspaceGuardError


REPO_ROOT = Path(__file__).resolve().parents[1]


class InvocationEnvelopeTests(unittest.TestCase):
    def test_dry_run_artifacts_are_schema_valid_and_local_to_workspace(self) -> None:
        settings = load_settings(REPO_ROOT)
        node = _demo_node()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "nodes" / node["id"] / "workspace"
            invoker = ClaudeCodeInvoker(workspace, settings, repo_root=REPO_ROOT)
            envelope = invoker.write_dry_run_artifacts(node)

            validate_named_schema("invocation_envelope", envelope)
            prompt_path = Path(envelope["prompt_path"])
            envelope_path = workspace / "invocation_envelope.json"

            self.assertTrue(prompt_path.exists())
            self.assertTrue(envelope_path.exists())
            prompt = prompt_path.read_text(encoding="utf-8")
            self.assertIn("Do not expand scope", prompt)
            self.assertIn("Active Lessons", prompt)
            self.assertIn("lesson_001", prompt)
            self.assertIn("Baseline Dossier Summary", prompt)
            self.assertIn("c1_sakana_ai_scientist_v2", prompt)
            self.assertIn("Failure Retrieval Index", prompt)
            self.assertIn("confounded_result", prompt)
            self.assertEqual(envelope["permission_mode"], "non_interactive_or_fail")
            self.assertEqual(envelope["allowed_read_roots"], [str(workspace.resolve())])
            self.assertFalse(envelope["command_plan"]["executes_in_dry_run"])
            self.assertIn("--tools", envelope["command_plan"]["args"])
            self.assertIn("--max-budget-usd", envelope["command_plan"]["args"])
            self.assertIn("--no-session-persistence", envelope["command_plan"]["args"])
            self.assertIn("ANTHROPIC_API_KEY", envelope["environment_policy"]["unset"])

            written = json.loads(envelope_path.read_text(encoding="utf-8"))
            self.assertEqual(written["node_id"], node["id"])

    def test_repo_root_cannot_be_worker_workspace(self) -> None:
        settings = load_settings(REPO_ROOT)
        invoker = ClaudeCodeInvoker(REPO_ROOT, settings, repo_root=REPO_ROOT)

        with self.assertRaisesRegex(WorkspaceGuardError, "repo root"):
            invoker.build_dry_run_invocation(_demo_node())

    def test_expected_output_path_cannot_escape_workspace(self) -> None:
        settings = load_settings(REPO_ROOT)
        node = _demo_node()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            invoker = ClaudeCodeInvoker(workspace, settings, repo_root=REPO_ROOT)
            envelope = invoker.build_dry_run_invocation(node)
            envelope["expected_output_path"] = str(Path(tmp) / "outside.json")

            with self.assertRaisesRegex(WorkspaceGuardError, "expected_output_path"):
                invoker.validate_invocation_envelope(envelope)

    def test_runner_job_is_not_a_claude_worker_role(self) -> None:
        settings = load_settings(REPO_ROOT)
        node = copy.deepcopy(_demo_node())
        node["runtime_profile"]["worker_type"] = "runner_job"
        with tempfile.TemporaryDirectory() as tmp:
            invoker = ClaudeCodeInvoker(Path(tmp) / "workspace", settings, repo_root=REPO_ROOT)

            with self.assertRaisesRegex(WorkspaceGuardError, "runner_job"):
                invoker.build_dry_run_invocation(node)

    def test_output_schema_must_be_worker_report_schema(self) -> None:
        settings = load_settings(REPO_ROOT)
        node = _demo_node()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            invoker = ClaudeCodeInvoker(workspace, settings, repo_root=REPO_ROOT)
            envelope = invoker.build_dry_run_invocation(node)
            envelope["output_schema"]["path"] = str(
                REPO_ROOT / "research_harness" / "schemas" / "node.schema.json"
            )

            with self.assertRaisesRegex(WorkspaceGuardError, "worker_report"):
                invoker.validate_invocation_envelope(envelope)


if __name__ == "__main__":
    unittest.main()
