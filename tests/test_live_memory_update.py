from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from research_harness.config import load_lessons, load_yaml
from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.live_memory_update import apply_live_memory_update
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _temp_repo(tmp: str) -> Path:
    repo = Path(tmp)
    shutil.copy(REPO_ROOT / "lessons.yaml", repo / "lessons.yaml")
    (repo / "memory").mkdir()
    shutil.copytree(REPO_ROOT / "memory" / "failures", repo / "memory" / "failures")
    return repo


def _worker_report(node_id: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "status": "blocked_permission",
        "claim_verdict_candidate": "not_evaluable",
        "metrics": {"claude_cli_reported_total_cost_usd": 0.01},
        "baselines": {},
        "disproof_conditions_hit": [],
        "artifacts": [],
        "unexpected_observations": [
            {
                "observation": "Claude CLI could not complete the bounded task.",
                "evidence": "Claude CLI reported permission denials.",
                "suggested_branch_type": None,
                "scope_relation": "operational_blocker",
            }
        ],
        "failure_record_candidate": {
            "category": "invalid_experiment",
            "tags": ["claude_cli", "permission_denied", "live_dispatch"],
            "reason": "Claude CLI reported permission denials.",
        },
    }


def _bundle(path: Path) -> dict[str, object]:
    node = _demo_node()
    node["id"] = "n_live_memory_001"
    worker_report = _worker_report(node["id"])
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
        "worker_report": worker_report,
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
            "final_verdict": "confounded_or_not_evaluable",
            "research_status": "confounded_or_not_evaluable",
            "next_transition": "needs_child_branch",
            "score_summary": {
                "validity": 3,
                "necessity": 3,
                "reproducibility": 3,
                "taste_alignment": 5,
            },
            "blocking_objections": [],
            "accepted_lesson_candidates": [
                "In live dispatch, permission denials are evidence about runtime setup, not claim support."
            ],
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


class LiveMemoryUpdateTests(unittest.TestCase):
    def test_memory_update_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _temp_repo(tmp)
            bundle_path = Path(tmp) / "bundle" / "live_reduction_bundle.json"
            bundle_path.parent.mkdir()
            _bundle(bundle_path)
            original_lessons = (repo / "lessons.yaml").read_text(encoding="utf-8")
            original_index = (repo / "memory" / "failures" / "index.yaml").read_text(
                encoding="utf-8"
            )

            summary = apply_live_memory_update(repo, bundle_path, approve=False)

            validate_named_schema("live_memory_update_summary", summary)
            self.assertEqual(summary["status"], "blocked_by_missing_approval")
            self.assertIsNone(summary["failure_memory"])
            self.assertEqual(summary["recorded_lessons"], [])
            self.assertEqual((repo / "lessons.yaml").read_text(encoding="utf-8"), original_lessons)
            self.assertEqual(
                (repo / "memory" / "failures" / "index.yaml").read_text(encoding="utf-8"),
                original_index,
            )

    def test_approved_memory_update_records_failure_and_lessons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _temp_repo(tmp)
            bundle_path = Path(tmp) / "bundle" / "live_reduction_bundle.json"
            bundle_path.parent.mkdir()
            _bundle(bundle_path)
            before_lessons = load_lessons(repo)
            before_index = load_yaml(repo / "memory" / "failures" / "index.yaml")
            before_failure_count = len(before_index["categories"]["invalid_experiment"]["files"])

            summary = apply_live_memory_update(repo, bundle_path, approve=True)

            validate_named_schema("live_memory_update_summary", summary)
            self.assertEqual(summary["status"], "applied")
            self.assertIsNotNone(summary["failure_memory"])
            self.assertTrue(Path(summary["failure_memory"]["record_path"]).exists())
            self.assertEqual(len(summary["recorded_lessons"]), 2)
            for lesson in summary["recorded_lessons"]:
                self.assertNotIn("\n", lesson["text"])
                self.assertLessEqual(len(lesson["text"]), 240)

            after_lessons = load_lessons(repo)
            self.assertEqual(
                len(after_lessons["active_lessons"]),
                len(before_lessons["active_lessons"]) + 2,
            )
            after_index = load_yaml(repo / "memory" / "failures" / "index.yaml")
            self.assertEqual(
                len(after_index["categories"]["invalid_experiment"]["files"]),
                before_failure_count + 1,
            )

    def test_approved_memory_update_does_not_duplicate_lessons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _temp_repo(tmp)
            bundle_path = Path(tmp) / "bundle" / "live_reduction_bundle.json"
            bundle_path.parent.mkdir()
            _bundle(bundle_path)

            first = apply_live_memory_update(repo, bundle_path, approve=True)
            second = apply_live_memory_update(repo, bundle_path, approve=True)

            self.assertEqual(len(first["recorded_lessons"]), 2)
            self.assertEqual(second["recorded_lessons"], [])


if __name__ == "__main__":
    unittest.main()
