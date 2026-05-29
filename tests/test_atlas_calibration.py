"""ADR 0010 — INV-atlas-self-gate, as a CLASS-PROPERTY test.

The atlas calibration gate is the keystone unknown of the whole atlas plan:
does measured co-deployment *materially out-separate* the strongest topical
baseline, or does it collapse into topical priming? This suite asserts the gate
LOGIC as ∀-properties over a deterministic grid (not single instances): the
pass criterion is pre-registered (delta never fit to the observed gap), a strong
topical baseline cannot be dodged, and honest_failure never falls back to
topical. The actual co-deployment *numbers* come from the offline distance
engine; this fixes the contract they are judged against.
"""

from __future__ import annotations

from itertools import product

import pytest

from research_harness.atlas import validate as V
from research_harness.atlas.validate import PreRegisteredCriterion as Crit

# Grid coarse enough to enumerate, fine enough to cross every boundary.
_LEVELS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
_DELTAS = [0.05, 0.1, 0.2, 0.3]
_BANDS = [0.3, 0.5, 0.7]
_N = 400  # noise_floor(400) = 0.05, so every grid delta is admissible


def _crit(delta, band, n=_N):
    return Crit(delta=delta, band_threshold=band, holdout_set_id="holdout_v1", holdout_size=n)


def test_inv_deployable_iff_separable_and_margin():
    # INV-atlas-self-gate: deployable ⟺ (co >= band) ∧ (co − strong_topical >= delta).
    # With no regions, the only other verdict is honest_failure.
    for co, top, delta, band in product(_LEVELS, _LEVELS, _DELTAS, _BANDS):
        out = V.calibration_verdict(
            co_deploy_separation=co, strong_topical_separation=top, criterion=_crit(delta, band)
        )
        deployable = out["verdict"] == V.ATLAS_DEPLOYABLE
        expected = (co >= band) and ((co - top) >= delta)
        assert deployable == expected, (co, top, delta, band, out["verdict"])
        if not deployable:
            assert out["verdict"] == V.ATLAS_HONEST_FAILURE


def test_inv_strong_topical_baseline_cannot_be_dodged():
    # A stronger topical baseline (higher topical separation) can only move the
    # verdict AWAY from deployable: the deployable set over `topical` is
    # downward-closed (top <= co − delta). Co-deployment must beat the STRONG
    # baseline, not a weak strawman.
    for co, delta, band in product(_LEVELS, _DELTAS, _BANDS):
        deployable_tops = [
            t for t in _LEVELS
            if V.calibration_verdict(
                co_deploy_separation=co, strong_topical_separation=t, criterion=_crit(delta, band)
            )["verdict"] == V.ATLAS_DEPLOYABLE
        ]
        if deployable_tops:
            hi = max(deployable_tops)
            # everything below the highest deployable topical is also deployable
            assert all(t in deployable_tops for t in _LEVELS if t <= hi)
            # and a baseline stronger than (co − delta) is never deployable
            assert all(t <= co - delta + 1e-9 for t in deployable_tops)


def test_inv_delta_is_pre_registered_not_fit_to_gap():
    # delta governs the verdict and is read only from the (pre-registered)
    # criterion: for a fixed measured gap, a delta just under it passes and a
    # delta just over it fails — the gate cannot be made to pass by observing
    # the gap first.
    co, top, band = 0.8, 0.3, 0.5  # measured margin = 0.5
    assert V.calibration_verdict(
        co_deploy_separation=co, strong_topical_separation=top, criterion=_crit(0.4, band)
    )["verdict"] == V.ATLAS_DEPLOYABLE  # 0.4 < 0.5 → passes
    assert V.calibration_verdict(
        co_deploy_separation=co, strong_topical_separation=top, criterion=_crit(0.3, band)
    )["verdict"] == V.ATLAS_DEPLOYABLE
    # a delta above the gap fails (and is still admissible: 0.6 > floor 0.05)
    out = V.calibration_verdict(
        co_deploy_separation=co, strong_topical_separation=top, criterion=_crit(0.6, band)
    )
    assert out["verdict"] == V.ATLAS_HONEST_FAILURE  # 0.6 > 0.5 gap


def test_inv_pre_registration_wellformedness():
    # The 1/sqrt(n) floor was ABOLISHED by ratification: delta now comes from the
    # frozen permutation-null rule (atlas.distance.permutation_delta), so a small
    # delta is admissible (the rule sets its value, not a fixed floor).
    # validate_pre_registration now only checks well-formedness.
    V.validate_pre_registration(_crit(0.1, 0.5, n=4))  # small delta now admissible
    for bad_delta in (-0.1, 1.5):
        with pytest.raises(V.CalibrationError):
            V.validate_pre_registration(_crit(bad_delta, 0.5))
    with pytest.raises(V.CalibrationError):
        V.validate_pre_registration(_crit(0.2, 1.5))  # band out of [0,1]
    with pytest.raises(V.CalibrationError):
        V.validate_pre_registration(Crit(delta=0.2, band_threshold=0.5, holdout_set_id="x", holdout_size=0))


def test_inv_bounded_ships_only_passing_regions_on_same_criterion():
    # overall fails, but a region clears the SAME pre-registered criterion →
    # bounded_result over exactly the passing regions.
    crit = _crit(0.3, 0.5)
    out = V.calibration_verdict(
        co_deploy_separation=0.55, strong_topical_separation=0.5,  # overall margin 0.05 < 0.3
        criterion=crit,
        region_separations={
            "physics": (0.9, 0.2),   # margin 0.7 ≥ 0.3, sep ≥ band → passes
            "biology": (0.6, 0.55),  # margin 0.05 < 0.3 → fails
            "econ": (0.4, 0.0),      # sep 0.4 < band 0.5 → fails
        },
    )
    assert out["verdict"] == V.ATLAS_BOUNDED
    assert out["restricted_regions"] == ["physics"]
    # every shipped region truly clears the criterion
    assert all(V._passes(*{"physics": (0.9, 0.2)}[r], crit) for r in out["restricted_regions"])


def test_inv_honest_failure_has_no_topical_fallback():
    out = V.calibration_verdict(
        co_deploy_separation=0.55, strong_topical_separation=0.5, criterion=_crit(0.3, 0.5),
        region_separations={"a": (0.6, 0.55), "b": (0.4, 0.4)},  # none pass
    )
    assert out["verdict"] == V.ATLAS_HONEST_FAILURE
    assert "restricted_regions" not in out
    joined = " ".join(out["reasons"]).lower()
    assert "no silent topical fallback" in joined and "not\nshipped" not in joined
    assert "topical" in joined  # names the likely collapse-into-topical failure
