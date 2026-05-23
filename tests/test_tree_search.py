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


def invalid_json_metrics_plan(node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    plan = build_demo_experiment_plan(node, run_dir)
    plan["source_files"][0]["content"] = "\n".join(
        [
            "from pathlib import Path",
            "artifacts = Path('artifacts')",
            "artifacts.mkdir(exist_ok=True)",
            "(artifacts / 'metrics.json').write_text('not-json\\n')",
            "",
        ]
    )
    return plan


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
            self.assertEqual(
                artifact["source_files"],
                ["nodes/n_demo_001/workspace/experiment.py"],
            )
            self.assertEqual(
                artifact["metrics_evidence_paths"],
                ["nodes/n_demo_001/workspace/artifacts/metrics.json"],
            )
            runner_result_path = run_dir / artifact["runner_result_path"]
            runner_result = json.loads(runner_result_path.read_text(encoding="utf-8"))
            validate_named_schema("runner_result", runner_result)
            self.assertEqual(runner_result["status"], "completed")
            self.assertEqual(runner_result["experiment_plan_id"], "plan_n_demo_001_smoke")
            self.assertEqual(
                runner_result["source_files"],
                [
                    str(
                        (
                            run_dir
                            / "nodes"
                            / "n_demo_001"
                            / "workspace"
                            / "experiment.py"
                        ).resolve()
                    )
                ],
            )
            worker_report = json.loads(
                (run_dir / "nodes" / "n_demo_001" / "worker_report.json").read_text(
                    encoding="utf-8"
                )
            )
            validate_named_schema("worker_report", worker_report)
            self.assertEqual(worker_report["metrics"]["schema_validity"], 1.0)
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
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "tree"
            result = run_mock_tree_search(
                REPO_ROOT,
                run_dir,
                max_steps=1,
                experiment_plan_builder=invalid_json_metrics_plan,
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

    def test_inconclusive_metrics_open_child_branch_until_step_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_mock_tree_search(
                REPO_ROOT,
                Path(tmp) / "tree",
                experiment_plan_builder=inconclusive_experiment_plan,
                max_steps=1,
            )

            state = result["search_state"]
            child_ids = [
                node["id"]
                for node in state["nodes"]
                if node.get("parent") == "n_demo_001"
            ]
            self.assertGreaterEqual(len(child_ids), 1)
            self.assertEqual(state["status"], "blocked")
            written = json.loads((Path(tmp) / "tree" / "search_state.json").read_text())
            self.assertEqual(written["status"], "blocked")

    def test_live_backend_name_is_rejected_by_tree_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "live_gate"):
                run_mock_tree_search(
                    REPO_ROOT,
                    Path(tmp) / "tree",
                    backend_name="claude_code_live",
                )


if __name__ == "__main__":
    unittest.main()
