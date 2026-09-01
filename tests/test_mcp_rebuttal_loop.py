"""Phase C tests: the full MCP rebuttal loop —
prepare_rebuttal_packet → submit_rebuttal_critic_review (per critic) →
submit_orchestrator_reduction → submit_ac_decision → submit_camera_ready_revision."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import research_harness.mcp_server as M


@pytest.fixture
def isolated_thread(tmp_path, monkeypatch):
    """Build a minimal but valid promoted-node tree on disk in tmp_path."""
    monkeypatch.setattr(M, "_repo_root", lambda: Path(__file__).resolve().parents[1])
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_test_rebuttal"
    tdir = tmp_path / "runs" / "threads" / tid
    pdir = tdir / "production"
    tree_dir = pdir / "tree"
    nodes_dir = tree_dir / "nodes"
    node_id = "n_root"
    monkeypatch.setattr(M, "_authoritative_active_node_id", lambda _tid: node_id)
    node_dir = nodes_dir / node_id
    node_dir.mkdir(parents=True, exist_ok=True)

    node = {
        "id": node_id, "type": "validity",
        "domain": "machine_alpha_signal_vs_datamining_discrimination",
        "stage": "promotion",
        "parent": None,
        "status": "promoted",
        "claim_contract": {
            "claim_under_test": "Pre-OOS classifier discriminates within band-matched cohorts.",
            "mandatory_baselines": ["current_best_known", "naive", "random_or_null"],
            "success_criteria": ["AUC >= 0.75"],
            "disproof_conditions": ["AUC <= 0.55 in any cell"],
        },
        "baseline_refs": [
            {"baseline_dossier_id": "bd_test", "roles": ["current_best_known", "naive", "random_or_null"], "candidates": []},
        ],
    }
    state = {
        "search_id": "test",
        "status": "running",
        "max_depth": 2,
        "max_debug_depth": 1,
        "scaleup_policy": "default",
        "sunk_cost_policy": "default",
        "nodes": [node],
        "promoted_node_ids": [node_id],
        "pruned_node_ids": [],
        "completed_node_ids": [node_id],
        "frontier": [],
        "transitions": [],
    }
    (tree_dir / "search_state.json").write_text(json.dumps(state), encoding="utf-8")
    (node_dir / "worker_report.json").write_text(json.dumps({
        "status": "completed",
        "claim_verdict_candidate": "supported",
        "metrics": {"auc_overall": 0.968, "lift_over_naive_auc": 0.435,
                    "lift_over_naive_ci95_low": 0.343, "lift_over_naive_ci95_high": 0.523},
        "baselines": {"current_best_known": 0.7, "naive": 0.533, "random_or_null": 0.510},
        "baseline_evidence_status": {"overall": "passed"},
        "artifacts": ["metrics.json"],
        "unexpected_observations": [],
        "disproof_conditions_hit": [],
    }), encoding="utf-8")
    (pdir / "intake_to_claim_dialog.json").write_text(json.dumps({
        "original_contract": {"claim_under_test": "Help me tell good alphas from data mining"},
        "new_contract": {"claim_under_test": node["claim_contract"]["claim_under_test"]},
    }), encoding="utf-8")
    return tid, node_id


def test_prepare_rebuttal_packet_surfaces_critic_bodies_and_intake(isolated_thread):
    tid, node_id = isolated_thread
    out = M.handle_prepare_rebuttal_packet({"thread_id": tid})
    assert out["status"] == "ok"
    assert out["promoted_node_id"] == node_id
    assert out["critic_list"], "must return routed critics"
    assert all("body" in c for c in out["critic_list"])
    assert out["original_user_problem"]
    assert out["reshaped_claim_under_test"]
    assert "PRACTITIONER" in out["practitioner_persona"].upper() or "practitioner" in out["practitioner_persona"]


def _practitioner_review(critic_id: str, node_id: str) -> dict:
    return {
        "critic_id": critic_id, "node_id": node_id,
        "verdict_candidate": "supported", "blocking": False,
        "scores": {"validity": 8, "necessity": 7, "reproducibility": 7, "taste_alignment": 8},
        "objections": [],
        "lesson_candidates": [],
        "failure_record_candidate": None,
        "so_what": ("The classifier separates genuine alphas from mined ones inside the IS-Sharpe band — "
                    "outside the band it collapses. Practical implication: do not deploy as a universal filter."),
        "next_actions": [{
            "action": "Wire LOCO-banded eval as a permanent CI gate",
            "owner_role": "quant_lead", "eta_weeks": 2,
            "prerequisite_evidence": "none",
        }],
        "practitioner_take": ("I would deploy this as a calibration filter with the band-matching guard rail and "
                              "a leakage canary on the footprint block. Without the canary I would not deploy."),
        "evidence_anchors": ["worker_report.metrics.auc_overall=0.968"],
        "direct_methodology_for_user": {
            "verdict": "partial",
            "methodology_summary": "Use the classifier as a pre-OOS calibration filter inside band-matched cohorts.",
            "gap_to_close": "Provide ready-to-deploy banded eval harness recipe.",
        },
    }


def test_full_rebuttal_loop_writes_artifacts(isolated_thread):
    tid, node_id = isolated_thread
    prep = M.handle_prepare_rebuttal_packet({"thread_id": tid})
    assert prep["status"] == "ok"
    routed_ids = [c["critic_id"] for c in prep["critic_list"]]
    assert routed_ids

    # Submit one review per routed critic.
    last = None
    for cid in routed_ids:
        last = M.handle_submit_rebuttal_critic_review({
            "thread_id": tid, "review": _practitioner_review(cid, node_id),
        })
        assert last["status"] == "ok"
    assert last["pending_critic_ids"] == []

    # Reduction.
    red = M.handle_submit_orchestrator_reduction({
        "thread_id": tid, "node_id": node_id,
        "final_verdict": "supported_with_scope_narrowing",
        "research_status": "supported_with_scope_narrowing",
        "score_summary": {"validity": 8, "necessity": 7, "reproducibility": 7, "taste_alignment": 8},
        "synthesis_message": "Cohort matters: the classifier discriminates within IS-Sharpe band-matched cohorts and is a calibration filter, not a universal classifier.",
        "blocking_objections": [],
        "accepted_lesson_candidates": [],
        "next_transition": "promoted",
    })
    assert red["status"] == "ok"

    # AC.
    ac = {
        "decision": "accept", "confidence": "medium",
        "score_summary": {"novelty": 7, "validity": 8, "necessity": 7,
                           "clarity": 7, "reproducibility": 7, "taste_alignment": 8},
        "blocking_reasons": [], "required_next_search_nodes": [],
        "camera_ready_conditions": ["disclose AI assistance"],
        "camera_ready_directives": [{
            "directive": "Add explicit footprint-leakage 0.090 ablation to Method §4.2 with the per-deployment risk framing the rebuttal made explicit.",
            "origin_critic_ids": [routed_ids[0]],
            "must_appear_in_section": "method",
            "rationale": "Without this the reader cannot tell why scope narrows to band-matched cohorts.",
        }],
        "advisor_message_to_professor": "The validity case is strong. Sharpen the boundary section so the leakage canary leads.",
        "rebuttal_synthesis": {
            "strongest_supporting_evidence": ["worker_report.metrics.lift_over_naive_auc=0.435"],
            "load_bearing_objections": ["band collapse"],
            "minority_dissent": [],
            "methodology_assessment": {
                "aggregate_verdict": "partial",
                "methodology_for_user": "Pre-OOS calibration filter inside band-matched cohorts.",
                "remaining_gap": "Provide banded eval harness recipe.",
            },
        },
    }
    out_ac = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": ac})
    assert out_ac["status"] == "ok"

    # Camera-ready revision.
    revision = {
        "thread_id": tid, "promoted_node_id": node_id,
        "responses_to_directives": [{
            "directive_index": 0, "status": "accepted_fully",
            "section_changes": [{"section": "method", "change_summary": "Added 0.090 ablation row + leakage framing."}],
            "professor_reply": "Folded in. Footprint block ablation appears as Table 2 in §4.2.",
        }],
        "scope_statement": "We claim pre-OOS discrimination of machine alphas within IS-Sharpe band-matched cohorts.",
        "mental_model_statement": ("The meta-classifier converts an alpha's symbolic expression + footprint + IS "
                                    "trajectory into a calibrated genuine-signal probability, useful as a pre-OOS "
                                    "calibration filter within IS-Sharpe band-matched cohorts."),
        "limitations_added": ["unbounded lift requires IS-Sharpe band matching"],
        "advisor_acknowledgement": "Internalized. Boundary section now leads with the leakage canary.",
    }
    out_rev = M.handle_submit_camera_ready_revision({
        "thread_id": tid, "camera_ready_revision": revision,
    })
    assert out_rev["status"] == "ok"


def test_ac_rejects_accept_with_empty_directives(isolated_thread):
    tid, node_id = isolated_thread
    prep = M.handle_prepare_rebuttal_packet({"thread_id": tid})
    for cid in [c["critic_id"] for c in prep["critic_list"]]:
        M.handle_submit_rebuttal_critic_review({
            "thread_id": tid, "review": _practitioner_review(cid, node_id),
        })
    M.handle_submit_orchestrator_reduction({
        "thread_id": tid, "node_id": node_id,
        "final_verdict": "supported",
        "research_status": "supported",
        "score_summary": {"validity": 8, "necessity": 7, "reproducibility": 7, "taste_alignment": 8},
        "synthesis_message": "All clean inside the band-matched scope.  We have enough evidence to publish.",
    })
    ac_empty = {
        "decision": "accept", "confidence": "high",
        "score_summary": {"novelty": 7, "validity": 8, "necessity": 7, "clarity": 7,
                           "reproducibility": 7, "taste_alignment": 8},
        "blocking_reasons": [], "required_next_search_nodes": [],
        "camera_ready_conditions": [],
        "camera_ready_directives": [],   # <-- forbidden
        "advisor_message_to_professor": "Looks great as is.",
        "rebuttal_synthesis": {
            "strongest_supporting_evidence": ["x"],
            "load_bearing_objections": [],
            "minority_dissent": [],
            "methodology_assessment": {
                "aggregate_verdict": "provides",
                "methodology_for_user": "Use the classifier as a pre-OOS calibration filter.",
                "remaining_gap": "",
            },
        },
    }
    out = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": ac_empty})
    assert out["status"] == "rejected"


def test_reduction_blocked_when_critics_missing(isolated_thread):
    tid, node_id = isolated_thread
    M.handle_prepare_rebuttal_packet({"thread_id": tid})
    out = M.handle_submit_orchestrator_reduction({
        "thread_id": tid, "node_id": node_id,
        "final_verdict": "supported",
        "research_status": "supported",
        "score_summary": {"validity": 8, "necessity": 7, "reproducibility": 7, "taste_alignment": 8},
        "synthesis_message": "synthesis " * 10,
    })
    assert out["status"] == "rejected"
    assert "missing" in out["reason"].lower()


def test_camera_ready_must_address_every_directive(isolated_thread):
    tid, node_id = isolated_thread
    prep = M.handle_prepare_rebuttal_packet({"thread_id": tid})
    for cid in [c["critic_id"] for c in prep["critic_list"]]:
        M.handle_submit_rebuttal_critic_review({
            "thread_id": tid, "review": _practitioner_review(cid, node_id),
        })
    M.handle_submit_orchestrator_reduction({
        "thread_id": tid, "node_id": node_id,
        "final_verdict": "supported",
        "research_status": "supported",
        "score_summary": {"validity": 8, "necessity": 7, "reproducibility": 7, "taste_alignment": 8},
        "synthesis_message": "synthesis " * 10,
    })
    ac = {
        "decision": "accept", "confidence": "high",
        "score_summary": {"novelty": 7, "validity": 8, "necessity": 7, "clarity": 7,
                           "reproducibility": 7, "taste_alignment": 8},
        "blocking_reasons": [], "required_next_search_nodes": [],
        "camera_ready_conditions": [],
        "camera_ready_directives": [
            {"directive": "Add canary description in method.", "origin_critic_ids": ["x"],
             "must_appear_in_section": "method", "rationale": "needed for camera-ready quality"},
            {"directive": "Add limitations note about band requirement.", "origin_critic_ids": ["x"],
             "must_appear_in_section": "limitations", "rationale": "must be visible to readers"},
        ],
        "advisor_message_to_professor": (
            "Two directives — please address both. The canary belongs in Method and the band limit "
            "belongs in Limitations; readers must see both before they can act on the result."
        ),
        "rebuttal_synthesis": {
            "strongest_supporting_evidence": ["x"],
            "load_bearing_objections": [],
            "minority_dissent": [],
            "methodology_assessment": {
                "aggregate_verdict": "partial",
                "methodology_for_user": "Use the meta-classifier as a pre-OOS filter within band-matched cohorts only.",
                "remaining_gap": "harness recipe.",
            },
        },
    }
    ac_out = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": ac})
    assert ac_out["status"] == "ok", ac_out

    # Revision only addresses index 0 — must be rejected.
    bad_rev = {
        "thread_id": tid, "promoted_node_id": node_id,
        "responses_to_directives": [{
            "directive_index": 0, "status": "accepted_fully",
            "section_changes": [{"section": "method", "change_summary": "Added canary."}],
            "professor_reply": "Done.",
        }],
        "scope_statement": "We claim pre-OOS discrimination within band-matched cohorts only.",
        "mental_model_statement": "The classifier acts as a pre-OOS calibration filter within band-matched cohorts.",
        "limitations_added": [],
        "advisor_acknowledgement": "Internalized advisor message.",
    }
    out = M.handle_submit_camera_ready_revision({
        "thread_id": tid, "camera_ready_revision": bad_rev,
    })
    assert out["status"] == "rejected"
    assert "directive" in out["reason"].lower()


def _run_to_ac(tid, node_id):
    """Run prepare_rebuttal_packet → submit reviews → reduction. Returns nothing."""
    prep = M.handle_prepare_rebuttal_packet({"thread_id": tid})
    for cid in [c["critic_id"] for c in prep["critic_list"]]:
        M.handle_submit_rebuttal_critic_review({
            "thread_id": tid, "review": _practitioner_review(cid, node_id),
        })
    M.handle_submit_orchestrator_reduction({
        "thread_id": tid, "node_id": node_id,
        "final_verdict": "supported_with_scope_narrowing",
        "research_status": "supported_with_scope_narrowing",
        "score_summary": {"validity": 8, "necessity": 7, "reproducibility": 7, "taste_alignment": 8},
        "synthesis_message": "Synthesis is operator-language. " * 4,
        "next_transition": "promoted",
    })


def _minimal_ac(decision="accept"):
    return {
        "decision": decision, "confidence": "high",
        "score_summary": {"novelty": 7, "validity": 8, "necessity": 7, "clarity": 7,
                          "reproducibility": 7, "taste_alignment": 8},
        "blocking_reasons": [], "required_next_search_nodes": [],
        "camera_ready_conditions": [],
        "camera_ready_directives": [{
            "directive": "Tighten scope statement to name the band-matching guard rail explicitly.",
            "origin_critic_ids": ["x"], "must_appear_in_section": "method",
            "rationale": "Without this the reader cannot tell why scope narrows.",
        }],
        "advisor_message_to_professor": (
            "The validity case stands inside its scope. Tighten the boundary section "
            "so the band-matching guard rail leads the discussion."
        ),
        "rebuttal_synthesis": {
            "strongest_supporting_evidence": ["x"],
            "load_bearing_objections": [],
            "minority_dissent": [],
            "methodology_assessment": {
                "aggregate_verdict": "partial",
                "methodology_for_user": "Use as a pre-OOS calibration filter.",
                "remaining_gap": "harness recipe.",
            },
        },
    }


def test_ac_confidence_downclamped_when_no_real_adapter(isolated_thread, tmp_path):
    """Rail 4: envelope with synthetic-only data sources clamps confidence to low."""
    tid, node_id = isolated_thread
    # Write a minimal envelope declaring only synthetic data.
    envelope = {
        "thread_id": tid,
        "data_sources_available": [{"kind": "synthetic", "id": "synth_default"}],
        "llm_oracles_available": [{"kind": "subscription_codex", "model": "gpt-5.6-sol"}],
        "compute_budget": {"max_runner_seconds_per_node": 900, "max_concurrent_nodes": 2, "max_total_node_hours": 8.0},
        "baseline_provenance_available": [{"candidate_id": "x", "provenance": "y"}],
        "operator_intent": {"target_deploy_grade_scope": "feasibility", "acceptable_alternative_scopes": ["feasibility"]},
    }
    (tmp_path / "runs" / "threads" / tid / "production" / "feasibility_envelope.json").write_text(
        json.dumps(envelope), encoding="utf-8"
    )
    _run_to_ac(tid, node_id)
    out = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": _minimal_ac("accept")})
    assert out["status"] == "ok"
    persisted = json.loads(
        (tmp_path / "runs" / "threads" / tid / "production" / "rebuttal" / "ac_decision.json").read_text(encoding="utf-8")
    )
    assert persisted["confidence"] == "low"
    assert any("real_adapter" in r for r in persisted["confidence_downclamp_reasons"])


def test_ac_accept_blocked_when_bound_anchor_unmeasured(isolated_thread, tmp_path):
    """Rail 3: operator-bound anchor with no metric value → accept blocked."""
    tid, node_id = isolated_thread
    gdir = tmp_path / "runs" / "threads" / tid / "grilling"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "grilling_session.json").write_text(
        json.dumps({"extracted": {"user_goal_anchors": [{
            "anchor_text": "real WorldQuant intranet transfer",
            "anchor_kind": "data_source", "must_be_measured": True,
            "bound_metric_key": "real_wq_transfer_ir_mean",
            "extraction_source": "operator",
        }]}}),
        encoding="utf-8",
    )
    _run_to_ac(tid, node_id)
    out = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": _minimal_ac("accept")})
    assert out["status"] == "rejected"
    assert "user_goal anchor-binding rail" in out["reason"]
    assert "real_wq_transfer_ir_mean" in out["reason"]


def test_ac_confidence_downclamped_on_unbound_anchor(isolated_thread, tmp_path):
    """Rail 3: unbound must_be_measured anchors → confidence clamp (not block)."""
    tid, node_id = isolated_thread
    gdir = tmp_path / "runs" / "threads" / tid / "grilling"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "grilling_session.json").write_text(
        json.dumps({"extracted": {"user_goal_anchors": [{
            "anchor_text": "실제 WorldQuant 데이터",
            "anchor_kind": "data_source", "must_be_measured": True,
            "bound_metric_key": None,
            "extraction_source": "auto_heuristic",
        }]}}),
        encoding="utf-8",
    )
    _run_to_ac(tid, node_id)
    out = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": _minimal_ac("accept")})
    assert out["status"] == "ok"
    persisted = json.loads(
        (tmp_path / "runs" / "threads" / tid / "production" / "rebuttal" / "ac_decision.json").read_text(encoding="utf-8")
    )
    assert persisted["confidence"] == "low"
    assert any("user_goal_anchors_unbound" in r for r in persisted["confidence_downclamp_reasons"])


def test_ac_confidence_downclamped_on_deterministic_dump_dossier(isolated_thread, tmp_path):
    """Rail 4 / Rail 2 signal: dossier with placeholder reason + TBD candidates clamps confidence."""
    tid, node_id = isolated_thread
    market_dir = tmp_path / "runs" / "threads" / tid / "market"
    market_dir.mkdir(parents=True, exist_ok=True)
    (market_dir / "baseline_dossier_candidate.yaml").write_text(
        "selected:\n"
        "  one_paragraph_reason: \"Top-ranked search result for the grilled claim.\"\n"
        "  risk_tags:\n"
        "    - \"operator_should_review\"\n"
        "candidates_index:\n"
        "  - id: c_naive_placeholder\n"
        "    method: \"naive: TBD (Professor will design)\"\n"
        "    decision: selected_as_naive\n",
        encoding="utf-8",
    )
    _run_to_ac(tid, node_id)
    out = M.handle_submit_ac_decision({"thread_id": tid, "ac_decision": _minimal_ac("accept")})
    assert out["status"] == "ok"
    persisted = json.loads(
        (tmp_path / "runs" / "threads" / tid / "production" / "rebuttal" / "ac_decision.json").read_text(encoding="utf-8")
    )
    assert persisted["confidence"] == "low"
    assert any("deterministic_dump" in r for r in persisted["confidence_downclamp_reasons"])
