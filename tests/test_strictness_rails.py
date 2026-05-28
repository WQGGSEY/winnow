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
