from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from research_harness.orchestrator.experiment_plan import build_demo_experiment_plan
from research_harness.orchestrator.tree_search import run_mock_tree_search
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _metrics_plan(
    node: dict[str, Any],
    run_dir: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    plan = build_demo_experiment_plan(node, run_dir)
    payload_json = json.dumps(payload, sort_keys=True)
    plan["source_files"][0]["content"] = (
        "\n".join(
            [
                "from pathlib import Path",
                "artifacts = Path('artifacts')",
                "artifacts.mkdir(exist_ok=True)",
                f"(artifacts / 'metrics.json').write_text({payload_json!r} + '\\n')",
                "",
            ]
        )
    )
    return plan


def failing_experiment_plan(node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    plan = build_demo_experiment_plan(node, run_dir)
    plan["source_files"][0]["content"] = "raise SystemExit(4)\n"
    return plan


def inconclusive_experiment_plan(node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return _metrics_plan(
        node,
        run_dir,
        {
            "metrics": {"schema_validity": 1.0},
            "claim_verdict_candidate": "inconclusive",
            "unexpected_observations": [
                {
                    "observation": "The smoke evidence is insufficient.",
                    "evidence": "Only schema_validity was measured.",
                    "suggested_branch_type": "validity",
                    "scope_relation": "operational_blocker",
                }
            ],
        },
    )


def missing_metrics_plan(node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    plan = build_demo_experiment_plan(node, run_dir)
    plan["source_files"][0]["content"] = "print('no metrics written')\n"
    return plan


def invalid_json_metrics_plan(node: dict[str, Any], run_dir: Path, payload: str = "not-json") -> dict[str, Any]:
    plan = build_demo_experiment_plan(node, run_dir)
    plan["source_files"][0]["content"] = "\n".join(
        [
            "from pathlib import Path",
            "artifacts = Path('artifacts')",
            "artifacts.mkdir(exist_ok=True)",
            f"(artifacts / 'metrics.json').write_text({payload!r})",
            "",
        ]
    )
    return plan


def baseline_dominated_plan(node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return _metrics_plan(
        node,
        run_dir,
        {
            "metrics": {
                "bounded_worker_success_rate": 0.91,
                "schema_validity": 1.0,
            },
            "baselines": {
                "current_best_known": 0.80,
                "naive_direct_port": 0.95,
                "random_or_null": 0.05,
            },
            "claim_verdict_candidate": "supported",
            "unexpected_observations": [],
        },
    )


class TreeSearchTests(unittest.TestCase):
    def test_mock_tree_search_promotes_root_with_default_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_mock_tree_search(REPO_ROOT, Path(tmp) / "tree")

            state = result["search_state"]
            validate_named_schema("search_state", state)
            self.assertEqual(state["status"], "completed")
            self.assertIn("n_demo_001", state["promoted_node_ids"])
            self.assertTrue((Path(tmp) / "tree" / "search_state.json").exists())

    def test_tree_search_writes_runner_artifacts_for_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "tree"
            result = run_mock_tree_search(
                REPO_ROOT,
                run_dir,
            )

            artifact = result["artifacts"][0]
            self.assertEqual(artifact["runner_status"], "completed")
            self.assertEqual(
                artifact["experiment_plan_path"],
                "nodes/n_demo_001/experiment_plan.json",
            )
            # With the Professor-generated thread template, source_files
            # now include the per-node experiment.py plus the shared _lib/
            # modules. Just assert the experiment script is in there.
            self.assertIn(
                "nodes/n_demo_001/workspace/src/experiment.py",
                artifact["source_files"],
            )
            self.assertEqual(
                artifact["metrics_evidence_paths"],
                ["nodes/n_demo_001/workspace/artifacts/metrics.json"],
            )
            runner_result_path = run_dir / artifact["runner_result_path"]
            runner_result = json.loads(runner_result_path.read_text(encoding="utf-8"))
            validate_named_schema("runner_result", runner_result)
            self.assertEqual(runner_result["status"], "completed")
            # Professor-generated plans use task_class="eval"; legacy
            # fallback was "smoke_test". Just assert the id matches the node.
            self.assertTrue(runner_result["experiment_plan_id"].startswith("plan_n_demo_001_"))
            # source_files now lists the Professor-emitted experiment.py
            # plus the shared _lib/ modules; assert experiment.py is present.
            expected_experiment = str(
                (
                    run_dir
                    / "nodes"
                    / "n_demo_001"
                    / "workspace"
                    / "src"
                    / "experiment.py"
                ).resolve()
            )
            self.assertIn(expected_experiment, runner_result["source_files"])
            worker_report = json.loads(
                (run_dir / "nodes" / "n_demo_001" / "worker_report.json").read_text(
                    encoding="utf-8"
                )
            )
            validate_named_schema("worker_report", worker_report)
            # Professor-generated experiment emits a `headline_metric` instead
            # of the demo's `schema_validity`; baseline check still must pass.
            self.assertIn("headline_metric", worker_report["metrics"])
            self.assertEqual(worker_report["baseline_evidence_status"]["overall"], "passed")
            node = next(
                node
                for node in result["search_state"]["nodes"]
                if node["id"] == artifact["node_id"]
            )
            self.assertIn(artifact["runner_result_path"], node["outputs"]["artifacts"])
            self.assertIn(artifact["experiment_plan_path"], node["outputs"]["artifacts"])
            self.assertIn(artifact["source_files"][0], node["outputs"]["artifacts"])

    def test_runner_failure_becomes_non_promotable_worker_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "tree"
            result = run_mock_tree_search(
                REPO_ROOT,
                run_dir,
                max_steps=1,
                experiment_plan_builder=failing_experiment_plan,
                record_runner_failures=False,
            )

            artifact = result["artifacts"][0]
            self.assertEqual(artifact["runner_status"], "failed")
            self.assertIsNone(artifact["runner_failure_memory"])
            runner_result = json.loads(
                (run_dir / artifact["runner_result_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(runner_result["exit_code"], 4)
            worker_report = json.loads(
                (run_dir / "nodes" / "n_demo_001" / "worker_report.json").read_text(
                    encoding="utf-8"
                )
            )
            validate_named_schema("worker_report", worker_report)
            self.assertEqual(worker_report["status"], "failed")
            self.assertEqual(worker_report["claim_verdict_candidate"], "not_evaluable")
            self.assertNotIn("n_demo_001", result["search_state"]["promoted_node_ids"])

    def test_missing_metrics_file_becomes_invalid_experiment_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "tree"
            result = run_mock_tree_search(
                REPO_ROOT,
                run_dir,
                max_steps=1,
                experiment_plan_builder=missing_metrics_plan,
                record_runner_failures=False,
            )

            artifact = result["artifacts"][0]
            self.assertEqual(artifact["runner_status"], "completed")
            self.assertEqual(artifact["worker_status"], "failed")
            self.assertIsNone(artifact["worker_failure_memory"])
            worker_report = json.loads(
                (run_dir / "nodes" / "n_demo_001" / "worker_report.json").read_text(
                    encoding="utf-8"
                )
            )
            validate_named_schema("worker_report", worker_report)
            self.assertEqual(
                worker_report["failure_record_candidate"]["category"],
                "invalid_experiment",
            )
            self.assertIn(
                "missing_metrics_file",
                worker_report["failure_record_candidate"]["tags"],
            )

    def test_invalid_metrics_json_becomes_invalid_experiment_report(self) -> None:
        for payload in ('not-json', '{"metrics":{"value":NaN}}', '{"metrics":{"value":Infinity}}', '{"metrics":{"value":1e999}}'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp) / "tree"
                result = run_mock_tree_search(
                    REPO_ROOT,
                    run_dir,
                    max_steps=1,
                    experiment_plan_builder=lambda node, directory: invalid_json_metrics_plan(node, directory, payload),
                    record_runner_failures=False,
                )

                artifact = result["artifacts"][0]
                self.assertEqual(artifact["runner_status"], "completed")
                self.assertEqual(artifact["worker_status"], "failed")
                worker_report = json.loads(
                    (run_dir / "nodes" / "n_demo_001" / "worker_report.json").read_text(
                        encoding="utf-8"
                    )
                )
                validate_named_schema("worker_report", worker_report)
                self.assertIn(
                    "invalid_metrics_json",
                    worker_report["failure_record_candidate"]["tags"],
                )

    def test_baseline_dominated_supported_metric_becomes_negative_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "tree"
            result = run_mock_tree_search(
                REPO_ROOT,
                run_dir,
                max_steps=1,
                experiment_plan_builder=baseline_dominated_plan,
                record_runner_failures=False,
            )

            worker_report = json.loads(
                (run_dir / "nodes" / "n_demo_001" / "worker_report.json").read_text(
                    encoding="utf-8"
                )
            )
            validate_named_schema("worker_report", worker_report)
            self.assertEqual(worker_report["status"], "completed")
            self.assertEqual(worker_report["claim_verdict_candidate"], "contradicted")
            self.assertEqual(worker_report["baseline_evidence_status"]["overall"], "failed")
            self.assertEqual(
                worker_report["failure_record_candidate"]["category"],
                "negative_result",
            )
            self.assertIn(
                "baseline_dominated_success",
                worker_report["failure_record_candidate"]["tags"],
            )
            self.assertIn(
                "naive_direct_port",
                worker_report["failure_record_candidate"]["tags"],
            )
            self.assertNotIn("n_demo_001", result["search_state"]["promoted_node_ids"])
            reduction = json.loads(
                (
                    run_dir
                    / "nodes"
                    / "n_demo_001"
                    / "orchestrator_reduction.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(reduction["final_verdict"], "contradicted")
            self.assertEqual(reduction["next_transition"], "pruned")
            self.assertEqual(reduction["child_branch_suggestions"], [])

    def test_inconclusive_metrics_close_direction_without_child_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_mock_tree_search(
                REPO_ROOT,
                Path(tmp) / "tree",
                experiment_plan_builder=inconclusive_experiment_plan,
                max_steps=1,
                record_runner_failures=False,
            )

            state = result["search_state"]
            child_ids = [
                node["id"]
                for node in state["nodes"]
                if node.get("parent") == "n_demo_001"
            ]
            self.assertEqual(child_ids, [])
            self.assertEqual(state["status"], "completed")
            written = json.loads((Path(tmp) / "tree" / "search_state.json").read_text())
            self.assertEqual(written["status"], "completed")

    def test_live_backend_name_is_rejected_by_tree_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "live_gate"):
                run_mock_tree_search(
                    REPO_ROOT,
                    Path(tmp) / "tree",
                    backend_name="codex_live",
                )


if __name__ == "__main__":
    unittest.main()
