"""End-to-end smoke for the Sakana-shaped treesearch mirror.

These tests prove:
  - Journal is a faithful claim-typed view onto search_state.
  - ParallelAgent.step() advances one node and respects type filters.
  - AgentManager.run() walks the 4 claim-typed stages and returns a
    summary with stage_history + coverage_by_node_type.

The mock backend / deterministic LocalRunner is the only executor;
live Claude stays off-tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.search_state import (
    initialize_search_state,
    search_policy_from_config,
)
from research_harness.orchestrator.treesearch import (
    AgentManager,
    Journal,
    Node,
    ParallelAgent,
    Stage,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_state():
    policy = search_policy_from_config(REPO_ROOT)
    state = initialize_search_state(
        search_id="s_test_mirror",
        root_node=_demo_node(),
        policy=policy,
    )
    state["status"] = "running"
    return state


def test_journal_views_search_state():
    state = _bootstrap_state()
    journal = Journal.from_search_state(state)

    assert len(journal) == 1
    root = journal[0]
    assert isinstance(root, Node)
    assert root.node_type == "capability"
    assert root.parent is None
    assert root.is_promoted is False
    assert root.is_buggy is False
    assert journal.draft_nodes[0].id == root.id
    assert journal.get_best_node() is None  # nothing promoted yet


def test_parallel_agent_step_runs_one_node(tmp_path: Path):
    state = _bootstrap_state()
    journal = Journal.from_search_state(state)
    from research_harness.config import load_settings

    agent = ParallelAgent(
        journal=journal,
        repo_root=REPO_ROOT,
        run_dir=tmp_path,
        settings=load_settings(REPO_ROOT),
        num_workers=1,
        admits_node_types={"capability"},
    )
    summaries = agent.step()
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["node_type"] == "capability"
    assert summary["worker_status"] in {"completed", "failed", "invalid_worker_output"}
    assert summary["next_transition"] in {
        "promoted",
        "pruned",
    }
    # Journal recorded the reduction.
    assert summary["node_id"] in journal.reductions


def test_parallel_agent_filters_by_node_type(tmp_path: Path):
    state = _bootstrap_state()
    journal = Journal.from_search_state(state)
    from research_harness.config import load_settings

    # No queued items of type "mechanism" → step returns empty.
    agent = ParallelAgent(
        journal=journal,
        repo_root=REPO_ROOT,
        run_dir=tmp_path,
        settings=load_settings(REPO_ROOT),
        num_workers=1,
        admits_node_types={"mechanism"},
    )
    summaries = agent.step()
    assert summaries == []


def test_agent_manager_runs_four_stages(tmp_path: Path):
    cfg = {
        "agent": {"steps": 2, "num_workers": 1},
        "stage_configs": {
            "scope_pinning": {"max_iterations": 1, "num_workers": 1},
            "baseline_evidence": {"max_iterations": 2, "num_workers": 1},
            "mechanism_or_necessity": {"max_iterations": 2, "num_workers": 1},
            "boundary_ablation": {"max_iterations": 2, "num_workers": 1},
        },
    }
    task_desc = {
        "plan_id": "test_mirror_smoke",
        "claim_under_test": _demo_node()["claim_contract"]["claim_under_test"],
    }
    manager = AgentManager(
        task_desc=task_desc,
        cfg=cfg,
        workspace_dir=tmp_path,
        repo_root=REPO_ROOT,
        root_node=_demo_node(),
    )
    assert [s.name for s in manager.stages] == [
        "scope_pinning",
        "baseline_evidence",
        "mechanism_or_necessity",
        "boundary_ablation",
    ]

    seen_steps: list[dict] = []
    seen_stages: list[str] = []
    summary = manager.run(
        step_callback=seen_steps.append,
        stage_callback=lambda stage, _summaries: seen_stages.append(stage.name),
    )

    # Every stage was visited at least once. With cycling, each stage may be
    # invoked multiple times until the queue drains; the first cycle visits
    # all four in order.
    first_four = seen_stages[:4]
    assert first_four == [
        "scope_pinning",
        "baseline_evidence",
        "mechanism_or_necessity",
        "boundary_ablation",
    ]
    assert summary["completed_stages"] == first_four
    assert "stage_history" in summary
    assert len(summary["stage_history"]) >= 4
    # Demo node is type=capability → baseline_evidence stage should have
    # actually executed at least one node in cycle 1.
    capability_history = next(
        entry
        for entry in summary["stage_history"]
        if entry["from_stage"].endswith("baseline_evidence")
    )
    assert capability_history["iterations_used"] >= 1
    # Final search_state remains schema-valid (manager validated it).
    assert summary["search_state"]["status"] in {"completed", "blocked"}


def test_stage_dataclass_exposes_claim_types():
    stage = Stage(
        name="x",
        description="y",
        claim_types_admitted={"capability"},
        max_iterations=3,
    )
    assert stage.claim_types_admitted == {"capability"}
    assert stage.num_workers == 1
    assert stage.exit_predicate is None
