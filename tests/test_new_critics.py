from __future__ import annotations

import copy
import unittest
from pathlib import Path

from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.orchestrator.demo import _demo_node


REPO_ROOT = Path(__file__).resolve().parents[1]


def _sample_worker_report(
    *,
    main_metric: float,
    current_best: float,
    has_json_artifact: bool = True,
) -> dict:
    return {
        "node_id": "n_demo_001",
        "status": "completed",
        "claim_verdict_candidate": "supported",
        "metrics": {"main_metric": main_metric},
        "baselines": {"current_best_known": current_best, "naive": 0.2, "random_or_null": 0.05},
        "baseline_evidence_status": {"overall": "passed", "results": []},
        "disproof_conditions_hit": [],
        "artifacts": ["artifacts/metrics.json"] if has_json_artifact else ["artifacts/run.log"],
        "unexpected_observations": [],
        "failure_record_candidate": None,
    }


class NewCriticPresenceTests(unittest.TestCase):
    def test_promotion_routing_includes_new_always_critics(self) -> None:
        node = _demo_node()
        bundle = select_critics(REPO_ROOT, node)
        ids = {c["critic_id"] for c in bundle["applied_critics"]}
        self.assertIn("senior_quant_researcher_v1", ids)
        self.assertIn("reproducibility_auditor_v1", ids)

    def test_capability_node_picks_experimental_methodologist(self) -> None:
        node = _demo_node()
        bundle = select_critics(REPO_ROOT, node)
        ids = {c["critic_id"] for c in bundle["applied_critics"]}
        self.assertIn("experimental_methodologist_v1", ids)

    def test_rebuttal_stage_picks_claim_skeptic(self) -> None:
        node = copy.deepcopy(_demo_node())
        node["stage"] = "rebuttal"
        bundle = select_critics(REPO_ROOT, node)
        ids = {c["critic_id"] for c in bundle["applied_critics"]}
        self.assertIn("claim_skeptic_v1", ids)


class QuantReviewBehaviorTests(unittest.TestCase):
    def _quant_review(self, *, main_metric, current_best, has_json=True):
        node = _demo_node()
        wr = _sample_worker_report(
            main_metric=main_metric,
            current_best=current_best,
            has_json_artifact=has_json,
        )
        bundle = select_critics(REPO_ROOT, node)
        reviews = run_critic_reviews(node, wr, bundle)
        for r in reviews:
            if r["critic_id"] == "senior_quant_researcher_v1":
                return r
        raise AssertionError("senior_quant_researcher_v1 not in reviews")

    def _repro_review(self, *, has_json):
        node = _demo_node()
        wr = _sample_worker_report(
            main_metric=0.6, current_best=0.5, has_json_artifact=has_json
        )
        bundle = select_critics(REPO_ROOT, node)
        reviews = run_critic_reviews(node, wr, bundle)
        for r in reviews:
            if r["critic_id"] == "reproducibility_auditor_v1":
                return r
        raise AssertionError("reproducibility_auditor_v1 not in reviews")

    def test_within_noise_improvement_lowers_validity_and_objects(self) -> None:
        review = self._quant_review(main_metric=0.501, current_best=0.500)
        self.assertLessEqual(review["scores"]["validity"], 6)
        self.assertFalse(review["blocking"])
        joined = " ".join(o["objection"] for o in review["objections"])
        self.assertIn("within noise", joined)

    def test_large_improvement_does_not_object(self) -> None:
        review = self._quant_review(main_metric=0.6, current_best=0.5)
        self.assertEqual(review["objections"], [])

    def test_no_baseline_means_no_quant_objection(self) -> None:
        node = _demo_node()
        wr = _sample_worker_report(main_metric=0.5, current_best=0.5)
        wr["baselines"] = {}
        bundle = select_critics(REPO_ROOT, node)
        reviews = run_critic_reviews(node, wr, bundle)
        quant = next(r for r in reviews if r["critic_id"] == "senior_quant_researcher_v1")
        self.assertEqual(quant["objections"], [])


class ReproducibilityReviewBehaviorTests(unittest.TestCase):
    def _repro_review(self, *, has_json):
        node = _demo_node()
        wr = _sample_worker_report(
            main_metric=0.6, current_best=0.5, has_json_artifact=has_json
        )
        bundle = select_critics(REPO_ROOT, node)
        reviews = run_critic_reviews(node, wr, bundle)
        for r in reviews:
            if r["critic_id"] == "reproducibility_auditor_v1":
                return r
        raise AssertionError("reproducibility_auditor_v1 not in reviews")

    def test_missing_json_metrics_lowers_reproducibility_score(self) -> None:
        r = self._repro_review(has_json=False)
        self.assertLessEqual(r["scores"]["reproducibility"], 5)
        self.assertTrue(any("JSON metrics" in o["objection"] for o in r["objections"]))

    def test_json_metrics_present_no_objection(self) -> None:
        r = self._repro_review(has_json=True)
        self.assertEqual(r["objections"], [])


if __name__ == "__main__":
    unittest.main()
