"""ADR 0009 (B1): frozen domain taxonomy + deterministic far-framing successors.

Distance feeds spawn selection ONLY (deterministic top-k over a pinned, immutable
operator table) — never promotion/winner-selection (alpha1 / #5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import research_harness.mcp_server as M
from research_harness import domain_taxonomy as DT
from research_harness.orchestrator.search_state import (
    initialize_search_state,
    search_policy_from_config,
)
from research_harness.orchestrator.search_state import add_child_nodes
from research_harness.orchestrator.treesearch.parallel_agent import (
    _build_far_framing_successors,
    _build_synthesis_node,
)
from research_harness.schemas.validator import validate_named_schema

REPO = Path(__file__).resolve().parents[1]


def _taxonomy():
    return {
        "version": "1",
        "domains": [
            {"id": "agent_harness", "label": "Agent harness"},
            {"id": "quant_finance", "label": "Quant finance"},
            {"id": "ecology", "label": "Ecology"},
            {"id": "immunology", "label": "Immunology"},
            {"id": "linguistics", "label": "Linguistics"},
        ],
        "distances": {
            "agent_harness|quant_finance": 0.35,
            "agent_harness|ecology": 0.78,
            "agent_harness|immunology": 0.84,
            "agent_harness|linguistics": 0.72,
        },
    }


# --- pure selector ------------------------------------------------------- #


def test_select_far_domains_topk_threshold_and_deterministic():
    sel = DT.select_far_domains("agent_harness", _taxonomy(), top_k=3, threshold=0.6)
    # far first, deterministic; quant_finance (0.35) excluded by threshold.
    assert sel == ["immunology", "ecology", "linguistics"]
    # Lower top_k truncates the same ordering.
    assert DT.select_far_domains("agent_harness", _taxonomy(), top_k=1, threshold=0.6) == ["immunology"]


def test_select_far_domains_unlisted_native_and_pairs():
    # Native domain not in the table -> nothing selectable.
    assert DT.select_far_domains("astronomy", _taxonomy(), top_k=3, threshold=0.6) == []
    # An unlisted pair is unknown distance -> not selectable (quant_finance has
    # no listed distance to ecology here).
    sel = DT.select_far_domains("quant_finance", _taxonomy(), top_k=3, threshold=0.6)
    assert sel == []  # only agent_harness|quant_finance is listed (0.35 < 0.6)


def test_taxonomy_hash_stable_and_content_sensitive():
    a = DT.taxonomy_hash(_taxonomy())
    assert a == DT.taxonomy_hash(_taxonomy())
    t2 = _taxonomy()
    t2["distances"]["agent_harness|ecology"] = 0.9
    assert DT.taxonomy_hash(t2) != a


# --- far-framing successor builder -------------------------------------- #


def _parent(nid="n_root", domain="agent_harness"):
    return {
        "id": nid, "type": "capability", "status": "promoted", "domain": domain,
        "stage": "experimentation", "parent": None,
        "lineage": {
            "root_goal_id": "rg1", "covers_goal_facets": [],
            "inherited_assumptions": [], "introduced_assumptions": [],
            "taste_constraints_applied": [],
        },
        "claim_contract": {
            "claim_under_test": "Method M beats baseline on the home task",
            "mandatory_baselines": ["naive: z"], "success_criteria": ["c1"],
            "disproof_conditions": ["d1"],
        },
        "baseline_refs": [],
        "runtime_profile": {"worker_type": "experiment_worker", "timeout_policy": "default"},
        "failure_retrieval": {"query_tags": ["capability"], "selected_fail_files": []},
        "outputs": {"artifacts": [], "verdict": None},
    }


def test_build_far_framing_successors_mints_one_per_domain():
    kids = _build_far_framing_successors(
        _parent(), ["ecology", "immunology"], parent_depth=0, max_depth=5,
    )
    assert [k["domain"] for k in kids] == ["ecology", "immunology"]
    for k in kids:
        assert k["lineage"]["far_framing_domain"] == k["domain"]
        assert k["domain"] in k["claim_contract"]["claim_under_test"]
        assert "forbidden_approaches" not in k["claim_contract"]  # positive parent
        validate_named_schema("node", k)


def test_build_far_framing_forbids_familiar_for_negative_parent():
    kids = _build_far_framing_successors(
        _parent(), ["ecology"], parent_depth=0, max_depth=5, forbid_parent_approach=True,
    )
    forb = kids[0]["claim_contract"]["forbidden_approaches"]
    assert forb and "Method M beats baseline" in forb[0]
    validate_named_schema("node", kids[0])


def test_build_far_framing_respects_depth_cap():
    assert _build_far_framing_successors(_parent(), ["ecology"], parent_depth=5, max_depth=5) == []


# --- pin tool: immutable, hash-stamped ---------------------------------- #


def test_pin_domain_taxonomy_explicit_and_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    tid = "t_pin"
    out = M.handle_pin_domain_taxonomy({"thread_id": tid, "taxonomy": _taxonomy()})
    assert out["status"] == "ok"
    digest = out["taxonomy_hash"]
    # Idempotent re-pin of identical content.
    again = M.handle_pin_domain_taxonomy({"thread_id": tid, "taxonomy": _taxonomy()})
    assert again["status"] == "ok" and again["taxonomy_hash"] == digest
    # A different table is refused — the construction may not swap the referent.
    t2 = _taxonomy()
    t2["distances"]["agent_harness|ecology"] = 0.99
    refused = M.handle_pin_domain_taxonomy({"thread_id": tid, "taxonomy": t2})
    assert refused["status"] == "rejected" and "IMMUTABLE" in refused["reason"]


def test_pin_domain_taxonomy_defaults_to_repo_config(tmp_path, monkeypatch):
    # Default source is the operator-curated configs/domain_taxonomy.json.
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    monkeypatch.setattr(M, "_repo_root", lambda: REPO)
    out = M.handle_pin_domain_taxonomy({"thread_id": "t_default"})
    assert out["status"] == "ok"
    assert "agent_harness" in out["domains"]


# --- minting integration + alpha1/#5 invariant -------------------------- #


def test_maybe_mint_far_framing_creates_children_when_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    monkeypatch.setattr(M, "_repo_root", lambda: REPO)  # real settings.json (far_framing enabled)
    tid = "t_mint"
    M.handle_pin_domain_taxonomy({"thread_id": tid, "taxonomy": _taxonomy()})
    node = _parent()
    state = initialize_search_state(
        search_id="s1", root_node=node, policy=search_policy_from_config(REPO)
    )
    ids = M._maybe_mint_far_framing(tid, state, node, forbid=False)
    assert len(ids) == 3  # top_k=3 default
    minted = [n for n in state["nodes"] if n["id"] in ids]
    assert all(n["lineage"]["far_framing_domain"] for n in minted)


def test_far_framing_disabled_mints_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    monkeypatch.setattr(M, "_repo_root", lambda: REPO)
    monkeypatch.setattr(M, "_far_framing_config", lambda s: {"enabled": False, "top_k": 3, "distance_threshold": 0.6})
    tid = "t_off"
    M.handle_pin_domain_taxonomy({"thread_id": tid, "taxonomy": _taxonomy()})
    node = _parent()
    state = {"nodes": [node], "frontier": [], "max_depth": 5}
    assert M._maybe_mint_far_framing(tid, state, node, forbid=False) == []


def test_distance_is_spawn_only_never_promotion():
    # alpha1 / #5: distance / taxonomy must not be referenced in the
    # reduction (promotion / winner-selection) path.
    red = (REPO / "research_harness" / "orchestrator" / "reduction.py").read_text()
    for token in ("domain_taxonomy", "select_far_domains", "far_framing_domain", "distance"):
        assert token not in red, f"{token!r} leaked into the promotion/reduction path"


# --- ADR 0009 (B2): synthesis node, no privileged novelty --------------- #


def test_synthesis_type_is_schedulable():
    # A synthesis node must be selectable (type weight + stage admission) — but
    # it runs LAST and earns no scheduling privilege.
    assert "synthesis" in M._TYPE_WEIGHTS
    assert M._TYPE_WEIGHTS["synthesis"] == max(M._TYPE_WEIGHTS.values())
    assert "synthesis" in M._STAGES[-1]["admits"]


def test_build_synthesis_node_recombines_siblings():
    parent = _parent()
    fars = _build_far_framing_successors(parent, ["ecology", "immunology"], parent_depth=0, max_depth=5)
    synth = _build_synthesis_node(parent, fars)
    assert synth["type"] == "synthesis"
    assert synth["lineage"]["synthesis_inputs"] == sorted(c["id"] for c in fars)
    assert "far_framing_domain" not in synth["lineage"]  # a synthesis is not a far node
    assert any("additive" in d for d in synth["claim_contract"]["disproof_conditions"])
    validate_named_schema("node", synth)
    # Needs >= 2 siblings to recombine.
    assert _build_synthesis_node(parent, fars[:1]) is None


def test_maybe_mint_synthesis_waits_for_all_siblings_then_idempotent(monkeypatch):
    parent = _parent()
    state = initialize_search_state(
        search_id="s1", root_node=parent, policy=search_policy_from_config(REPO)
    )
    fars = _build_far_framing_successors(parent, ["ecology", "immunology"], parent_depth=0, max_depth=5)
    add_child_nodes(state, parent["id"], fars, reason="far")
    far_ids = {f["id"] for f in fars}
    # Not all terminal yet -> no synthesis.
    assert M._maybe_mint_synthesis("t", state, fars[0]) is None
    for n in state["nodes"]:
        if n["id"] in far_ids:
            n["status"] = "promoted"
    synth_id = M._maybe_mint_synthesis("t", state, fars[0])
    assert synth_id == f"{parent['id']}_synth"
    synth = next(n for n in state["nodes"] if n["id"] == synth_id)
    assert synth["lineage"]["synthesis_inputs"] == sorted(far_ids)
    # Idempotent: a synthesis already exists.
    assert M._maybe_mint_synthesis("t", state, fars[0]) is None


def test_far_framing_does_not_recurse_from_synthesis_or_far_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    monkeypatch.setattr(M, "_repo_root", lambda: REPO)
    tid = "t_norecurse"
    M.handle_pin_domain_taxonomy({"thread_id": tid, "taxonomy": _taxonomy()})
    parent = _parent()
    state = initialize_search_state(
        search_id="s1", root_node=parent, policy=search_policy_from_config(REPO)
    )
    synth_node = {**parent, "id": "n_root_synth", "type": "synthesis"}
    assert M._maybe_mint_far_framing(tid, state, synth_node, forbid=False) == []
    far_node = {**parent, "id": "n_root_far01_ecology"}
    far_node["lineage"] = {**parent["lineage"], "far_framing_domain": "ecology"}
    assert M._maybe_mint_far_framing(tid, state, far_node, forbid=False) == []


# --- alpha1 / #5: novelty + distance never reward an output ------------- #


def test_alpha1_novelty_never_gates_promotion_or_selection():
    # Novelty is recorded (ac_decision.score_summary) but must NEVER feed a
    # gate/promotion/selection. The promotion reducer neither computes nor reads
    # novelty; node selection is by type weight + depth, not by any score.
    red = (REPO / "research_harness" / "orchestrator" / "reduction.py").read_text()
    assert "novelty" not in red, "novelty leaked into the promotion/reduction path"
    server = (REPO / "research_harness" / "mcp_server.py").read_text()
    fresh = server[server.index("# --- Fresh path: pick by stage"):]
    fresh = fresh[: fresh.index("if not candidates:")]
    assert "_TYPE_WEIGHTS" in fresh  # selection IS by type weight ...
    assert "novelty" not in fresh and "score_summary" not in fresh  # ... never by score


def test_alpha1_promotion_is_verdict_based_not_scored():
    from research_harness.orchestrator.reduction import reduce_node
    reviews = [{
        "critic_id": "c1", "node_id": "n_root", "verdict_candidate": "supported",
        "blocking": False,
        "scores": {"validity": 8, "necessity": 7, "reproducibility": 7, "taste_alignment": 8},
        "objections": [], "lesson_candidates": [],
    }]
    red = reduce_node(_parent(), {"claim_verdict_candidate": "supported"}, reviews)
    assert "novelty" not in red["score_summary"]  # promotion never scores novelty
    assert red["next_transition"] == "promoted"   # decided by verdict, not a score
