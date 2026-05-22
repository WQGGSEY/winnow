from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from research_harness.config import load_settings
from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.reduction import ReductionError, reduce_node
from research_harness.publishing.ac import decide_acceptance
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def worker_report(status: str, verdict: str = "supported") -> dict[str, Any]:
    report = {
        "node_id": "n_demo_001",
        "status": status,
        "claim_verdict_candidate": verdict,
        "metrics": {"partial": 1.0},
        "baselines": {"naive": 0.5},
        "disproof_conditions_hit": [],
        "artifacts": ["partial.log"],
        "unexpected_observations": [],
        "failure_record_candidate": None,
    }
    validate_named_schema("worker_report", report)
    return report


class AdversarialWorkerStateTests(unittest.TestCase):
    def test_reduction_requires_critic_reviews(self) -> None:
        with self.assertRaisesRegex(ReductionError, "critic review"):
            reduce_node(_demo_node(), worker_report("completed"), [])

    def test_permission_block_cannot_be_promoted_even_if_worker_claims_supported(self) -> None:
        node = _demo_node()
        report = worker_report("blocked_permission", "supported")
        reviews = run_critic_reviews(node, report, select_critics(REPO_ROOT, node))
        reduction = reduce_node(node, report, reviews)
        ac_decision = decide_acceptance(reviews, load_settings(REPO_ROOT))

        self.assertEqual(reduction["next_transition"], "needs_child_branch")
        self.assertEqual(reduction["final_verdict"], "confounded_or_not_evaluable")
        self.assertIn("failure_branch_prior", reduction)
        self.assertEqual(ac_decision["decision"], "reject")

    def test_timeout_partial_cannot_support_claim_by_itself(self) -> None:
        node = _demo_node()
        report = worker_report("timeout_or_turn_exhausted", "supported")
        reviews = run_critic_reviews(node, report, select_critics(REPO_ROOT, node))
        reduction = reduce_node(node, report, reviews)
        objections = [
            objection["objection"]
            for review in reviews
            for objection in review["objections"]
        ]

        self.assertEqual(reduction["research_status"], "confounded_or_not_evaluable")
        self.assertTrue(any("timeout_or_turn_exhausted" in item for item in objections))

    def test_invalid_worker_output_forces_operational_blocker(self) -> None:
        node = _demo_node()
        report = worker_report("invalid_worker_output", "not_evaluable")
        reviews = run_critic_reviews(node, report, select_critics(REPO_ROOT, node))
        reduction = reduce_node(node, report, reviews)

        self.assertEqual(reduction["research_status"], "confounded_or_not_evaluable")
        self.assertGreater(len(reduction["blocking_objections"]), 0)


if __name__ == "__main__":
    unittest.main()
