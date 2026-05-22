from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from research_harness.memory.baseline_dossier import (
    BaselineDossierError,
    build_baseline_resolution_report,
    load_baseline_dossier,
    validate_baseline_dossier,
)
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


class BaselineDossierTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

