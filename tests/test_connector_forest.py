"""Tests for connector forest seeding — N claims -> N coexisting roots (Slice D1)."""

from __future__ import annotations

import pytest

from research_harness.connector.forest import build_forest_search_state
from research_harness.orchestrator.search_state import validate_search_state

_POLICY = {
    "max_depth": 5,
    "max_debug_depth": 2,
    "num_drafts": 3,
    "sunk_cost_policy": "progress_gated",
    "scaleup_policy": "disallow_by_default",
}


def _valid_grilling():
    return {
        "session_id": "grill_forest01",
        "status": "done",
        "user_goal": "predict equity returns from order flow",
        "max_rounds": 8,
        "model": "claude-x",
        "created_at": "2026-05-30T00:00:00Z",
        "rounds": [],
        "usage_estimate": {"rounds_used": 1, "total_cost_usd": 0.0,
                            "total_input_tokens": 0, "total_output_tokens": 0},
        "extracted": {
            "root_goal_id": "rg_equity",
            "domain": "stock_market",
            "node_type": "validity",
            "claim_under_test": "It is possible to predict equity returns from order flow",
            "mandatory_baselines": ["current_best: TBD"],
            "success_criteria": ["TBD"],
            "disproof_conditions": ["TBD"],
            "goal_facets": [],
            "taste_constraints": [],
            "search_query_seed": "equity returns order flow",
        },
    }


def _claim(code, name, claim_text):
    return {
        "field": {"code": code, "name": name, "archive": code.split(".")[0]},
        "claim_contract": {
            "claim_under_test": claim_text,
            "mandatory_baselines": ["current_best: X", "naive: Y", "random: Z"],
            "success_criteria": ["beats X out of sample"],
            "disproof_conditions": ["no improvement on holdout"],
        },
        "far_ness_note": "keeps the far mechanism",
        "method_num_papers": 2,
    }


def _claims():
    return [
        _claim("q-bio.PE", "Populations and Evolution", "Replicator dynamics predicts the target."),
        _claim("cs.IT", "Information Theory", "A rate-distortion bound predicts the target."),
        _claim("math.CT", "Category Theory", "A functorial mapping predicts the target."),
    ]


def test_forest_has_n_coexisting_roots_each_seeded():
    state = build_forest_search_state(
        _valid_grilling(), _claims(), search_id="s_forest", policy=_POLICY
    )
    roots = [n for n in state["nodes"] if n["parent"] is None]
    assert len(roots) == 3  # N coexisting roots
    # every node id is unique.
    ids = [n["id"] for n in state["nodes"]]
    assert len(set(ids)) == len(ids)
    # each root has at least one draft child.
    for root in roots:
        children = [n for n in state["nodes"] if n["parent"] == root["id"]]
        assert children, f"root {root['id']} has no draft children"
    # frontier carries all three roots.
    root_frontier = [f for f in state["frontier"] if f["parent"] is None]
    assert len(root_frontier) == 3


def test_forest_roots_carry_reduced_claims_and_provenance():
    claims = _claims()
    state = build_forest_search_state(
        _valid_grilling(), claims, search_id="s_forest", policy=_POLICY
    )
    roots = [n for n in state["nodes"] if n["parent"] is None]
    claim_texts = {r["claim_contract"]["claim_under_test"] for r in roots}
    assert claim_texts == {c["claim_contract"]["claim_under_test"] for c in claims}
    # node ids encode the field slug; provenance note is recorded.
    assert any("q_bio_pe" in r["id"] for r in roots)
    prov = [a for r in roots for a in r["lineage"]["introduced_assumptions"]
            if "connector far-framing" in a]
    assert len(prov) == 3


def test_forest_state_is_schema_valid():
    state = build_forest_search_state(
        _valid_grilling(), _claims(), search_id="s_forest", policy=_POLICY
    )
    validate_search_state(state)  # raises if invalid


def test_forest_propagates_supervisor_owned_adapter_selection():
    selection = {"adapter_id": "local_data", "snapshot_id": "as_" + "a" * 64}
    state = build_forest_search_state(
        _valid_grilling(),
        _claims(),
        search_id="s_forest",
        policy=_POLICY,
        deploy_grade_scope="deployment",
        data_selection=selection,
    )

    for node in state["nodes"]:
        contract = node["claim_contract"]
        assert contract["deploy_grade_scope"] == "deployment"
        assert contract["data_source_anchor"] == "local_data"
        assert contract["data_source_snapshot_id"] == selection["snapshot_id"]


def test_empty_claims_raises():
    with pytest.raises(ValueError):
        build_forest_search_state(
            _valid_grilling(), [], search_id="s_forest", policy=_POLICY
        )
