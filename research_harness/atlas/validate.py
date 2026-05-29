"""ADR 0010 (T1-04) — the atlas's external-falsifier: its self-gate.

The measured atlas is a CONSTRUCTION and is not assumed to work. It ships only
if its calibration passes against an operator/literature-authored known
cross-domain-transfer held-out set the atlas did not author (authorship
separation). The pass criterion — the held-out set, the band threshold, and the
margin ``delta`` — is **PRE-REGISTERED before any co-deployment is measured**;
fitting ``delta`` to the observed gap would be RSTF's cardinal sin (moving the
goalpost after seeing the metric) recursed onto the atlas. The operator has a
stake in shipping, so ``delta`` rests on an independent ground — it must clear
the held-out-set noise floor.

Verdict taxonomy mirrors a research run: ``deployable`` / ``bounded_result`` /
``honest_failure``. ``deployable`` requires co-deployment to MATERIALLY
out-separate the STRONGEST topical baseline (by ``delta``) — else the metric is
just "topical + noise" and adds nothing (INV-discrimination applied to the atlas
itself, with the topical baseline as the strong known-only baseline).
``bounded_result`` ships a restricted atlas over only the regions that pass.
``honest_failure`` ships no measured atlas — far-framing keeps the operator
table; there is NO silent fallback to topical.

Pure module: no I/O. The separation scalars (how well a distance metric ranks
known-transfer pairs as nearer than noise pairs on the held-out set) are computed
by the offline distance engine and passed in; the gate never derives them or
``delta`` from each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ATLAS_DEPLOYABLE = "deployable"
ATLAS_BOUNDED = "bounded_result"
ATLAS_HONEST_FAILURE = "honest_failure"


class CalibrationError(ValueError):
    """Raised when the pre-registered criterion is itself inadmissible."""


@dataclass(frozen=True)
class PreRegisteredCriterion:
    """The atlas's 'frozen question' — fixed BEFORE co-deployment is measured.

    ``delta``           required margin: co-deployment separation must exceed the
                        strongest topical baseline's separation by at least this.
                        Its VALUE is computed by the operator-frozen
                        permutation-null rule (atlas.distance.permutation_delta:
                        the 95th percentile of the gain under label shuffles of
                        the frozen held-out) — NOT a hand-set number and NOT fit
                        to the observed gain. (The earlier 1/sqrt(n) floor was
                        abolished by ratification in favour of this stronger,
                        empirical rule.)
    ``band_threshold``  minimum separation to count the metric as 'separable'.
    ``holdout_set_id``  provenance of the operator-frozen held-out set.
    ``holdout_size``    its size (recorded for provenance).
    """

    delta: float
    band_threshold: float
    holdout_set_id: str
    holdout_size: int


def validate_pre_registration(crit: PreRegisteredCriterion) -> None:
    """Reject a criterion that is malformed *on its own terms*. ``delta`` is
    produced by the frozen permutation-null rule (computed on the frozen held-out
    before the real-label gain is compared), so the protection against a
    hand-tuned tiny delta is the rule itself, not a fixed floor; here we only
    check well-formedness."""
    if not crit.holdout_set_id:
        raise CalibrationError("held-out set has no provenance id — authorship separation unverifiable.")
    if crit.holdout_size < 1:
        raise CalibrationError("held-out set is empty; cannot pre-register a criterion.")
    if not (0.0 <= crit.delta <= 1.0):
        raise CalibrationError(
            f"delta={crit.delta!r} must be in [0,1]; it is computed by the frozen "
            "permutation-null rule, never hand-set."
        )
    if not (0.0 <= crit.band_threshold <= 1.0):
        raise CalibrationError("band_threshold must be in [0,1].")


def _passes(co: float, topical: float, crit: PreRegisteredCriterion) -> bool:
    """deployable-region predicate: separable AND out-separates the strong
    topical baseline by the pre-registered margin."""
    return co >= crit.band_threshold and (co - topical) >= crit.delta


def calibration_verdict(
    *,
    co_deploy_separation: float,
    strong_topical_separation: float,
    criterion: PreRegisteredCriterion,
    region_separations: dict[str, tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Decide deployable / bounded_result / honest_failure from MEASURED
    separations against the PRE-REGISTERED criterion.

    ``strong_topical_separation`` MUST be the strongest available topical
    baseline's separation (the harness's best-effort embedding method) — a weak
    strawman here would let co-deployment pass trivially (the same hole T2-03
    closes for the falsifier, recursed onto the atlas).

    ``region_separations``: {region_id: (co_deploy_sep, strong_topical_sep)} for
    the bounded path — ship only the regions that clear the SAME pre-registered
    criterion.

    ``delta`` is read only from ``criterion`` (pre-registered), never derived
    from the measured separations.
    """
    validate_pre_registration(criterion)

    margin = co_deploy_separation - strong_topical_separation
    if _passes(co_deploy_separation, strong_topical_separation, criterion):
        return {
            "verdict": ATLAS_DEPLOYABLE,
            "margin_over_topical": margin,
            "delta": criterion.delta,
            "reasons": [
                f"co-deployment separation {co_deploy_separation:.4g} >= band "
                f"{criterion.band_threshold} and out-separates the strongest topical "
                f"baseline by {margin:.4g} >= pre-registered delta {criterion.delta:.4g}."
            ],
        }

    passing = sorted(
        rid for rid, (co, top) in (region_separations or {}).items()
        if _passes(co, top, criterion)
    )
    if passing:
        return {
            "verdict": ATLAS_BOUNDED,
            "restricted_regions": passing,
            "margin_over_topical": margin,
            "delta": criterion.delta,
            "reasons": [
                f"overall fails (margin {margin:.4g} < delta {criterion.delta:.4g}), but "
                f"{len(passing)} region(s) clear the pre-registered criterion — ship a "
                "restricted atlas over them; far-framing uses measured distance there, "
                "the operator table elsewhere."
            ],
        }

    return {
        "verdict": ATLAS_HONEST_FAILURE,
        "margin_over_topical": margin,
        "delta": criterion.delta,
        "reasons": [
            f"co-deployment does not out-separate the strongest topical baseline by "
            f"delta {criterion.delta:.4g} (margin {margin:.4g}) anywhere — the metric is "
            "topical + noise (likely collapse into topical priming). Measured atlas NOT "
            "shipped; far-framing keeps the operator-band table. No silent topical fallback."
        ],
    }
