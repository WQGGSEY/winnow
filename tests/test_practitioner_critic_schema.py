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
