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
from research_harness.orchestrator.live_dispatch import run_live_dispatch_once
from research_harness.orchestrator.live_reduction_apply import (
    LiveReductionApplyError,
    apply_live_reduction,
)
from research_harness.orchestrator.live_reduction_ingest import build_live_reduction_bundle
from research_harness.orchestrator.search_state import validate_search_state
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _auth_status() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["codex", "login", "status"],
        returncode=0,
        stdout="Logged in using ChatGPT\n",
        stderr="",
    )


def _codex_stdout(message: str) -> str:
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "apply-test"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 3, "cached_input_tokens": 0, "output_tokens": 40},
            },
        )
    ) + "\n"


def _search_state(node: dict[str, object]) -> dict[str, object]:
    state = {
        "search_id": "s_live_apply_test",
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
                "reason": "test live apply",
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


def _worker_task_result(
    run_dir: Path,
    node_id: str,
    *,
    verdict: str = "supported",
) -> dict[str, object]:
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
        "summary": "fake live apply worker result",
        "source_patch": None,
        "observed_result": {
            "claim_verdict_candidate": verdict,
            "metrics": {"live_apply_json_contract": 1},
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


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _make_bundle(root: Path, *, node_id: str = "n_live_apply_001") -> tuple[Path, Path]:
    node = _demo_node()
    node["id"] = node_id
    state_path = root / "search_state.json"
    _write_json(state_path, _search_state(node))
    run_dir = root / "dispatch"

    def fake_runner(
        args: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=_codex_stdout(json.dumps(_worker_task_result(run_dir, node_id))),
            stderr="",
        )

    with patch.dict(os.environ, {}, clear=True):
        with patch(
            "research_harness.workers.codex_invoker.subprocess.run",
            return_value=_auth_status(),
        ):
            run_live_dispatch_once(
                REPO_ROOT,
                state_path,
                run_dir=run_dir,
                codex_path="/usr/local/bin/codex",
                billing_ack=True,
                execution_ack=True,
                command_runner=fake_runner,
            )

    bundle = build_live_reduction_bundle(
        REPO_ROOT,
        run_dir / "live_node_dispatch.json",
    )
    return Path(bundle["bundle_path"]), state_path


class LiveReductionApplyTests(unittest.TestCase):
    def test_apply_requires_approval_and_does_not_mutate_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path, state_path = _make_bundle(Path(tmp))
            original_state = json.loads(state_path.read_text(encoding="utf-8"))

            summary = apply_live_reduction(bundle_path, approve=False)

            validate_named_schema("live_reduction_apply_summary", summary)
            self.assertEqual(summary["status"], "blocked_by_missing_approval")
            self.assertEqual(summary["state_mutation"], "forbidden")
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original_state)

    def test_approved_apply_promotes_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path, state_path = _make_bundle(Path(tmp))

            summary = apply_live_reduction(bundle_path, approve=True)

            validate_named_schema("live_reduction_apply_summary", summary)
            self.assertEqual(summary["status"], "applied")
            self.assertEqual(summary["applied_transition"], "promoted")
            self.assertEqual(summary["created_child_ids"], [])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            validate_search_state(state)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["promoted_node_ids"], ["n_live_apply_001"])
            self.assertEqual(state["nodes"][0]["status"], "promoted")
            self.assertEqual(state["frontier"][0]["status"], "done")

    def test_stale_search_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path, state_path = _make_bundle(Path(tmp))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "running"
            _write_json(state_path, state)

            with self.assertRaisesRegex(LiveReductionApplyError, "changed"):
                apply_live_reduction(bundle_path, approve=True)

    def test_approved_apply_closes_legacy_child_branch_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path, state_path = _make_bundle(Path(tmp), node_id="n_live_branch_001")
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            bundle["orchestrator_reduction"]["next_transition"] = "needs_child_branch"
            bundle["orchestrator_reduction"]["child_branch_suggestions"] = [
                {
                    "type": "validity",
                    "reason": "Check live result with a narrower control.",
                    "source": "live_reduction_apply_test",
                }
            ]
            _write_json(bundle_path, bundle)

            summary = apply_live_reduction(bundle_path, approve=True)

            self.assertEqual(summary["status"], "applied")
            self.assertEqual(summary["applied_transition"], "pruned")
            self.assertEqual(summary["created_child_ids"], [])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            validate_search_state(state)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["nodes"][0]["status"], "pruned")
            self.assertEqual(len(state["nodes"]), 1)


if __name__ == "__main__":
    unittest.main()
