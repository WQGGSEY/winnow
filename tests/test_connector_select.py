"""Tests for select-strongest forest survivor ranking (Slice D2 core)."""

from __future__ import annotations

from research_harness.connector.select import select_strongest


def _c(node_id, verdict, depth):
    return {"node_id": node_id, "verdict_strength": verdict, "distinct_attempts": depth}


def test_verdict_strength_is_primary():
    out = select_strongest([
        _c("a", "internally_valid", 99),   # deep but weak
        _c("b", "construct_valid", 1),     # shallow but stronger
        _c("c", "transfer_valid", 1),      # strongest
    ])
    assert out["winner"]["node_id"] == "c"
    assert [r["node_id"] for r in out["ranking"]] == ["c", "b", "a"]


def test_depth_breaks_strength_ties():
    out = select_strongest([
        _c("a", "construct_valid", 3),
        _c("b", "construct_valid", 7),  # same strength, deeper
        _c("c", "construct_valid", 1),
    ])
    assert out["winner"]["node_id"] == "b"


def test_node_id_breaks_full_ties_deterministically():
    out = select_strongest([
        _c("z", "construct_valid", 5),
        _c("a", "construct_valid", 5),  # same strength + depth -> smallest id wins
    ])
    assert out["winner"]["node_id"] == "a"


def test_lucky_shallow_does_not_beat_genuinely_stronger():
    # the CONTEXT guarantee: a deep internally_valid survivor must not win over
    # a construct_valid one.
    out = select_strongest([
        _c("lucky", "internally_valid", 50),
        _c("earned", "construct_valid", 2),
    ])
    assert out["winner"]["node_id"] == "earned"


def test_unknown_verdict_ranks_below_reachable():
    out = select_strongest([
        _c("unknown", "transfer_invalid_typo", 100),
        _c("real", "internally_valid", 0),
    ])
    assert out["winner"]["node_id"] == "real"


def test_empty_has_no_winner():
    out = select_strongest([])
    assert out["winner"] is None
    assert out["ranking"] == []
