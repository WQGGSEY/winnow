from __future__ import annotations

import unittest

from research_harness.orchestrator.demo import _demo_node
from research_harness.schemas.validator import validate_named_schema


class SearchStateSchemaTests(unittest.TestCase):
    def test_search_state_frontier_and_transition_schemas_validate(self) -> None:
        frontier_item = {
            "node_id": "n_demo_001",
            "parent": None,
            "depth": 0,
            "priority": 1.0,
            "stage": "experimentation",
            "status": "queued",
            "reason": "root",
        }
        transition = {
            "node_id": "n_demo_001",
            "from_status": "ready",
            "to_status": "running",
            "event": "dequeue",
            "reason": "frontier selected",
            "created_child_ids": [],
        }
        search_state = {
            "search_id": "s_demo",
            "status": "initialized",
            "max_depth": 5,
            "max_debug_depth": 2,
            "sunk_cost_policy": "progress_gated",
            "scaleup_policy": "disallow_by_default",
            "frontier": [frontier_item],
            "nodes": [_demo_node()],
            "completed_node_ids": [],
            "promoted_node_ids": [],
            "pruned_node_ids": [],
            "transitions": [transition],
        }

        validate_named_schema("frontier_item", frontier_item)
        validate_named_schema("node_transition", transition)
        validate_named_schema("search_state", search_state)


if __name__ == "__main__":
    unittest.main()
