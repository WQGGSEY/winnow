# ADR 0011 — Abandon the measured atlas and far-framing domain-connection

- **Status**: Accepted
- **Date**: 2026-05-30
- **Supersedes**: ADR 0010 (measured atlas + self-gate) entirely; the
  *distant-transfer / far-framing* (B-series) parts of ADR 0009. The
  *declared-vs-measured* honesty of ADR 0009 (A-series) and ADRs 0006–0008
  **remain in force**.
- **Removes**: `research_harness/atlas/`, `atlas_build/`,
  `research_harness/domain_taxonomy.py`, `configs/domain_taxonomy.json`,
  `domain_atlas` + `domain_taxonomy` schemas, the `pin_atlas_version` and
  `pin_domain_taxonomy` MCP tools/handlers, the far-framing-successor and
  synthesis-node builders, the `synthesis` node type and the
  `far_framing_domain` / `synthesis_inputs` / `forbidden_approaches` node-schema
  fields, the `persona_enforcement.far_framing` settings, the `atlas-builder`
  extra, and the atlas + far-framing test suites.
- **Keeps** (not domain-connection — falsifier/honesty infrastructure): A1
  behavioral-distance falsifier guards (`falsifier.py`, `falsifier_probe.py`),
  A2 attemptability, A3 reality-cap, and the B3 patchwork-insufficiency /
  `transfer_evidence_admissible` guards (they gate hollow *cross-generator*
  transfer evidence and have no far-framing dependency).

## Context — the atlas failed its own gate, conclusively

ADR 0010 built a **measured concept atlas**: model-relative *behavioral
co-deployment* distance (the model solves a battery of problems; a fixed
detector scans the worked solutions for which method-nodes were actually
deployed; co-deployed pairs are "near"). It was to replace the hand-written
distances of the ADR 0009 far-framing taxonomy, and it self-gated against an
operator-frozen, authorship-separated held-out of documented cross-domain
transfers, with a **pre-registered permutation-δ** margin over the strongest
topical (SPECTER) baseline.

A first read failed but was underpowered and contaminated. A **conclusive redo**
was run under the discipline's own bar — *fair, blind, dense*:

- **Fair**: a confirmed detector false-negative (a blind solver wrote
  "maximum-entropy" / "Lagrange multipliers"; the literal signature missed them)
  was closed by an operator-ratified phrasing-robust detector revision
  (hyphen/whitespace/plural normalization + a uniform standard-synonym pass).
  Verified zero remaining false-negatives.
- **Blind**: the battery was authored by a fresh subagent blind to the
  positives, and solved by fresh subagents blind to the method vocabulary and
  the held-out (independent of the measuring/orchestrating model).
- **Dense**: a 38-problem battery (30 + an 8-problem coverage top-up), 71%
  pair-fill, 6 of 7 held-out positives behaviorally supported.

**Result: `honest_failure`.** Co-deployment separation 0.6071 vs topical 0.6165
→ gain **−0.009**, far below the pre-registered δ **0.207**. (On the 6
expressible positives the sign even flipped positive, +0.0285 — but ~7× below δ,
i.e. dominated by topical priming.)

## Why this is a permanent dead end, not a tuning problem

1. **A behavioral metric is bounded by the measuring model's repertoire.**
   "Measure co-deployment by watching the model solve" can only see methods the
   model deploys. At least two method-nodes (`maximum_entropy`,
   `selection_under_constraint`) are behaviorally near-absent in the model's
   natural problem-solving — *even when problems explicitly invite them*, the
   model substitutes other standard tools (Tikhonov regularization,
   method-of-moments, Bayesian inversion, ALNS, RL). Held-out positive
   `maximum_entropy↔network_flow` therefore had **zero** support regardless of
   detector or battery quality. Transfers built on niche-for-the-model methods
   are structurally invisible.
2. **Topical similarity already captures what co-deployment captures, and more
   cleanly.** Where co-deployment carried any signal it did not exceed a plain
   topical embedding by a meaningful margin.
3. The δ bar is high because the held-out is small (7 positives → high
   permutation-null variance); a definitive pass would need either a far larger
   separation or a much larger frozen held-out.

The far-framing hand-table (the atlas's fallback) was independently shown
*sufficient at the current single-native-hub scale*. The operator nonetheless
elected a **clean-slate redesign** of the domain-connecting mechanism rather
than keep it — so far-framing is removed too.

## Decision

Remove the atlas and the far-framing/synthesis domain-connection mechanism in
full (see *Removes*). A new domain-connecting mechanism will be designed
separately; **this ADR records that behavioral co-deployment measured via the
solving model is a closed approach for it** — do not rebuild domain distance on
it.

## Durable lesson

A *measured* successor to a hand-set value is only worth shipping if it beats the
strongest cheap baseline on a frozen, authorship-separated referent by a
pre-registered margin. When the measurement is *behavioral*, it inherits the
instrument's blind spots: it cannot see what the measuring model does not do.
The honesty gate worked exactly as designed — it caught a plausible
construction that did not beat its baseline, and refused to ship it.
