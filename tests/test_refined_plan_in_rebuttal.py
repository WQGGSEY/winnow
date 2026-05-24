from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_harness.publishing.rebuttal import build_rebuttal_packet


def _refined_plan(tmp: Path) -> str:
    plan = {
        "type": "refined_research_plan",
        "plan_id": "rrp_test_001",
        "status": "done",
        "source_grilling_session_id": "grill_x",
        "source_market_brief_id": "mrb_x",
        "claim_under_test": "X",
        "mandatory_baselines": ["A"],
        "success_criteria": ["B"],
        "disproof_conditions": ["C"],
        "validation_procedure": {
            "primary_metric": {"name": "ndcg_at_10", "operator": "greater_than", "threshold": 0.03},
            "splits": {"eval": "test"},
            "statistical_test": {"name": "paired_t_test", "alpha": 0.05},
            "n_seeds": 5,
            "decision_rule": "all_beat_and_p_lt_0.05",
        },
        "dataset_specs": [
            {"id": "ds_legalbench", "type": "benchmark", "role": "evaluation"}
        ],
        "unresolved_dataset_specs": [],
        "sota_reconciliations": [
            {
                "conflict": "Novelty claim conflicts with arxiv.org/abs/2305.xxxxx prior work.",
                "evidence_paper_url": "https://arxiv.org/abs/2305.xxxxx",
                "user_choice": "narrow_claim",
                "resolution": "Narrowed to legal domain only.",
                "resolved_at_round": 2,
            }
        ],
        "acknowledged_limitations": ["Toy synthetic eval only."],
        "rounds": [],
        "model": "sonnet",
        "created_at": "2026-05-25T00:00:00+00:00",
        "usage_estimate": {
            "rounds_used": 3,
            "total_cost_usd": 0.03,
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
        },
        "plan_path": str(tmp / "refined_research_plan.json"),
        "dataset_manifest_path": str(tmp / "dataset_manifest.json"),
        "error": None,
    }
    plan_path = Path(plan["plan_path"])
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return str(plan_path)


def _state_with_refined_plan(plan_path: str) -> dict:
    return {
        "node": {
            "id": "n_x",
            "type": "capability",
            "status": "promoted",
            "domain": "retrieval",
            "stage": "rebuttal",
            "parent": None,
            "lineage": {
                "root_goal_id": "rg_x",
                "covers_goal_facets": [],
                "inherited_assumptions": [
                    "Outer orchestrator owns search policy.",
                    "Workers are bounded tools.",
                    "refined_research_plan: rrp_test_001",
                    f"refined_research_plan_path: {plan_path}",
                ],
                "introduced_assumptions": [],
                "taste_constraints_applied": [],
            },
            "claim_contract": {
                "claim_under_test": "X",
                "mandatory_baselines": ["A"],
                "success_criteria": ["B"],
                "disproof_conditions": ["C"],
            },
            "baseline_refs": [
                {"baseline_dossier_id": "bd_x", "candidate_ids": [], "roles": ["current_best_known", "naive", "random_or_null"]}
            ],
            "runtime_profile": {"worker_type": "experiment_worker", "timeout_policy": "task_class_dependent", "turn_budget": 6},
            "failure_retrieval": {"query_tags": [], "selected_fail_files": []},
            "outputs": {"artifacts": [], "verdict": None},
        },
        "worker_report": {
            "node_id": "n_x",
            "status": "completed",
            "claim_verdict_candidate": "supported",
            "metrics": {"ndcg_at_10": 0.45},
            "baselines": {"current_best_known": 0.42, "naive": 0.30, "random_or_null": 0.05},
            "baseline_evidence_status": {"overall": "passed", "results": []},
            "disproof_conditions_hit": [],
            "artifacts": ["artifacts/metrics.json"],
            "unexpected_observations": [],
            "failure_record_candidate": None,
        },
        "orchestrator_reduction": {
            "node_id": "n_x",
            "final_verdict": "supported_with_scope_narrowing",
            "research_status": "supported_with_scope_narrowing",
            "next_transition": "promoted",
            "score_summary": {"validity": 8, "necessity": 7, "reproducibility": 7, "taste_alignment": 8},
            "blocking_objections": [],
            "accepted_lesson_candidates": [],
            "failure_branch_prior": {"source": "failure_memory", "query_tags": [], "selected_failure_files": [], "risk_controls": [], "branch_suggestions": []},
            "child_branch_suggestions": [],
        },
        "critic_reviews": [],
    }


class RefinedPlanInRebuttalTests(unittest.TestCase):
    def test_packet_surfaces_refined_plan_when_path_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = _refined_plan(Path(tmp))
            state = _state_with_refined_plan(plan_path)
            out = Path(tmp) / "packet.md"
            text = build_rebuttal_packet(state, out)
            self.assertIn("## Refined Research Plan", text)
            self.assertIn("rrp_test_001", text)
            self.assertIn("ndcg_at_10 greater_than 0.03", text)
            self.assertIn("paired_t_test alpha=0.05", text)
            self.assertIn("n_seeds=5", text)
            self.assertIn("ds_legalbench", text)
            self.assertIn("narrow_claim", text)
            self.assertIn("Narrowed to legal domain only.", text)
            self.assertIn("Toy synthetic eval only.", text)

    def test_packet_omits_refined_plan_section_when_no_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _state_with_refined_plan("/nonexistent/path.json")
            # remove the path line entirely
            state["node"]["lineage"]["inherited_assumptions"] = [
                line for line in state["node"]["lineage"]["inherited_assumptions"]
                if "refined_research_plan_path" not in line
            ]
            out = Path(tmp) / "packet.md"
            text = build_rebuttal_packet(state, out)
            self.assertNotIn("## Refined Research Plan", text)


if __name__ == "__main__":
    unittest.main()
