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

# --- ADR 0006 rev.2: cross_generator_transfer guard verdicts ------------- #
# A cross_generator_transfer rho is reportable as transfer evidence ONLY when
# the harness can MEASURE that generator B is structurally distinct from A
# (behavioral distance), the rankings actually discriminate the pipelines
# (rank variance), and the predicate is not a chance artifact (null floor).
# Any guard failing types the result as not-passed so a degenerate / hollow
# rho (the thread_c8919361 failure: rho on a non-distinct B) cannot launder
# into a passing screen. The distinctness is MEASURED by the harness running
# both generators, never asserted by a worker-supplied label/hash.
VERDICT_PASSED = "passed"            # predicate true AND all guards ok
VERDICT_FAILED = "failed"            # predicate false, guards ok (clean non-transfer)
VERDICT_DEGENERATE = "degenerate"    # rankings do not discriminate the pipelines
VERDICT_UNINFORMATIVE = "uninformative"  # B not measurably distinct, or chance artifact
VERDICT_INVALID = "invalid"          # malformed input

# Guard thresholds (overridable via settings persona_enforcement.falsifier_guards).
DEFAULT_MIN_BEHAVIORAL_DISTANCE = 0.15  # min A/B output-distribution divergence
DEFAULT_MIN_RANK_VARIANCE = 0.05        # rank-vector variance floor (normalized)
DEFAULT_MAX_TIE_FRACTION = 0.5          # max fraction of tied entries
DEFAULT_NULL_FLOOR_K = 1.0              # threshold must beat k * null std (1/sqrt(n-1))


class FalsifierError(ValueError):
    """Raised when a falsifier predicate cannot be evaluated."""


def rank_discrimination(values: list[float]) -> dict[str, Any]:
    """Pure: does this ranking actually distinguish the pipelines? Returns
    {variance, tie_fraction, ok}. A near-constant or heavily-tied ranking
    carries no transferable signal regardless of the rho it produces."""
    n = len(values)
    if n < _MIN_RANK_VECTOR_LEN:
        return {"variance": 0.0, "tie_fraction": 1.0, "ok": False}
    ranks = _ranks(values)
    mean_r = sum(ranks) / n
    raw_var = sum((r - mean_r) ** 2 for r in ranks) / n
    # Normalize by the variance of a perfect 1..n ranking so the floor is
    # scale-free across vector lengths.
    perfect = _ranks(list(range(n)))
    mean_p = sum(perfect) / n
    perfect_var = sum((r - mean_p) ** 2 for r in perfect) / n or 1.0
    norm_var = raw_var / perfect_var
    distinct = len({round(v, 12) for v in values})
    tie_fraction = 1.0 - (distinct / n)
    return {"variance": norm_var, "tie_fraction": tie_fraction, "ok": None}


def behavioral_distance(
    samples_a: dict[str, list[float]], samples_b: dict[str, list[float]]
) -> float:
    """Pure: structural divergence between two generators' outputs on the SAME
    harness-chosen probe. Per shared numeric column, the max of the
    pooled-std-normalized mean gap and the std gap; overall = max over columns.
    Disjoint column schemas → 1.0 (trivially distinct). Identical generators on
    the same seed → 0.0. This is the MEASURED replacement for label-distinctness:
    it expresses 'same family yet structurally distinct', which a label cannot."""
    cols_a = {k for k, v in samples_a.items() if v}
    cols_b = {k for k, v in samples_b.items() if v}
    shared = cols_a & cols_b
    if not shared:
        return 1.0 if (cols_a or cols_b) else 0.0

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs)

    def _std(xs: list[float], m: float) -> float:
        return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5

    worst = 0.0
    for col in shared:
        a, b = samples_a[col], samples_b[col]
        ma, mb = _mean(a), _mean(b)
        sa, sb = _std(a, ma), _std(b, mb)
        pooled = (sa + sb) / 2.0 or 1e-9
        mean_gap = abs(ma - mb) / pooled
        std_gap = abs(sa - sb) / pooled
        worst = max(worst, mean_gap, std_gap)
    return worst


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def known_method_transfer(
    samples_a: dict[str, list[float]], samples_b: dict[str, list[float]]
) -> float | None:
    """ADR 0010 (T2-03): the harness's STRONG known-only baseline transfer. For
    each shared numeric column compute a standard statistic (mean, std, median)
    on A and on B, rank the columns by it, and take the Spearman rho of the
    A-ranking vs the B-ranking; return the MAX over statistics — the strongest
    standard ('known') method's transfer (best-effort known recombination).

    HARNESS-constructed from the harness-chosen probe — the worker never supplies
    it, so it can never be a weak strawman (the hole T2-03 closes). If even this
    strongest standard method clears the transfer predicate, the bar is
    patchwork-clearable and the result is uninformative. None when fewer than
    _MIN_RANK_VECTOR_LEN shared numeric columns make the baseline uncomputable."""
    shared = sorted(c for c in (set(samples_a) & set(samples_b)) if samples_a[c] and samples_b[c])
    if len(shared) < _MIN_RANK_VECTOR_LEN:
        return None

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs)

    def _std(xs: list[float]) -> float:
        m = _mean(xs)
        return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5

    best: float | None = None
    for stat in (_mean, _std, _median):
        a_vec = [stat(samples_a[c]) for c in shared]
        b_vec = [stat(samples_b[c]) for c in shared]
        try:
            rho = spearman_rho(a_vec, b_vec)
        except FalsifierError:
            continue  # degenerate stat vector (all columns equal under this stat)
        best = rho if best is None else max(best, rho)
    return best


def null_floor_satisfied(n: int, op: str, threshold: float, k: float) -> bool:
    """Pure: would a no-information ranking plausibly clear this predicate? The
    permutation null for Spearman rho has mean 0 and std ~1/sqrt(n-1). For a
    '>=' / '>' predicate the bar must sit at least k null-stds above 0, else a
    chance ranking clears it and the screen is uninformative. Non-directional
    ops are not floored here."""
    if n < 2:
        return False
    null_std = 1.0 / ((n - 1) ** 0.5)
    if op in (">=", ">"):
        return threshold >= k * null_std
    # '<=' / '<' / '==' are not chance-clearable in the transfer direction.
    return True


def patchwork_probe_applies_to_claim(subject_role: str | None) -> bool:
    """ADR 0009 (B3): does the patchwork-insufficiency probe (a known-methods
    baseline must NOT clear the bar — the null floor above) gate the CLAIM's
    falsifier? True when X is the target (``method_is_solution``): a baseline
    clearing the bar means the bar is too low to force a structurally new
    construction. False when the method is the test subject
    (``method_is_subject``, e.g. a screen): the probe is a generator/screen
    DESIGN-axis concern, not a claim gate. The axis is read from the FROZEN
    question — the construction cannot author it, so a node cannot relabel
    itself to switch the probe off. Absent defaults conservatively to True."""
    return subject_role != "method_is_subject"


def transfer_evidence_admissible(result: dict[str, Any]) -> bool:
    """ADR 0010 (T2-05 / INV-evidence): may this result's ``observed`` be cited as
    TRANSFER evidence downstream (render / paper / methodology)? Only a PASSING
    REAL referent counts — a passing cross_generator_transfer is a SCREEN
    (air-gapped, not reality-close), and any non-passed verdict
    (uninformative / degenerate / failed / invalid) is nothing. When this is
    False, ``observed`` must not appear as transfer evidence anywhere downstream."""
    return result.get("verdict") == VERDICT_PASSED and result.get("kind") == "real_holdout"


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
    measured_behavioral_distance: float | None = None,
    measured_known_baseline_transfer: float | None = None,
    guard_thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the envelope's external_falsifier predicate over held-out
    evidence and return a FalsifierResult dict.

    ``falsifier`` is ``feasibility_envelope.external_falsifier``.
    ``evidence`` is kind-specific:
      - cross_generator_transfer: {ranking_a: [...], ranking_b: [...]}
        (ranking_b is the HELD-OUT generator B; same pipeline order).
      - real_holdout: {observed: <number measured on the real adapter>}.

    ``measured_behavioral_distance`` (cross_generator_transfer only) is the
    HARNESS-measured A/B output divergence from falsifier_probe — it is passed
    in by the handler that actually ran the two generators, never read from the
    worker's evidence. None means the harness could not measure distinctness
    (no harness-loadable generators supplied) → the result is uninformative.

    Raises FalsifierError on malformed input. The verdict/pass is derived here,
    never supplied by the caller.
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

    gt = {
        "min_behavioral_distance": DEFAULT_MIN_BEHAVIORAL_DISTANCE,
        "min_rank_variance": DEFAULT_MIN_RANK_VARIANCE,
        "max_tie_fraction": DEFAULT_MAX_TIE_FRACTION,
        "null_floor_k": DEFAULT_NULL_FLOOR_K,
        **(guard_thresholds or {}),
    }

    result_evidence: dict[str, Any] = {}
    guards: dict[str, Any] = {}
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
        ra = [float(x) for x in ranking_a]
        rb = [float(x) for x in ranking_b]
        result_evidence = {
            "ranking_a": ranking_a,
            "ranking_b": ranking_b,
            "pipeline_labels": evidence.get("pipeline_labels"),
        }

        # Guard (b): the rankings must actually discriminate the pipelines.
        disc_a = rank_discrimination(ra)
        disc_b = rank_discrimination(rb)
        rank_ok = (
            disc_a["variance"] >= gt["min_rank_variance"]
            and disc_b["variance"] >= gt["min_rank_variance"]
            and disc_a["tie_fraction"] <= gt["max_tie_fraction"]
            and disc_b["tie_fraction"] <= gt["max_tie_fraction"]
        )
        guards["rank_discrimination"] = {"ranking_a": disc_a, "ranking_b": disc_b, "ok": rank_ok}

        try:
            observed = spearman_rho(ra, rb)
        except FalsifierError:
            observed = None  # constant vector — degenerate, handled below

        # Guard (c): the predicate must beat the permutation-null noise band.
        null_ok = null_floor_satisfied(len(ra), op, threshold, gt["null_floor_k"])
        guards["null_floor"] = {"n": len(ra), "k": gt["null_floor_k"], "ok": null_ok}

        # Guard (a): generator B must be MEASURABLY distinct from A. Computed by
        # the harness running both generators; absence (None) = cannot certify.
        behavioral_ok = (
            measured_behavioral_distance is not None
            and measured_behavioral_distance >= gt["min_behavioral_distance"]
        )
        guards["behavioral_distance"] = {
            "observed": measured_behavioral_distance,
            "min": gt["min_behavioral_distance"],
            "ok": behavioral_ok,
        }

        # Guard (d): a STRONG harness-constructed known-only baseline. If the best
        # standard method already transfers across A,B (clears the predicate), the
        # transfer bar is patchwork-clearable. Harness-computed from the probe
        # (never worker-supplied, so no weak strawman). None = uncomputable, N/A.
        known_clears = (
            measured_known_baseline_transfer is not None
            and evaluate_predicate(measured_known_baseline_transfer, op, threshold)
        )
        guards["known_baseline"] = {
            "observed": measured_known_baseline_transfer,
            "clears_predicate": known_clears,
            "ok": not known_clears,
        }

        if observed is None or not rank_ok:
            verdict = VERDICT_DEGENERATE
            observed = observed if observed is not None else 0.0
        elif not behavioral_ok or not null_ok or known_clears:
            verdict = VERDICT_UNINFORMATIVE
        elif evaluate_predicate(observed, op, threshold):
            verdict = VERDICT_PASSED
        else:
            verdict = VERDICT_FAILED
    else:  # real_holdout — the strong scalar; no air-gapped distinctness guards.
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
        verdict = VERDICT_PASSED if evaluate_predicate(observed, op, threshold) else VERDICT_FAILED
        # Bar-sanity (skill-isolation) guard — the real_holdout analogue of the
        # cross_generator_transfer known-baseline guard. If a NO-SKILL exposure
        # baseline (e.g. a leveraged buy-and-hold sweep, passed as
        # exposure_null_observed) ALSO clears the predicate, the bar measures
        # EXPOSURE (beta), not skill (alpha) — the pass is hollow, so type it
        # UNINFORMATIVE rather than let an exposure-gameable bar certify a result.
        exposure_null = evidence.get("exposure_null_observed")
        if exposure_null is not None:
            null_clears = evaluate_predicate(float(exposure_null), op, threshold)
            guards["exposure_null"] = {
                "observed": float(exposure_null),
                "clears_predicate": null_clears,
                "ok": not null_clears,
            }
            if null_clears and verdict == VERDICT_PASSED:
                verdict = VERDICT_UNINFORMATIVE

    passed = verdict == VERDICT_PASSED
    return {
        "thread_id": thread_id,
        "kind": kind,
        "holdout_source_id": holdout_source_id,
        "predicate": {"metric": predicate["metric"], "op": op, "threshold": threshold},
        "observed": observed,
        "passed": passed,
        "verdict": verdict,
        "transfer_evidence_admissible": passed and kind == "real_holdout",
        "guards": guards,
        "produced_by": PRODUCED_BY,
        "evidence": {k: v for k, v in result_evidence.items() if v is not None},
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
