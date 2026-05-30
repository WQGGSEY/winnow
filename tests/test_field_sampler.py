"""Tests for the connector field source (Slice A of ADR 0012)."""

from __future__ import annotations

from research_harness.connector.field_sampler import (
    field_permutation,
    load_categories,
    sample_fields,
)


def test_namespace_loads_and_is_well_formed():
    cats = load_categories()
    # arXiv had 155 leaf categories at freeze time; allow growth on refresh but
    # never silently collapse to a tiny list.
    assert len(cats) >= 150
    codes = [c["code"] for c in cats]
    assert len(set(codes)) == len(codes)  # codes unique
    for c in cats:
        assert c["code"] and isinstance(c["code"], str)
        assert c["name"] and isinstance(c["name"], str)
        assert c["archive"] and isinstance(c["archive"], str)
        # archive is the pre-dot prefix (or the whole code for no-dot archives).
        assert c["code"].split(".")[0] == c["archive"]
    # spot-check a known entry survived the parse faithfully.
    by_code = {c["code"]: c["name"] for c in cats}
    assert by_code.get("cs.AI") == "Artificial Intelligence"


def test_load_categories_returns_immutable_shared_list():
    a = load_categories()
    assert isinstance(a, tuple)
    # cached: same object back.
    assert load_categories() is a


def test_permutation_is_a_true_permutation():
    cats = load_categories()
    perm = field_permutation(seed=7)
    assert len(perm) == len(cats)
    perm_codes = [c["code"] for c in perm]
    assert len(set(perm_codes)) == len(perm_codes)  # no repeats
    assert set(perm_codes) == {c["code"] for c in cats}  # same set


def test_permutation_is_deterministic_per_seed():
    assert [c["code"] for c in field_permutation(seed=42)] == [
        c["code"] for c in field_permutation(seed=42)
    ]


def test_different_seeds_give_different_orders():
    # 155! orderings — a collision between two seeds is astronomically unlikely.
    assert [c["code"] for c in field_permutation(seed=1)] != [
        c["code"] for c in field_permutation(seed=2)
    ]


def test_permutation_does_not_mutate_frozen_list():
    before = [c["code"] for c in load_categories()]
    field_permutation(seed=3)
    after = [c["code"] for c in load_categories()]
    assert before == after  # frozen list order untouched


def test_sample_fields_draws_n_distinct():
    drawn = sample_fields(10, seed=99)
    codes = [c["code"] for c in drawn]
    assert len(codes) == 10
    assert len(set(codes)) == 10
    namespace = {c["code"] for c in load_categories()}
    assert set(codes) <= namespace


def test_sample_more_than_namespace_returns_all_without_repeats():
    total = len(load_categories())
    drawn = sample_fields(total + 50, seed=5)
    assert len(drawn) == total
    assert len({c["code"] for c in drawn}) == total


def test_sample_zero_or_negative_is_empty():
    assert sample_fields(0, seed=1) == []
    assert sample_fields(-3, seed=1) == []
