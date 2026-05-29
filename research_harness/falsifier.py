"""ADR 0006 — the external-falsifier gate's deterministic core.

The harness, NOT worker experiment code, owns the predicate computation
that decides whether a research goal counts as *achieved*. This module is
that owner. It is pure (no I/O, no LLM) so the pass/fail boolean falls out
of a deterministic comparison the success-seeking run cannot stamp
directly.

Two falsifier kinds:

- ``real_holdout`` — the strong falsifier: a predicate evaluated on a
  registered ``real_adapter`` data source. The observed value is supplied
  (the harness has no real adapter to execute air-gapped) but the
  pass/fail is harness-derived.
- ``cross_generator_transfer`` — the weak air-gapped falsifier: the
  worker's pipeline ranking under generator A must be preserved on a
  held-out *distinct* generator B (Spearman rho >= threshold). The LLM
  supplies the two ranking vectors; the harness computes rho. The worker
  cannot write ``passed=true`` — it must supply held-out rankings that the
  deterministic predicate accepts.

The honest limit (recorded in ADR 0006, not hidden): air-gapped, even
generator B is ultimately LLM-touched, so ``cross_generator_transfer``
raises the cost of gaming without eliminating it. Only ``real_holdout``
closes the loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

PRODUCED_BY = "harness_falsifier_module"
_TRANSFER_METRIC = "spearman_rho"
_MIN_RANK_VECTOR_LEN = 3


class FalsifierError(ValueError):
    """Raised when a falsifier predicate cannot be evaluated."""


def derive_max_attestable_status(envelope: dict[str, Any] | None) -> str:
    """ADR 0006: the harness-owned ceiling on goal achievement.

    'goal_achieved' is reachable ONLY when the envelope registers an
    external_falsifier with kind != 'none'. Absent / 'none' →
    'unverified_screen' (achieved=true is structurally refused). This is
    derived fresh from external_falsifier so a hand-written envelope that
    never went through submit_feasibility_envelope still gets the correct
    ceiling — the operator cannot widen it by stamping the field directly.
    """
    falsifier = (envelope or {}).get("external_falsifier") or {}
    kind = falsifier.get("kind")
    if kind in {"real_holdout", "cross_generator_transfer"}:
        return "goal_achieved"
    return "unverified_screen"


def evaluate_predicate(observed: float, op: str, threshold: float) -> bool:
    if op == ">=":
        return observed >= threshold
    if op == ">":
        return observed > threshold
    if op == "<=":
        return observed <= threshold
    if op == "<":
        return observed < threshold
    if op == "==":
        return observed == threshold
    raise FalsifierError(f"unsupported predicate op: {op!r}")


def _ranks(values: list[float]) -> list[float]:
    """Fractional (average-of-ties) ranks, 1-based."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based average rank for the tie block
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_a == 0 or var_b == 0:
        raise FalsifierError(
            "rank vector has zero variance (all equal) — Spearman rho is "
            "undefined; supply rankings that actually distinguish the pipelines."
        )
    return cov / (var_a**0.5 * var_b**0.5)


def spearman_rho(ranking_a: list[float], ranking_b: list[float]) -> float:
    """Spearman rank-correlation: Pearson over the rank-transformed inputs.

    Accepts either raw scores or ranks — both reduce to the same rho.
    """
    if len(ranking_a) != len(ranking_b):
        raise FalsifierError(
            f"ranking_a (len {len(ranking_a)}) and ranking_b (len "
            f"{len(ranking_b)}) must be the same length and aligned by pipeline."
        )
    if len(ranking_a) < _MIN_RANK_VECTOR_LEN:
        raise FalsifierError(
            f"need >= {_MIN_RANK_VECTOR_LEN} paired pipelines to measure transfer; "
            f"got {len(ranking_a)}."
        )
    return _pearson(_ranks(ranking_a), _ranks(ranking_b))


def compute_falsifier_result(
    *,
    thread_id: str,
    falsifier: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate the envelope's external_falsifier predicate over held-out
    evidence and return a FalsifierResult dict.

    ``falsifier`` is ``feasibility_envelope.external_falsifier``.
    ``evidence`` is kind-specific:
      - cross_generator_transfer: {ranking_a: [...], ranking_b: [...]}
        (ranking_b is the HELD-OUT generator B; same pipeline order).
      - real_holdout: {observed: <number measured on the real adapter>}.

    Raises FalsifierError on malformed input. The pass/fail is derived
    here, never supplied by the caller.
    """
    kind = falsifier.get("kind")
    if kind == "none" or not kind:
        raise FalsifierError(
            "external_falsifier.kind is 'none' — there is nothing to falsify. "
            "Register a real_holdout or cross_generator_transfer falsifier first."
        )
    if kind not in {"real_holdout", "cross_generator_transfer"}:
        raise FalsifierError(f"unknown falsifier kind: {kind!r}")

    holdout_source_id = falsifier.get("holdout_source_id")
    if not holdout_source_id:
        raise FalsifierError(
            "external_falsifier.holdout_source_id is required to compute a result."
        )
    predicate = falsifier.get("predicate") or {}
    for key in ("metric", "op", "threshold"):
        if key not in predicate:
            raise FalsifierError(f"external_falsifier.predicate missing {key!r}")
    op = predicate["op"]
    threshold = float(predicate["threshold"])

    result_evidence: dict[str, Any] = {}
    if kind == "cross_generator_transfer":
        if predicate["metric"] != _TRANSFER_METRIC:
            raise FalsifierError(
                f"cross_generator_transfer requires predicate.metric="
                f"{_TRANSFER_METRIC!r}, got {predicate['metric']!r}."
            )
        ranking_a = evidence.get("ranking_a")
        ranking_b = evidence.get("ranking_b")
        if not isinstance(ranking_a, list) or not isinstance(ranking_b, list):
            raise FalsifierError(
                "cross_generator_transfer evidence requires list ranking_a "
                "(generator A) and ranking_b (held-out generator B)."
            )
        observed = spearman_rho(
            [float(x) for x in ranking_a],
            [float(x) for x in ranking_b],
        )
        result_evidence = {
            "ranking_a": ranking_a,
            "ranking_b": ranking_b,
            "pipeline_labels": evidence.get("pipeline_labels"),
        }
    else:  # real_holdout
        if "observed" not in evidence:
            raise FalsifierError(
                "real_holdout evidence requires 'observed' (the metric value "
                "measured on the real adapter)."
            )
        observed = float(evidence["observed"])
        result_evidence = {
            "observed": observed,
            "adapter_provenance": evidence.get("adapter_provenance"),
        }

    passed = evaluate_predicate(observed, op, threshold)
    return {
        "thread_id": thread_id,
        "kind": kind,
        "holdout_source_id": holdout_source_id,
        "predicate": {"metric": predicate["metric"], "op": op, "threshold": threshold},
        "observed": observed,
        "passed": passed,
        "produced_by": PRODUCED_BY,
        "evidence": {k: v for k, v in result_evidence.items() if v is not None},
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
