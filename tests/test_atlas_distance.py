"""ADR 0010 (T1-03) — behavioral co-deployment distance machinery (pure pieces)."""

from __future__ import annotations

from itertools import combinations

from research_harness.atlas import distance as D

NODES = [
    {"id": "equilibrium_fixed_point", "behavioral_signature": ["fixed point", "Nash"]},
    {"id": "bayesian_inference", "behavioral_signature": ["posterior", "Bayes"]},
    {"id": "network_flow", "behavioral_signature": ["graph", "contagion"]},
]


def test_detect_methods_is_behavioral():
    # Detection is on the WORKED SOLUTION text, not a self-report.
    sol = "We find the fixed point by setting the derivative to zero, then update the posterior via Bayes."
    assert D.detect_methods(sol, NODES) == {"equilibrium_fixed_point", "bayesian_inference"}
    assert D.detect_methods("a plain paragraph with no method machinery", NODES) == set()


def test_co_deployment_distance_extremes():
    ids = ["a", "b", "c"]
    deployments = [{"a", "b"}, {"a", "b"}, {"a", "b"}, {"c"}]
    d = D.co_deployment_distances(deployments, ids)
    assert d[("a", "b")] < d[("a", "c")]      # a,b co-deploy → nearer
    assert d[("a", "c")] == 1.0               # never co-deployed → maximally distant


def test_separation_auc_perfect_and_reversed():
    dist = {("a", "b"): 0.1, ("c", "d"): 0.9}
    assert D.separation_auc(dist, [("a", "b")], [("c", "d")]) == 1.0
    assert D.separation_auc(dist, [("c", "d")], [("a", "b")]) == 0.0


def test_permutation_delta_is_deterministic():
    co = {("a", "b"): 0.1, ("a", "c"): 0.9, ("b", "c"): 0.5}
    top = {("a", "b"): 0.5, ("a", "c"): 0.5, ("b", "c"): 0.5}
    pairs = list(co)
    d1 = D.permutation_delta(co, top, pairs, 1, shuffles=200, seed=7)
    d2 = D.permutation_delta(co, top, pairs, 1, shuffles=200, seed=7)
    assert d1 == d2  # frozen seed → reproducible delta


def test_run_calibration_smoke_and_co_beats_neutral_topical():
    nodes = [{"id": x, "behavioral_signature": [x]} for x in ["a", "b", "c", "d"]]
    ids = [n["id"] for n in nodes]
    # 'a' and 'b' co-deploy strongly; topical is neutral (chance) everywhere.
    deployments = [{"a", "b"} for _ in range(8)] + [{"c"}, {"d"}, {"a", "c"}, {"b", "d"}]
    topical = {tuple(sorted(p)): 0.5 for p in combinations(ids, 2)}
    heldout = {
        "node_pair_mapping": {"positives": [{"pair": ["a", "b"]}]},
        "delta_rule": {"shuffles": 300, "seed": 7},
        "band_threshold": 0.6,
        "frozen_by": "op",
    }
    out = D.run_calibration(deployments=deployments, nodes=nodes, topical_distances=topical, heldout=heldout)
    assert out["verdict"] in {"deployable", "bounded_result", "honest_failure"}
    assert out["co_separation"] >= out["topical_separation"]  # co-deployment separates; topical is chance
    assert "delta_used" in out and 0.0 <= out["delta_used"] <= 1.0
