"""Tests for the connector->production handoff MCP handler (Slice E-handoff).

Isolated to a tmp repo root via monkeypatch — never touches real runs/threads.
"""

from __future__ import annotations

import json

import research_harness.mcp_server as mcp
from research_harness.orchestrator import search_state as ss

_POLICY = {
    "max_depth": 5,
    "max_debug_depth": 2,
    "num_drafts": 3,
    "sunk_cost_policy": "progress_gated",
    "scaleup_policy": "disallow_by_default",
}


def _valid_grilling():
    return {
        "session_id": "grill_handoff01",
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


def _claim(code, name, text):
    return {
        "field": {"code": code, "name": name, "archive": code.split(".")[0]},
        "claim_contract": {
            "claim_under_test": text,
            "mandatory_baselines": ["current_best: X", "naive: Y", "random: Z"],
            "success_criteria": ["beats X out of sample"],
            "disproof_conditions": ["no improvement on holdout"],
        },
        "far_ness_note": "keeps the far mechanism",
        "method_num_papers": 1,
    }


def _connector_session(claims, status="completed"):
    return {"session_id": "conn_x", "type": "connector_session", "status": status, "claims": claims}


def _setup(tmp_path, tid, *, grilling=True, connector=None):
    tdir = tmp_path / "runs" / "threads" / tid
    if grilling:
        (tdir / "grilling").mkdir(parents=True, exist_ok=True)
        (tdir / "grilling" / "grilling_session.json").write_text(json.dumps(_valid_grilling()))
    if connector is not None:
        (tdir / "connector").mkdir(parents=True, exist_ok=True)
        (tdir / "connector" / "connector_session.json").write_text(json.dumps(connector))
    return tdir


def _patch(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(ss, "search_policy_from_config", lambda rr: _POLICY)


def test_seed_forest_ok_writes_multiroot_state(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    claims = [
        _claim("q-bio.PE", "Populations and Evolution", "Replicator dynamics predicts the target."),
        _claim("cs.IT", "Information Theory", "A rate-distortion bound predicts the target."),
        _claim("math.CT", "Category Theory", "A functorial mapping predicts the target."),
    ]
    tdir = _setup(tmp_path, "thread_h1", connector=_connector_session(claims))
    out = mcp.handle_seed_forest_from_connector({"thread_id": "thread_h1"})
    assert out["status"] == "ok"
    assert out["num_roots"] == 3
    state = json.loads((tdir / "production" / "tree" / "search_state.json").read_text())
    roots = [n for n in state["nodes"] if n["parent"] is None]
    assert len(roots) == 3


def test_no_claims_routes_to_honest_failure(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    _setup(tmp_path, "thread_h2", connector=_connector_session([], status="completed_no_claims"))
    out = mcp.handle_seed_forest_from_connector({"thread_id": "thread_h2"})
    assert out["status"] == "no_claims"


def test_refuses_when_already_seeded(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    claims = [_claim("q-bio.PE", "Populations and Evolution", "A claim.")]
    tdir = _setup(tmp_path, "thread_h3", connector=_connector_session(claims))
    (tdir / "production" / "tree").mkdir(parents=True)
    (tdir / "production" / "tree" / "search_state.json").write_text("{}")
    out = mcp.handle_seed_forest_from_connector({"thread_id": "thread_h3"})
    assert out["status"] == "rejected"
    assert "already seeded" in out["reason"]


def test_missing_connector_rejected(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    _setup(tmp_path, "thread_h4", connector=None)
    out = mcp.handle_seed_forest_from_connector({"thread_id": "thread_h4"})
    assert out["status"] == "rejected"
    assert "connector_session.json missing" in out["reason"]


def test_incomplete_connector_rejected(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    _setup(tmp_path, "thread_h5", connector=_connector_session([], status="blocked_by_gate"))
    out = mcp.handle_seed_forest_from_connector({"thread_id": "thread_h5"})
    assert out["status"] == "rejected"
    assert "did not complete" in out["reason"]
