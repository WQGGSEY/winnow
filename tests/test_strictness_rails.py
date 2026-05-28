"""Unit tests for the strictness-rail helpers introduced after thread_e5b277f9
review. AC-handler integration is covered in test_mcp_rebuttal_loop.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import research_harness.mcp_server as M


# --- Rail 2: deterministic-dump dossier detection ------------------------ #


def test_detect_deterministic_dump_flags_top_ranked_reason(tmp_path):
    bd = tmp_path / "baseline_dossier_candidate.yaml"
    bd.write_text(
        "selected:\n"
        "  one_paragraph_reason: \"Top-ranked search result for the grilled claim.\"\n",
        encoding="utf-8",
    )
    signals = M._detect_deterministic_dump_dossier(bd)
    assert signals
    assert any("placeholder pattern" in s for s in signals)


def test_detect_deterministic_dump_flags_operator_review_risk_tag(tmp_path):
    bd = tmp_path / "baseline_dossier_candidate.yaml"
    bd.write_text(
        "selected:\n"
        "  one_paragraph_reason: \"This is a genuinely operator-reviewed reason that names a paper and a metric.\"\n"
        "  risk_tags: [\"operator_should_review\"]\n",
        encoding="utf-8",
    )
    signals = M._detect_deterministic_dump_dossier(bd)
    assert signals
    assert any("operator-review marker" in s for s in signals)


def test_detect_deterministic_dump_flags_tbd_naive_candidate(tmp_path):
    bd = tmp_path / "baseline_dossier_candidate.yaml"
    bd.write_text(
        "selected:\n"
        "  one_paragraph_reason: \"Real reason citing a paper and metric.\"\n"
        "candidates_index:\n"
        "  - id: c_naive\n"
        "    method: \"naive: TBD (Professor will design)\"\n"
        "    decision: selected_as_naive\n",
        encoding="utf-8",
    )
    signals = M._detect_deterministic_dump_dossier(bd)
    assert signals
    assert any("unresolved placeholder" in s for s in signals)


def test_detect_deterministic_dump_passes_real_dossier(tmp_path):
    bd = tmp_path / "baseline_dossier_candidate.yaml"
    bd.write_text(
        "selected:\n"
        "  one_paragraph_reason: \"Sakana AI Scientist v2 reports AUC=0.86 on the X benchmark with the canonical eval split.\"\n"
        "  risk_tags: [\"reproduction_burden\"]\n"
        "candidates_index:\n"
        "  - id: c1\n"
        "    method: \"momentum_factor_model_lopez_de_prado\"\n"
        "    decision: selected\n"
        "  - id: c2\n"
        "    method: \"shuffle_within_field_null\"\n"
        "    decision: selected_as_random_or_null\n",
        encoding="utf-8",
    )
    signals = M._detect_deterministic_dump_dossier(bd)
    assert signals == []


def test_detect_deterministic_dump_empty_file_no_signal(tmp_path):
    bd = tmp_path / "baseline_dossier_candidate.yaml"
    bd.write_text("", encoding="utf-8")
    assert M._detect_deterministic_dump_dossier(bd) == []


def test_detect_deterministic_dump_missing_file_no_signal(tmp_path):
    assert M._detect_deterministic_dump_dossier(tmp_path / "does_not_exist.yaml") == []


# --- Rail 1: successor verdict aggregator -------------------------------- #


def _build_minimal_tree(tmp_path, monkeypatch, parent_id, child_verdicts):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_agg"
    tree_dir = tmp_path / "runs" / "threads" / tid / "production" / "tree"
    nodes_dir = tree_dir / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)

    nodes = [{"id": parent_id, "parent": None, "status": "promoted"}]
    for i, verdict in enumerate(child_verdicts):
        child_id = f"{parent_id}_c{i}"
        nodes.append({"id": child_id, "parent": parent_id, "status": "pruned"})
        if verdict is not None:
            (nodes_dir / child_id).mkdir(parents=True, exist_ok=True)
            (nodes_dir / child_id / "mcp_professor_decision.json").write_text(
                json.dumps({"node_id": child_id, "final_verdict": verdict}),
                encoding="utf-8",
            )
    (tree_dir / "search_state.json").write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    return tid


def test_aggregator_counts_contradicted_and_blocking_children(tmp_path, monkeypatch):
    tid = _build_minimal_tree(
        tmp_path, monkeypatch, "n_root",
        ["contradicted", "blocked_by_operational_issue", "supported"],
    )
    agg = M._aggregate_successor_verdicts(tid, "n_root")
    assert agg["evaluated"] == 3
    assert agg["negative_count"] == 2
    assert len(agg["blocking_children"]) == 1
    assert agg["blocking_children"][0][1] == "blocked_by_operational_issue"


def test_aggregator_skips_children_without_decision_file(tmp_path, monkeypatch):
    tid = _build_minimal_tree(tmp_path, monkeypatch, "n_root", [None, None, "contradicted"])
    agg = M._aggregate_successor_verdicts(tid, "n_root")
    assert agg["total_children"] == 3
    assert agg["evaluated"] == 1
    assert agg["negative_count"] == 1


def test_aggregator_empty_tree_returns_zeros(tmp_path, monkeypatch):
    tid = _build_minimal_tree(tmp_path, monkeypatch, "n_root", [])
    agg = M._aggregate_successor_verdicts(tid, "n_root")
    assert agg["total_children"] == 0
    assert agg["evaluated"] == 0


# --- Rail 4: confidence down-clamp signal aggregation -------------------- #


def test_downclamp_signals_synthetic_only_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_dc"
    pdir = tmp_path / "runs" / "threads" / tid / "production"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "feasibility_envelope.json").write_text(
        json.dumps({"data_sources_available": [{"kind": "synthetic", "id": "x"}]}),
        encoding="utf-8",
    )
    reasons = M._compute_ac_downclamp_signals(
        tid, {"evaluated": 0, "negative_count": 0}
    )
    assert any("real_adapter" in r for r in reasons)


def test_downclamp_signals_real_adapter_present(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_dc"
    pdir = tmp_path / "runs" / "threads" / tid / "production"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "feasibility_envelope.json").write_text(
        json.dumps({"data_sources_available": [{"kind": "real_adapter", "id": "crsp_v1"}]}),
        encoding="utf-8",
    )
    reasons = M._compute_ac_downclamp_signals(
        tid, {"evaluated": 0, "negative_count": 0}
    )
    assert not any("real_adapter" in r for r in reasons)


def test_downclamp_signals_high_negative_ratio(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    reasons = M._compute_ac_downclamp_signals(
        "t_x", {"evaluated": 3, "negative_count": 2}
    )
    assert any("successor_negative_ratio" in r for r in reasons)


# --- Rail 5: must_revise_root sentinel ----------------------------------- #


def _state_with_promoted_and_children(parent_status_pairs):
    """Build a search_state where 'n_promoted' has the given children statuses."""
    nodes = [
        {"id": "n_root", "parent": None, "status": "pruned"},
        {"id": "n_promoted", "parent": "n_root", "status": "promoted"},
        {"id": "n_alt1", "parent": "n_root", "status": "pruned"},
        {"id": "n_alt2", "parent": "n_root", "status": "pruned"},
    ]
    for cid, status in parent_status_pairs:
        nodes.append({"id": cid, "parent": "n_promoted", "status": status})
    return {"nodes": nodes}


def test_must_revise_root_fires_on_three_negative_children():
    state = _state_with_promoted_and_children([
        ("n_promoted_c1", "pruned"),
        ("n_promoted_c2", "pruned"),
        ("n_promoted_c3", "pruned"),
    ])
    signal = M._detect_must_revise_root_signal(state)
    assert signal is not None
    assert signal["promoted_node_id"] == "n_promoted"
    assert sorted(signal["negative_children"]) == ["n_promoted_c1", "n_promoted_c2", "n_promoted_c3"]
    # dangling alternative roots surface for operator pickup
    assert sorted(signal["alternative_root_candidates"]) == ["n_alt1", "n_alt2"]


def test_must_revise_root_silent_when_any_promoted_child():
    state = _state_with_promoted_and_children([
        ("n_promoted_c1", "pruned"),
        ("n_promoted_c2", "pruned"),
        ("n_promoted_c3", "promoted"),
    ])
    assert M._detect_must_revise_root_signal(state) is None


def test_must_revise_root_silent_below_threshold():
    state = _state_with_promoted_and_children([
        ("n_promoted_c1", "pruned"),
        ("n_promoted_c2", "pruned"),
    ])
    assert M._detect_must_revise_root_signal(state) is None


# --- Rail 3: user_goal anchor extraction + AC binding check ------------- #


def test_anchor_extractor_picks_korean_real_and_scale_triggers():
    from research_harness.agents.grilling import extract_user_goal_anchor_candidates
    ug = ("worldquant에서는 수백만개의 data가 있어. 그런데 실제 데이터들은 "
          "intranet 환경에 있어. 모방한 데이터셋을 수백만 단위로 구성하고 싶어.")
    anchors = extract_user_goal_anchor_candidates(ug)
    kinds = {a["anchor_kind"] for a in anchors}
    assert "data_source" in kinds
    assert "scale" in kinds
    assert all(a["must_be_measured"] for a in anchors)
    assert all(a["bound_metric_key"] is None for a in anchors)
    assert all(a["extraction_source"] == "auto_heuristic" for a in anchors)


def test_anchor_extractor_picks_english_real_and_transfer_triggers():
    from research_harness.agents.grilling import extract_user_goal_anchor_candidates
    ug = "Can a model trained on synthetic data transfer to real production traffic?"
    anchors = extract_user_goal_anchor_candidates(ug)
    kinds = {a["anchor_kind"] for a in anchors}
    assert "data_source" in kinds  # 'real ' and 'production' triggers
    assert "method" in kinds       # 'transfer' trigger


def test_anchor_extractor_empty_goal_returns_empty():
    from research_harness.agents.grilling import extract_user_goal_anchor_candidates
    assert extract_user_goal_anchor_candidates("") == []


def test_anchor_extractor_dedupes_same_window_same_kind():
    from research_harness.agents.grilling import extract_user_goal_anchor_candidates
    # "real" and "real " both match the same window — should dedupe.
    ug = "real data real data"
    anchors = extract_user_goal_anchor_candidates(ug)
    texts = [a["anchor_text"] for a in anchors]
    assert len(texts) == len(set(texts))


def _write_grilling_with_anchors(tmp_path, tid, anchors):
    gdir = tmp_path / "runs" / "threads" / tid / "grilling"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "grilling_session.json").write_text(
        json.dumps({"extracted": {"user_goal_anchors": anchors}}),
        encoding="utf-8",
    )


def test_anchor_check_flags_unbound_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_anchor"
    _write_grilling_with_anchors(tmp_path, tid, [
        {"anchor_text": "real WorldQuant data", "must_be_measured": True, "bound_metric_key": None},
    ])
    out = M._check_user_goal_anchor_bindings(tid, {"metrics": {"x": 1}})
    assert out["unbound"] == ["real WorldQuant data"]
    assert out["unmeasured"] == []


def test_anchor_check_flags_bound_but_unmeasured(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_anchor"
    _write_grilling_with_anchors(tmp_path, tid, [
        {"anchor_text": "real WQ ir", "must_be_measured": True, "bound_metric_key": "real_wq_ir_mean"},
    ])
    out = M._check_user_goal_anchor_bindings(tid, {"metrics": {"synthetic_ir": 0.65}})
    assert out["unbound"] == []
    assert out["unmeasured"] == [("real WQ ir", "real_wq_ir_mean")]


def test_anchor_check_passes_when_bound_and_measured(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_anchor"
    _write_grilling_with_anchors(tmp_path, tid, [
        {"anchor_text": "real WQ ir", "must_be_measured": True, "bound_metric_key": "real_wq_ir_mean"},
    ])
    out = M._check_user_goal_anchor_bindings(tid, {"metrics": {"real_wq_ir_mean": 0.42}})
    assert out["unbound"] == []
    assert out["unmeasured"] == []


def test_anchor_check_skips_must_not_measure(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_anchor"
    _write_grilling_with_anchors(tmp_path, tid, [
        {"anchor_text": "real WQ", "must_be_measured": False, "bound_metric_key": None},
    ])
    out = M._check_user_goal_anchor_bindings(tid, {"metrics": {}})
    assert out == {"unbound": [], "unmeasured": []}


# --- Multi-root: alternative claim formulations ------------------------- #


def test_derive_alternative_formulations_emits_strong_plus_feasibility():
    from research_harness.agents.grilling import derive_alternative_formulations
    out = derive_alternative_formulations(
        primary_claim="synthetic data transfers meaningfully to real production traffic",
        user_goal="…",
    )
    assert len(out) == 2
    kinds = [f["scope_kind"] for f in out]
    assert kinds == ["strong", "feasibility_narrowed"]
    assert all(f["formulation_id"].startswith("acf_") for f in out)
    # Truth conditions differ: strong claim asserts real-data transfer;
    # feasibility-narrowed survives without real-data measurement.
    assert "feasibility scope" in out[1]["claim_under_test"]
    assert out[0]["ranked_priority"] == 1
    assert out[1]["ranked_priority"] == 2


def test_derive_alternative_formulations_empty_primary_returns_empty():
    from research_harness.agents.grilling import derive_alternative_formulations
    assert derive_alternative_formulations(primary_claim="", user_goal="anything") == []


def test_seed_drafts_from_root_accepts_explicit_root_id():
    from research_harness.orchestrator.treesearch.drafts import seed_drafts_from_root
    from research_harness.orchestrator.search_state import initialize_search_state

    def _root(rid, claim):
        return {
            "id": rid, "type": "capability", "status": "ready", "stage": "promotion",
            "domain": "d", "parent": None,
            "claim_contract": {"claim_under_test": claim, "mandatory_baselines": ["b"],
                                "success_criteria": ["s"], "disproof_conditions": ["d"]},
            "baseline_refs": [{"baseline_dossier_id": "bd_x", "candidate_ids": [], "roles": ["current_best_known"]}],
            "lineage": {"root_goal_id": "rg_" + rid, "inherited_assumptions": [], "introduced_assumptions": [],
                         "covers_goal_facets": [], "taste_constraints_applied": []},
            "runtime_profile": {"worker_type": "experiment_worker", "timeout_policy": "task_class_dependent", "turn_budget": 6},
            "failure_retrieval": {"query_tags": [], "selected_fail_files": []},
            "outputs": {"artifacts": [], "verdict": None},
        }

    policy = {"max_depth": 3, "max_debug_depth": 1, "sunk_cost_policy": "default",
              "scaleup_policy": "default", "num_drafts": 2}
    state = initialize_search_state(search_id="s_test", root_node=_root("n_a", "x"), policy=policy)
    # Add a second parent=null root via the same frontier-aware path.
    state["nodes"].append(_root("n_b", "y"))
    state["frontier"].append({"node_id": "n_b", "parent": None, "depth": 0,
                               "priority": 1.0, "stage": "promotion",
                               "status": "queued", "reason": "alt root"})
    # Default (no root_id) → drafts the first parent=None node (n_a)
    draft_ids_a = seed_drafts_from_root(state, num_drafts=2, max_depth=3)
    assert draft_ids_a and all(d.startswith("n_a") for d in draft_ids_a)
    # Explicit root_id=n_b → drafts n_b
    draft_ids_b = seed_drafts_from_root(state, num_drafts=2, max_depth=3, root_id="n_b")
    assert draft_ids_b and all(d.startswith("n_b") for d in draft_ids_b)


def test_get_next_admissible_node_returns_must_revise_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_revise"
    tree_dir = tmp_path / "runs" / "threads" / tid / "production" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    state = _state_with_promoted_and_children([
        ("n_promoted_c1", "pruned"),
        ("n_promoted_c2", "pruned"),
        ("n_promoted_c3", "pruned"),
    ])
    # Empty frontier so the no-admissible-node branch executes.
    state["frontier"] = []
    state["promoted_node_ids"] = ["n_promoted"]
    (tree_dir / "search_state.json").write_text(json.dumps(state), encoding="utf-8")
    out = M.handle_get_next_admissible_node({"thread_id": tid})
    assert out["status"] == "must_revise_root"
    assert out["promoted_node_id"] == "n_promoted"
    assert "revise_root_after_reject" in out["next_tool_choices"]


def test_must_revise_root_surfaces_unseeded_formulations(tmp_path, monkeypatch):
    """Rail 5 + multi-root: when sentinel fires AND grilling has unseeded
    formulations, surface them so the operator can call
    seed_alternative_root_formulation rather than hand-roll a new claim."""
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_revise"
    tree_dir = tmp_path / "runs" / "threads" / tid / "production" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    state = _state_with_promoted_and_children([
        ("n_promoted_c1", "pruned"),
        ("n_promoted_c2", "pruned"),
        ("n_promoted_c3", "pruned"),
    ])
    state["frontier"] = []
    state["promoted_node_ids"] = ["n_promoted"]
    (tree_dir / "search_state.json").write_text(json.dumps(state), encoding="utf-8")
    gdir = tmp_path / "runs" / "threads" / tid / "grilling"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "grilling_session.json").write_text(json.dumps({
        "extracted": {"alternative_claim_formulations": [
            {"formulation_id": "acf_strong_x", "scope_kind": "strong",
             "scope_note": "deployment", "ranked_priority": 1, "claim_under_test": "x"},
            {"formulation_id": "acf_feasibility_x", "scope_kind": "feasibility_narrowed",
             "scope_note": "feasibility", "ranked_priority": 2, "claim_under_test": "x narrow"},
        ]}
    }), encoding="utf-8")
    out = M.handle_get_next_admissible_node({"thread_id": tid})
    assert out["status"] == "must_revise_root"
    assert "seed_alternative_root_formulation" in out["next_tool_choices"]
    assert out["next_tool_choices"][0] == "seed_alternative_root_formulation"
    assert len(out["unseeded_alternative_formulations"]) == 2
    assert out["unseeded_alternative_formulations"][0]["scope_kind"] == "strong"


def test_seed_alternative_root_formulation_adds_parallel_root(tmp_path, monkeypatch):
    """seed_alternative_root_formulation: happy path — formulation is pulled
    from grilling, a new parent=null root is appended, drafts get seeded
    under it, and the original root remains untouched."""
    from research_harness.orchestrator.search_state import initialize_search_state

    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    monkeypatch.setattr(M, "_repo_root", lambda: Path(__file__).resolve().parents[1])

    tid = "t_seed"
    pdir = tmp_path / "runs" / "threads" / tid / "production"
    tree_dir = pdir / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    primary_root = {
        "id": "n_primary_root", "type": "validity", "status": "ready", "stage": "promotion",
        "domain": "d", "parent": None,
        "claim_contract": {"claim_under_test": "primary claim",
                            "mandatory_baselines": ["b"], "success_criteria": ["s"],
                            "disproof_conditions": ["d"]},
        "baseline_refs": [{"baseline_dossier_id": "bd_x", "candidate_ids": [],
                            "roles": ["current_best_known"]}],
        "lineage": {"root_goal_id": "rg_thing", "inherited_assumptions": [],
                     "introduced_assumptions": [], "covers_goal_facets": [],
                     "taste_constraints_applied": []},
        "runtime_profile": {"worker_type": "experiment_worker",
                             "timeout_policy": "task_class_dependent", "turn_budget": 6},
        "failure_retrieval": {"query_tags": [], "selected_fail_files": []},
        "outputs": {"artifacts": [], "verdict": None},
    }
    policy = {"max_depth": 3, "max_debug_depth": 1, "sunk_cost_policy": "default",
              "scaleup_policy": "default", "num_drafts": 2}
    state = initialize_search_state(search_id="s_x", root_node=primary_root, policy=policy)
    (tree_dir / "search_state.json").write_text(json.dumps(state), encoding="utf-8")

    gdir = tmp_path / "runs" / "threads" / tid / "grilling"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "grilling_session.json").write_text(json.dumps({
        "session_id": "grill_test", "status": "done", "user_goal": "primary",
        "max_rounds": 1, "rounds": [], "model": "mock",
        "created_at": "2026-05-29T00:00:00+00:00",
        "usage_estimate": {"rounds_used": 0, "total_cost_usd": 0.0,
                            "total_input_tokens": 0, "total_output_tokens": 0},
        "extracted": {
            "root_goal_id": "rg_thing", "domain": "d", "node_type": "validity",
            "claim_under_test": "primary claim",
            "mandatory_baselines": ["b"], "success_criteria": ["s"],
            "disproof_conditions": ["d"], "goal_facets": [], "taste_constraints": [],
            "search_query_seed": "x",
            "alternative_claim_formulations": [
                {"formulation_id": "acf_feasibility_alt", "scope_kind": "feasibility_narrowed",
                 "scope_note": "feasibility variant", "ranked_priority": 2,
                 "claim_under_test": "primary claim at feasibility scope"},
            ],
        },
    }), encoding="utf-8")

    out = M.handle_seed_alternative_root_formulation({
        "thread_id": tid, "formulation_id": "acf_feasibility_alt",
    })
    assert out["status"] == "ok"
    assert out["new_root_id"].endswith("acf_feasibility_alt")
    assert out["scope_kind"] == "feasibility_narrowed"
    assert out["seeded_draft_ids"], "drafts should have been seeded under the new root"

    persisted = json.loads((tree_dir / "search_state.json").read_text(encoding="utf-8"))
    parent_null_nodes = [n for n in persisted["nodes"] if n.get("parent") is None]
    assert len(parent_null_nodes) == 2, "both primary and alternative roots must be parent=null"
    assert {n["id"] for n in parent_null_nodes} == {"n_primary_root", out["new_root_id"]}
    # Primary root's claim must be untouched.
    primary = next(n for n in persisted["nodes"] if n["id"] == "n_primary_root")
    assert primary["claim_contract"]["claim_under_test"] == "primary claim"
    # Alt root carries the formulation's claim.
    alt = next(n for n in persisted["nodes"] if n["id"] == out["new_root_id"])
    assert alt["claim_contract"]["claim_under_test"] == "primary claim at feasibility scope"


def test_seed_alternative_root_formulation_rejects_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_seed"
    gdir = tmp_path / "runs" / "threads" / tid / "grilling"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "grilling_session.json").write_text(json.dumps({
        "extracted": {"alternative_claim_formulations": [
            {"formulation_id": "acf_one", "scope_kind": "strong",
             "scope_note": "x", "ranked_priority": 1, "claim_under_test": "x"},
        ]}
    }), encoding="utf-8")
    out = M.handle_seed_alternative_root_formulation({
        "thread_id": tid, "formulation_id": "acf_does_not_exist",
    })
    assert out["status"] == "rejected"
    assert "not found" in out["reason"]


def test_seed_alternative_root_formulation_requires_grilling(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    out = M.handle_seed_alternative_root_formulation({
        "thread_id": "t_missing", "formulation_id": "acf_x",
    })
    assert out["status"] == "rejected"
    assert "grilling_session" in out["reason"]


# --- Hands-free auto-resolver: inline dispatch from Rail 5 -------------- #


def _grilling_with_two_formulations(tmp_path, tid):
    gdir = tmp_path / "runs" / "threads" / tid / "grilling"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "grilling_session.json").write_text(json.dumps({
        "session_id": "grill_test", "status": "done", "user_goal": "primary",
        "max_rounds": 1, "rounds": [], "model": "mock",
        "created_at": "2026-05-29T00:00:00+00:00",
        "usage_estimate": {"rounds_used": 0, "total_cost_usd": 0.0,
                            "total_input_tokens": 0, "total_output_tokens": 0},
        "extracted": {
            "root_goal_id": "rg_thing", "domain": "d", "node_type": "validity",
            "claim_under_test": "primary claim",
            "mandatory_baselines": ["b"], "success_criteria": ["s"],
            "disproof_conditions": ["d"], "goal_facets": [], "taste_constraints": [],
            "search_query_seed": "x",
            "alternative_claim_formulations": [
                {"formulation_id": "acf_strong_primary", "scope_kind": "strong",
                 "scope_note": "strong", "ranked_priority": 1,
                 "claim_under_test": "primary claim"},
                {"formulation_id": "acf_feasibility_primary",
                 "scope_kind": "feasibility_narrowed",
                 "scope_note": "feasibility variant", "ranked_priority": 2,
                 "claim_under_test": "primary at feasibility scope"},
            ],
        },
    }), encoding="utf-8")


def _seeded_primary_state(tmp_path, tid, primary_root_id="n_synth_test_root"):
    from research_harness.orchestrator.search_state import initialize_search_state

    tree_dir = tmp_path / "runs" / "threads" / tid / "production" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    primary_root = {
        "id": primary_root_id, "type": "validity", "status": "promoted",
        "stage": "promotion", "domain": "d", "parent": None,
        "claim_contract": {"claim_under_test": "primary",
                            "mandatory_baselines": ["b"], "success_criteria": ["s"],
                            "disproof_conditions": ["d"]},
        "baseline_refs": [{"baseline_dossier_id": "bd_x", "candidate_ids": [],
                            "roles": ["current_best_known"]}],
        "lineage": {"root_goal_id": "rg_thing", "inherited_assumptions": [],
                     "introduced_assumptions": [], "covers_goal_facets": [],
                     "taste_constraints_applied": []},
        "runtime_profile": {"worker_type": "experiment_worker",
                             "timeout_policy": "task_class_dependent", "turn_budget": 6},
        "failure_retrieval": {"query_tags": [], "selected_fail_files": []},
        "outputs": {"artifacts": [], "verdict": None},
    }
    policy = {"max_depth": 3, "max_debug_depth": 1, "sunk_cost_policy": "default",
              "scaleup_policy": "default", "num_drafts": 2}
    state = initialize_search_state(search_id="s_x", root_node=primary_root, policy=policy)
    # Three pruned children → Rail 5 fires.
    for i in range(1, 4):
        cid = f"{primary_root_id}_c{i}"
        state["nodes"].append({**primary_root, "id": cid, "parent": primary_root_id, "status": "pruned"})
        state["frontier"].append({"node_id": cid, "parent": primary_root_id, "depth": 1,
                                   "priority": 0.5, "stage": "promotion", "status": "pruned",
                                   "reason": "test"})
    state["promoted_node_ids"] = [primary_root_id]
    # Empty frontier so get_next_admissible_node falls through to the
    # no-admissible-node branch where Rail 5 fires.
    state["frontier"] = []
    (tree_dir / "search_state.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def test_rail5_auto_dispatches_seed_alternative_root(tmp_path, monkeypatch):
    """Hands-free: must_revise_root with unseeded formulation → server inline
    calls seed_alternative_root_formulation; response carries auto_resolved."""
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    monkeypatch.setattr(M, "_repo_root", lambda: Path(__file__).resolve().parents[1])
    tid = "t_handsfree"
    _seeded_primary_state(tmp_path, tid)
    _grilling_with_two_formulations(tmp_path, tid)
    out = M.handle_get_next_admissible_node({"thread_id": tid})
    assert out["status"] == "must_revise_root"
    assert "auto_action_suggestion" in out
    assert out["auto_action_suggestion"]["tool"] == "seed_alternative_root_formulation"
    assert out["auto_action_suggestion"]["args"]["formulation_id"] == "acf_strong_primary"  # priority 1
    # Server auto-dispatched the suggestion inline.
    assert "auto_resolved" in out
    assert out["auto_resolved"]["dispatched"] is True
    assert out["auto_resolved"]["result"]["status"] == "ok"
    # The new root is now in search_state.
    state_after = json.loads(
        (tmp_path / "runs" / "threads" / tid / "production" / "tree" / "search_state.json").read_text(encoding="utf-8")
    )
    parent_null = [n for n in state_after["nodes"] if n.get("parent") is None]
    assert len(parent_null) == 2
    # Auto-action persisted to history.
    history_path = tmp_path / "runs" / "threads" / tid / "production" / "auto_actions.jsonl"
    assert history_path.exists()
    history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line]
    assert history[-1]["outcome"] == "ok"


def test_rail5_no_auto_dispatch_when_no_unseeded_formulations(tmp_path, monkeypatch):
    """Rail 5 fires but grilling has no alternatives → no auto_action_suggestion."""
    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    tid = "t_no_alt"
    _seeded_primary_state(tmp_path, tid)
    # No grilling file → no formulations.
    out = M.handle_get_next_admissible_node({"thread_id": tid})
    assert out["status"] == "must_revise_root"
    assert "auto_action_suggestion" not in out
    assert "auto_resolved" not in out


def test_auto_dispatch_refuses_loop_on_repeated_failure(tmp_path, monkeypatch):
    """Safety: if the same auto-action already failed, server records the
    refusal and does NOT dispatch again — chain breaks, operator escalates."""
    from research_harness.orchestrator.auto_resolver import (
        pick_auto_action, record_auto_action,
    )

    monkeypatch.setattr(M, "_thread_dir", lambda tid: tmp_path / "runs" / "threads" / tid)
    monkeypatch.setattr(M, "_repo_root", lambda: Path(__file__).resolve().parents[1])
    tid = "t_loop"
    _seeded_primary_state(tmp_path, tid)
    _grilling_with_two_formulations(tmp_path, tid)
    # Pre-poison history with a failed attempt for the priority-1 formulation.
    poisoned_action = pick_auto_action({"auto_action_suggestion": {
        "tool": "seed_alternative_root_formulation",
        "args": {"thread_id": tid, "formulation_id": "acf_strong_primary"},
        "source_rail": "rail_5_must_revise_root",
        "rationale": "earlier attempt", "confidence": "high",
    }})
    record_auto_action(
        tmp_path / "runs" / "threads" / tid, poisoned_action,
        outcome="rejected", dispatch_result={"status": "rejected"},
    )
    out = M.handle_get_next_admissible_node({"thread_id": tid})
    assert out["status"] == "must_revise_root"
    assert out["auto_resolved"]["dispatched"] is False
    assert "identical" in out["auto_resolved"]["reason"]
