# ADR 0010 — Measured atlas, its self-gate, and INV-class-property acceptance

- **Status**: Accepted (TRACK 2 honesty-hardening + atlas pin shipped; offline atlas build + Sprint-2 integration pending)
- **Date**: 2026-05-29
- **Extends**: ADR 0009 (declared→measured; far-framing/synthesis on an operator-curated taxonomy). Replaces the taxonomy's *hand-written* distances and *stated* native domain with a *measured* atlas + *measured* projection, and moves acceptance from per-instance tests to INV class-property tests.
- **Relates to**: `research_harness/atlas/` (`validate.py` self-gate, `pin.py`), `domain_atlas` / `domain_taxonomy` / `falsifier_result` / `frozen_question` schemas, `research_harness/falsifier.py` (+`falsifier_probe.py`), `research_harness/domain_taxonomy.py`, `research_harness/mcp_server.py`, `research_harness/thread_supervisor.py`, `settings.json` (`persona_enforcement.far_framing`), `pyproject.toml` (`atlas-builder` extra).

## Context — the fixed point recurses

ADR 0009 closed the declared→measured leak in the falsifier and pinned an
operator-curated taxonomy. Two declared values remained — the taxonomy's
hand-written *distances* and the *stated* native domain — and acceptance was
still per-instance. Pushing the discipline down to the distance metric and the
projection exposed a **scale-invariant fixed point**:

> Every **gate** needs a *strong baseline*; every **selection input** needs a
> *frozen referent*; every **fallback** needs the *best-available policy*. The
> discipline must reproduce at every layer it spawns — not just the run layer.

So the atlas (a new construction) is itself **gated** with an honest-failure
terminal; its restricted region is a **frozen referent**; the metric-failure
fallback is the **best-available policy** (operator-band, not farthest).

## Decision

### The measured atlas + its self-gate (TRACK 1)
- Distance is **model-relative co-deployment** measured on a fixed battery, never topical (SPECTER2) similarity (topical is a *separated baseline* used only in the gate). `domain_atlas` schema requires `distance_source = co_deployment`.
- **`atlas.validate` is the atlas's external falsifier.** Calibration against an operator/literature-authored known-transfer held-out set yields `{deployable, bounded_result, honest_failure}`. **deployable** requires co-deployment to out-separate the **strongest** topical baseline by a **pre-registered** margin δ; δ (+ band threshold + held-out set) is frozen **before** measurement and must clear the held-out-size noise floor — fitting δ to the observed gap would be RSTF re-anchoring recursed onto the atlas. **honest_failure** (co-deployment collapses into topical) ships nothing; far-framing keeps the operator table — no silent topical fallback. **bounded_result** ships a restricted atlas over only the calibrated regions.
- **Pin/version (T1-06):** `pin_atlas_version` hash-stamps a *passed* atlas, immutable per thread (mirror of `pin_domain_taxonomy`). Only deployable/bounded_result are pinnable; `atlas.pin.atlas_pinnable` enforces it. The offline build (harvest/enrich/distance/calibrate) lives behind the `atlas-builder` extra and never runs in the air-gapped harness, which reads only the pinned artifact.

### TRACK 2 — the same discipline, hardened (shipped)
- **T2-03 discrimination:** a **strong, harness-constructed** known-only baseline (the strongest standard-statistic transfer on the same probe; the worker can never supply it → no strawman). If even it clears the predicate → `uninformative`.
- **T2-05 evidence:** harness-stamped `transfer_evidence_admissible` (true only for a passing real_holdout) + a regression lock that the falsifier value never enters `publishing/`.
- **T2-10 reality-cap:** methodology `provides` is **structurally unreachable** without a verified real referent (not merely down-clamped on a weak path).
- **T2-12a band-policy:** far-but-bridgeable band `[band_lo, band_hi]` retires top-k-farthest (the noise end is excluded however far). Band *policy* is independent of the distance *source*; honest-fail falls back to operator-band, never operator-farthest.
- **T2-17 axis:** `subject_role` is **operator-set** and pinned with the frozen question (a gate-routing value → strongest authorship cut); not auto-derived from prose; omission defaults conservatively (probe stays on).
- **T2-18 terminal taxonomy:** `construct_valid_screen` renders a distinct **bounded_result** (a bounded positive), not honest-failure; applies to runs and to the atlas build.
- **Two distances stay distinct objects:** generator-distinctness (A1, output divergence) and atlas abstraction-distance (co-deployment) share the *machinery* but never the *θ value* — separate calibration, separate object.

### Acceptance: INV class-property tests
Every merge is gated by **∀-property** tests over synthetic spectra, not single instances. The INVs (INV-distinctness, -non-degenerate, -discrimination, -evidence, -attemptable-authorship, -no-premature-quit, -weak-clamp, -reality-cap, -novelty-inert, -projection-authorship, -atlas-self-gate, -atlas-representativeness, -restricted-membership, -band-policy-independent, -synthesis-no-launder, -terminal-taxonomy) are the contract; an instance passing is insufficient. **UNLOCK** (Sprint-2 atlas integration) = INV-distinctness ∧ -non-degenerate ∧ -discrimination green.

## Consequences / honest limits

- The atlas may **honest-fail** (co-deployment collapses into topical priming) — a *planned* terminal, not a setback; the operator table then remains permanently, stated.
- An atlas-**deployable** verdict is only as trustworthy as the pre-registered known-transfer held-out set is **representative** (INV-atlas-representativeness): a small/biased set can pass the gate spuriously. The operator's coverage blind-spot re-enters as gate-trustworthiness — stated, strengthened (never closed) by a richer set.
- Projection (Sprint 2) derives native-location from frozen-question **prose** via model behavior — a prose-authorship surface `subject_role` does not have. Accepted because projection feeds **generation (band-selection)**, not a gate; the gate backstops, so a gamed projection only wastes fan-out.
- ADR 0008 stands: none of this reaches `transfer_valid` air-gapped; co-deployment/behavioral measures remain *screens*, necessary-not-sufficient.
- "Nothing regresses" is **not** claimed: until atlas v1 pins, far-framing runs operator-band over declared distances (a known interim improvement over farthest, still declared); a full-auto single model reaches only up to unbridged-recombination novelty.
