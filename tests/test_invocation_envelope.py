from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from research_harness.config import load_settings
from research_harness.orchestrator.demo import _demo_node
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.codex_invoker import CodexInvoker
from research_harness.workers.output_repair import parse_or_repair_json
from research_harness.workers.workspace import WorkspaceGuardError


REPO_ROOT = Path(__file__).resolve().parents[1]


class InvocationEnvelopeTests(unittest.TestCase):
    def test_dry_run_artifacts_are_schema_valid_and_local_to_workspace(self) -> None:
        settings = load_settings(REPO_ROOT)
        node = _demo_node()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "nodes" / node["id"] / "workspace"
            invoker = CodexInvoker(workspace, settings, repo_root=REPO_ROOT)
            envelope = invoker.write_dry_run_artifacts(node)

            validate_named_schema("invocation_envelope", envelope)
            prompt_path = Path(envelope["prompt_path"])
            worker_task_path = Path(envelope["worker_task_path"])
            envelope_path = workspace / "invocation_envelope.json"

            self.assertTrue(prompt_path.exists())
            self.assertTrue(worker_task_path.exists())
            self.assertTrue(envelope_path.exists())
            prompt = prompt_path.read_text(encoding="utf-8")
            self.assertIn("Do not expand scope", prompt)
            self.assertIn("worker_task_result", prompt)
            self.assertIn("branch_suggestions", prompt)
            self.assertIn("unexpected_observations must be an array of objects", prompt)
            self.assertIn("must stay []", prompt)
            self.assertIn("do not emit label/rationale/may_create", prompt)
            self.assertIn("do not convert lessons/baselines/failures", prompt)
            self.assertIn("Active Lessons", prompt)
            self.assertIn("lesson_001", prompt)
            self.assertIn("Baseline Dossier Summary", prompt)
            self.assertIn("c1_sakana_ai_scientist_v2", prompt)
            self.assertIn("Failure Retrieval Index", prompt)
            self.assertIn("confounded_result", prompt)
            self.assertIn("relevant_failures", prompt)
            self.assertIn("n_demo_001__invalid_experiment", prompt)
            self.assertEqual(envelope["permission_mode"], "non_interactive_or_fail")
            self.assertEqual(envelope["output_schema"]["name"], "worker_task_result")
            self.assertEqual(envelope["allowed_read_roots"], [str(workspace.resolve())])
            self.assertFalse(envelope["command_plan"]["executes_in_dry_run"])
            self.assertIn("exec", envelope["command_plan"]["args"])
            self.assertIn("--ephemeral", envelope["command_plan"]["args"])
            self.assertIn("--ignore-user-config", envelope["command_plan"]["args"])
            self.assertIn("--ignore-rules", envelope["command_plan"]["args"])
            self.assertIn("--output-schema", envelope["command_plan"]["args"])
            self.assertEqual(envelope["environment_policy"]["unset"], [])
            # Prompt grew when Professor-generated reusable lib started shipping
            # alongside the per-node experiment.py. Still want a hard upper bound
            # to catch genuine prompt bloat, just at the new baseline.
            self.assertLess(len(prompt), 12000)

            written = json.loads(envelope_path.read_text(encoding="utf-8"))
            self.assertEqual(written["node_id"], node["id"])
            worker_task = json.loads(worker_task_path.read_text(encoding="utf-8"))
            validate_named_schema("worker_task", worker_task)
            self.assertFalse(worker_task["branch_policy"]["may_create_branches"])
            self.assertEqual(
                set(worker_task["allowed_output_kinds"]),
                {"source_patch", "observed_result"},
            )

            output_section = prompt.split("## Canonical Output JSON\n", 1)[1]
            output_template = parse_or_repair_json(
                output_section,
                "worker_task_result",
            )
            self.assertFalse(output_template.repaired)
            self.assertEqual(
                output_template.data["observed_result"]["unexpected_observations"],
                [],
            )
            self.assertEqual(output_template.data["branch_suggestions"], [])

    def test_repo_root_cannot_be_worker_workspace(self) -> None:
        settings = load_settings(REPO_ROOT)
        invoker = CodexInvoker(REPO_ROOT, settings, repo_root=REPO_ROOT)

        with self.assertRaisesRegex(WorkspaceGuardError, "repo root"):
            invoker.build_dry_run_invocation(_demo_node())

    def test_expected_output_path_cannot_escape_workspace(self) -> None:
        settings = load_settings(REPO_ROOT)
        node = _demo_node()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            invoker = CodexInvoker(workspace, settings, repo_root=REPO_ROOT)
            envelope = invoker.build_dry_run_invocation(node)
            envelope["expected_output_path"] = str(Path(tmp) / "outside.json")

            with self.assertRaisesRegex(WorkspaceGuardError, "expected_output_path"):
                invoker.validate_invocation_envelope(envelope)

    def test_runner_job_is_not_a_codex_worker_role(self) -> None:
        settings = load_settings(REPO_ROOT)
        node = copy.deepcopy(_demo_node())
        node["runtime_profile"]["worker_type"] = "runner_job"
        with tempfile.TemporaryDirectory() as tmp:
            invoker = CodexInvoker(Path(tmp) / "workspace", settings, repo_root=REPO_ROOT)

            with self.assertRaisesRegex(WorkspaceGuardError, "runner_job"):
                invoker.build_dry_run_invocation(node)

    def test_output_schema_must_be_worker_task_result_schema(self) -> None:
        settings = load_settings(REPO_ROOT)
        node = _demo_node()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            invoker = CodexInvoker(workspace, settings, repo_root=REPO_ROOT)
            envelope = invoker.build_dry_run_invocation(node)
            envelope["output_schema"]["path"] = str(
                REPO_ROOT / "research_harness" / "schemas" / "node.schema.json"
            )

            with self.assertRaisesRegex(WorkspaceGuardError, "worker_task_result"):
                invoker.validate_invocation_envelope(envelope)


if __name__ == "__main__":
    unittest.main()
