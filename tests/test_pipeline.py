from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_harness.config import load_settings
from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.orchestrator.demo import _demo_node, run_demo
from research_harness.publishing.ac import ACDecisionError, decide_acceptance
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.claude_code_invoker import ClaudeCodeInvoker
from research_harness.workers.mock_backend import MockWorkerBackend


REPO_ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def test_demo_pipeline_writes_schema_valid_gate_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = run_demo(REPO_ROOT, Path(tmp) / "run")

            worker_report = json.loads((run_dir / "worker_report.json").read_text())
            ac_decision = json.loads((run_dir / "ac_decision.json").read_text())
            bundle = json.loads((run_dir / "research_state_bundle.json").read_text())

            validate_named_schema("worker_report", worker_report)
            validate_named_schema("ac_decision", ac_decision)
            validate_named_schema("invocation_envelope", bundle["invocation_envelope"])
            validate_named_schema("experiment_plan", bundle["experiment_plan"])
            validate_named_schema("job_manifest", bundle["job_manifest"])
            validate_named_schema("runner_result", bundle["runner_result"])
            self.assertEqual(
                bundle["job_manifest"]["experiment_plan_id"],
                bundle["experiment_plan"]["plan_id"],
            )
            self.assertEqual(bundle["runner_result"]["status"], "completed")
            self.assertTrue((run_dir / "experiment_plan.json").exists())
            self.assertEqual(
                bundle["source_files"],
                ["nodes/n_demo_001/workspace/experiment.py"],
            )
            self.assertEqual(worker_report["metrics"]["schema_validity"], 1.0)
            self.assertEqual(
                bundle["metrics_evidence_paths"],
                ["nodes/n_demo_001/workspace/artifacts/metrics.json"],
            )
            self.assertIsNone(bundle["runner_failure_memory"])
            self.assertIsNone(bundle["worker_failure_memory"])
            self.assertEqual(ac_decision["decision"], "accept")
            self.assertFalse(bundle["baseline_resolution"]["webfetch_executed"])
            self.assertIn("rebuttal_critic_reviews", bundle)
            self.assertIn("failure_branch_prior", bundle)
            self.assertTrue((run_dir / "interactive_summary.html").exists())

    def test_critic_blocks_missing_current_best_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            node = _demo_node()
            node["baseline_refs"][0]["roles"] = ["naive", "random_or_null"]
            report = MockWorkerBackend().run(node, Path(tmp))
            bundle = select_critics(REPO_ROOT, node)
            reviews = run_critic_reviews(node, report, bundle)

            objections = [
                objection["objection"]
                for review in reviews
                for objection in review["objections"]
            ]
            self.assertTrue(any("current_best_known" in objection for objection in objections))
            self.assertTrue(any(review["blocking"] for review in reviews))

    def test_ac_requires_reviews(self) -> None:
        with self.assertRaisesRegex(ACDecisionError, "at least one"):
            decide_acceptance([], load_settings(REPO_ROOT))

    def test_auth_preflight_blocks_anthropic_api_key_in_subscription_mode(self) -> None:
        settings = load_settings(REPO_ROOT)
        invoker = ClaudeCodeInvoker(REPO_ROOT, settings)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret"}, clear=False):
            result = invoker.auth_preflight()

        self.assertFalse(result.ok)
        self.assertEqual(result.mode, "api_key_would_take_precedence")

    def test_auth_preflight_passes_without_anthropic_api_key(self) -> None:
        settings = load_settings(REPO_ROOT)
        invoker = ClaudeCodeInvoker(REPO_ROOT, settings)

        with patch.dict(os.environ, {}, clear=True):
            result = invoker.auth_preflight()

        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "subscription_oauth")

    def test_auth_preflight_probes_subscription_status_without_pii(self) -> None:
        settings = load_settings(REPO_ROOT)
        invoker = ClaudeCodeInvoker(REPO_ROOT, settings)
        status = subprocess.CompletedProcess(
            args=["claude", "auth", "status", "--json"],
            returncode=0,
            stdout=json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "email": "redacted@example.com",
                    "orgId": "redacted",
                    "subscriptionType": "max",
                }
            ),
            stderr="",
        )

        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "research_harness.workers.claude_code_invoker.subprocess.run",
                return_value=status,
            ):
                result = invoker.auth_preflight(
                    claude_path="/usr/local/bin/claude",
                    probe_cli_status=True,
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.details["subscription_type"], "max")
        self.assertNotIn("email", result.details)


if __name__ == "__main__":
    unittest.main()
