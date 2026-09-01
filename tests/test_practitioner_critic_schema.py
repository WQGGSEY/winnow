"""Phase A tests: practitioner persona contract + critic_review + ac_decision schemas."""

from __future__ import annotations

import pytest

from research_harness.schemas.validator import (
    SchemaValidationError,
    validate_named_schema,
)


def _valid_critic_review() -> dict:
    return {
        "critic_id": "senior_quant_researcher_v1",
        "node_id": "n_root",
        "verdict_candidate": "supported",
        "blocking": False,
        "scores": {"validity": 8, "necessity": 7, "reproducibility": 7, "taste_alignment": 8},
        "objections": [],
        "lesson_candidates": [],
        "failure_record_candidate": None,
        "so_what": "The classifier discriminates real-vs-mined alphas within IS-Sharpe band-matched cohorts; outside the band the signal collapses.",
        "next_actions": [
            {
                "action": "Wire LOCO-banded eval as a permanent CI gate",
                "owner_role": "quant_lead",
                "eta_weeks": 2,
                "prerequisite_evidence": "none",
            }
        ],
        "practitioner_take": "I would deploy this as a calibration filter with the band-matching guard rail and a leakage canary on the footprint block. Without the canary I would not deploy.",
        "evidence_anchors": ["worker_report.metrics.auc_overall=0.968"],
        "direct_methodology_for_user": {
            "verdict": "partial",
            "methodology_summary": "Use the classifier as a pre-OOS filter on band-matched cohorts.",
            "gap_to_close": "Provide the banded eval harness recipe.",
        },
    }


def test_critic_review_accepts_full_practitioner_payload():
    validate_named_schema("critic_review", _valid_critic_review())


def test_critic_review_rejects_missing_so_what():
    r = _valid_critic_review()
    del r["so_what"]
    with pytest.raises(SchemaValidationError, match="so_what"):
        validate_named_schema("critic_review", r)


def test_critic_review_rejects_missing_direct_methodology():
    r = _valid_critic_review()
    del r["direct_methodology_for_user"]
    with pytest.raises(SchemaValidationError, match="direct_methodology_for_user"):
        validate_named_schema("critic_review", r)


def test_critic_review_rejects_missing_next_actions():
    r = _valid_critic_review()
    r["next_actions"] = []
    with pytest.raises(SchemaValidationError):
        validate_named_schema("critic_review", r)


def test_critic_review_rejects_empty_evidence_anchors():
    r = _valid_critic_review()
    r["evidence_anchors"] = []
    with pytest.raises(SchemaValidationError):
        validate_named_schema("critic_review", r)


def test_critic_review_rejects_invalid_methodology_verdict():
    r = _valid_critic_review()
    r["direct_methodology_for_user"]["verdict"] = "maybe"
    with pytest.raises(SchemaValidationError):
        validate_named_schema("critic_review", r)


def _valid_ac_decision() -> dict:
    return {
        "decision": "accept",
        "confidence": "medium",
        "score_summary": {
            "novelty": 7, "validity": 8, "necessity": 7,
            "clarity": 7, "reproducibility": 7, "taste_alignment": 8,
        },
        "blocking_reasons": [],
        "required_next_search_nodes": [],
        "camera_ready_conditions": ["disclose AI assistance"],
        "camera_ready_directives": [
            {
                "directive": "Add explicit footprint-leakage 0.090 ablation to Method §4.2 with the per-deployment risk framing.",
                "origin_critic_ids": ["senior_quant_researcher_v1"],
                "must_appear_in_section": "method",
                "rationale": "Without this the reader cannot tell why scope narrows.",
            }
        ],
        "advisor_message_to_professor": "The validity case is strong. Sharpen the boundary section so the reader sees the leakage canary you have.",
        "rebuttal_synthesis": {
            "strongest_supporting_evidence": ["worker_report.metrics.lift_over_naive_auc=0.435"],
            "load_bearing_objections": ["IS-Sharpe band collapse"],
            "minority_dissent": [],
            "methodology_assessment": {
                "aggregate_verdict": "partial",
                "methodology_for_user": "Use as calibration filter within band-matched cohorts.",
                "remaining_gap": "Provide eval harness recipe.",
            },
        },
    }


def test_ac_decision_accepts_full_payload():
    validate_named_schema("ac_decision", _valid_ac_decision())


def test_ac_decision_rejects_missing_camera_ready_directives():
    d = _valid_ac_decision()
    del d["camera_ready_directives"]
    with pytest.raises(SchemaValidationError, match="camera_ready_directives"):
        validate_named_schema("ac_decision", d)


def test_ac_decision_rejects_empty_camera_ready_directives():
    d = _valid_ac_decision()
    d["camera_ready_directives"] = []
    with pytest.raises(SchemaValidationError):
        validate_named_schema("ac_decision", d)


def test_ac_decision_rejects_missing_methodology_assessment():
    d = _valid_ac_decision()
    del d["rebuttal_synthesis"]["methodology_assessment"]
    with pytest.raises(SchemaValidationError, match="methodology_assessment"):
        validate_named_schema("ac_decision", d)


# --- ADR 0008 rev.2 (A3): methodology clamp to the weaker path ---------- #

import json
from types import SimpleNamespace

import research_harness.mcp_server as M


def _set_verdict(decision, verdict):
    decision["rebuttal_synthesis"]["methodology_assessment"]["aggregate_verdict"] = verdict
    return decision


def _survived_adversary_report(qid="q1"):
    return {
        "thread_id": "t", "question_id": qid, "construction_ref": "c1",
        "produced_by": "construct_adversary", "budget_total": 8,
        "pass_but_wrong_region": ["a world where measurement passes yet answer is no"],
        "worlds_tested": [
            {"world_id": f"w{i}", "world_description": "d" * 12,
             "measurement_passes": True, "frozen_answer": "yes"}
            for i in range(3)
        ],
        "breaking_instance": None,
    }


def _broken_adversary_report(qid="q1"):
    rep = _survived_adversary_report(qid)
    rep["worlds_tested"][0]["frozen_answer"] = "no"  # pass-but-wrong -> broken
    return rep


def _setup_rebuttal(tmp_path, monkeypatch, tid, *, falsifier=None, adversary=None, frozen_q=None, envelope=None):
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    rdir = tmp_path / "runs" / "threads" / tid / "production" / "rebuttal"
    rdir.mkdir(parents=True, exist_ok=True)
    pdir = tmp_path / "runs" / "threads" / tid / "production"
    if falsifier is not None:
        (rdir / "falsifier_result.json").write_text(json.dumps(falsifier))
    if adversary is not None:
        (rdir / "construct_adversary_report.json").write_text(json.dumps(adversary))
    if frozen_q is not None:
        (pdir / "frozen_question.json").write_text(json.dumps(frozen_q))
    if envelope is not None:
        (pdir / "feasibility_envelope.json").write_text(json.dumps(envelope))


def _verified_real_referent():
    """An envelope + matching passing falsifier_result that makes
    _real_referent_verified True (INV-reality-cap unlock)."""
    env = {"external_falsifier": {"kind": "real_holdout", "holdout_source_id": "wq_intranet",
                                  "predicate": {"metric": "ir_mean", "op": ">=", "threshold": 0.4}}}
    fr = {"thread_id": "t", "kind": "real_holdout", "holdout_source_id": "wq_intranet",
          "predicate": {"metric": "ir_mean", "op": ">=", "threshold": 0.4},
          "observed": 0.51, "passed": True, "verdict": "passed",
          "produced_by": "harness_falsifier_module"}
    return env, fr


def test_methodology_clamped_when_falsifier_uninformative(tmp_path, monkeypatch):
    # The PM case: construct-adversary survived (clean) but falsifier uninformative.
    # 'provides' must be clamped to 'partial' (construct-valid by design,
    # unverified by measurement) — a clean pass cannot launder over a weak path.
    tid = "t_a3a"
    _setup_rebuttal(
        tmp_path, monkeypatch, tid,
        falsifier={"kind": "cross_generator_transfer", "passed": False, "verdict": "uninformative"},
        adversary=_survived_adversary_report("q1"),
        frozen_q={"question_id": "q1"},
    )
    d = _set_verdict(_valid_ac_decision(), "provides")
    out = M._clamp_methodology_assessment(tid, {}, d)
    ms = out["rebuttal_synthesis"]["methodology_assessment"]
    assert ms["aggregate_verdict"] == "partial"
    assert "clamp_reason" in ms and "uninformative" in ms["clamp_reason"]
    validate_named_schema("ac_decision", out)


def test_methodology_provides_allowed_only_with_verified_real_referent(tmp_path, monkeypatch):
    # INV-reality-cap: 'provides' stays ONLY with a VERIFIED real referent
    # (envelope real_holdout + matching passing harness falsifier_result).
    tid = "t_a3b"
    env, fr = _verified_real_referent()
    binding = SimpleNamespace(
        contract_id="contract_" + "a" * 64,
        attempt_id="attempt_current",
        direction_id="direction_" + "b" * 64,
        node_id="n_current",
        manifest_id="acqmanifest_" + "c" * 64,
    )
    fr.update(
        {
            "contract_id": binding.contract_id,
            "attempt_id": binding.attempt_id,
            "direction_id": binding.direction_id,
            "node_id": binding.node_id,
            "manifest_id": binding.manifest_id,
        }
    )
    monkeypatch.setattr(M, "_authoritative_strong_binding", lambda _tid: binding)
    _setup_rebuttal(tmp_path, monkeypatch, tid, falsifier=fr, envelope=env)
    d = _set_verdict(_valid_ac_decision(), "provides")
    out = M._clamp_methodology_assessment(tid, {}, d)
    assert out["rebuttal_synthesis"]["methodology_assessment"]["aggregate_verdict"] == "provides"
    assert "clamp_reason" not in out["rebuttal_synthesis"]["methodology_assessment"]


def test_methodology_clamped_to_absent_when_adversary_broken(tmp_path, monkeypatch):
    tid = "t_a3c"
    _setup_rebuttal(
        tmp_path, monkeypatch, tid,
        adversary=_broken_adversary_report("q1"),
        frozen_q={"question_id": "q1"},
    )
    d = _set_verdict(_valid_ac_decision(), "provides")
    out = M._clamp_methodology_assessment(tid, {}, d)
    assert out["rebuttal_synthesis"]["methodology_assessment"]["aggregate_verdict"] == "absent"


def test_methodology_provides_unreachable_without_real_referent(tmp_path, monkeypatch):
    # INV-reality-cap (T2-10): with NO real referent, 'provides' is structurally
    # unreachable — capped to 'partial' even though no weak path was executed.
    tid = "t_a3d"
    _setup_rebuttal(tmp_path, monkeypatch, tid)
    d = _set_verdict(_valid_ac_decision(), "provides")
    out = M._clamp_methodology_assessment(tid, {}, d)
    ms = out["rebuttal_synthesis"]["methodology_assessment"]
    assert ms["aggregate_verdict"] == "partial"
    assert "INV-reality-cap" in ms["clamp_reason"]


def test_methodology_partial_stands_without_real_referent(tmp_path, monkeypatch):
    # 'partial' is reachable air-gapped, so it is not clamped by the reality-cap.
    tid = "t_a3f"
    _setup_rebuttal(tmp_path, monkeypatch, tid)
    d = _set_verdict(_valid_ac_decision(), "partial")
    out = M._clamp_methodology_assessment(tid, {}, d)
    assert out["rebuttal_synthesis"]["methodology_assessment"]["aggregate_verdict"] == "partial"


def test_methodology_clamp_never_raises_authored(tmp_path, monkeypatch):
    # Cap only LOWERS: an authored 'absent' is never raised even with clean paths.
    tid = "t_a3e"
    _setup_rebuttal(
        tmp_path, monkeypatch, tid,
        falsifier={"kind": "real_holdout", "passed": True, "verdict": "passed"},
    )
    d = _set_verdict(_valid_ac_decision(), "absent")
    out = M._clamp_methodology_assessment(tid, {}, d)
    assert out["rebuttal_synthesis"]["methodology_assessment"]["aggregate_verdict"] == "absent"
