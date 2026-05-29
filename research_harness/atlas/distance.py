"""ADR 0010 (T1-03) — behavioral co-deployment distance + calibration machinery.

BEHAVIORAL, not declared: the model SOLVES each battery problem, and the fixed
``detect_methods`` matcher scans the WORKED SOLUTION for each node's
behavioral_signature — it never asks the model "which methods did you use". Two
nodes co-deploy when their machinery actually appears together in solutions;
distance = 1 - normalized co-deployment (NPMI). The topical baseline (SPECTER2)
is computed separately and passed in — never mixed into this primary.

Pure module: no I/O, no model calls. The worked solutions and the topical matrix
are passed in; the model-solving + SPECTER2 steps live in the offline runner.
"""

from __future__ import annotations

import math
import random
import re
from typing import Any, Iterable


def _bounded(pat: str) -> str:
    """Wrap a signature pattern in word boundaries on any alphanumeric edge, so
    'graph' does not match inside 'paragraph' and 'Nash' not inside 'gnashing'.
    Non-word edges (e.g. 'P\\(...\\)', 'dS/dt') are left as-is."""
    pre = r"\b" if (pat[:1].isalnum() or pat[:1] == "_") else ""
    suf = r"\b" if (pat[-1:].isalnum() or pat[-1:] == "_") else ""
    return pre + pat + suf


def detect_methods(solution_text: str, nodes: list[dict[str, Any]]) -> set[str]:
    """Behavioral detection: which node signatures actually appear in the worked
    solution (not a self-report). A node is 'deployed' if any of its
    behavioral_signature patterns matches the solution text (word-boundary aware
    so substrings inside larger words do not spuriously match)."""
    text = solution_text.lower()
    deployed: set[str] = set()
    for n in nodes:
        for pat in n.get("behavioral_signature", []):
            p = pat.lower()
            try:
                if re.search(_bounded(p), text):
                    deployed.add(n["id"])
                    break
            except re.error:
                if p in text:  # malformed pattern → literal substring fallback
                    deployed.add(n["id"])
                    break
    return deployed


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def co_deployment_distances(
    deployments: list[set[str]], node_ids: Iterable[str]
) -> dict[tuple[str, str], float]:
    """deployments: one detected-node set per solved problem. Returns
    {pair: distance} over all node pairs, distance = 1 - (NPMI+1)/2 in [0,1]
    (NPMI = normalized pointwise mutual information of co-deployment)."""
    ids = sorted(node_ids)
    p = len(deployments) or 1
    count = {nid: sum(1 for d in deployments if nid in d) for nid in ids}
    dists: dict[tuple[str, str], float] = {}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            co = sum(1 for d in deployments if a in d and b in d)
            pa, pb, pab = count[a] / p, count[b] / p, co / p
            if pab <= 0.0 or pa <= 0.0 or pb <= 0.0:
                npmi = -1.0  # never co-deployed → maximally distant
            else:
                npmi = math.log(pab / (pa * pb)) / (-math.log(pab))
                npmi = max(-1.0, min(1.0, npmi))
            dists[_pair_key(a, b)] = 1.0 - (npmi + 1.0) / 2.0
    return dists


def separation_auc(
    distances: dict[tuple[str, str], float],
    positives: Iterable[tuple[str, str]],
    noise: Iterable[tuple[str, str]],
) -> float:
    """AUC = P(dist(positive) < dist(noise)) over all positive x noise pairs;
    ties = 0.5. Higher = the metric ranks documented-bridgeable pairs as nearer
    (more co-deployed) than non-bridgeable noise pairs. 0.5 = chance."""
    pos = [distances[p] for p in positives if p in distances]
    neg = [distances[p] for p in noise if p in distances]
    if not pos or not neg:
        return 0.5
    wins = sum((1.0 if dp < dn else 0.5 if dp == dn else 0.0) for dp in pos for dn in neg)
    return wins / (len(pos) * len(neg))


def permutation_delta(
    distances_co: dict[tuple[str, str], float],
    distances_top: dict[tuple[str, str], float],
    all_pairs: list[tuple[str, str]],
    n_pos: int,
    *,
    shuffles: int,
    seed: int,
) -> float:
    """Operator-frozen delta rule: the 95th percentile of (auc_co - auc_top) over
    label shuffles that pick n_pos random 'positive' pairs (rest noise). The null
    uses shuffled labels, so it is independent of the REAL positive labels — delta
    is not fit to the observed gain. Returned raw (may be <0); the caller clamps
    to the gate's [0,1] domain."""
    rng = random.Random(seed)
    pairs = list(all_pairs)
    gains: list[float] = []
    for _ in range(shuffles):
        shuffled = pairs[:]
        rng.shuffle(shuffled)
        pos = set(shuffled[:n_pos])
        noise = [p for p in pairs if p not in pos]
        gains.append(
            separation_auc(distances_co, pos, noise)
            - separation_auc(distances_top, pos, noise)
        )
    gains.sort()
    idx = min(len(gains) - 1, max(0, math.ceil(0.95 * len(gains)) - 1))
    return gains[idx]


def run_calibration(
    *,
    deployments: list[set[str]],
    nodes: list[dict[str, Any]],
    topical_distances: dict[tuple[str, str], float],
    heldout: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the keystone read: co-deployment distances from the worked
    solutions, AUC separations on the frozen held-out, the permutation delta, and
    the atlas self-gate verdict. The topical baseline is supplied (SPECTER2),
    never derived here."""
    from research_harness.atlas.validate import PreRegisteredCriterion, calibration_verdict

    node_ids = [n["id"] for n in nodes]
    co = co_deployment_distances(deployments, node_ids)
    positives = [_pair_key(*p["pair"]) for p in heldout["node_pair_mapping"]["positives"]]
    pos_set = set(positives)
    all_pairs = list(co.keys())
    noise = [p for p in all_pairs if p not in pos_set]

    co_sep = separation_auc(co, positives, noise)
    top_sep = separation_auc(topical_distances, positives, noise)
    dr = heldout["delta_rule"]
    delta_raw = permutation_delta(
        co, topical_distances, all_pairs, len(positives),
        shuffles=int(dr["shuffles"]), seed=int(dr["seed"]),
    )
    delta = max(0.0, min(1.0, delta_raw))  # clamp to the gate's domain; raw recorded
    crit = PreRegisteredCriterion(
        delta=delta,
        band_threshold=float(heldout["band_threshold"]),
        holdout_set_id=str(heldout.get("frozen_by", "operator")) + "_heldout",
        holdout_size=len(positives),
    )
    verdict = calibration_verdict(
        co_deploy_separation=co_sep, strong_topical_separation=top_sep, criterion=crit
    )
    return {
        "co_separation": round(co_sep, 4),
        "topical_separation": round(top_sep, 4),
        "gain": round(co_sep - top_sep, 4),
        "delta_permutation_raw": round(delta_raw, 4),
        "delta_used": round(delta, 4),
        "n_positives": len(positives),
        "co_deployment_distances": {f"{a}|{b}": round(d, 4) for (a, b), d in sorted(co.items())},
        **verdict,
    }
