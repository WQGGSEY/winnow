from __future__ import annotations

import unittest
from pathlib import Path

from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.search_state import (
    SearchStateError,
    initialize_search_state,
    search_policy_from_config,
    transition_node,
)
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


class SearchStateTests(unittest.TestCase):
    def test_initializes_search_state_from_policy(self) -> None:
        node = _demo_node()
        policy = search_policy_from_config(REPO_ROOT)
        state = initialize_search_state(
            search_id="s_demo",
            root_node=node,
            policy=policy,
        )

        validate_named_schema("search_state", state)
        self.assertEqual(state["frontier"][0]["node_id"], node["id"])
        self.assertEqual(state["max_depth"], 5)
        self.assertEqual(state["sunk_cost_policy"], "progress_gated")
        self.assertEqual(state["scaleup_policy"], "disallow_by_default")
        self.assertIn("current_branch_has_positive_signal", policy["scaleup_requires"])
        self.assertIsInstance(policy["num_drafts"], int)
        self.assertGreaterEqual(policy["num_drafts"], 1)

    def test_valid_transition_chain_records_history(self) -> None:
        state = initialize_search_state(
            search_id="s_demo",
            root_node=_demo_node(),
            policy=search_policy_from_config(REPO_ROOT),
        )

        transition_node(
            state,
            "n_demo_001",
            "running",
            event="dequeue",
            reason="frontier selected",
        )
        transition_node(
            state,
            "n_demo_001",
            "completed_worker_report",
            event="worker_report",
            reason="worker returned schema-valid report",
        )

        self.assertEqual(len(state["transitions"]), 2)
        self.assertIn("n_demo_001", state["completed_node_ids"])
        self.assertEqual(state["frontier"][0]["status"], "done")

    def test_invalid_transition_is_rejected(self) -> None:
        state = initialize_search_state(
            search_id="s_demo",
            root_node=_demo_node(),
            policy=search_policy_from_config(REPO_ROOT),
        )

        with self.assertRaisesRegex(SearchStateError, "invalid node transition"):
            transition_node(
                state,
                "n_demo_001",
                "promoted",
                event="skip",
                reason="should not skip review",
            )


if __name__ == "__main__":
    unittest.main()
