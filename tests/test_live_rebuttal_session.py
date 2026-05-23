from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.live_rebuttal_session import build_live_rebuttal_session
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _worker_report(node_id: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "status": "completed",
        "claim_verdict_candidate": "supported",
        "metrics": {"live_rebuttal_json_contract": 1},
        "baselines": {"current_best_known": "checked"},
        "disproof_conditions_hit": [],
        "artifacts": [],
        "unexpected_observations": [],
        "failure_record_candidate": None,
    }


def _bundle(path: Path, *, promoted: bool = True) -> dict[str, object]:
    node = _demo_node()
    node["id"] = "n_live_rebuttal_001"
    next_transition = "promoted" if promoted else "needs_child_branch"
    final_verdict = (
        "supported_with_scope_narrowing"
        if promoted
        else "confounded_or_not_evaluable"
    )
    bundle = {
        "type": "live_reduction_bundle",
        "status": "ready_for_apply",
        "node_id": node["id"],
        "search_state_path": str(path.parent / "search_state.json"),
        "search_state_sha256": "0" * 64,
        "dispatch_path": str(path.parent / "live_node_dispatch.json"),
        "live_summary_path": str(path.parent / "live_worker_run_summary.json"),
        "worker_report_path": str(path.parent / "worker_report.json"),
        "critic_review_bundle_path": str(path.parent / "live_critic_review_bundle.json"),
        "orchestrator_reduction_path": str(path.parent / "live_orchestrator_reduction.json"),
        "bundle_path": str(path),
        "state_mutation": "forbidden",
        "memory_mutation": "forbidden",
        "apply_required": True,
        "node": node,
        "worker_report": _worker_report(node["id"]),
        "critic_routing": {"applied_critics": []},
        "critic_reviews": [],
        "failure_branch_prior": {
            "source": "failure_memory",
            "query_tags": [],
            "selected_failure_files": [],
            "risk_controls": [],
            "branch_suggestions": [],
        },
        "orchestrator_reduction": {
            "node_id": node["id"],
            "final_verdict": final_verdict,
            "research_status": final_verdict,
            "next_transition": next_transition,
            "score_summary": {
                "validity": 8,
                "necessity": 8,
                "reproducibility": 8,
                "taste_alignment": 8,
            },
            "blocking_objections": [],
            "accepted_lesson_candidates": [],
            "failure_branch_prior": {
                "source": "failure_memory",
                "query_tags": [],
                "selected_failure_files": [],
                "risk_controls": [],
                "branch_suggestions": [],
            },
            "child_branch_suggestions": [],
        },
        "error": None,
    }
    validate_named_schema("live_reduction_bundle", bundle)
    path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return bundle


class LiveRebuttalSessionTests(unittest.TestCase):
    def test_promoted_bundle_builds_rebuttal_session_and_ac_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = Path(tmp) / "live_reduction_bundle.json"
            _bundle(bundle_path, promoted=True)

            session = build_live_rebuttal_session(REPO_ROOT, bundle_path)

            validate_named_schema("live_rebuttal_session", session)
            validate_named_schema("ac_decision", session["ac_decision"])
            self.assertEqual(session["status"], "completed")
            self.assertEqual(session["ac_decision"]["decision"], "accept")
            self.assertTrue(session["publication_ready"])
            self.assertEqual(session["state_mutation"], "forbidden")
            self.assertEqual(session["memory_mutation"], "forbidden")
            self.assertTrue(Path(session["rebuttal_packet_path"]).exists())
            self.assertTrue(Path(session["orchestrator_rebuttal_path"]).exists())
            self.assertTrue(Path(session["rebuttal_critic_bundle_path"]).exists())
            self.assertTrue(Path(session["ac_decision_path"]).exists())
            research_bundle = json.loads(
                Path(session["research_state_bundle_path"]).read_text(encoding="utf-8")
            )
            self.assertIn("rebuttal_critic_reviews", research_bundle)
            self.assertIn("ac_decision", research_bundle)

    def test_non_promoted_bundle_is_blocked_before_rebuttal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = Path(tmp) / "live_reduction_bundle.json"
            _bundle(bundle_path, promoted=False)

            session = build_live_rebuttal_session(REPO_ROOT, bundle_path)

            validate_named_schema("live_rebuttal_session", session)
            self.assertEqual(session["status"], "blocked_not_promoted")
            self.assertFalse(session["publication_ready"])
            self.assertIsNone(session["ac_decision"])
            self.assertIsNone(session["rebuttal_packet_path"])
            self.assertTrue(Path(session["session_path"]).exists())


if __name__ == "__main__":
    unittest.main()
