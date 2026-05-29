# ADR 0008 — Two-axis closeness: construct adversary + verdict-strength types

- **Status**: Accepted
- **Date**: 2026-05-29
- **Extends**: ADR 0006 (external-falsifier gate), ADR 0007 (premature
  termination / funded failure). Reclassifies ADR 0006's
  `cross_generator_transfer` and replaces the binary `max_attestable_status`
  ceiling with a referent-keyed type ladder.
- **Relates to**: `research_harness/verdict_strength.py`,
  `research_harness/construct_adversary.py`, `frozen_question` +
  `construct_adversary_report` schemas, `CONTEXT.md` (verdict_strength,
  referent_ledger, frozen_question, construct_adversary, authorship_separation).

## Context — the leak in what we shipped

PR #1 shipped `cross_generator_transfer`: the harness computed a Spearman ρ,
but the **ranking vectors were supplied by the proposer** in
`compute_falsifier_result`. The harness owned the arithmetic; the proposer
owned the inputs. That is the one leak every prior fix was chasing — *the
check depends on a value the checked process authored* — so that "falsifier"
is co-optimizable by construction.

The general principle this forces:

> **closeness is never proven by the proposer's assertion. It is proven only
> by funding an adversary against a referent the proposer did not author, and
> the adversary failing to break it.** Asking an LLM "is this close?" yields an
> assertion, and assertions are co-optimizable. So the correct form of
> "enforcement" is not *checking the proposer* — it is *funding an adversary*.

And closeness splits into two axes with different referents and **different
enforceability**, which must not be conflated:

- **Axis 1 — close to the QUESTION (construct validity).** The question is a
  formal object: it can be frozen and an adversary funded against it without
  touching reality. **Air-gapped-enforceable.**
- **Axis 2 — close to REALITY (external validity).** The adversary is also
  inside the system; it can only check the system's *model* of reality, which
  is co-authored. An adversary that certifies reality-closeness without a
  reality referent cannot exist. **Air-gapped-unenforceable — only boundable.**

## Decision

### 1. Verdict-strength type system (the binding meta-invariant)

A verdict's maximum strength is bounded by the strongest referent its
falsifier was actually run (and harness-checked) against. The ladder:

    internally_valid  <  construct_valid  <  transfer_valid

| verdict | referent | how earned | air-gapped |
|---|---|---|---|
| `internally_valid` | internal | the run's own metrics; no funded adversary | floor |
| `construct_valid` | frozen question | a funded construct-adversary **failed** to break it | **earnable** |
| `transfer_valid` | real | a falsifier ran against a real referent and passed | **unreachable** |

The harness **derives** the strength from the referent ledger and stamps it on
the attestation — the LLM never authors `verdict_strength`. A verdict above the
available referent is **unconstructable**, not rejected-after-the-fact: the
reachable vocabulary does not contain it. This is how Axis 2 is enforced —
`achieved=true` (which asserts the result is usable for the user's *real*
decision = reality-close = `transfer_valid`) is *unsayable* without a real
referent. The system is **constitutionally silent** about the gap it cannot
measure. The only entrance that breaks the silence is a single real scalar the
**operator** registers (their intranet) — that is not a limitation, it is the
definition of the axis.

### 2. The funded construct-adversary (Axis 1, built fully)

`construct_valid` is earned only by a funded adversary failing:

1. **Authorship separation (hard invariant).** The question is pinned
   (`pin_frozen_question`) from a *pre-construction* artifact (grilling) with a
   provenance hash and is **immutable**. The construction cannot restate or
   modify the referent it is judged against. The adversary's information is the
   frozen question — the proposer did not author it, so it does not share the
   proposer's blind spot (the failure mode of same-prior personas/holdouts).
2. **Declared `pass_but_wrong` region.** Every construction declares the worlds
   where its measurement passes yet the frozen answer is NO. Empty/absent →
   invalid (every non-trivial measurement has such worlds; claiming none is a
   lie).
3. **The adversary searches that region** (`submit_construct_adversary_report`).
   Its sole reward is finding a pass-but-wrong instance. Found → `broken`
   (construct-invalid). A funded search that finds none → `survived`
   (`construct_valid`).
4. **Verdict only on funded failure.** budget-0 / shallow search → auto-invalid.
   The harness **re-derives** the verdict from the enumerated search
   (`construct_adversary.py`) — a self-reported `survived` that enumerated a
   breaking world is overruled. This is ADR 0007's funded-failure depth gate
   aimed precisely at the closeness adversary.

The adversary is authored to **break** and has **no accept authority** — its
decorrelation comes from different information (the frozen question) and a
different incentive (break, not pass).

### 3. Reclassify `cross_generator_transfer`

Because its holdout rankings are proposer-authored, `cross_generator_transfer`
is a **kill-capable screen, not a strength-certifier**: failing it can block,
but passing it raises strength by nothing. Only `real_holdout` reaches
`transfer_valid`. (Necessary-condition cascade: stack independent screens; any
one fails → out; all pass → still "necessary, not sufficient." More screens
narrow the surviving space — but **narrowing ≠ closing**, and the type system
says so by keeping `transfer_valid` unreachable.)

### 4. Meta-invariant — `unverified` typing

Every proposer-controlled check-input is typed `unverified`; a verdict
depending on an unverified input cannot reach a passing terminal. Concretely:
only harness-executed, harness-checked evidence raises `verdict_strength`; a
proposer assertion never does. Every closeness claim must enclose an *executed*
falsifier (the harness runs it) — a *described* one is auto-`unverified`.

## Consequences

- `achieved=true` is now gated on `transfer_valid` — **unreachable air-gapped**.
  Every air-gapped thread caps at `construct_valid` (strongest) or
  `internally_valid` (floor). This is strictly more honest and intentionally
  blocks the operator's air-gapped workflow from claiming goal-achievement
  until they register a real referent. The `falsification_gate` toggle (default
  on) still applies.
- New honest terminals: `accept_with_construct_valid` (Axis 1 survived) and the
  existing `accept_with_unverified_screen` (floor). `not_achieved` (a real
  referent registered but its falsifier not yet passed) stays a retry.
- The envelope's `max_attestable_status` (ADR 0006) is now advisory; the
  ledger-derived `verdict_strength` is authoritative at attestation time.
- The MCP server must be restarted to load the two new tools + the rewired
  attestation gate.

## What stays honestly out of reach

Every air-gapped lever — temperature, decomposition, self-play, memory, the
construct-adversary — reweights within the model's manifold and certifies at
most `construct_valid`. None can distinguish a `true-hard` negative from a
`model-zero` negative, and none generates the rare framing outside the
manifold. That residual is what multi-model and a real referent buy. The
single-model honest ceiling is: **"survived the widest funded attack reachable
within this model's representation space, against a question the run did not
author."** Reality-closeness above that is the operator's scalar to grant.
