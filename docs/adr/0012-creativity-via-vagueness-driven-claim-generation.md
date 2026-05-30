# ADR 0012 — Creativity via vagueness-driven diverse claim-generation

- **Status**: Accepted (design; not yet implemented)
- **Date**: 2026-05-30
- **Supersedes**: ADR 0011's *open goal* (reach non-obvious cross-domain framings).
  ADR 0011 abandoned the measured atlas + far-framing as a dead end; this is the
  replacement, built the opposite way — **generation + gate, not measured distance.**
- **Builds on**: the claim-structured production gate — ADR 0006 (external
  falsifier), 0007 (earned depth, not polarity), 0008 (verdict-strength /
  construct-adversary). The harness is structured by **claims, not metrics**; this
  design adds only a *generation front-end* and reuses the existing claim-gate for
  all judging.
- **Relates to (CONTEXT.md)**: [[creativity]], [[abstraction (de-domained)]],
  [[reading (field-forced)]], [[prune-1]], [[reduction]],
  [[market research (per-reading, far-method)]], [[research forest + single output]],
  [[firewall (P-withholding)]].

## Context

The harness is **claim-structured, not metric-structured**: a result is a claim
that survives an adversarial gate, never a maximized number. After ADR 0011
abandoned the measured atlas, the open goal remains: make **creativity** possible
— reach non-obvious framings of a problem P — without (a) trusting LLM
assertions, (b) letting the success-seeking run author its own bar, or (c)
repeating the atlas failure (a *measured* cross-domain distance that did not beat
a topical baseline).

Thesis: **make P's abstraction deliberately vague; vagueness admits many
readings; each reading is a different far-domain framing → diversity → creativity**
— provided each diverse claim still EARNS its standing through the existing gate.

## Decision

A generation front-end produces diverse research **claims** about P; each is
judged by the existing claim-gate; the single strongest-earned survivor is the
one output.

```
grilling (capture P) → abstraction (ONE, vague, de-domained, structure-preserving)
  ⟂ firewall (below here is P-blind, abstraction text only) ⟂
→ reading: force the LLM to read the abstraction through a RANDOMLY-sampled external field
→ prune-1: independent coherence-vs-abstraction check (P-blind, binary + lenient); resample to quota
→ market: per reading, research the far method in its field (P-blind)
  ⟂ reduction (P re-enters, P-aware) ⟂
→ reduction: faithfully translate the far method onto P → a P-claim (construction-worker)
→ production forest: each P-claim = a root → existing claim-gate per claim   [multi-root = NEW]
→ SELECT the single strongest-earned survivor (verdict_strength, then depth) = one paper
   (0 survivors → honest-failure)
```

Load-bearing principles:
1. **creativity = diverse generation × earned gate-survival.** Diversity alone is
   noise; the gate is what makes a far claim count.
2. **Diversity is forced, not asked.** One LLM told to "be diverse" mode-collapses.
   Diversity = vagueness (permits readings) × **random external field-forcing**
   (realizes them). The LLM only does conditional work (read through an assigned
   field); it never *selects* the field, and never picks "the best K" (that
   re-collapses).
3. **Reliability lives in the executed gate, never in stacked LLM judges.** prune-1
   and reduction are best-effort and may hallucinate; their errors are absorbed by
   the gate (executed evidence + frozen referents). Verifying an LLM judge with
   another LLM judge is an infinite regress of unreliable judges — refused.
4. **Firewall must be reliable (asymmetry vs prune-1).** A prune-1 miss is
   recoverable (the gate filters it); a firewall leak collapses readings to
   P-adjacent and is **not** recoverable (the gate filters, it cannot regenerate
   lost diversity). So P-blindness is enforced structurally (P absent from those
   calls' context) + a de-domaining scan of the abstraction.
5. **Authorship-separation reused at P-level.** The bar — P's success-definition
   (frozen_question, external falsifier, baselines, success/disproof, AC
   thresholds) — is frozen at grilling / operator / config BEFORE generation;
   every forest claim is judged against that *same frozen P-bar*. Generation
   produces approaches, never the definition of "P solved." (`falsifier.passed` is
   harness-derived; `frozen_question` is immutable — the run cannot author its bar.)

## Rejected alternatives (recorded so they are not re-invented)

- **Multi-grain abstraction (several `A_i` at different vagueness levels).**
  Diversity comes from the *readings*, not the abstraction count; grain is one
  knob and a mis-set grain is caught downstream (too-vague → fails reduction;
  too-concrete → fails the non-obvious bar). m× cost, no diversity gain.
- **LLM self-generates diverse fields.** RLHF'd LLMs mode-collapse to obvious
  neighbours; forbid-the-obvious on names is weak. Diversity must be *externally*
  forced.
- **LLM picks the "best K" fields.** "Best" re-collapses to obvious. Selection
  must maximize *spread* (random), not relevance.
- **A small hand-picked field roster.** That is the retired ADR 0011 atlas cap
  (the operator's blind-spot ceiling) reincarnated.
- **Process a huge field list exhaustively.** Cost explosion. Resolution: the huge
  list is a *sampling source* — breadth (list size) and cost (K sampled) are
  decoupled by sampling.
- **prune-1 by LLM self-reported confidence.** A *declared* value (the disease the
  harness fights), and it kills creative low-confidence readings. Use behavioral
  skeleton-construction by an *independent* call instead.
- **Make prune-1 reliable by stacking LLM judges.** Infinite regress. prune-1 is
  lenient triage; reliability is the gate's.
- **Forest-level multiple-testing (deflate the best metric for N trials — DSR/PBO
  as a harness layer).** A *fabrication*: metric-thinking imported onto a
  claim-structured harness. "Many claims → one passes by luck" is handled by the
  per-claim gate (validity / reproducibility / mechanism / necessity — luck has no
  mechanism and does not reproduce), i.e. `power = spawn-diversity × gate-strength`,
  not a bolted-on statistical layer. DSR/PBO/Sharpe are *domain content* of a quant
  experiment template, **never** harness machinery — the harness is domain-general.
- **Synthesize the survivors into one combined paper.** Risks forced/additive
  recombination ("combined → passes but wrong") and is hard to gate honestly.
- **Emit the full set of survivors.** "There are no scattered papers" — output is
  ONE; other survivors are search by-products.

## New vs reuse

- **New (build):** abstraction; reading (random field-forcing); prune-1; the
  per-reading P-blind market variant; reduction (construction-worker); forest
  activation (the multi-root scaffold — `seed_drafts_from_root` `root_id`,
  `alternative_root` tools — exists but has never run); select-strongest.
- **Reuse (unchanged):** the entire claim-gate (typed-stage decomposition, critic
  axes, falsifier, construct-adversary, AC, attestation, verdict-strength),
  authorship-separation / frozen referents, honest-failure terminals.

## Consequences

- The harness gains a front-end that turns one problem P into a forest of diverse,
  gate-judged claims and surfaces one earned creative result.
- Honesty is preserved by *reusing* the executed gate; the front-end adds breadth,
  the gate keeps it honest (`power = diversity × gate-strength`).
- Per-step front-end unreliability (prune-1, reduction hallucination) is
  deliberately tolerated and absorbed by the gate — except the firewall, enforced
  reliably because its failure is unrecoverable.
- Quant (DSR/Sharpe/backtest) is explicitly **not** part of this — it is one
  domain's experiment-template content; the design is domain-general.
