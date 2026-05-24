from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from research_harness.orchestrator.demo import _demo_node
from research_harness.production_runner import run_production_pipeline


REPO_ROOT = Path(__file__).resolve().parents[1]


def _retrieval_root_node() -> dict:
    """Demo node retargeted at the bundled retrieval template so the pipeline
    runs on real (toy nDCG) evidence instead of the fallback demo plan."""

    node = copy.deepcopy(_demo_node())
    node["domain"] = "retrieval"
    return node


class ProductionRunnerTests(unittest.TestCase):
    def test_mock_end_to_end_writes_summary_and_publication_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            summary = run_production_pipeline(
                REPO_ROOT, run_dir, publish=True, root_node=_retrieval_root_node()
            )

            self.assertEqual(summary["type"], "production_run_summary")
            self.assertEqual(summary["preflight_status"], "passed")
            self.assertEqual(summary["tree_search_status"], "completed")
            self.assertGreater(len(summary["promoted_node_ids"]), 0)
            self.assertFalse(summary["evidence_is_fake"])
            self.assertEqual(summary["fallback_node_ids"], [])

            tree_state_path = Path(summary["tree_search_state_path"])
            self.assertTrue(tree_state_path.exists())

            rebuttal = summary["rebuttal_summary"]
            self.assertIsNotNone(rebuttal)
            for key in (
                "rebuttal_packet_path",
                "orchestrator_rebuttal_path",
                "rebuttal_critic_bundle_path",
                "ac_decision_path",
                "research_state_bundle_path",
            ):
                self.assertTrue(Path(rebuttal[key]).exists(), key)

            dispatch = summary["publication_dispatch"]
            self.assertIsNotNone(dispatch)
            self.assertEqual(dispatch["decision"], "accept")
            self.assertFalse(dispatch["evidence_is_fake"])
            self.assertIsNone(dispatch["blocked_reason"])
            rendered = [item["output"] for item in dispatch["rendered_artifacts"]]
            self.assertIn("interactive_html", rendered)
            self.assertIn("slides_html", rendered)
            for item in dispatch["rendered_artifacts"]:
                self.assertTrue(Path(item["artifact_path"]).exists())

            written_summary = json.loads(
                (run_dir / "production_run_summary.json").read_text()
            )
            self.assertEqual(written_summary, summary)

    def test_fallback_demo_run_blocks_publish_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            # default _demo_node has domain="agent_harness" which has no template
            # under experiment_plan_templates/, so the demo fallback runs.
            summary = run_production_pipeline(REPO_ROOT, run_dir, publish=True)

            self.assertTrue(summary["evidence_is_fake"])
            self.assertIn("n_demo_001", summary["fallback_node_ids"])

            dispatch = summary["publication_dispatch"]
            self.assertIsNotNone(dispatch)
            self.assertTrue(dispatch["evidence_is_fake"])
            self.assertEqual(dispatch["rendered_artifacts"], [])
            self.assertIn("evidence_is_fake=true", dispatch["blocked_reason"])
            self.assertFalse((run_dir / "publication" / "interactive_summary.html").exists())
            self.assertFalse((run_dir / "publication" / "slides_summary.html").exists())

    def test_no_publish_flag_skips_renderer_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            summary = run_production_pipeline(REPO_ROOT, run_dir, publish=False)

            self.assertIsNone(summary["publication_dispatch"])
            self.assertIsNotNone(summary["rebuttal_summary"])
            self.assertFalse((run_dir / "publication").exists())

    def test_invalid_backend_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                run_production_pipeline(
                    REPO_ROOT,
                    Path(tmp) / "run",
                    backend_name="claude_code_live",
                )


if __name__ == "__main__":
    unittest.main()
