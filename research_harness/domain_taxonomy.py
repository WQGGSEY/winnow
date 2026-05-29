"""ADR 0009 (B1): the frozen domain taxonomy + deterministic far-domain selector.

The operator curates a domain graph + pairwise conceptual distances OFFLINE — the
cheapest external-manifold addition (a human adds reach the run cannot). The
construction loop can neither author nor edit it: it is pinned per-thread with a
provenance hash (mirror of pin_frozen_question), so a later construction cannot
swap a convenient table in.

D_i selection is PURE POLICY over the frozen table: a deterministic top-k of the
domains at least ``threshold`` away from the native domain — never single-argmax
(one farthest domain need not hold a bridgeable X; the top-k set is the substrate
B2's synthesis recombines), and never a value the run authored to pass.

The honest cost (ADR 0009 / alpha3): coverage == the operator's concept coverage.
A domain that could bridge to X but the operator never listed is unreachable —
the taxonomy imports the operator's blind spot. That bound is accepted by design:
it sits offline, auditable, fixable, and un-gameable in the table rather than
inside the run's gameable loop.

Pure module: no I/O beyond hashing a passed-in dict.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def distance(taxonomy: dict[str, Any], a: str, b: str) -> float | None:
    """Symmetric conceptual distance in [0,1] from the frozen table; None if the
    pair is not listed (an unlisted pair is NOT selectable — distance to it is
    unknown, so it cannot be certified far)."""
    if a == b:
        return 0.0
    return (taxonomy.get("distances") or {}).get(_pair_key(a, b))


def select_far_domains(
    native_id: str,
    taxonomy: dict[str, Any],
    *,
    top_k: int,
    band_lo: float,
    band_hi: float = 1.0,
) -> list[str]:
    """ADR 0009 / T2-12a — deterministic top-k domains in the FAR-BUT-BRIDGEABLE
    band ``band_lo <= distance <= band_hi`` from native_id.

    This is **not** top-k-farthest: a domain beyond ``band_hi`` is treated as
    noise (too far to bridge) and excluded *however far it is* — the farthest
    domain is the most likely to be unbridgeable, so selecting it was a
    known-wrong policy. Within the band, ordered by distance desc (the far end of
    the bridgeable region first), ties by id asc — deterministic over the frozen
    table. Returns [] if the native domain is absent or nothing falls in the
    band. The band edges are the SOURCE-dependent threshold (operator-set on the
    operator table, atlas-calibrated where an atlas covers the region); the band
    *policy* itself is independent of the distance source (INV-band-policy-independent)."""
    if band_hi < band_lo:
        return []
    scored: list[tuple[float, str]] = []
    for d in taxonomy.get("domains") or []:
        did = d.get("id") if isinstance(d, dict) else None
        if not did or did == native_id:
            continue
        dist = distance(taxonomy, native_id, did)
        if dist is None or dist < band_lo or dist > band_hi:
            continue
        scored.append((float(dist), str(did)))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [did for _, did in scored[: max(0, int(top_k))]]


def taxonomy_hash(taxonomy: dict[str, Any]) -> str:
    """Stable 16-hex provenance hash of the taxonomy content."""
    canon = json.dumps(taxonomy, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()[:16]
