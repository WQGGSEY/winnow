from __future__ import annotations

import copy
import hashlib
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from research_harness.memory.baseline_dossier import (
    BaselineDossierError,
    build_baseline_resolution_report,
    dossier_path,
    load_baseline_dossier,
    validate_baseline_selection,
    validate_baseline_dossier,
)
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


class BaselineDossierTests(unittest.TestCase):
    def _unqualified_dossier(self, root: Path) -> dict:
        candidates_dir = root / "candidates"
        candidates_dir.mkdir()
        candidate_ids = ["c_best", "c_naive", "c_null"]
        for candidate_id in candidate_ids:
            (candidates_dir / f"{candidate_id}.md").write_text(candidate_id)
        return {
            "id": "bd_test",
            "created_at": "2026-09-05",
            "query": "test baseline candidates",
            "problem_scope": {"task": "task"},
            "selected": None,
            "candidates_index": [
                {
                    "id": candidate_id,
                    "method": candidate_id,
                    "decision": "unqualified",
                    "reason_tags": ["discovered"],
                    "detail_file": f"candidates/{candidate_id}.md",
                }
                for candidate_id in candidate_ids
            ],
            "source_index": [
                {
                    "id": f"s{index}",
                    "url": f"https://example.com/{candidate_id}",
                    "accessed_at": "2026-09-05",
                    "supports": [candidate_id],
                }
                for index, candidate_id in enumerate(candidate_ids, 1)
            ],
            "refresh_policy": {"required_before": ["promotion"]},
        }

    def _qualification(self, root: Path) -> dict:
        node_dir = root / "tree" / "nodes" / "n_baseline"
        workspace = node_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (node_dir / "node.json").write_text('{"id": "n_baseline"}')
        (node_dir / "experiment_plan.json").write_text(
            '{"plan_id": "p_baseline", "source_files": [{"path": "src/run.py", "content": "print(1)\\n"}]}'
        )
        (node_dir / "worker_report.json").write_text(
            '{"baselines": {"best": 0.5, "naive": 0.5, "null": 0.5}}'
        )
        roles = ["current_best_known", "naive", "random_or_null"]
        candidates = ["c_best", "c_naive", "c_null"]
        return {
            "dossier_id": "bd_test",
            "assignments": [
                {
                    "role": role,
                    "candidate_id": candidate,
                    "source_ids": [f"s{index}"],
                    "comparison": {
                        "task_id": "task-v1",
                        "dataset_id": "data-v1",
                        "split_id": "test-v1",
                        "budget_id": "budget-v1",
                        "metric_id": "accuracy",
                    },
                    "implementation": {
                        "kind": "local_source",
                        "identifier": "src/run.py",
                        "version": hashlib.sha256(b"print(1)\n").hexdigest(),
                    },
                    "reproducibility_receipt": {
                        "node_path": "tree/nodes/n_baseline/node.json",
                        "experiment_plan_path": "tree/nodes/n_baseline/experiment_plan.json",
                        "worker_report_path": "tree/nodes/n_baseline/worker_report.json",
                        "node_dir": "tree/nodes/n_baseline",
                        "tree_dir": "tree",
                        "metric_id": "accuracy",
                        "baseline_key": {"current_best_known": "best", "naive": "naive", "random_or_null": "null"}[role],
                        "metric_value": 0.5,
                    },
                }
                for index, (role, candidate) in enumerate(zip(roles, candidates), 1)
            ],
        }

    def test_packaged_dossier_is_available_without_operator_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dossier = load_baseline_dossier(
                Path(tmp),
                "bd_agent_harness_20260523",
            )

        self.assertEqual(
            dossier["selected"]["candidate_id"],
            "c1_sakana_ai_scientist_v2",
        )

    def test_operator_dossier_with_same_id_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            packaged_dir = dossier_path(repo, "bd_agent_harness_20260523").parent
            operator_dir = repo / "memory" / "baseline_dossiers"
            shutil.copytree(packaged_dir, operator_dir)
            operator_path = operator_dir / "bd_agent_harness_20260523.yaml"
            operator_path.write_text(
                operator_path.read_text(encoding="utf-8").replace(
                    'query: "best-known method for automated AI research harness with agentic tree search"',
                    'query: "operator-local override"',
                ),
                encoding="utf-8",
            )

            dossier = load_baseline_dossier(repo, "bd_agent_harness_20260523")

        self.assertEqual(dossier["query"], "operator-local override")

    def test_existing_dossier_is_schema_valid_and_complete(self) -> None:
        dossier = load_baseline_dossier(REPO_ROOT, "bd_agent_harness_20260523")

        validate_named_schema("baseline_dossier", dossier)
        self.assertEqual(dossier["selected"]["candidate_id"], "c1_sakana_ai_scientist_v2")

    def test_resolution_report_is_dry_run_and_records_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "baseline_resolution.md"
            report = build_baseline_resolution_report(
                REPO_ROOT,
                "bd_agent_harness_20260523",
                output,
            )

            self.assertFalse(report["webfetch_executed"])
            self.assertEqual(report["naive"], "c2_direct_api_port")
            self.assertEqual(report["random_or_null"], "c3_no_orchestrated_harness")
            self.assertTrue(output.exists())

    def test_selected_candidate_must_exist(self) -> None:
        dossier = copy.deepcopy(load_baseline_dossier(REPO_ROOT, "bd_agent_harness_20260523"))
        dossier["selected"]["candidate_id"] = "missing"

        with self.assertRaisesRegex(BaselineDossierError, "selected candidate_id"):
            validate_baseline_dossier(REPO_ROOT, dossier)

    def test_required_baseline_decisions_must_exist(self) -> None:
        dossier = copy.deepcopy(load_baseline_dossier(REPO_ROOT, "bd_agent_harness_20260523"))
        dossier["candidates_index"] = [
            candidate
            for candidate in dossier["candidates_index"]
            if candidate["decision"] != "selected_as_naive"
        ]

        with self.assertRaisesRegex(BaselineDossierError, "selected_as_naive"):
            validate_baseline_dossier(REPO_ROOT, dossier)

    def test_candidate_detail_file_must_exist(self) -> None:
        dossier = copy.deepcopy(load_baseline_dossier(REPO_ROOT, "bd_agent_harness_20260523"))
        dossier["candidates_index"][0]["detail_file"] = "candidates/missing.md"

        with self.assertRaisesRegex(BaselineDossierError, "detail_file missing"):
            validate_baseline_dossier(REPO_ROOT, dossier)

    def test_source_urls_must_be_http(self) -> None:
        dossier = copy.deepcopy(load_baseline_dossier(REPO_ROOT, "bd_agent_harness_20260523"))
        dossier["source_index"][0]["url"] = "file:///tmp/local"

        with self.assertRaisesRegex(BaselineDossierError, "http"):
            validate_baseline_dossier(REPO_ROOT, dossier)

    def test_unqualified_candidates_require_a_separate_selection_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dossier = self._unqualified_dossier(root)
            validate_baseline_dossier(root, dossier, base_dir=root)
            self.assertIsNone(dossier["selected"])
            self.assertEqual(
                {candidate["decision"] for candidate in dossier["candidates_index"]},
                {"unqualified"},
            )

    def test_selection_requires_comparable_executed_baselines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dossier = self._unqualified_dossier(root)
            qualification = self._qualification(root)
            qualification["assignments"][0]["candidate_id"] = "c_naive"
            qualification["assignments"][0]["source_ids"] = ["s2"]
            qualification["assignments"][1]["candidate_id"] = "c_best"
            qualification["assignments"][1]["source_ids"] = ["s1"]

            with mock.patch(
                "research_harness.orchestrator.strong_result.verify_strong_execution_evidence",
                return_value={
                    "job_manifest_sha256": "1" * 64,
                    "runner_result_sha256": "2" * 64,
                },
            ) as verify:
                result = validate_baseline_selection(
                    root, dossier, qualification, artifact_root=root, dossier_base_dir=root
                )

            self.assertEqual(result["assignments"]["current_best_known"], "c_naive")
            self.assertIn("scientific role suitability", result["qualification_limit"])
            self.assertEqual(verify.call_count, 3)

    def test_selection_rejects_mismatched_comparison_and_runner_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dossier = self._unqualified_dossier(root)
            qualification = self._qualification(root)
            qualification["assignments"][1]["comparison"]["split_id"] = "other"
            with self.assertRaisesRegex(BaselineDossierError, "same task"):
                validate_baseline_selection(
                    root, dossier, qualification, artifact_root=root, dossier_base_dir=root
                )

            qualification = self._qualification(root)
            with mock.patch(
                "research_harness.orchestrator.strong_result.verify_strong_execution_evidence",
                side_effect=ValueError("runner command mismatch"),
            ):
                with self.assertRaisesRegex(BaselineDossierError, "runner evidence"):
                    validate_baseline_selection(
                        root, dossier, qualification, artifact_root=root, dossier_base_dir=root
                    )

    def test_selection_rejects_one_measurement_reused_for_two_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dossier = self._unqualified_dossier(root)
            qualification = self._qualification(root)
            qualification["assignments"][1]["reproducibility_receipt"][
                "baseline_key"
            ] = "best"
            with mock.patch(
                "research_harness.orchestrator.strong_result.verify_strong_execution_evidence",
                return_value={
                    "job_manifest_sha256": "1" * 64,
                    "runner_result_sha256": "2" * 64,
                },
            ), self.assertRaisesRegex(BaselineDossierError, "multiple roles"):
                validate_baseline_selection(
                    root,
                    dossier,
                    qualification,
                    artifact_root=root,
                    dossier_base_dir=root,
                )


if __name__ == "__main__":
    unittest.main()
