"""ADR 0010 (T1-06) — atlas pin/version: only a calibration-PASSED measured
atlas is pinnable (deployable / bounded_result); honest_failure ships nothing.
Hash-stamped + immutable, mirroring pin_domain_taxonomy.
"""

from __future__ import annotations

import json

import pytest

import research_harness.mcp_server as M
from research_harness.atlas import pin as P
from research_harness.schemas.validator import SchemaValidationError, validate_named_schema


def _atlas(verdict="deployable", *, distance_source="co_deployment", restricted=None):
    a = {
        "version": "1",
        "distance_source": distance_source,
        "measuring_model_id": "pinned-model@2026-05",
        "calibration": {
            "verdict": verdict, "band_threshold": 0.6, "delta": 0.1,
            "holdout_set_id": "known_transfer_v1", "holdout_size": 400,
            "margin_over_topical": 0.3,
        },
        "domains": [
            {"id": "agent_harness", "label": "Agent harness"},
            {"id": "ecology", "label": "Ecology"},
            {"id": "immunology", "label": "Immunology"},
        ],
        "distances": {
            "agent_harness|ecology": 0.78,
            "agent_harness|immunology": 0.84,
            "ecology|immunology": 0.45,
        },
    }
    if restricted is not None:
        a["restricted_regions"] = restricted
    return a


# --- pure pinnability gate ---------------------------------------------- #


def test_atlas_pinnable_only_passed_verdicts():
    assert P.atlas_pinnable(_atlas("deployable"))[0] is True
    assert P.atlas_pinnable(_atlas("bounded_result", restricted=["ecology"]))[0] is True
    # honest_failure is not pinnable (no measured atlas ships).
    ok, reason = P.atlas_pinnable({**_atlas(), "calibration": {"verdict": "honest_failure"}})
    assert ok is False and "honest_failure" in reason
    # bounded_result must name its restricted region set.
    ok, reason = P.atlas_pinnable(_atlas("bounded_result"))
    assert ok is False and "restricted_regions" in reason
    # topical distance source is never the pinned primary.
    ok, reason = P.atlas_pinnable(_atlas(distance_source="topical"))
    assert ok is False and "co_deployment" in reason


# --- schema: honest_failure / topical are structurally non-pinnable ----- #


def test_domain_atlas_schema_accepts_deployable_rejects_honest_failure():
    validate_named_schema("domain_atlas", _atlas("deployable"))
    bad = _atlas()
    bad["calibration"]["verdict"] = "honest_failure"  # not in the enum
    with pytest.raises(SchemaValidationError):
        validate_named_schema("domain_atlas", bad)


# --- handler: pin, immutability, gate ----------------------------------- #


def test_handle_pin_atlas_version_pins_and_is_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    tid = "t_atlas"
    out = M.handle_pin_atlas_version({"thread_id": tid, "atlas": _atlas("deployable")})
    assert out["status"] == "ok" and out["calibration_verdict"] == "deployable"
    digest = out["atlas_hash"]
    assert M._pinned_atlas(tid)["source_provenance"] == f"atlas_sha256:{digest}"
    # idempotent on identical content
    again = M.handle_pin_atlas_version({"thread_id": tid, "atlas": _atlas("deployable")})
    assert again["status"] == "ok" and again["atlas_hash"] == digest
    # a different atlas is refused — immutable
    drift = _atlas("deployable")
    drift["distances"]["agent_harness|ecology"] = 0.99
    refused = M.handle_pin_atlas_version({"thread_id": tid, "atlas": drift})
    assert refused["status"] == "rejected" and "IMMUTABLE" in refused["reason"]


def test_handle_pin_atlas_version_rejects_bounded_without_regions(tmp_path, monkeypatch):
    # Passes schema (restricted_regions optional) but the pinnability gate rejects it.
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    out = M.handle_pin_atlas_version({"thread_id": "t_b", "atlas": _atlas("bounded_result")})
    assert out["status"] == "rejected" and "restricted_regions" in out["reason"]


def test_handle_pin_atlas_version_rejects_honest_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_thread_dir", lambda t: tmp_path / "runs" / "threads" / t)
    bad = _atlas()
    bad["calibration"]["verdict"] = "honest_failure"
    out = M.handle_pin_atlas_version({"thread_id": "t_hf", "atlas": bad})
    assert out["status"] == "rejected"  # schema enum rejects honest_failure
