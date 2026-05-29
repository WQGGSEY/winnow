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
    # ADR 0006 rev.2: a pass now requires the harness to have MEASURED that B is
    # structurally distinct from A (measured_behavioral_distance, supplied by the
    # handler that ran both generators), not just a high rho.
    r = F.compute_falsifier_result(
        thread_id="t",
        falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [3, 1, 2, 4], "ranking_b": [3, 1, 2, 4]},
        measured_behavioral_distance=0.5,
    )
    assert r["passed"] is True
    assert r["verdict"] == "passed"
    assert r["observed"] == pytest.approx(1.0)
    assert r["produced_by"] == "harness_falsifier_module"
    validate_named_schema("falsifier_result", r)


def test_compute_cross_generator_fail_on_inverted_ranking():
    r = F.compute_falsifier_result(
        thread_id="t",
        falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [1, 2, 3, 4], "ranking_b": [4, 3, 2, 1]},
        measured_behavioral_distance=0.5,
    )
    assert r["passed"] is False
    assert r["verdict"] == "failed"  # clean non-transfer (guards ok, predicate not met)


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


# --- ADR 0006 rev.2: behavioral-distance guards ------------------------- #


def test_behavioral_distance_identical_and_distinct():
    same = {"x": [0.0, 1.0, 2.0, 3.0], "y": [1.0, 1.0, 1.0, 1.0]}
    assert F.behavioral_distance(same, same) == pytest.approx(0.0)
    far = {"x": [10.0, 11.0, 12.0, 13.0], "y": [1.0, 1.0, 1.0, 1.0]}
    assert F.behavioral_distance(same, far) > 0.15
    # Disjoint schemas are trivially distinct.
    assert F.behavioral_distance({"x": [1.0]}, {"z": [1.0]}) == pytest.approx(1.0)


def test_rank_discrimination_flags_constant_and_tied():
    good = F.rank_discrimination([3, 1, 2, 4])
    assert good["variance"] > 0.05 and good["tie_fraction"] == 0.0
    flat = F.rank_discrimination([5, 5, 5, 5])
    assert flat["variance"] == 0.0 and flat["tie_fraction"] == pytest.approx(0.75)


def test_null_floor_rejects_too_low_a_bar_for_tiny_n():
    # n=3 -> null std = 1/sqrt(2) ~ 0.707; a >= 0.3 bar is chance-clearable.
    assert F.null_floor_satisfied(3, ">=", 0.3, 1.0) is False
    assert F.null_floor_satisfied(3, ">=", 0.8, 1.0) is True
    # Larger n tightens the null band so a moderate bar is fine.
    assert F.null_floor_satisfied(26, ">=", 0.6, 1.0) is True


def test_cross_generator_uninformative_without_generators():
    # No harness-loadable generators -> distinctness cannot be measured.
    r = F.compute_falsifier_result(
        thread_id="t", falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [3, 1, 2, 4], "ranking_b": [3, 1, 2, 4]},
        measured_behavioral_distance=None,
    )
    assert r["passed"] is False
    assert r["verdict"] == "uninformative"
    assert r["guards"]["behavioral_distance"]["ok"] is False


def test_cross_generator_uninformative_when_B_not_distinct():
    # Generators measured but NON-distinct (distance below threshold).
    r = F.compute_falsifier_result(
        thread_id="t", falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [3, 1, 2, 4], "ranking_b": [3, 1, 2, 4]},
        measured_behavioral_distance=0.01,
    )
    assert r["passed"] is False
    assert r["verdict"] == "uninformative"


def test_cross_generator_degenerate_on_tied_rankings():
    # 4 of 5 tied (tie_fraction 0.6 > 0.5) -> rankings do not discriminate.
    r = F.compute_falsifier_result(
        thread_id="t", falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [5, 5, 5, 5, 6], "ranking_b": [5, 5, 5, 5, 6]},
        measured_behavioral_distance=0.5,
    )
    assert r["passed"] is False
    assert r["verdict"] == "degenerate"


def test_cross_generator_degenerate_on_constant_ranking():
    # All-equal -> Spearman undefined; compute maps it to a verdict, not a raise.
    r = F.compute_falsifier_result(
        thread_id="t", falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [7, 7, 7, 7], "ranking_b": [1, 2, 3, 4]},
        measured_behavioral_distance=0.5,
    )
    assert r["verdict"] == "degenerate"
    assert r["passed"] is False


def test_cross_generator_passes_with_distinct_generators_and_signal():
    r = F.compute_falsifier_result(
        thread_id="t", falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [1, 2, 3, 4, 5], "ranking_b": [1, 2, 3, 5, 4]},
        measured_behavioral_distance=0.4,
    )
    assert r["verdict"] == "passed"
    assert r["passed"] is True


def test_c8919361_hollow_rho_is_blocked():
    # KEYSTONE acceptance: a perfect rho computed against a generator B that is
    # NOT structurally distinct from A (same DGP -> behavioral distance 0) is the
    # thread_c8919361 hollow pass. It must NOT be 'passed'; rho is not transfer
    # evidence. A label/hash could not catch this; the measured distance does.
    r = F.compute_falsifier_result(
        thread_id="thread_c8919361", falsifier=_xgen_falsifier(),
        evidence={"ranking_a": [3, 1, 2, 4], "ranking_b": [3, 1, 2, 4]},
        measured_behavioral_distance=0.0,  # same DGP on the harness probe
    )
    assert r["observed"] == pytest.approx(1.0)  # rho is still 1.0 ...
    assert r["passed"] is False                 # ... but it does NOT pass
    assert r["verdict"] == "uninformative"


# --- ADR 0006 rev.2: handler runs the probe end-to-end ------------------ #


def _xgen_recipe(mu):
    return {"shape": "tabular", "columns": [{"name": "f", "distribution": "normal", "params": {"mu": mu, "sigma": 1.0}}]}


def _setup_falsifier_repo(tmp_path, monkeypatch, tid, falsifier):
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    monkeypatch.setattr(M, "_repo_root", lambda: tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"data_adapters": {"registered": []}}))
    pdir = tmp_path / "runs" / "threads" / tid / "production"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "feasibility_envelope.json").write_text(
        json.dumps(_base_envelope(tid, external_falsifier=falsifier))
    )


def test_handler_probe_passes_on_distinct_recipes(tmp_path, monkeypatch):
    tid = "t_probe_pass"
    _setup_falsifier_repo(tmp_path, monkeypatch, tid, _xgen_falsifier())
    out = M.handle_compute_falsifier_result({
        "thread_id": tid,
        "evidence": {
            "ranking_a": [1, 2, 3, 4, 5],
            "ranking_b": [1, 2, 3, 5, 4],
            "generator_a": _xgen_recipe(0.0),
            "generator_b": _xgen_recipe(5.0),  # structurally distinct: shifted mean
        },
    })
    assert out["status"] == "ok"
    assert out["verdict"] == "passed"
    assert out["guards"]["behavioral_distance"]["observed"] > 0.15


def test_handler_probe_uninformative_on_identical_recipes(tmp_path, monkeypatch):
    tid = "t_probe_uninf"
    _setup_falsifier_repo(tmp_path, monkeypatch, tid, _xgen_falsifier())
    out = M.handle_compute_falsifier_result({
        "thread_id": tid,
        "evidence": {
            "ranking_a": [3, 1, 2, 4],
            "ranking_b": [3, 1, 2, 4],
            "generator_a": _xgen_recipe(0.0),
            "generator_b": _xgen_recipe(0.0),  # SAME DGP -> distance 0
        },
    })
    assert out["status"] == "ok"
    assert out["verdict"] == "uninformative"
    assert out["passed"] is False


# --- ADR 0009 (B3): patchwork-probe axis bound to the frozen referent --- #


def test_patchwork_probe_routing_pure():
    # X-as-target gates the claim; method-as-subject routes to the design axis;
    # absent defaults conservatively to gating the claim.
    assert F.patchwork_probe_applies_to_claim("method_is_solution") is True
    assert F.patchwork_probe_applies_to_claim(None) is True
    assert F.patchwork_probe_applies_to_claim("method_is_subject") is False


def test_falsifier_stamps_probe_axis_from_frozen_referent(tmp_path, monkeypatch):
    tid = "t_probe_axis"
    _setup_falsifier_repo(tmp_path, monkeypatch, tid, _xgen_falsifier())
    # The screen thread: the METHOD is the test subject -> design-axis routing,
    # read from the IMMUTABLE frozen question (not a per-node judgment).
    (tmp_path / "runs" / "threads" / tid / "production" / "frozen_question.json").write_text(
        json.dumps({"question_id": "q1", "subject_role": "method_is_subject"})
    )
    M.handle_compute_falsifier_result({
        "thread_id": tid,
        "evidence": {"ranking_a": [1, 2, 3, 4], "ranking_b": [1, 2, 4, 3],
                     "generator_a": _xgen_recipe(0.0), "generator_b": _xgen_recipe(5.0)},
    })
    fr = json.loads(
        (tmp_path / "runs" / "threads" / tid / "production" / "rebuttal"
         / "falsifier_result.json").read_text()
    )
    axis = fr["guards"]["patchwork_probe_axis"]
    assert axis["subject_role"] == "method_is_subject"
    assert axis["applies_to"] == "screen_design_axis"
    assert axis["subject_role_source"] == "frozen_question"


def test_falsifier_probe_axis_defaults_to_claim_when_unpinned(tmp_path, monkeypatch):
    tid = "t_probe_axis_default"
    _setup_falsifier_repo(tmp_path, monkeypatch, tid, _xgen_falsifier())  # no frozen_question
    M.handle_compute_falsifier_result({
        "thread_id": tid,
        "evidence": {"ranking_a": [1, 2, 3, 4], "ranking_b": [1, 2, 4, 3],
                     "generator_a": _xgen_recipe(0.0), "generator_b": _xgen_recipe(5.0)},
    })
    fr = json.loads(
        (tmp_path / "runs" / "threads" / tid / "production" / "rebuttal"
         / "falsifier_result.json").read_text()
    )
    axis = fr["guards"]["patchwork_probe_axis"]
    assert axis["applies_to"] == "claim_falsifier"  # conservative default
    assert axis["subject_role_source"] == "default"


def test_frozen_question_schema_accepts_subject_role():
    validate_named_schema("frozen_question", {
        "thread_id": "t", "question_id": "q1",
        "formal_statement": "f" * 45, "true_iff": "the answer is yes when " + "x" * 20,
        "subject_role": "method_is_subject",
        "source_artifact": "operator_pinned", "source_provenance": "pin_abcd1234",
        "frozen_at_phase": "production_entry",
    })


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
        measured_behavioral_distance=0.5,  # a genuine passing screen
    )
    assert fr["verdict"] == "passed"
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


# --- ADR 0007 rev.2 (A2): attemptability stamped from envelope membership #


def test_attemptability_stamped_from_envelope_membership(tmp_path, monkeypatch):
    tid = "t_attempt"
    _setup_thread(tmp_path, monkeypatch, tid, _base_envelope(tid))  # data source id "g"
    att = _attestation(False, with_followup=True)
    att["required_additional_research"] = [
        {"axis": "validity", "experiment": "e" * 25, "rationale": "r" * 25},
        {"axis": "validity", "experiment": "e" * 25, "rationale": "r" * 25,
         "required_resources": [{"kind": "data_source", "ref": "g"}]},          # present
        {"axis": "validity", "experiment": "e" * 25, "rationale": "r" * 25,
         "required_resources": [{"kind": "data_source", "ref": "wq_real"}]},     # absent
        {"axis": "validity", "experiment": "e" * 25, "rationale": "r" * 25,
         "required_resources": [{"kind": "llm_oracle", "ref": "live_anthropic_api"}]},  # absent
    ]
    out = M.handle_submit_professor_user_goal_attestation({"thread_id": tid, "attestation": att})
    assert out["status"] == "ok"
    persisted = json.loads(
        (tmp_path / "runs" / "threads" / tid / "production" / "rebuttal"
         / "user_goal_attestation.json").read_text()
    )
    flags = [r["attemptable_in_envelope"] for r in persisted["required_additional_research"]]
    # no resources -> attemptable; present -> attemptable (misdeclaration caught);
    # absent data_source / absent oracle -> not attemptable.
    assert flags == [True, True, False, False]
    assert len(persisted["attemptability_audit"]) == 2


def test_attemptability_overwrites_llm_authored_value(tmp_path, monkeypatch):
    # alpha2: the LLM cannot author attemptable_in_envelope — the harness derives it.
    tid = "t_attempt_overwrite"
    _setup_thread(tmp_path, monkeypatch, tid, _base_envelope(tid))
    att = _attestation(False, with_followup=True)
    att["required_additional_research"] = [
        {"axis": "validity", "experiment": "e" * 25, "rationale": "r" * 25,
         "required_resources": [{"kind": "data_source", "ref": "wq_real"}],  # genuinely absent
         "attemptable_in_envelope": True},  # LLM lie — must be overwritten to False
    ]
    M.handle_submit_professor_user_goal_attestation({"thread_id": tid, "attestation": att})
    persisted = json.loads(
        (tmp_path / "runs" / "threads" / tid / "production" / "rebuttal"
         / "user_goal_attestation.json").read_text()
    )
    assert persisted["required_additional_research"][0]["attemptable_in_envelope"] is False


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
