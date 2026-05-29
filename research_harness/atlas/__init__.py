"""ADR 0010 — the measured concept atlas (offline-built, self-gated).

The atlas is a CONSTRUCTION: a harvested concept manifold whose pairwise
distances are model-relative co-deployment behavior (measured), not topical
embedding similarity (declared/leaky). Per the harness's own discipline, a
construction is never assumed to work — it is gated. ``atlas.validate`` is that
gate (the atlas's external falsifier); the rest of the package (harvest, enrich,
distance, grain-cut, pin, update) is the offline build that only ever emits a
*passed* pinned artifact for the air-gapped harness to read.
"""
