from __future__ import annotations

import unittest

from research_harness.orchestrator.child_nodes import draft_child_nodes
from research_harness.orchestrator.demo import _demo_node
from research_harness.schemas.validator import validate_named_schema


class ChildNodeTests(unittest.TestCase):
    def test_branch_suggestion_becomes_ready_child_node(self) -> None:
        parent = _demo_node()
        reduction = {
            "next_transition": "needs_child_branch",
            "child_branch_suggestions": [
                {
                    "type": "validity",
                    "reason": "Control for prior permission failure.",
                    "source": "failure_memory:invalid_experiment/example.md",
                }
            ],
        }

        children = draft_child_nodes(parent, reduction, parent_depth=0, max_depth=5)

        self.assertEqual(len(children), 1)
        child = children[0]
        validate_named_schema("node", child)
        self.assertEqual(child["status"], "ready")
        self.assertEqual(child["parent"], parent["id"])
        self.assertIn(
            "invalid_experiment/example.md",
            child["failure_retrieval"]["selected_fail_files"],
        )

    def test_no_child_when_depth_exhausted(self) -> None:
        children = draft_child_nodes(
            _demo_node(),
            {
                "next_transition": "needs_child_branch",
                "child_branch_suggestions": [{"type": "validity", "reason": "x"}],
            },
            parent_depth=5,
            max_depth=5,
        )

        self.assertEqual(children, [])

    def test_promoted_reduction_does_not_create_child(self) -> None:
        children = draft_child_nodes(
            _demo_node(),
            {"next_transition": "promoted", "child_branch_suggestions": []},
            parent_depth=0,
            max_depth=5,
        )

        self.assertEqual(children, [])


if __name__ == "__main__":
    unittest.main()
