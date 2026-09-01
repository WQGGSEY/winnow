from __future__ import annotations

import unittest
from pathlib import Path

from research_harness.config import load_settings
from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.orchestrator.branch_prior import build_failure_branch_prior
from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.reduction import reduce_node
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _blocked_report() -> dict[str, object]:
    report = {
        "node_id": "n_demo_001",
        "status": "timeout_or_turn_exhausted",
        "claim_verdict_candidate": "not_evaluable",
        "metrics": {},
        "baselines": {},
        "disproof_conditions_hit": [],
        "artifacts": [],
        "unexpected_observations": [],
        "failure_record_candidate": None,
    }
    validate_named_schema("worker_report", report)
    return report


class BranchPriorTests(unittest.TestCase):
    def test_failure_branch_prior_contains_risk_controls(self) -> None:
        prior = build_failure_branch_prior(
            REPO_ROOT,
            _demo_node(),
            load_settings(REPO_ROOT),
        )

        self.assertEqual(prior["source"], "failure_memory")
        self.assertGreaterEqual(len(prior["risk_controls"]), 2)
        self.assertTrue(
            all("failure_file" in control for control in prior["risk_controls"])
        )

    def test_reduction_retains_failure_prior_without_generating_children(self) -> None:
        node = _demo_node()
        settings = load_settings(REPO_ROOT)
        prior = build_failure_branch_prior(REPO_ROOT, node, settings)
        reviews = run_critic_reviews(node, _blocked_report(), select_critics(REPO_ROOT, node))

        reduction = reduce_node(node, _blocked_report(), reviews, branch_prior=prior)

        self.assertEqual(reduction["next_transition"], "pruned")
        self.assertGreaterEqual(len(reduction["failure_branch_prior"]["risk_controls"]), 2)
        self.assertEqual(reduction["child_branch_suggestions"], [])


if __name__ == "__main__":
    unittest.main()
