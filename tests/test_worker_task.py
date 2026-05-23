from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.experiment_plan import build_demo_experiment_plan
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.worker_task import (
    WorkerTaskError,
    build_worker_task,
    validate_worker_task,
    worker_report_from_task_result,
    write_worker_task,
)


class WorkerTaskTests(unittest.TestCase):
    def test_experiment_worker_task_is_schema_valid_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            node = _demo_node()
            workspace = Path(tmp) / "workspace"
            plan = build_demo_experiment_plan(node, Path(tmp) / "run")

            task = write_worker_task(node, workspace, experiment_plan=plan)

            validate_named_schema("worker_task", task)
            self.assertTrue((workspace / "worker_task.json").exists())
            self.assertEqual(
                set(task["allowed_output_kinds"]),
                {"source_patch", "observed_result"},
            )
            self.assertFalse(task["branch_policy"]["may_create_branches"])
            self.assertEqual(
                task["experiment_plan_summary"]["plan_id"],
                plan["plan_id"],
            )

    def test_worker_task_cannot_change_claim_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            node = _demo_node()
            task = build_worker_task(node, Path(tmp) / "workspace")
            task["scope_locks"]["claim_under_test"] = "different claim"

            with self.assertRaisesRegex(WorkerTaskError, "claim_under_test"):
                validate_worker_task(node, task, Path(tmp) / "workspace")

    def test_worker_task_cannot_grant_branch_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            node = _demo_node()
            task = build_worker_task(node, Path(tmp) / "workspace")
            task["branch_policy"]["may_create_branches"] = True

            with self.assertRaisesRegex(WorkerTaskError, "branch creation"):
                validate_worker_task(node, task, Path(tmp) / "workspace")

    def test_task_result_branch_suggestion_does_not_create_branch(self) -> None:
        node = _demo_node()
        with tempfile.TemporaryDirectory() as tmp:
            task = build_worker_task(node, Path(tmp) / "workspace")
            task_result = {
                "task_id": task["task_id"],
                "node_id": node["id"],
                "status": "completed",
                "output_kind": "observed_result",
                "summary": "Observed insufficient evidence.",
                "source_patch": None,
                "observed_result": {
                    "claim_verdict_candidate": "inconclusive",
                    "metrics": {"live_smoke_json_contract": 1},
                    "baselines": {},
                    "disproof_conditions_hit": [],
                    "artifacts": [],
                    "unexpected_observations": [],
                },
                "branch_suggestions": [
                    {
                        "suggested_branch_type": "validity",
                        "description": "Check whether the evidence is sufficient.",
                        "evidence": "Only a smoke metric was observed.",
                    }
                ],
                "scope_check": {
                    "claim_changed": False,
                    "baselines_changed": False,
                    "shared_memory_write_attempted": False,
                    "branch_created": False,
                    "files_written_outside_workspace": False,
                },
            }

            report = worker_report_from_task_result(
                task_result,
                task,
                {"subtype": "success", "is_error": False},
            )

            validate_named_schema("worker_report", report)
            self.assertEqual(report["claim_verdict_candidate"], "inconclusive")
            self.assertEqual(
                report["unexpected_observations"][0]["scope_relation"],
                "branch_suggestion_only",
            )

    def test_task_result_scope_violation_becomes_worker_report_failure(self) -> None:
        node = _demo_node()
        with tempfile.TemporaryDirectory() as tmp:
            task = build_worker_task(node, Path(tmp) / "workspace")
            task_result = {
                "task_id": task["task_id"],
                "node_id": node["id"],
                "status": "completed",
                "output_kind": "observed_result",
                "summary": "Created a branch, which is forbidden.",
                "source_patch": None,
                "observed_result": None,
                "branch_suggestions": [],
                "scope_check": {
                    "claim_changed": False,
                    "baselines_changed": False,
                    "shared_memory_write_attempted": False,
                    "branch_created": True,
                    "files_written_outside_workspace": False,
                },
            }

            report = worker_report_from_task_result(
                task_result,
                task,
                {"subtype": "success", "is_error": False},
            )

            self.assertEqual(report["status"], "invalid_worker_output")
            self.assertEqual(
                report["failure_record_candidate"]["category"],
                "scope_violation",
            )

    def test_runner_job_role_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            node = copy.deepcopy(_demo_node())
            node["runtime_profile"]["worker_type"] = "runner_job"

            with self.assertRaisesRegex(WorkerTaskError, "runner_job"):
                build_worker_task(node, Path(tmp) / "workspace")


if __name__ == "__main__":
    unittest.main()
