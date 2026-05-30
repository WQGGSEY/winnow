# ADR 0009 — Declared-vs-measured honesty + distant-transfer novelty

- **Status**: Accepted for the **declared-vs-measured / A-series** honesty (in force). The **distant-transfer / far-framing (B-series)** — operator taxonomy, far-framing successors, synthesis nodes — is **SUPERSEDED by ADR 0011 and removed**; see 0011 for the conclusive `honest_failure` that retired it.
- **Date**: 2026-05-29
- **Extends**: ADR 0006 (external-falsifier gate), ADR 0007 (premature
  termination / depth gate), ADR 0008 (two-axis closeness / verdict-strength).
- **Relates to**: `research_harness/falsifier.py`,
  `research_harness/falsifier_probe.py`, `research_harness/domain_taxonomy.py`,
  `research_harness/mcp_server.py` (falsifier handler, attestation handler,
  honest-failure render, AC decision, `submit_professor_decision`),
  `falsifier_result` / `user_goal_attestation` / `ac_decision` /
  `frozen_question` / `node` / `domain_taxonomy` schemas, `settings.json`
  (`persona_enforcement.falsifier_guards` / `far_framing`).

## Context — one disease in three honesty leaks, plus a novelty ceiling

Diagnosis of `thread_c8919361` plus review of the harness surfaced **one
recurring disease**: *a declared / labeled value sitting where a measured /
derived / referent-bound value belongs* — and it survives in exactly the places
where **measuring is expensive and labeling is cheap**. A hash-pinned *string the
run authored* is still the killed pattern: pinning provenance ≠ measuring the
property.

It appeared in three honesty leaks and shaped one novelty fix:

1. **Hollow `rho` (A1).** `cross_generator_transfer` asserted generator B was a
   "distinct" held-out generator by **label difference**. A same-family /
   shared-prior B passed, producing a `rho=1.0` reported as transfer evidence. A
   label cannot express the frozen question's *"same family yet structurally
   distinct"* requirement.
2. **Premature give-up (A2).** An honest-failure terminal was reached while a
   `required_additional_research` item was still **attemptable in-envelope**.
3. **Optimistic methodology (A3).** A clean construct-adversary pass laundered a
   "screen validated / provides" methodology verdict over a falsifier that was
   uninformative.
4. **Novelty ceiling (B).** Successors were `mechanism / necessity / boundary` —
   analytical decomposition *within the home manifold*, which yields patchwork,
   never the distant transfer where a genuinely new construction (X) could live.

## Decision

Replace **declared → measured / derived / referent-bound** at each site, gate
both doors, and re-bias generation toward forced distant transfer — but only
behind the keystone, because the gate that kills patchwork is the only selection
pressure that makes distant framing pay off.

### A1 (keystone) — behavioral-distance falsifier guards
`falsifier.py` gains a verdict ladder `{passed, failed, degenerate,
uninformative, invalid}`; `passed = predicate true AND all guards ok`. For
`cross_generator_transfer`:
- **(a) behavioral distance (MEASURED).** `falsifier_probe.py` runs generator A
  and B through `datasets/registry.materialize` on a **harness-fixed seed/probe**
  and measures output-distribution divergence. Identical generators → distance 0
  → `uninformative`. The worker supplies *which* recipes; the harness *runs*
  them. Absent harness-loadable recipes → cannot certify → `uninformative`.
- **(b) rank-discrimination floor** → `degenerate` when the rankings don't
  separate the pipelines.
- **(c) null floor** → `uninformative` when the predicate sits at the
  permutation-null noise band (also reused as B3's patchwork-insufficiency probe).

Behavioral distance is **necessary, not sufficient**: it certifies measured
divergence on harness inputs, not divergence on the frozen question's axis
(reality-closeness stays `transfer_valid`, unreachable air-gapped).

### A2 — attemptability derived from envelope membership
`required_additional_research` items carry structured `required_resources`;
the harness STAMPS `attemptable_in_envelope` by membership-checking each against
the `feasibility_envelope` (the LLM never authors the boolean; a declaration may
only *raise* the bar; naming a present resource keeps the item attemptable, so a
misdeclaration to dodge work is caught; a genuinely-absent claim is
operator-flagged, not silent-logged). `render_honest_failure_paper` refuses while
any attemptable item is undone → routes to fan-out.

### A3 — weaker-path methodology clamp
`submit_ac_decision` caps `methodology_assessment.aggregate_verdict` to the
weaker of the executed verification paths (a run-but-not-passed falsifier forbids
`provides`; a broken adversary forces `absent`), recording `clamp_reason`. The
cap only lowers, and only when a path was executed and came back weak.

### B1 — forced distant-transfer successors (pure-policy selection)
An operator-curated `domain_taxonomy` (graph + conceptual distances) is pinned
per-thread, hash-stamped + immutable (`pin_domain_taxonomy`). On promotion /
`needs_child_branch` the harness mints far-framing successors by **deterministic
top-k** over the frozen table (`domain_taxonomy.select_far_domains`) — not
single-argmax, never an LLM-authored domain. A negatively-resolved parent's
successors carry a `forbidden_approaches` hard constraint (forbid-the-familiar).
Distance feeds **spawn selection only**, never promotion/winner-selection.

### B2 — synthesis node (no privileged novelty)
After all far-framing siblings terminate, the harness mints ONE `synthesis` node
recombining their partial mappings (`synthesis_inputs` = the harness's record of
which completed). It earns **no** novelty credit for being a synthesis — it
re-enters the same critic / reduction / falsifier / construct-adversary gates,
and "additive recombination (y+z)" is pinned as a disproof condition so a summing
synthesis is a breakable pass-but-wrong world.

### B3 — patchwork-insufficiency probe, axis bound to the referent
The probe is A1(c) reused. Its routing axis is `frozen_question.subject_role`
(`method_is_solution` → gates the claim; `method_is_subject` → generator/screen
design axis), pinned PRE-construction and immutable — never a per-node judgment a
patchwork node could flip to switch the probe off its own claim.

### α invariants (enforced throughout)
- **α1**: novelty/methodology are never an *output reward*. `score_summary.novelty`
  is recorded but never gates; promotion is verdict-based; selection is by type
  weight + depth. Distance feeds spawn, never winner-selection.
- **α2**: authorship cut on every new field — the cut is a *measured/checked*
  fact, not a hash-pinned declaration: behavioral distance (run), attemptability
  (envelope membership), `subject_role` / taxonomy / `synthesis_inputs`
  (frozen-referent or harness-recorded).
- **α3**: cost/termination honesty + residual acceptance. The honest-failure
  report states the limits: full-auto single-model reaches up to
  unbridged-recombination novelty, cannot reach absent-concept novelty, cannot
  tell which a failure is; the taxonomy imports the operator's blind spot
  (accepted by design — offline, auditable, un-gameable); behavioral distance is
  necessary, not sufficient.

## Consequences

- The keystone (A1) ships *with* the measured distinctness check, not a label
  placeholder — the `thread_c8919361` hollow pass is reproduced as a test and
  blocked. Guard thresholds are tunable (`persona_enforcement.falsifier_guards`)
  but the guard is intrinsic to an honest verdict, not a disableable rail.
- Honest-failure is now only reachable once attemptable in-envelope work
  (including the far-framing successors) is genuinely exhausted.
- Far-framing is low-hit-rate by design and needs budget; it is gated behind A1
  because without the gate, distant nodes lose to cheap patchwork.
- **Accepted bound**: the taxonomy's coverage is the operator's concept coverage.
  We put the manifold ceiling where it is auditable and fixable (the offline
  table) rather than inside the run's gameable loop — the human is the cheapest
  external-manifold adder.
- The probe / behavioral measures remain *screens*, not strength-certifiers:
  none of this reaches `transfer_valid` air-gapped (ADR 0008 stands).
