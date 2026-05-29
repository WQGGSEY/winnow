"""ADR 0006 — external-falsifier gate.

Covers the spine change: achieved=true is structurally unreachable without a
harness-owned falsifier result, and the air-gapped ceiling is
unverified_screen. Motivated by runs/threads/thread_e5b277f9 (RSTF), which
attested achieved=true on same-DGP self-grading.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import research_harness.mcp_server as M
import research_harness.thread_supervisor as S
from research_harness import falsifier as F
from research_harness.schemas.validator import validate_named_schema


# --- falsifier.py pure core --------------------------------------------- #


def test_spearman_identical_and_reversed():
    assert F.spearman_rho([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert F.spearman_rho([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_rejects_mismatched_length_and_too_short():
    with pytest.raises(F.FalsifierError):
        F.spearman_rho([1, 2, 3], [1, 2])
    with pytest.raises(F.FalsifierError):
        F.spearman_rho([1, 2], [1, 2])


def test_spearman_rejects_zero_variance():
    with pytest.raises(F.FalsifierError):
        F.spearman_rho([5, 5, 5, 5], [1, 2, 3, 4])


def test_evaluate_predicate_ops():
    assert F.evaluate_predicate(0.7, ">=", 0.6)
    assert not F.evaluate_predicate(0.5, ">=", 0.6)
    assert F.evaluate_predicate(0.5, "<", 0.6)
    with pytest.raises(F.FalsifierError):
        F.evaluate_predicate(1.0, "≥", 0.6)


def test_derive_ceiling_from_envelope():
    assert F.derive_max_attestable_status({}) == "unverified_screen"
    assert F.derive_max_attestable_status({"external_falsifier": {"kind": "none"}}) == "unverified_screen"
    assert F.derive_max_attestable_status(
        {"external_falsifier": {"kind": "cross_generator_transfer"}}
    ) == "goal_achieved"
    assert F.derive_max_attestable_status(
        {"external_falsifier": {"kind": "real_holdout"}}
    ) == "goal_achieved"


def _xgen_falsifier():
    return {
        "kind": "cross_generator_transfer",
        "holdout_source_id": "gen_b",
        "predicate": {"metric": "spearman_rho", "op": ">=", "threshold": 0.6},
    }


def test_compute_cross_generator_pass_and_validates():
    r = F.compute_falsifier_result(
        thread_id="t",
        falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [3, 1, 2, 4], "ranking_b": [3, 1, 2, 4]},
    )
    assert r["passed"] is True
    assert r["observed"] == pytest.approx(1.0)
    assert r["produced_by"] == "harness_falsifier_module"
    validate_named_schema("falsifier_result", r)


def test_compute_cross_generator_fail_on_inverted_ranking():
    r = F.compute_falsifier_result(
        thread_id="t",
        falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [1, 2, 3, 4], "ranking_b": [4, 3, 2, 1]},
    )
    assert r["passed"] is False


def test_compute_real_holdout_pass():
    r = F.compute_falsifier_result(
        thread_id="t",
        falsifier={
            "kind": "real_holdout",
            "holdout_source_id": "wq_intranet",
            "predicate": {"metric": "ir_mean", "op": ">=", "threshold": 0.4},
        },
        evidence={"observed": 0.51},
    )
    assert r["passed"] is True
    assert r["observed"] == pytest.approx(0.51)


def test_compute_rejects_kind_none_and_wrong_metric():
    with pytest.raises(F.FalsifierError):
        F.compute_falsifier_result(thread_id="t", falsifier={"kind": "none"}, evidence={})
    bad = _xgen_falsifier()
    bad["predicate"]["metric"] = "ir_mean"  # cross_generator must be spearman_rho
    with pytest.raises(F.FalsifierError):
        F.compute_falsifier_result(
            thread_id="t", falsifier=bad,
            evidence={"ranking_a": [1, 2, 3], "ranking_b": [1, 2, 3]},
        )


# --- envelope submit stamps max_attestable_status ----------------------- #


def _base_envelope(tid, **overrides):
    env = {
        "thread_id": tid,
        "data_sources_available": [{"kind": "synthetic", "id": "g"}],
        "llm_oracles_available": [{"kind": "subscription_claude_code"}],
        "compute_budget": {
            "max_runner_seconds_per_node": 900,
            "max_concurrent_nodes": 2,
            "max_total_node_hours": 8.0,
        },
        "baseline_provenance_available": [{"candidate_id": "x", "provenance": "y"}],
        "operator_intent": {
            "target_deploy_grade_scope": "feasibility",
            "acceptable_alternative_scopes": ["feasibility", "directional"],
        },
    }
    env.update(overrides)
    return env


def test_submit_envelope_stamps_unverified_screen_when_no_falsifier(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_env"
    out = M.handle_submit_feasibility_envelope(
        {"thread_id": tid, "envelope": _base_envelope(tid)},
        settings={"data_adapters": {"registered": []}},
    )
    assert out["status"] == "ok"
    assert out["max_attestable_status"] == "unverified_screen"
    persisted = json.loads(
        (tmp_path / "runs" / "threads" / tid / "production" / "feasibility_envelope.json").read_text()
    )
    assert persisted["max_attestable_status"] == "unverified_screen"


def test_submit_envelope_stamps_goal_achieved_with_falsifier_and_overwrites_operator_value(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_env2"
    env = _base_envelope(
        tid,
        external_falsifier=_xgen_falsifier(),
        max_attestable_status="unverified_screen",  # operator lie — must be overwritten
    )
    out = M.handle_submit_feasibility_envelope(
        {"thread_id": tid, "envelope": env},
        settings={"data_adapters": {"registered": []}},
    )
    assert out["status"] == "ok"
    assert out["max_attestable_status"] == "goal_achieved"


# --- supervisor bootstrap default --------------------------------------- #


def test_bootstrap_envelope_defaults_to_unverified_screen(tmp_path):
    repo = tmp_path
    (repo / "settings.json").write_text(json.dumps({"data_adapters": {"registered": []}}))
    tid = "t_boot"
    (repo / "runs" / "threads" / tid / "production").mkdir(parents=True)
    env = S.bootstrap_envelope_if_missing(repo, tid, target_scope="deployment")
    assert env is not None
    assert env["external_falsifier"]["kind"] == "none"
    assert env["external_falsifier"]["registered_by"] == "supervisor_bootstrap"
    assert env["max_attestable_status"] == "unverified_screen"
    validate_named_schema("feasibility_envelope", env)


# --- attestation gate (the spine) --------------------------------------- #


def _attestation(achieved, *, with_followup):
    att = {
        "thread_id": "t",
        "promoted_node_id": "n1",
        "user_intake_recap": "u" * 45,
        "achieved": achieved,
        "what_user_can_do_with_this_paper": "w" * 90,
        "evidence_anchors_back_to_intake": ["anchor-one", "anchor-two"],
        "required_additional_research": (
            [{"axis": "validity", "experiment": "e" * 25, "rationale": "r" * 25}]
            if with_followup else []
        ),
    }
    return att


def _setup_thread(tmp_path, monkeypatch, tid, envelope, *, gate=True):
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    monkeypatch.setattr(M, "_falsification_gate_enabled", lambda settings: gate)
    pdir = tmp_path / "runs" / "threads" / tid / "production"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "feasibility_envelope.json").write_text(json.dumps(envelope))
    (pdir / "rebuttal").mkdir(parents=True, exist_ok=True)


def _real_falsifier(holdout="wq_intranet"):
    return {
        "kind": "real_holdout",
        "holdout_source_id": holdout,
        "predicate": {"metric": "ir_mean", "op": ">=", "threshold": 0.4},
    }


def _write_real_falsifier_result(tmp_path, tid, *, observed, holdout="wq_intranet"):
    fr = F.compute_falsifier_result(
        thread_id=tid, falsifier=_real_falsifier(holdout),
        evidence={"observed": observed},
    )
    (tmp_path / "runs" / "threads" / tid / "production" / "rebuttal"
     / "falsifier_result.json").write_text(json.dumps(fr))


def test_gate_refuses_achieved_true_without_referent(tmp_path, monkeypatch):
    # ADR 0008: no referent -> internally_valid -> transfer_valid unconstructable.
    tid = "t_gate1"
    _setup_thread(tmp_path, monkeypatch, tid, _base_envelope(tid))
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(True, with_followup=False)}
    )
    assert out["status"] == "rejected"
    assert "UNCONSTRUCTABLE" in out["reason"]
    assert out["referent_ledger"]["max_reachable_verdict"] == "internally_valid"


def test_gate_refuses_achieved_true_when_real_referent_registered_but_no_result(tmp_path, monkeypatch):
    tid = "t_gate2"
    env = _base_envelope(tid, external_falsifier=_real_falsifier())
    _setup_thread(tmp_path, monkeypatch, tid, env)  # no falsifier_result on disk
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(True, with_followup=False)}
    )
    assert out["status"] == "rejected"
    assert "transfer_valid" in out["reason"]


def test_gate_allows_achieved_true_with_passing_real_referent(tmp_path, monkeypatch):
    # ADR 0008: only a REAL referent reaches transfer_valid -> goal_achieved.
    tid = "t_gate3"
    env = _base_envelope(tid, external_falsifier=_real_falsifier())
    _setup_thread(tmp_path, monkeypatch, tid, env)
    _write_real_falsifier_result(tmp_path, tid, observed=0.55)  # >= 0.4 -> passes
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(True, with_followup=False)}
    )
    assert out["status"] == "ok"
    assert out["attested_status"] == "goal_achieved"
    assert out["verdict_strength"] == "transfer_valid"


def test_cross_generator_transfer_does_not_unlock_goal_achieved(tmp_path, monkeypatch):
    # ADR 0008 reclassification: cross_generator_transfer is proposer-authored;
    # a passing result is a screen, NOT a strength-certifier. achieved=true stays
    # refused (transfer_valid unconstructable).
    tid = "t_gate3b"
    env = _base_envelope(tid, external_falsifier=_xgen_falsifier())
    _setup_thread(tmp_path, monkeypatch, tid, env)
    fr = F.compute_falsifier_result(
        thread_id=tid, falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [3, 1, 2, 4], "ranking_b": [3, 1, 2, 4]},
    )
    (tmp_path / "runs" / "threads" / tid / "production" / "rebuttal"
     / "falsifier_result.json").write_text(json.dumps(fr))
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(True, with_followup=False)}
    )
    assert out["status"] == "rejected"
    assert out["referent_ledger"]["has_real_referent"] is False


def test_gate_refuses_when_real_falsifier_failed(tmp_path, monkeypatch):
    tid = "t_gate4"
    env = _base_envelope(tid, external_falsifier=_real_falsifier())
    _setup_thread(tmp_path, monkeypatch, tid, env)
    _write_real_falsifier_result(tmp_path, tid, observed=0.1)  # < 0.4 -> fails
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(True, with_followup=False)}
    )
    assert out["status"] == "rejected"


def test_gate_refuses_on_holdout_mismatch(tmp_path, monkeypatch):
    tid = "t_gate5"
    env = _base_envelope(tid, external_falsifier=_real_falsifier(holdout="wq_intranet"))
    _setup_thread(tmp_path, monkeypatch, tid, env)
    # Passing result, but computed against a DIFFERENT (easier) holdout.
    _write_real_falsifier_result(tmp_path, tid, observed=0.9, holdout="some_other_source")
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(True, with_followup=False)}
    )
    assert out["status"] == "rejected"  # holdout mismatch -> real referent not verified


def test_achieved_false_stamps_unverified_screen_when_no_referent(tmp_path, monkeypatch):
    tid = "t_gate6"
    _setup_thread(tmp_path, monkeypatch, tid, _base_envelope(tid))
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(False, with_followup=True)}
    )
    assert out["status"] == "ok"
    assert out["attested_status"] == "unverified_screen"
    assert out["verdict_strength"] == "internally_valid"


def test_achieved_false_stamps_not_achieved_when_real_referent_registered(tmp_path, monkeypatch):
    # A real referent is registered but its falsifier hasn't passed -> retry.
    tid = "t_gate7"
    env = _base_envelope(tid, external_falsifier=_real_falsifier())
    _setup_thread(tmp_path, monkeypatch, tid, env)
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(False, with_followup=True)}
    )
    assert out["status"] == "ok"
    assert out["attested_status"] == "not_achieved"


def test_gate_disabled_allows_achieved_true_without_referent(tmp_path, monkeypatch):
    tid = "t_gate8"
    _setup_thread(tmp_path, monkeypatch, tid, _base_envelope(tid), gate=False)
    out = M.handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": _attestation(True, with_followup=False)}
    )
    assert out["status"] == "ok"  # toggle off reverts to pre-gate behaviour


# --- is_terminal: unverified_screen is first-class ---------------------- #


def _write_summary(repo, tid, summary):
    pdir = repo / "runs" / "threads" / tid / "production"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "production_run_summary.json").write_text(json.dumps(summary))


def _write_attestation(repo, tid, attestation):
    rdir = repo / "runs" / "threads" / tid / "production" / "rebuttal"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "user_goal_attestation.json").write_text(json.dumps(attestation))


def test_is_terminal_unverified_screen_terminates(tmp_path):
    tid = "t_term1"
    _write_summary(tmp_path, tid, {"outcome": "honest_failure"})
    _write_attestation(tmp_path, tid, {"achieved": False, "attested_status": "unverified_screen"})
    is_term, label = S.is_terminal(tmp_path, tid)
    assert is_term is True
    assert label == "accept_with_unverified_screen"


def test_is_terminal_not_achieved_honest_failure_retries(tmp_path):
    tid = "t_term2"
    _write_summary(tmp_path, tid, {"outcome": "honest_failure"})
    _write_attestation(tmp_path, tid, {"achieved": False, "attested_status": "not_achieved"})
    is_term, label = S.is_terminal(tmp_path, tid)
    assert is_term is False


def test_is_terminal_goal_achieved_still_terminates(tmp_path):
    tid = "t_term3"
    _write_summary(tmp_path, tid, {
        "outcome": "accept",
        "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
        "publication_dispatch": {"rendered_artifacts": ["paper.html"]},
    })
    _write_attestation(tmp_path, tid, {"achieved": True, "attested_status": "goal_achieved"})
    is_term, label = S.is_terminal(tmp_path, tid)
    assert is_term is True
    assert label == "accept_with_goal_achieved"
