from __future__ import annotations

import copy
import tempfile
import unittest
from unittest import mock
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
    def test_source_diagnostics_find_missing_curve_without_executing_module(self) -> None:
        from research_harness.orchestrator.experiment_plan import python_source_diagnostics

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'must_not_execute'
            source = ("import math\nfrom pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n"
                      "def train():\n    curve.append(1)\n")
            diagnostics = python_source_diagnostics([{'path': 'train.py', 'content': source}])
            self.assertFalse(marker.exists())
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]['message'], "undefined name 'curve'")
            self.assertEqual(diagnostics[0]['line'], 5)

    def test_single_baseline_preflight_executes_without_weakening_research_plan(self) -> None:
        import json
        from research_harness.runner.baseline_preflight import execute_baseline_preflight

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = repo / "thread"
            tree = tdir / "production/tree"
            tree.mkdir(parents=True)
            envelope = {"compute_budget": {"max_runner_seconds_per_node": 3600, "max_total_node_hours": 1}}
            (tdir / "production/feasibility_envelope.json").write_text(json.dumps(envelope))
            node = _demo_node()
            plan = build_demo_experiment_plan(node, tree)
            requirement = plan["baseline_evidence_requirements"][2]
            plan["baseline_evidence_requirements"] = [requirement]
            plan["resources"]["timeout_sec"] = 3600
            payload = {"metrics": {requirement["metric_key"]: 0.5}, "baselines": {requirement["baseline_key"]: 0.5}, "claim_verdict_candidate": "supported"}
            plan["source_files"] = [{"path": "experiment.py", "purpose": "receipt integration fixture", "content": "from pathlib import Path\nPath('artifacts').mkdir(exist_ok=True)\nPath('artifacts/metrics.json').write_text(" + repr(json.dumps(payload)) + ")\n"}]
            plan["expected_outputs"]["metrics_files"] = ["artifacts/metrics.json"]
            with self.assertRaisesRegex(ExperimentPlanError, "missing required baseline"):
                validate_experiment_plan(node, plan, tree)
            first = execute_baseline_preflight(repo, tdir, node=node, plan=plan, role=requirement["role"], settings={})
            self.assertEqual(first["status"], "executed")
            self.assertFalse(first["scientific_approval"])
            self.assertEqual(set(first["worker_report"]["baselines"]), {requirement["baseline_key"]})
            receipt_file = tree / first["reproducibility_receipt"]["node_dir"] / "workspace/runner_result.json"
            stamp = receipt_file.stat().st_mtime_ns
            second = execute_baseline_preflight(repo, tdir, node=node, plan=plan, role=requirement["role"], settings={})
            self.assertEqual(first, second)
            self.assertEqual(receipt_file.stat().st_mtime_ns, stamp)
            report_path = receipt_file.parent.parent / 'worker_report.json'
            report_bytes = report_path.read_bytes()
            extra_baseline_report = json.loads(report_bytes)
            extra_baseline_report['baselines']['unqualified_control'] = 0.4
            report_path.write_text(json.dumps(extra_baseline_report))
            with self.assertRaisesRegex(ValueError, 'only its actually executed baseline key'):
                execute_baseline_preflight(repo, tdir, node=node, plan=plan, role=requirement['role'], settings={})
            report_path.write_bytes(report_bytes)
            import research_harness.mcp_server as srv
            with mock.patch.object(srv, '_thread_dir', return_value=tdir):
                state = srv.handle_get_research_state({'thread_id': 'test'}, {})
                (tdir / 'thread.json').write_text(json.dumps({'current_phase': 'production'}))
                current = srv.handle_get_research_state({'thread_id': 'test'}, {})
                full = srv.handle_get_research_state({'thread_id': 'test', 'view': 'full'}, {})
                self.assertEqual(current['view'], 'current')
                self.assertEqual(current['baseline_preparation'], state['baseline_preparation'])
                self.assertEqual(current['compute_budget'], envelope['compute_budget'])
                self.assertEqual(current['thread_dir'], str(tdir.resolve()))
                self.assertNotIn('baseline_analysis_md', current)
                self.assertIn('baseline_analysis_md', full)
            preparation = state['baseline_preparation']
            self.assertEqual(preparation['attempt_count'], 1)
            self.assertEqual(preparation['recent_attempts'][0]['status'], 'comparison_failed')
            self.assertFalse(preparation['qualification_recorded'])
            self.assertIsNone(state['search_state'])

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
