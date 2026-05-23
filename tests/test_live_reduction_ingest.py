from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.live_dispatch import (
    build_live_dispatch_plan,
    run_live_dispatch_once,
)
from research_harness.orchestrator.live_reduction_ingest import (
    LiveReductionIngestError,
    build_live_reduction_bundle,
)
from research_harness.orchestrator.search_state import validate_search_state
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _auth_status() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["claude", "auth", "status", "--json"],
        returncode=0,
        stdout=json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "subscriptionType": "max",
            }
        ),
        stderr="",
    )


def _search_state(node: dict[str, object]) -> dict[str, object]:
    state = {
        "search_id": "s_live_reduction_test",
        "status": "initialized",
        "max_depth": 5,
        "max_debug_depth": 2,
        "sunk_cost_policy": "progress_gated",
        "scaleup_policy": "disallow_by_default",
        "frontier": [
            {
                "node_id": node["id"],
                "parent": node.get("parent"),
                "depth": 0,
                "priority": 1.0,
                "stage": node["stage"],
                "status": "queued",
                "reason": "test live reduction",
            }
        ],
        "nodes": [node],
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
        "summary": "fake live reduction worker result",
        "source_patch": None,
        "observed_result": {
            "claim_verdict_candidate": "supported",
            "metrics": {"live_reduction_json_contract": 1},
            "baselines": {"current_best_known": "checked"},
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


class LiveReductionIngestTests(unittest.TestCase):
    def test_builds_reduction_bundle_without_mutating_search_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node = _demo_node()
            node["id"] = "n_live_reduce_001"
            state = _search_state(node)
            original_state = deepcopy(state)
            state_path = root / "search_state.json"
            _write_state(state_path, state)
            run_dir = root / "dispatch"

            def fake_runner(
                args: list[str],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                cli_result = {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "result": json.dumps(_worker_task_result(run_dir, node["id"])),
                    "total_cost_usd": 0.004,
                    "usage": {
                        "input_tokens": 3,
                        "cache_creation_input_tokens": 20,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 40,
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

            dispatch_path = run_dir / "live_node_dispatch.json"
            self.assertEqual(dispatch["status"], "completed")
            bundle = build_live_reduction_bundle(REPO_ROOT, dispatch_path)

            validate_named_schema("live_reduction_bundle", bundle)
            self.assertEqual(bundle["status"], "ready_for_apply")
            self.assertEqual(bundle["node_id"], "n_live_reduce_001")
            self.assertEqual(bundle["state_mutation"], "forbidden")
            self.assertEqual(bundle["memory_mutation"], "forbidden")
            self.assertTrue(bundle["apply_required"])
            self.assertEqual(bundle["orchestrator_reduction"]["next_transition"], "promoted")
            self.assertGreater(len(bundle["critic_reviews"]), 0)
            self.assertTrue(Path(bundle["critic_review_bundle_path"]).exists())
            self.assertTrue(Path(bundle["orchestrator_reduction_path"]).exists())
            self.assertTrue(Path(bundle["bundle_path"]).exists())
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original_state)

    def test_plan_only_dispatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node = _demo_node()
            state_path = root / "search_state.json"
            _write_state(state_path, _search_state(node))

            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "research_harness.workers.claude_code_invoker.subprocess.run",
                    return_value=_auth_status(),
                ):
                    build_live_dispatch_plan(
                        REPO_ROOT,
                        state_path,
                        run_dir=root / "dispatch",
                        claude_path="/usr/local/bin/claude",
                        billing_ack=True,
                    )

            with self.assertRaisesRegex(LiveReductionIngestError, "execute_once"):
                build_live_reduction_bundle(
                    REPO_ROOT,
                    root / "dispatch" / "live_node_dispatch.json",
                )


if __name__ == "__main__":
    unittest.main()
