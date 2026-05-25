"""Regression: when Professor follow-ups are dropped because parent_depth
hit max_depth, the caller must see the drop reason — not a silent empty
list. Also covers the empty-successor-claim drop case."""

from __future__ import annotations

import copy

from research_harness.orchestrator.treesearch.parallel_agent import (
    _build_follow_up_children,
)


def _parent_node() -> dict:
    return {
        "id": "n_root", "type": "validity",
        "domain": "machine_alpha_signal_vs_datamining_discrimination",
        "stage": "experimentation",
        "parent": None,
        "status": "promoted",
        "claim_contract": {
            "claim_under_test": "Pre-OOS calibration claim.",
            "mandatory_baselines": ["current_best_known", "naive", "random_or_null"],
            "success_criteria": ["AUC >= 0.75"],
            "disproof_conditions": ["AUC <= 0.55"],
        },
        "baseline_refs": [
            {"baseline_dossier_id": "bd_x",
             "roles": ["current_best_known", "naive", "random_or_null"]}
        ],
        "lineage": {
            "root_goal_id": "g_test",
            "covers_goal_facets": ["x"],
            "inherited_assumptions": [],
            "introduced_assumptions": [],
            "taste_constraints_applied": [],
        },
        "failure_retrieval": {"query_tags": [], "selected_fail_files": []},
        "runtime_profile": {"worker_type": "experiment_worker",
                            "turn_budget": 6,
                            "timeout_policy": "task_class_dependent"},
        "outputs": {"artifacts": [], "verdict": None},
    }


def _follow_ups() -> list[dict]:
    return [
        {"successor_claim": "Test mechanism via footprint ablation.",
         "type": "mechanism", "rationale": "Identify load-bearing feature block."},
        {"successor_claim": "Test necessity by removing IS-Sharpe band.",
         "type": "necessity", "rationale": "Probe the band-matching assumption."},
        {"successor_claim": "",
         "type": "boundary", "rationale": "Empty — should be dropped with reason."},
    ]


def test_depth_limit_surfaces_drop_reason():
    parent = _parent_node()
    dropped: list[dict] = []
    children = _build_follow_up_children(
        parent, _follow_ups(),
        parent_depth=5, max_depth=5,
        dropped_followups=dropped,
    )
    assert children == []
    assert len(dropped) == 3, "every follow-up should be logged with a reason"
    for entry in dropped:
        assert entry["reason"].startswith("depth_limit_reached")
        assert "max_depth=5" in entry["reason"]


def test_empty_successor_claim_surfaces_drop_reason():
    parent = _parent_node()
    dropped: list[dict] = []
    children = _build_follow_up_children(
        parent, _follow_ups(),
        parent_depth=1, max_depth=5,
        dropped_followups=dropped,
    )
    # 2 valid + 1 empty -> 2 children, 1 drop with reason
    assert len(children) == 2
    assert len(dropped) == 1
    assert dropped[0]["reason"].startswith("empty_successor_claim")


def test_no_drop_list_is_backward_compatible():
    """Callers that don't pass dropped_followups still get the legacy
    empty-list-on-overflow shape with no exceptions raised."""
    parent = _parent_node()
    out = _build_follow_up_children(
        parent, _follow_ups(),
        parent_depth=5, max_depth=5,
    )
    assert out == []
    out2 = _build_follow_up_children(
        parent, _follow_ups(),
        parent_depth=1, max_depth=5,
    )
    assert len(out2) == 2  # 2 valid (empty one silently skipped — old behavior)


def test_num_drafts_flows_from_harness_yaml(tmp_path, monkeypatch):
    """search_policy_from_config exposes num_drafts read from configs/harness.yaml."""
    from research_harness.orchestrator.search_state import search_policy_from_config
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[1]
    policy = search_policy_from_config(repo_root)
    # Whatever the operator set in configs/harness.yaml.
    assert "num_drafts" in policy
    assert isinstance(policy["num_drafts"], int)
    assert policy["num_drafts"] >= 1
