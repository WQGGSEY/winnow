from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_harness.publishing.rebuttal import (
    build_orchestrator_rebuttal,
    build_rebuttal_packet,
)


def _sample_state(*, blocking: bool = False) -> dict:
    node = {
        "id": "n_sample_001",
        "type": "capability",
        "status": "promoted",
        "domain": "retrieval",
        "stage": "rebuttal",
        "parent": None,
        "lineage": {
            "root_goal_id": "rg_sample",
            "covers_goal_facets": ["performance"],
            "inherited_assumptions": [],
            "introduced_assumptions": [],
            "taste_constraints_applied": ["claim_first"],
        },
        "claim_contract": {
            "claim_under_test": "Method X improves nDCG@10 over BGE-large on legal QA.",
            "mandatory_baselines": ["BGE-large", "BM25", "random"],
            "success_criteria": ["nDCG@10 +5%"],
            "disproof_conditions": ["nDCG@10 within noise"],
        },
        "baseline_refs": [
            {
                "baseline_dossier_id": "bd_sample_dossier",
                "candidate_ids": ["c1", "c2", "c3"],
                "roles": ["current_best_known", "naive", "random_or_null"],
            }
        ],
        "runtime_profile": {
            "worker_type": "experiment_worker",
            "timeout_policy": "task_class_dependent",
            "turn_budget": 6,
        },
        "failure_retrieval": {"query_tags": ["retrieval"], "selected_fail_files": []},
        "outputs": {"artifacts": [], "verdict": None},
    }
    worker_report = {
        "node_id": "n_sample_001",
        "status": "completed",
        "claim_verdict_candidate": "supported",
        "metrics": {"ndcg_at_10": 0.42, "bounded_worker_success_rate": 1.0},
        "baselines": {"bge_large": 0.40, "bm25": 0.30, "random": 0.05},
        "baseline_evidence_status": {
            "overall": "passed",
            "results": [
                {"role": "current_best_known", "status": "passed", "reason": ""},
                {"role": "naive", "status": "passed", "reason": ""},
                {"role": "random_or_null", "status": "passed", "reason": ""},
            ],
        },
        "disproof_conditions_hit": [],
        "artifacts": [
            "nodes/n_sample_001/workspace/artifacts/metrics.json",
            "nodes/n_sample_001/workspace/runner_result.json",
        ],
        "unexpected_observations": [
            {
                "observation": "Improvement larger on long queries than short ones.",
                "evidence": "metrics.json split breakdown",
                "scope_relation": "within_claim",
                "suggested_branch_type": "boundary",
            }
        ],
        "failure_record_candidate": None,
    }
    reduction = {
        "node_id": "n_sample_001",
        "final_verdict": "supported_with_scope_narrowing" if not blocking else "confounded_or_not_evaluable",
        "research_status": "supported_with_scope_narrowing" if not blocking else "confounded_or_not_evaluable",
        "next_transition": "promoted" if not blocking else "needs_child_branch",
        "score_summary": {
            "validity": 8,
            "necessity": 7,
            "reproducibility": 7,
            "taste_alignment": 8,
        },
        "blocking_objections": [
            {
                "objection": "Necessity not established against same-split supervised baseline.",
                "required_resolution": "Add supervised baseline comparison.",
            }
        ]
        if blocking
        else [],
        "accepted_lesson_candidates": [
            "In retrieval, contrastive gains require same-split supervised comparison.",
        ],
        "failure_branch_prior": {
            "source": "failure_memory",
            "query_tags": ["retrieval"],
            "selected_failure_files": [],
            "risk_controls": [],
            "branch_suggestions": [],
        },
        "child_branch_suggestions": [
            {
                "type": "validity",
                "reason": "Unexpected observation should be checked.",
                "source": "worker_report.unexpected_observations",
            }
        ],
    }
    return {
        "node": node,
        "worker_report": worker_report,
        "orchestrator_reduction": reduction,
        "critic_reviews": [
            {"reviewer": "critic_alpha", "scores": {}, "blocking": False, "objections": []},
            {"reviewer": "critic_beta", "scores": {}, "blocking": False, "objections": []},
        ],
    }


class RebuttalTextTests(unittest.TestCase):
    def test_packet_has_no_legacy_v0_mock_phrases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "packet.md"
            text = build_rebuttal_packet(_sample_state(), out)
            for phrase in (
                "v0 packet",
                "v0 mock",
                "mock worker is used only",
                "demonstrates the harness architecture",
                "schema smoke testing",
            ):
                self.assertNotIn(phrase, text, f"legacy phrase leaked: {phrase!r}")

    def test_orchestrator_rebuttal_has_no_legacy_v0_mock_phrases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "orch.md"
            text = build_orchestrator_rebuttal(_sample_state(), out)
            for phrase in (
                "v0 mock",
                "does not execute additional experiments",
            ):
                self.assertNotIn(phrase, text, f"legacy phrase leaked: {phrase!r}")

    def test_packet_includes_real_evidence_from_worker_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "packet.md"
            state = _sample_state()
            text = build_rebuttal_packet(state, out)
            # status, verdict, transition surfaced
            self.assertIn("Worker status: completed", text)
            self.assertIn("Worker verdict candidate: supported", text)
            self.assertIn("Next transition: promoted", text)
            # metric/baseline keys surfaced
            self.assertIn("ndcg_at_10", text)
            self.assertIn("bge_large", text)
            # baseline evidence status surfaced
            self.assertIn("Overall: passed", text)
            # unexpected observation surfaced (not hidden by "mock" boilerplate)
            self.assertIn("Improvement larger on long queries", text)
            # artifact paths surfaced
            self.assertIn("metrics.json", text)
            # score_summary surfaced
            self.assertIn("validity: 8", text)

    def test_orchestrator_rebuttal_lists_blocking_objections_and_child_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "orch.md"
            text = build_orchestrator_rebuttal(_sample_state(blocking=True), out)
            self.assertIn("Necessity not established", text)
            self.assertIn("Required resolution: Add supervised baseline comparison.", text)
            self.assertIn("[validity]", text)
            self.assertIn("limited-depth", text)

    def test_orchestrator_rebuttal_handles_no_blocking_objections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "orch.md"
            text = build_orchestrator_rebuttal(_sample_state(blocking=False), out)
            self.assertIn("No publication-blocking objection", text)
            # still lists lessons + child branches
            self.assertIn("contrastive gains require same-split supervised", text)

    def test_packet_disproof_section_handles_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "packet.md"
            text = build_rebuttal_packet(_sample_state(), out)
            self.assertIn("## Disproof Conditions Hit", text)
            self.assertIn("(none triggered)", text)


if __name__ == "__main__":
    unittest.main()
