from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.live_dispatch import (
    LiveDispatchError,
    build_live_dispatch_plan,
    run_live_dispatch_once,
    select_live_dispatch_target,
)
from research_harness.orchestrator.search_state import validate_search_state
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _auth_status(subscription_type: str = "max") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["claude", "status", "--format", "json"],
        returncode=0,
        stdout=json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "subscriptionType": subscription_type,
            }
        ),
        stderr="",
    )


def _node(node_id: str) -> dict[str, object]:
    node = _demo_node()
    node["id"] = node_id
    return node


def _frontier_item(node_id: str, *, priority: float, status: str = "queued") -> dict[str, object]:
    return {
        "node_id": node_id,
        "parent": None,
        "depth": 0,
        "priority": priority,
        "stage": "promotion",
        "status": status,
        "reason": "test frontier",
    }


def _search_state() -> dict[str, object]:
    state = {
        "search_id": "s_live_dispatch_test",
        "status": "initialized",
        "max_depth": 5,
        "max_debug_depth": 2,
        "sunk_cost_policy": "progress_gated",
        "scaleup_policy": "disallow_by_default",
        "frontier": [
            _frontier_item("n_low_priority", priority=0.2),
            _frontier_item("n_high_priority", priority=0.9),
        ],
        "nodes": [
            _node("n_low_priority"),
            _node("n_high_priority"),
        ],
        "completed_node_ids": [],
        "promoted_node_ids": [],
        "pruned_node_ids": [],
        "transitions": [],
    }
    validate_search_state(state)
    return state


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _worker_task_result(run_dir: Path, node_id: str) -> dict[str, object]:
    worker_task = json.loads(
        (
            run_dir
            / "nodes"
            / node_id
            / "workspace"
            / "worker_task.json"
        ).read_text(encoding="utf-8")
    )
    return {
        "task_id": worker_task["task_id"],
        "node_id": node_id,
        "status": "completed",
        "output_kind": "observed_result",
        "summary": "live dispatch fake run only",
        "source_patch": None,
        "observed_result": {
            "claim_verdict_candidate": "not_evaluable",
            "metrics": {"live_dispatch_json_contract": 1},
            "baselines": {},
            "disproof_conditions_hit": [],
            "artifacts": [],
            "unexpected_observations": [],
        },
        "branch_suggestions": [],
        "scope_check": {
            "claim_changed": False,
            "baselines_changed": False,
            "shared_memory_write_attempted": False,
            "branch_created": False,
            "files_written_outside_workspace": False,
        },
    }


class LiveDispatchTests(unittest.TestCase):
    def test_select_live_dispatch_target_prefers_highest_priority_without_mutation(self) -> None:
        state = _search_state()
        before = deepcopy(state)

        target = select_live_dispatch_target(state)

        self.assertEqual(target["node"]["id"], "n_high_priority")
        self.assertEqual(target["frontier_item"]["priority"], 0.9)
        self.assertEqual(state, before)

    def test_non_queued_explicit_node_is_rejected(self) -> None:
        state = _search_state()
        state["frontier"][1]["status"] = "done"
        validate_search_state(state)

        with self.assertRaisesRegex(LiveDispatchError, "not queued"):
            select_live_dispatch_target(state, node_id="n_high_priority")

    def test_build_dispatch_plan_writes_gated_plan_for_requested_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "search_state.json"
            state = _search_state()
            original_state_text = json.dumps(state, indent=2) + "\n"
            state_path.write_text(original_state_text, encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "research_harness.workers.claude_code_invoker.subprocess.run",
                    return_value=_auth_status(),
                ):
                    dispatch = build_live_dispatch_plan(
                        REPO_ROOT,
                        state_path,
                        run_dir=root / "dispatch",
                        node_id="n_low_priority",
                        claude_path="/usr/local/bin/claude",
                        billing_ack=True,
                    )

            validate_named_schema("live_node_dispatch", dispatch)
            self.assertEqual(dispatch["mode"], "plan")
            self.assertEqual(dispatch["status"], "ready_to_manually_run")
            self.assertEqual(dispatch["node_id"], "n_low_priority")
            self.assertEqual(dispatch["tree_search_mutation"], "forbidden")
            self.assertTrue(Path(dispatch["live_plan_path"]).exists())
            self.assertTrue((root / "dispatch" / "live_node_dispatch.json").exists())
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state_text)

    def test_run_dispatch_once_blocks_without_execution_ack_before_claude_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "search_state.json"
            _write_state(state_path, _search_state())

            def fail_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                raise AssertionError("Claude runner must not be called without execution ack")

            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "research_harness.workers.claude_code_invoker.subprocess.run",
                    return_value=_auth_status(),
                ):
                    dispatch = run_live_dispatch_once(
                        REPO_ROOT,
                        state_path,
                        run_dir=root / "dispatch",
                        node_id="n_high_priority",
                        claude_path="/usr/local/bin/claude",
                        billing_ack=True,
                        execution_ack=False,
                        command_runner=fail_runner,
                    )

            validate_named_schema("live_node_dispatch", dispatch)
            self.assertEqual(dispatch["mode"], "execute_once")
            self.assertEqual(dispatch["status"], "blocked_by_execution_ack")
            self.assertEqual(dispatch["live_summary_status"], "blocked_by_execution_ack")
            self.assertTrue(Path(dispatch["live_summary_path"]).exists())

    def test_run_dispatch_once_executes_fake_claude_in_selected_node_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "dispatch"
            state_path = root / "search_state.json"
            state = _search_state()
            original_state = deepcopy(state)
            _write_state(state_path, state)

            def fake_runner(
                args: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                self.assertIn("--system-prompt", args)
                self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
                cli_result = {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "result": json.dumps(
                        _worker_task_result(run_dir, "n_high_priority")
                    ),
                    "total_cost_usd": 0.004,
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 20,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 30,
                    },
                    "modelUsage": {"claude-sonnet-test": {"costUSD": 0.004}},
                    "permission_denials": [],
                }
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=json.dumps(cli_result),
                    stderr="",
                )

            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "research_harness.workers.claude_code_invoker.subprocess.run",
                    return_value=_auth_status(),
                ):
                    dispatch = run_live_dispatch_once(
                        REPO_ROOT,
                        state_path,
                        run_dir=run_dir,
                        claude_path="/usr/local/bin/claude",
                        billing_ack=True,
                        execution_ack=True,
                        command_runner=fake_runner,
                    )

            validate_named_schema("live_node_dispatch", dispatch)
            self.assertEqual(dispatch["status"], "completed")
            self.assertEqual(dispatch["node_id"], "n_high_priority")
            self.assertEqual(dispatch["live_summary_status"], "completed")
            self.assertTrue((run_dir / "live_worker_run_summary.json").exists())
            self.assertTrue(
                (
                    run_dir
                    / "nodes"
                    / "n_high_priority"
                    / "workspace"
                    / "worker_report.json"
                ).exists()
            )
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original_state)

    def test_cli_no_queued_node_reports_json_error_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _search_state()
            for item in state["frontier"]:
                item["status"] = "done"
            validate_search_state(state)
            state_path = Path(tmp) / "search_state.json"
            _write_state(state_path, state)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "research_harness.orchestrator.live_dispatch",
                    "--search-state",
                    str(state_path),
                ],
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["type"], "live_node_dispatch_error")
            self.assertEqual(payload["status"], "blocked_no_dispatch_target")
            self.assertIn("no queued frontier item", payload["error"])
            self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
