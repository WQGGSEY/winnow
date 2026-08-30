"""ADR 0008 — two-axis closeness: construct-adversary + verdict-strength types.

Axis 1 (construct validity) is air-gapped-enforceable: a funded adversary
searches the pass_but_wrong region of the FROZEN question and earns
construct_valid only by failing to break it. Axis 2 (reality closeness) is
constitutionally unsayable: transfer_valid is unconstructable without a real
referent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import research_harness.mcp_server as M
import research_harness.thread_supervisor as S
from research_harness import construct_adversary as A
from research_harness import verdict_strength as V
from research_harness.schemas.validator import validate_named_schema

REPO = Path(__file__).resolve().parents[1]


# --- verdict_strength type system --------------------------------------- #


def test_ladder_and_reachability():
    assert V.VERDICT_LADDER == ["internally_valid", "construct_valid", "transfer_valid"]
    led_none = V.referent_ledger(real_referent_verified=False, construct_referent_verified=False)
    led_con = V.referent_ledger(real_referent_verified=False, construct_referent_verified=True)
    led_real = V.referent_ledger(real_referent_verified=True, construct_referent_verified=False)
    assert led_none["max_reachable_verdict"] == "internally_valid"
    assert led_con["max_reachable_verdict"] == "construct_valid"
    assert led_real["max_reachable_verdict"] == "transfer_valid"
    # transfer_valid is unsayable (unreachable) under a construct-only ledger.
    assert V.is_reachable("construct_valid", led_con) is True
    assert V.is_reachable("transfer_valid", led_con) is False
    assert V.is_reachable("transfer_valid", led_real) is True


# --- construct_adversary funded-failure evaluation ---------------------- #


def _world(wid, passes, answer):
    return {"world_id": wid, "world_description": f"world {wid} description xx",
            "measurement_passes": passes, "frozen_answer": answer}


def _report(**over):
    r = {
        "produced_by": "construct_adversary",
        "budget_total": 12,
        "worlds_tested": [_world("w1", True, "yes"), _world("w2", False, "no"),
                          _world("w3", True, "yes"), _world("w4", False, "no")],
        "breaking_instance": None,
    }
    r.update(over)
    return r


def test_adversary_survived_on_funded_failure():
    out = A.evaluate_construct_adversary_report(_report())
    assert out["verdict"] == "survived"
    assert out["distinct_worlds"] == 4


def test_adversary_broken_when_pass_but_wrong_world_enumerated():
    out = A.evaluate_construct_adversary_report(_report(
        worlds_tested=[_world("w1", True, "yes"), _world("w2", True, "no"), _world("w3", True, "yes")]))
    assert out["verdict"] == "broken"
    assert out["break_world_ids"] == ["w2"]


def test_adversary_broken_overrules_self_claimed_survival():
    # Even with breaking_instance=None, an enumerated break is detected.
    rep = _report(worlds_tested=[_world("w1", True, "no"), _world("w2", False, "no"), _world("w3", True, "yes")])
    assert A.evaluate_construct_adversary_report(rep)["verdict"] == "broken"


def test_adversary_invalid_when_budget_zero():
    assert A.evaluate_construct_adversary_report(_report(budget_total=0))["verdict"] == "invalid"


def test_adversary_invalid_when_search_too_shallow():
    out = A.evaluate_construct_adversary_report(_report(worlds_tested=[_world("w1", True, "yes")]))
    assert out["verdict"] == "invalid"


def test_adversary_invalid_on_bad_provenance():
    assert A.evaluate_construct_adversary_report(_report(produced_by="proposer"))["verdict"] == "invalid"


# --- pin_frozen_question (authorship separation) ------------------------ #


def _patch(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "_repo_root", lambda: REPO)
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)


def _write_grilling(tmp_path, tid):
    g = tmp_path / "runs" / "threads" / tid / "grilling"
    g.mkdir(parents=True, exist_ok=True)
    (g / "grilling_session.json").write_text(json.dumps({"extracted": {"claim_under_test": "x"}}))


def _frozen_q(tid):
    return {
        "thread_id": tid, "question_id": "q1",
        "formal_statement": "Does the air-gapped pipeline selection transfer to real capital?",
        "true_iff": "the selected pipeline's real-data IR exceeds the threshold",
        "source_artifact": "grilling_session",
        "frozen_at_phase": "production_entry",
    }


def test_pin_frozen_question_stamps_provenance(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    tid = "t_pin"
    _write_grilling(tmp_path, tid)
    out = M.handle_pin_frozen_question({"thread_id": tid, "question": _frozen_q(tid)})
    assert out["status"] == "ok"
    pinned = json.loads((tmp_path / "runs" / "threads" / tid / "production" / "frozen_question.json").read_text())
    assert pinned["source_provenance"].startswith("grilling_sha256:")
    validate_named_schema("frozen_question", pinned)


def test_pin_frozen_question_is_immutable(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    tid = "t_pin2"
    _write_grilling(tmp_path, tid)
    M.handle_pin_frozen_question({"thread_id": tid, "question": _frozen_q(tid)})
    q2 = _frozen_q(tid)
    q2["formal_statement"] = "A different, re-authored question that the construction prefers xx"
    out = M.handle_pin_frozen_question({"thread_id": tid, "question": q2})
    assert out["status"] == "rejected"
    assert "IMMUTABLE" in out["reason"]


# --- submit_construct_adversary_report ---------------------------------- #


def _adv_report(tid, **over):
    r = {
        "thread_id": tid, "question_id": "q1", "construction_ref": "n_root",
        "budget_total": 12,
        "pass_but_wrong_region": ["worlds where the synthetic regime matches by luck"],
        "worlds_tested": [_world("w1", True, "yes"), _world("w2", False, "no"),
                          _world("w3", True, "yes"), _world("w4", False, "no")],
        "breaking_instance": None,
        "produced_by": "construct_adversary",
    }
    r.update(over)
    return r


def _setup_q(tmp_path, monkeypatch, tid):
    _patch(monkeypatch, tmp_path)
    _write_grilling(tmp_path, tid)
    M.handle_pin_frozen_question({"thread_id": tid, "question": _frozen_q(tid)})


def test_submit_adversary_requires_frozen_question(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    tid = "t_adv0"
    (tmp_path / "runs" / "threads" / tid / "production").mkdir(parents=True, exist_ok=True)
    out = M.handle_submit_construct_adversary_report({"thread_id": tid, "report": _adv_report(tid)})
    assert out["status"] == "rejected"
    assert "frozen_question" in out["reason"]


def test_submit_adversary_rejects_question_id_mismatch(tmp_path, monkeypatch):
    tid = "t_adv1"
    _setup_q(tmp_path, monkeypatch, tid)
    out = M.handle_submit_construct_adversary_report(
        {"thread_id": tid, "report": _adv_report(tid, question_id="some_other_q")})
    assert out["status"] == "rejected"
    assert "frozen question" in out["reason"]


def test_submit_adversary_survived(tmp_path, monkeypatch):
    tid = "t_adv2"
    _setup_q(tmp_path, monkeypatch, tid)
    out = M.handle_submit_construct_adversary_report({"thread_id": tid, "report": _adv_report(tid)})
    assert out["status"] == "ok"
    assert out["harness_verdict"] == "survived"


def test_submit_adversary_broken(tmp_path, monkeypatch):
    tid = "t_adv3"
    _setup_q(tmp_path, monkeypatch, tid)
    rep = _adv_report(tid, worlds_tested=[_world("w1", True, "no"), _world("w2", True, "yes"), _world("w3", False, "no")])
    out = M.handle_submit_construct_adversary_report({"thread_id": tid, "report": rep})
    assert out["status"] == "ok"
    assert out["harness_verdict"] == "broken"


# --- end-to-end: construct_valid attestation path ----------------------- #


def _base_env(tid):
    return {
        "thread_id": tid,
        "data_sources_available": [{"kind": "synthetic", "id": "g"}],
        "llm_oracles_available": [{"kind": "subscription_claude_code"}],
        "compute_budget": {"max_runner_seconds_per_node": 900, "max_concurrent_nodes": 2, "max_total_node_hours": 8.0},
        "baseline_provenance_available": [{"candidate_id": "x", "provenance": "y"}],
        "operator_intent": {"target_deploy_grade_scope": "feasibility", "acceptable_alternative_scopes": ["feasibility"]},
        "external_falsifier": {"kind": "none", "registered_by": "supervisor_bootstrap"},
    }


def _attestation(achieved):
    return {
        "thread_id": "t", "promoted_node_id": "n_root", "user_intake_recap": "u" * 45,
        "achieved": achieved, "what_user_can_do_with_this_paper": "w" * 90,
        "evidence_anchors_back_to_intake": ["anchor-one", "anchor-two"],
        # achieved=true requires an EMPTY follow-up list; achieved=false requires >=1.
        "required_additional_research": (
            [] if achieved
            else [{"axis": "validity", "experiment": "e" * 25, "rationale": "r" * 25}]
        ),
    }


def test_construct_valid_screen_when_adversary_survived(tmp_path, monkeypatch):
    tid = "t_cv"
    _setup_q(tmp_path, monkeypatch, tid)
    monkeypatch.setattr(M, "_falsification_gate_enabled", lambda s: True)
    pdir = tmp_path / "runs" / "threads" / tid / "production"
    (pdir / "feasibility_envelope.json").write_text(json.dumps(_base_env(tid)))
    # A surviving construct-adversary report on disk -> construct referent verified.
    M.handle_submit_construct_adversary_report({"thread_id": tid, "report": _adv_report(tid)})
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(False)})
    assert out["status"] == "ok"
    assert out["verdict_strength"] == "construct_valid"
    assert out["attested_status"] == "construct_valid_screen"


def test_construct_valid_still_refuses_achieved_true(tmp_path, monkeypatch):
    # construct_valid is necessary, NOT sufficient — reality-closeness stays unsayable.
    tid = "t_cv2"
    _setup_q(tmp_path, monkeypatch, tid)
    monkeypatch.setattr(M, "_falsification_gate_enabled", lambda s: True)
    pdir = tmp_path / "runs" / "threads" / tid / "production"
    (pdir / "feasibility_envelope.json").write_text(json.dumps(_base_env(tid)))
    M.handle_submit_construct_adversary_report({"thread_id": tid, "report": _adv_report(tid)})
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(True)})
    assert out["status"] == "rejected"
    assert "UNCONSTRUCTABLE" in out["reason"]


def test_is_terminal_construct_valid_screen_is_not_strong_completion(tmp_path):
    tid = "t_cvterm"
    pdir = tmp_path / "runs" / "threads" / tid / "production"
    (pdir / "rebuttal").mkdir(parents=True, exist_ok=True)
    (pdir / "production_run_summary.json").write_text(json.dumps({"outcome": "honest_failure"}))
    (pdir / "rebuttal" / "user_goal_attestation.json").write_text(
        json.dumps({"achieved": False, "attested_status": "construct_valid_screen"}))
    is_term, label = S.is_terminal(tmp_path, tid)
    assert is_term is False
    assert label is None
