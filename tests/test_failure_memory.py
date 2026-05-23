from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from research_harness.config import load_yaml
from research_harness.memory.failure_memory import (
    FailureMemoryError,
    record_failure_candidate,
    record_runner_failure_candidate,
)
from research_harness.orchestrator.demo import _demo_node


REPO_ROOT = Path(__file__).resolve().parents[1]


class FailureMemoryTests(unittest.TestCase):
    def _temp_repo(self, tmp: str) -> Path:
        repo = Path(tmp)
        (repo / "memory").mkdir()
        shutil.copytree(REPO_ROOT / "memory" / "failures", repo / "memory" / "failures")
        return repo

    def _blocked_report(self) -> dict[str, object]:
        return {
            "node_id": "n_demo_001",
            "status": "blocked_permission",
            "claim_verdict_candidate": "not_evaluable",
            "metrics": {"claude_cli_reported_total_cost_usd": 0.01},
            "baselines": {},
            "disproof_conditions_hit": [],
            "artifacts": [],
            "unexpected_observations": [
                {
                    "observation": "Claude CLI did not produce an acceptable worker_report.",
                    "evidence": "Claude CLI reported permission denials.",
                    "suggested_branch_type": None,
                    "scope_relation": "operational_blocker",
                }
            ],
            "failure_record_candidate": {
                "category": "invalid_experiment",
                "tags": ["claude_cli", "permission_denied"],
                "reason": "Claude CLI reported permission denials.",
            },
        }

    def test_records_failure_file_index_entry_and_one_line_lesson(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._temp_repo(tmp)
            before = load_yaml(repo / "memory" / "failures" / "index.yaml")
            initial_count = len(before["categories"]["invalid_experiment"]["files"])

            result = record_failure_candidate(
                repo,
                _demo_node(),
                self._blocked_report(),
                source_artifact="runs/manual_live_smoke/claude_stdout.json",
            )

            self.assertIsNotNone(result)
            self.assertTrue(result.record_path.exists())
            self.assertNotIn("\n", result.lesson)
            self.assertLessEqual(len(result.lesson), 240)
            index = load_yaml(result.index_path)
            files = index["categories"]["invalid_experiment"]["files"]
            self.assertEqual(len(files), initial_count + 1)
            self.assertTrue(str(files[-1]).startswith("invalid_experiment/"))

    def test_unknown_category_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._temp_repo(tmp)
            report = self._blocked_report()
            report["failure_record_candidate"]["category"] = "unknown"

            with self.assertRaisesRegex(FailureMemoryError, "unknown failure category"):
                record_failure_candidate(repo, _demo_node(), report)

    def test_missing_candidate_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._temp_repo(tmp)
            report = self._blocked_report()
            report["failure_record_candidate"] = None

            self.assertIsNone(record_failure_candidate(repo, _demo_node(), report))

    def test_failure_memory_module_cli_records_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._temp_repo(tmp)
            node_path = repo / "node.json"
            report_path = repo / "worker_report.json"
            node_path.write_text(json.dumps(_demo_node()), encoding="utf-8")
            report_path.write_text(json.dumps(self._blocked_report()), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "research_harness.memory.failure_memory",
                    "--repo-root",
                    str(repo),
                    "--node",
                    str(node_path),
                    "--worker-report",
                    str(report_path),
                    "--source-artifact",
                    "runs/manual_live_smoke/claude_stdout.json",
                ],
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertTrue(summary["recorded"])
            self.assertTrue(Path(summary["record_path"]).exists())

    def test_records_runner_failure_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._temp_repo(tmp)
            runner_result = {
                "job_id": "job_1",
                "experiment_plan_id": "plan_1",
                "node_id": "n_demo_001",
                "status": "failed",
                "exit_code": 2,
                "elapsed_sec": 0.1,
                "timeout_sec": 60,
                "workspace": str(repo),
                "source_files": [str(repo / "experiment.py")],
                "command": ["python", "-c", "raise SystemExit(2)"],
                "stdout_path": str(repo / "stdout.log"),
                "stderr_path": str(repo / "stderr.log"),
                "failure_record_candidate": {
                    "category": "invalid_experiment",
                    "tags": ["runner", "failed", "smoke_test"],
                    "reason": "runner command exited with code 2",
                },
            }

            result = record_runner_failure_candidate(repo, _demo_node(), runner_result)

            self.assertIsNotNone(result)
            self.assertTrue(result.record_path.exists())


if __name__ == "__main__":
    unittest.main()
