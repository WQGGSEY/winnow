from __future__ import annotations

import copy
import unittest

from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.validation import ValidationError, validate_node_invariants
from research_harness.schemas.validator import SchemaValidationError, validate_named_schema


class SchemaValidationTests(unittest.TestCase):
    def test_demo_node_satisfies_schema_and_invariants(self) -> None:
        node = _demo_node()

        validate_named_schema("node", node)
        validate_node_invariants(node)

    def test_bad_worker_report_enum_is_rejected(self) -> None:
        report = {
            "node_id": "n_demo_001",
            "status": "looks_good",
            "claim_verdict_candidate": "supported",
            "metrics": {},
            "baselines": {},
            "disproof_conditions_hit": [],
            "artifacts": [],
            "unexpected_observations": [],
        }

        with self.assertRaisesRegex(SchemaValidationError, "not in enum"):
            validate_named_schema("worker_report", report)

    def test_missing_baseline_roles_are_rejected_for_claim_nodes(self) -> None:
        node = copy.deepcopy(_demo_node())
        node["baseline_refs"][0]["roles"] = ["naive", "random_or_null"]

        with self.assertRaisesRegex(ValidationError, "current_best_known"):
            validate_node_invariants(node)

    def test_missing_claim_contract_baseline_is_rejected(self) -> None:
        node = copy.deepcopy(_demo_node())
        node["claim_contract"]["mandatory_baselines"] = []

        with self.assertRaisesRegex(ValidationError, "mandatory_baselines"):
            validate_node_invariants(node)

    def test_ac_decision_score_bounds_are_enforced(self) -> None:
        decision = {
            "decision": "accept",
            "confidence": "medium",
            "score_summary": {
                "novelty": 10,
                "validity": 11,
                "necessity": 7,
                "clarity": 7,
                "reproducibility": 7,
                "taste_alignment": 8,
            },
            "blocking_reasons": [],
            "required_next_search_nodes": [],
            "camera_ready_conditions": [],
        }

        with self.assertRaisesRegex(SchemaValidationError, "above maximum"):
            validate_named_schema("ac_decision", decision)


if __name__ == "__main__":
    unittest.main()

