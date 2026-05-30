"""Tests for select_strongest_survivor + forest helpers (Slice D2).

Isolated to a tmp repo root via monkeypatch — never touches real runs/threads.
"""

from __future__ import annotations

import json

import research_harness.mcp_server as mcp


def _node(nid, parent, status="promoted"):
    return {"id": nid, "parent": parent, "status": status, "claim_contract": {}}


def _write_state(tdir, nodes, promoted):
    p = tdir / "production" / "tree"
    p.mkdir(parents=True, exist_ok=True)
    (p / "search_state.json").write_text(
        json.dumps({"nodes": nodes, "promoted_node_ids": promoted})
    )


def _write_att(tdir, root_id, verdict):
    d = tdir / "production" / "rebuttal" / root_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "user_goal_attestation.json").write_text(json.dumps({"verdict_strength": verdict}))


def test_picks_strongest_by_verdict_strength(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_repo_root", lambda: tmp_path)
    tid = "thread_s1"
    tdir = tmp_path / "runs" / "threads" / tid
    _write_state(tdir, [_node("r_int", None), _node("r_con", None), _node("r_tra", None)],
                 ["r_int", "r_con", "r_tra"])
    _write_att(tdir, "r_int", "internally_valid")
    _write_att(tdir, "r_con", "construct_valid")
    _write_att(tdir, "r_tra", "transfer_valid")
    out = mcp.handle_select_strongest_survivor({"thread_id": tid})
    assert out["status"] == "ok"
    assert out["selected_root_id"] == "r_tra"
    assert out["verdict_strength"] == "transfer_valid"
    assert [r["node_id"] for r in out["ranking"]] == ["r_tra", "r_con", "r_int"]
    sel = json.loads((tdir / "production" / "tree" / "forest_selection.json").read_text())
    assert sel["selected_root_id"] == "r_tra"
    assert sel["num_survivors"] == 3


def test_honest_failure_when_no_survivors(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_repo_root", lambda: tmp_path)
    tid = "thread_s2"
    tdir = tmp_path / "runs" / "threads" / tid
    _write_state(tdir, [_node("r1", None, status="ready"), _node("r2", None, status="ready")], [])
    out = mcp.handle_select_strongest_survivor({"thread_id": tid})
    assert out["status"] == "honest_failure"


def test_incomplete_when_survivor_lacks_attestation(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_repo_root", lambda: tmp_path)
    tid = "thread_s3"
    tdir = tmp_path / "runs" / "threads" / tid
    _write_state(tdir, [_node("r_a", None), _node("r_b", None)], ["r_a", "r_b"])
    _write_att(tdir, "r_a", "construct_valid")  # r_b has no per-root attestation
    out = mcp.handle_select_strongest_survivor({"thread_id": tid})
    assert out["status"] == "incomplete"
    assert out["pending_roots"] == ["r_b"]


def test_depth_breaks_strength_tie(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_repo_root", lambda: tmp_path)
    tid = "thread_s4"
    tdir = tmp_path / "runs" / "threads" / tid
    # both roots construct_valid; r_b has a deeper subtree (an extra ran child).
    _write_state(
        tdir,
        [_node("r_a", None), _node("r_b", None),
         _node("r_b_child", "r_b", status="completed_worker_report")],
        ["r_a", "r_b"],
    )
    _write_att(tdir, "r_a", "construct_valid")
    _write_att(tdir, "r_b", "construct_valid")
    out = mcp.handle_select_strongest_survivor({"thread_id": tid})
    assert out["status"] == "ok"
    assert out["selected_root_id"] == "r_b"  # deeper subtree wins the strength tie


def test_promoted_descendant_makes_root_a_survivor(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_repo_root", lambda: tmp_path)
    tid = "thread_s5"
    tdir = tmp_path / "runs" / "threads" / tid
    # root not promoted, but a child is -> root is a survivor via the descendant.
    _write_state(
        tdir,
        [_node("r_a", None, status="needs_child_branch"),
         _node("r_a_child", "r_a", status="promoted")],
        ["r_a_child"],
    )
    _write_att(tdir, "r_a", "construct_valid")
    out = mcp.handle_select_strongest_survivor({"thread_id": tid})
    assert out["status"] == "ok"
    assert out["selected_root_id"] == "r_a"
    assert out["selected_promoted_node_id"] == "r_a_child"
