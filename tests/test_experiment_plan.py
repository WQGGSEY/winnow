from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.experiment_plan import (
    ExperimentPlanError,
    build_demo_experiment_plan,
    build_job_manifest_from_experiment_plan,
    materialize_experiment_plan,
    validate_experiment_plan,
)
from research_harness.schemas.validator import validate_named_schema


class ExperimentPlanTests(unittest.TestCase):
    def test_demo_experiment_plan_is_schema_valid_and_materializes_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            node = _demo_node()
            plan = build_demo_experiment_plan(node, run_dir)

            validate_named_schema("experiment_plan", plan)
            validate_experiment_plan(node, plan, run_dir)
            source_files = materialize_experiment_plan(node, plan, run_dir)

            self.assertEqual(source_files, ["experiment.py"])
            self.assertEqual(
                {
                    requirement["role"]
                    for requirement in plan["baseline_evidence_requirements"]
                },
                {"current_best_known", "naive", "random_or_null"},
            )
            self.assertTrue((Path(plan["workspace"]) / "experiment.py").is_file())

    def test_job_manifest_is_derived_from_experiment_plan_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            node = _demo_node()
            plan = build_demo_experiment_plan(node, run_dir)

            manifest = build_job_manifest_from_experiment_plan(node, plan, run_dir)

            validate_named_schema("job_manifest", manifest)
            self.assertEqual(manifest["experiment_plan_id"], plan["plan_id"])
            self.assertEqual(manifest["claim_contract"], node["claim_contract"])
            self.assertEqual(
                manifest["baseline_evidence_requirements"],
                plan["baseline_evidence_requirements"],
            )
            self.assertEqual(manifest["source_files"], ["experiment.py"])

    def test_plan_cannot_override_node_claim_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            node = _demo_node()
            plan = copy.deepcopy(build_demo_experiment_plan(node, run_dir))
            plan["mandatory_baselines"] = ["naive: direct port only"]

            with self.assertRaisesRegex(ExperimentPlanError, "mandatory_baselines"):
                validate_experiment_plan(node, plan, run_dir)

    def test_plan_source_paths_must_be_workspace_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            node = _demo_node()
            plan = build_demo_experiment_plan(node, run_dir)
            plan["source_files"][0]["path"] = "/tmp/experiment.py"

            with self.assertRaisesRegex(ExperimentPlanError, "source_files"):
                validate_experiment_plan(node, plan, run_dir)

    def test_plan_must_include_all_mandatory_baseline_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            node = _demo_node()
            plan = build_demo_experiment_plan(node, run_dir)
            plan["baseline_evidence_requirements"] = [
                requirement
                for requirement in plan["baseline_evidence_requirements"]
                if requirement["role"] != "naive"
            ]

            with self.assertRaisesRegex(ExperimentPlanError, "naive"):
                validate_experiment_plan(node, plan, run_dir)


if __name__ == "__main__":
    unittest.main()
