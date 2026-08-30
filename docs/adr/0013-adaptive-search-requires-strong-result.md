# ADR 0013: Adaptive search requires a strong result

- **Status**: Accepted
- **Date**: 2026-08-31
- **Supersedes**: ADR 0012's `0 survivors -> honest-failure` terminal rule
- **Builds on**: ADR 0006 through ADR 0009 and ADR 0012's frozen problem-level bar

## Context

The active production path evaluates a fixed batch of connector-generated
claims. A negative Professor decision can enter `needs_child_branch` without
creating a child. Fresh work is selected by stage, claim type, depth, and ID,
not by what previous experiments taught the system. When the queue becomes
empty, the path proceeds toward publication readiness and may terminate as an
honest failure or an unverified screen.

The recorded arXiv run demonstrates the consequence. All 24 nodes were pruned,
none of the 24 negative decisions created a follow-up, no adaptive child was
created, and 79.1667 percent of worker reports repeated an evidence payload.
This is a fixed batch evaluator, not an adaptive research search.

ADR 0006 through ADR 0009 correctly prevent unsupported strength. Their
falsifier, construct-adversary, critic, AC, attestation, referent-strength, and
measured-versus-declared gates remain authoritative. The missing part is the
policy that creates, learns from, and allocates work before those gates.

## Decision

The harness will represent adaptive research with four separate domain concepts:

- `ResearchGoal` owns an immutable problem-level success bar.
- `StrategyFamily` identifies a causal mechanism and intervention.
- `DiagnosticExperiment` identifies an executable recipe and its predictions.
- `Observation` records measured facts and explicit belief updates.

The active MCP controller is the only search-policy writer. Negative evidence
must change the next work. A conclusive negative decision atomically does one of
the following:

1. Materialize at least two pairwise distinct causal strategy candidates under
   the same frozen success bar.
2. Record `paused_needs_expansion` with the missing capability, the best next
   experiment, and a concrete resume condition.

Strategy, experiment, and observation identities are content-derived. A changed
label, claim rewrite, or verification-axis prefix does not create new work.
Equivalent executable work is rejected before a runner invocation.

Frontier priority is harness-derived and auditable. It combines frozen-bar gap
coverage, expected information gain, mechanism novelty against refuted
strategies, capability fit, and normalized cost. An LLM may propose the domain
content but cannot author the aggregate priority.

The scientific disposition has three values:

- `continue`
- `paused_needs_expansion`
- `goal_achieved`

Empty frontier, maximum depth, and budget exhaustion produce a pause. Honest
failure papers, bounded results, construct-valid screens, and unverified screens
may be rendered as progress artifacts, but they are not scientific terminals.

Only a verified strong-result receipt can produce `goal_achieved`. Its required
referent strength follows the frozen operator scope. Deployment goals require
real transfer evidence. A narrower scope cannot be silently substituted.

## Consequences

- The harness cannot claim completion merely because its current batch ended.
- Negative results become useful observations that alter strategy generation and
  selection.
- Repeated work is visible and rejected before spending execution budget.
- Runs that need more data, compute, tooling, or strategy generation stop at an
  exact, resumable location.
- Existing integrity gates stay strict. Search becomes more persistent without
  making weak evidence look strong.
- A strong strategy cannot be guaranteed to exist for every question. The
  harness guarantees truthful progress semantics and adaptive use of evidence,
  not success by infinite retry.

## Rejected alternatives

### Make the current loop unbounded

Rejected because repetition does not create information. Without strategy and
experiment identity, an unbounded loop can spend indefinitely on equivalent
work.

### Only materialize `needs_child_branch` follow-ups

Rejected as incomplete. It fixes the missing-child symptom but retains mutable
bars, verification-axis clones, fixed selection order, duplicate experiments,
and weak terminal semantics.

### Replace the active state with an event-sourced system immediately

Rejected for the first slice. The production path has one MCP writer and a
file-backed frontend. Event sourcing would add projection and migration risk
before the adaptive policy is proven.

### Weaken the gate until a survivor appears

Rejected. It would optimize for declared success rather than a strong strategy.
The gate remains fixed; generation, learning, and allocation change.
