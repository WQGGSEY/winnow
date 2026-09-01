"""ADR 0007 — premature termination is the enemy.

Covers the depth gate on the lazy door (honest-failure render requires earned
depth, not a one-shot give-up) and lever 0 adversarial-dominant aggregation
(a grounded critic kill freezes accept until defeated on merits). Corrects
ADR 0006's unconditional honest-failure terminal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import research_harness.mcp_server as M

REPO = Path(__file__).resolve().parents[1]


def _patch(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "_repo_root", lambda: REPO)
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)


def _node(nid, parent, status, *, scope=None, scope_kind=None):
    cc = {}
    if scope:
        cc["deploy_grade_scope"] = scope
    if scope_kind:
        cc["scope_kind"] = scope_kind
    return {"id": nid, "parent": parent, "status": status, "claim_contract": cc}


def _write_state(tmp_path, tid, nodes):
    tree = tmp_path / "runs" / "threads" / tid / "production" / "tree"
    (tree / "nodes").mkdir(parents=True, exist_ok=True)
    promoted = [n["id"] for n in nodes if n.get("status") == "promoted"]
    (tree / "search_state.json").write_text(
        json.dumps({"nodes": nodes, "promoted_node_ids": promoted}), encoding="utf-8"
    )


def _attestation_neg(mechanism=None):
    att = {
        "thread_id": "t", "promoted_node_id": "root",
        "user_intake_recap": "u" * 45, "achieved": False,
        "what_user_can_do_with_this_paper": "w" * 90,
        "evidence_anchors_back_to_intake": ["anchor-one", "anchor-two"],
        "required_additional_research": [
            {"axis": "validity", "experiment": "e" * 25, "rationale": "r" * 25}],
        "attested_status": "not_achieved",
    }
    if mechanism:
        att["load_bearing_mechanism"] = mechanism
    return att


def _write_attestation(tmp_path, tid, att):
    rd = tmp_path / "runs" / "threads" / tid / "production" / "rebuttal"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "user_goal_attestation.json").write_text(json.dumps(att), encoding="utf-8")


# --- lever 0: adversarial-dominant aggregation -------------------------- #


def test_collect_and_undefeated_kills(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    tid = "t_kills"
    rd = tmp_path / "runs" / "threads" / tid / "production" / "rebuttal" / "rebuttal_reviews"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "claim_skeptic_v1.json").write_text(json.dumps(
        {"critic_id": "claim_skeptic_v1", "verdict_candidate": "contradicted", "blocking": False}))
    (rd / "invariants_v1.json").write_text(json.dumps(
        {"critic_id": "invariants_v1", "verdict_candidate": "supported", "blocking": True}))
    (rd / "happy_v1.json").write_text(json.dumps(
        {"critic_id": "happy_v1", "verdict_candidate": "supported", "blocking": False}))
    kills = M._collect_rebuttal_kills(tid)
    assert {k["critic_id"] for k in kills} == {"claim_skeptic_v1", "invariants_v1"}
    # claim_skeptic defeated with a substantive rebuttal; invariants not.
    undefeated = M._undefeated_kills(kills, [
        {"critic_id": "claim_skeptic_v1", "defeated": True, "defeat_rebuttal": "x" * 50},
        {"critic_id": "invariants_v1", "defeated": True, "defeat_rebuttal": "short"},  # too short
    ])
    assert [k["critic_id"] for k in undefeated] == ["invariants_v1"]


def _setup_ac(tmp_path, tid, *, kill=True, blocking_objections=None):
    _write_state(tmp_path, tid, [_node("root", None, "promoted", scope="deployment")])
    rd = tmp_path / "runs" / "threads" / tid / "production" / "rebuttal"
    (rd / "rebuttal_reviews").mkdir(parents=True, exist_ok=True)
    if kill:
        (rd / "rebuttal_reviews" / "claim_skeptic_v1.json").write_text(json.dumps(
            {"critic_id": "claim_skeptic_v1", "verdict_candidate": "contradicted", "blocking": False}))
    (rd / "orchestrator_reduction.json").write_text(json.dumps(
        {"node_id": "root", "final_verdict": "supported",
         "blocking_objections": blocking_objections or []}))


def _ac_accept():
    return {
        "decision": "accept", "confidence": "high",
        "score_summary": {"novelty": 7, "validity": 8, "necessity": 7, "clarity": 7,
                          "reproducibility": 7, "taste_alignment": 8},
        "blocking_reasons": [], "required_next_search_nodes": [],
        "camera_ready_conditions": [],
        "camera_ready_directives": [{
            "directive": "Tighten the scope statement to name the guard rail.",
            "origin_critic_ids": ["claim_skeptic_v1"], "must_appear_in_section": "method",
            "rationale": "Without this the reader cannot tell why scope narrows.",
        }],
        "advisor_message_to_professor": (
            "The validity case stands inside its declared scope; tighten the "
            "boundary section so the guard rail leads the discussion."
        ),
        "rebuttal_synthesis": {
            "strongest_supporting_evidence": ["x"], "load_bearing_objections": [],
            "minority_dissent": [],
            "methodology_assessment": {
                "aggregate_verdict": "provides",
                "methodology_for_user": "Use as a pre-OOS calibration filter.",
                "remaining_gap": "",
            },
        },
    }


def test_ac_accept_blocked_by_undefeated_kill(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setattr(M, "_adversarial_dominance_enabled", lambda s: True)
    tid = "t_ac_kill"
    _setup_ac(tmp_path, tid, kill=True, blocking_objections=[])
    out = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": _ac_accept()})
    assert out["status"] == "rejected"
    assert "adversarial-dominance" in out["reason"]
    assert out["undefeated_kills"] == ["claim_skeptic_v1"]


def test_ac_accept_allowed_when_kill_defeated(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setattr(M, "_adversarial_dominance_enabled", lambda s: True)
    tid = "t_ac_defeated"
    _setup_ac(tmp_path, tid, kill=True, blocking_objections=[
        {"critic_id": "claim_skeptic_v1", "defeated": True,
         "defeat_rebuttal": "The overclaim objection is answered by the held-out re-measurement showing the effect persists."},
    ])
    out = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": _ac_accept()})
    assert out["status"] == "ok"


def test_ac_accept_happy_path_no_kill(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setattr(M, "_adversarial_dominance_enabled", lambda s: True)
    tid = "t_ac_nokill"
    _setup_ac(tmp_path, tid, kill=False)
    out = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": _ac_accept()})
    assert out["status"] == "ok"


def test_ac_dominance_disabled_allows_accept_over_kill(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setattr(M, "_adversarial_dominance_enabled", lambda s: False)
    tid = "t_ac_off"
    _setup_ac(tmp_path, tid, kill=True, blocking_objections=[])
    out = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": _ac_accept()})
    assert out["status"] == "ok"  # toggle off reverts to pre-ADR-0007 behaviour


def test_orchestrator_reduction_blocked_by_undefeated_kill(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setattr(M, "_adversarial_dominance_enabled", lambda s: True)
    monkeypatch.setattr(M, "_authoritative_active_node_id", lambda _tid: "root")
    tid = "t_red_kill"
    # Promoted node + routing + the killing review submitted.
    _write_state(tmp_path, tid, [_node("root", None, "promoted", scope="deployment")])
    rd = tmp_path / "runs" / "threads" / tid / "production" / "rebuttal"
    (rd / "rebuttal_reviews").mkdir(parents=True, exist_ok=True)
    (rd / "rebuttal_reviews" / "claim_skeptic_v1.json").write_text(json.dumps(
        {"critic_id": "claim_skeptic_v1", "verdict_candidate": "contradicted", "blocking": False}))
    (rd / "rebuttal_routing.json").write_text(json.dumps(
        {"applied_critics": [{"critic_id": "claim_skeptic_v1"}]}))
    out = M.handle_submit_orchestrator_reduction({
        "thread_id": tid, "node_id": "root",
        "final_verdict": "supported_with_scope_narrowing",
        "synthesis_message": "s" * 65, "blocking_objections": [],
    })
    assert out["status"] == "rejected"
    assert "adversarial-dominance" in out["reason"]
