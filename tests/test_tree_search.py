from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from research_harness.orchestrator.tree_search import run_mock_tree_search
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


class BlockingBackend:
    def run(self, node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
        return {
            "node_id": node["id"],
            "status": "timeout_or_turn_exhausted",
            "claim_verdict_candidate": "not_evaluable",
            "metrics": {},
            "baselines": {},
            "disproof_conditions_hit": [],
            "artifacts": [],
            "unexpected_observations": [],
            "failure_record_candidate": None,
        }


class TreeSearchTests(unittest.TestCase):
    def test_mock_tree_search_promotes_root_with_default_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_mock_tree_search(REPO_ROOT, Path(tmp) / "tree")

            state = result["search_state"]
            validate_named_schema("search_state", state)
            self.assertEqual(state["status"], "completed")
            self.assertIn("n_demo_001", state["promoted_node_ids"])
            self.assertTrue((Path(tmp) / "tree" / "search_state.json").exists())

    def test_blocking_backend_opens_child_branch_until_step_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_mock_tree_search(
                REPO_ROOT,
                Path(tmp) / "tree",
                backend=BlockingBackend(),
                max_steps=1,
            )

            state = result["search_state"]
            child_ids = [
                node["id"]
                for node in state["nodes"]
                if node.get("parent") == "n_demo_001"
            ]
            self.assertGreaterEqual(len(child_ids), 1)
            self.assertEqual(state["status"], "blocked")
            written = json.loads((Path(tmp) / "tree" / "search_state.json").read_text())
            self.assertEqual(written["status"], "blocked")

    def test_live_backend_name_is_rejected_by_tree_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "live_gate"):
                run_mock_tree_search(
                    REPO_ROOT,
                    Path(tmp) / "tree",
                    backend_name="claude_code_live",
                )


if __name__ == "__main__":
    unittest.main()
