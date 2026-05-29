"""ADR 0010 (T1-06) — atlas pin/version: the authorship cut between the offline
build and the air-gapped harness.

Only a calibration-PASSED atlas (deployable / bounded_result — see
``atlas.validate``) is pinnable for harness use; an honest_failure atlas ships
nothing and the operator table remains. The pinned artifact is hash-stamped and
IMMUTABLE per thread (mirror of pin_frozen_question / pin_domain_taxonomy), so a
later construction cannot swap the atlas it is judged against. Pure module: no
I/O (the MCP handler does the read/write); reuses the generic JSON content hash.
"""

from __future__ import annotations

from typing import Any

from research_harness.domain_taxonomy import taxonomy_hash as atlas_hash  # generic JSON content hash

# honest_failure is intentionally absent — it is not a pinnable (shippable) atlas.
PINNABLE_VERDICTS = frozenset({"deployable", "bounded_result"})


def atlas_pinnable(atlas: dict[str, Any]) -> tuple[bool, str]:
    """Gate which atlases may be pinned for harness use. Returns (ok, reason).

    - calibration.verdict must be deployable / bounded_result (honest_failure =>
      no measured atlas ships; far-framing keeps the operator table).
    - distance_source must be co_deployment (measured); topical is a separated
      baseline, never the pinned primary.
    """
    verdict = (atlas.get("calibration") or {}).get("verdict")
    if verdict not in PINNABLE_VERDICTS:
        return False, (
            f"atlas calibration verdict={verdict!r} is not pinnable; only "
            f"{sorted(PINNABLE_VERDICTS)} ship. honest_failure means no measured atlas "
            "is pinned — the operator table remains (no silent topical fallback)."
        )
    if atlas.get("distance_source") != "co_deployment":
        return False, (
            "atlas distance_source must be 'co_deployment' (measured); topical similarity "
            "is a separated baseline used only in the calibration gate, never the pinned primary."
        )
    if verdict == "bounded_result" and not atlas.get("restricted_regions"):
        return False, (
            "bounded_result atlas must list restricted_regions (the calibrated subset "
            "far-framing may use measured distance within)."
        )
    return True, "ok"
