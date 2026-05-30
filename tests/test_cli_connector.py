"""Test the research_runner `connector` subcommand wiring (Slice: headless CLI).

run_domain_connector is stubbed — this verifies arg->param plumbing only, no
live claude calls.
"""

from __future__ import annotations

import json

import research_harness.research_runner as rr


def _valid_grilling():
    return {
        "session_id": "grill_cli01",
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


class _FakeOutcome:
    def __init__(self, session):
        self.session = session


def test_cli_connector_wires_args(tmp_path, monkeypatch):
    captured = {}

    def fake_run(repo_root, grilling_session, **kw):
        captured["repo_root"] = repo_root
        captured["grilling"] = grilling_session
        captured["kw"] = kw
        return _FakeOutcome({"status": "completed", "claims": [], "session_id": "conn_x"})

    monkeypatch.setattr(rr, "run_domain_connector", fake_run)
    gs = tmp_path / "grilling_session.json"
    gs.write_text(json.dumps(_valid_grilling()))

    out = rr.cmd_connector(
        tmp_path,
        grilling_session_path=gs,
        run_dir=tmp_path / "connector",
        billing_ack=True,
        execution_ack=True,
        quota=5,
        max_fields_tried=20,
        field_seed=42,
    )
    assert out["status"] == "completed"
    kw = captured["kw"]
    assert kw["billing_ack"] is True and kw["execution_ack"] is True
    assert kw["quota"] == 5 and kw["max_fields_tried"] == 20 and kw["field_seed"] == 42
    assert kw["run_dir"] == tmp_path / "connector"
    assert captured["grilling"]["session_id"] == "grill_cli01"


def test_cli_connector_rejects_unfinished_grilling(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "run_domain_connector", lambda *a, **k: _FakeOutcome({}))
    gs = tmp_path / "grilling_session.json"
    bad = _valid_grilling()
    bad["status"] = "in_progress"
    gs.write_text(json.dumps(bad))
    try:
        rr.cmd_connector(tmp_path, grilling_session_path=gs, run_dir=None,
                         billing_ack=True, execution_ack=True)
        assert False, "expected ResearchRunnerError for unfinished grilling"
    except rr.ResearchRunnerError:
        pass
