# Strong-strategy adaptive search synthesis

## Decision

Use Candidate B as the migration base and graft Candidate A's content identities,
transaction receipts, typed strong-result receipt, and fail-closed goal migration.

The active file-backed MCP controller remains the only policy owner during the
first vertical slice. The existing `search_state.json` stays authoritative while
the adaptive domain is introduced. Adaptive-owned facts have one authoritative
representation. Legacy node and frontier fields are projections, not a second
place where search policy may be authored.

## Required domain model

- `ResearchGoal` owns one immutable, problem-level success bar and its digest.
- `StrategyFamily` identifies a causal mechanism and executable intervention.
  A verification-axis label or a rewritten claim is not a new family.
- `DiagnosticExperiment` identifies an executable recipe against the frozen bar.
- `Observation` contains measured facts, evidence digests, and explicit belief
  updates. Declared LLM values remain auditable but cannot clear a gate.
- `SearchDisposition` has only `continue`, `paused_needs_expansion`, and
  `goal_achieved`.

## Active-path invariants

1. Every strategy and experiment references the same frozen goal bar. A child
   cannot replace mandatory baselines, success criteria, disproof conditions, or
   deployment scope.
2. A conclusive negative decision atomically creates at least two pairwise
   distinct causal strategies or records a resumable pause with the missing
   capability and best next experiment.
3. Strategy, experiment, and observation identities come from canonical domain
   content. Duplicate work is rejected before consuming a runner invocation.
4. Frontier priority is computed by the harness from observable components:
   frozen-bar gap coverage, information target, novelty from refuted mechanisms,
   capability fit, and estimated cost. The LLM cannot submit an overall score.
5. Empty frontier, depth limits, and budget limits are pauses. They are not
   scientific completion.
6. Only a verified strong-result receipt can produce `goal_achieved`. The receipt
   reuses the existing falsifier, construct-adversary, critic, AC, attestation,
   referent-strength, and measured-versus-declared checks.
7. State mutations are idempotent. A command ID and expected revision prevent a
   retried or stale command from applying twice.

## First vertical slice

The first slice proves one active-MCP transition from negative evidence to a
newly selected, non-duplicate experiment.

1. Add pure content identities, expansion validation, priority calculation, and
   disposition derivation.
2. Require negative Professor decisions to provide at least two causal strategy
   candidates. Materialize them immediately with the parent's frozen bar.
3. Reject repeated strategy candidates and exact executable-template duplicates
   before a runner invocation.
4. Select queued work using persisted, harness-derived priority components.
5. Return `paused_needs_expansion` when no useful work remains. Publication and
   negative reports remain progress artifacts, not scientific terminals.
6. Replay the recorded arXiv trace through the active MCP handlers and compare it
   with `.audit/strong-strategy-baseline.json`.

## Deferred work

- A separate `search_program.json` source of truth and read-only v1 projection.
- Full experiment leases and immutable artifact-addressed worker ingestion.
- Cross-thread belief priors.
- Native frontend views for beliefs, strategies, and pause records.
- Deletion of the dormant `AgentManager` and `ParallelAgent` policies after the
  active path has replay parity.

## Arena result

Candidate A scored 26/30 unweighted. Candidate B scored 24/30 unweighted. The
cross-judge selected B after weighting migration risk and a verifiable first
vertical slice. A's full separate-state migration was rejected for this slice
because it combines policy replacement, persistence migration, frontend
projection, supervisor migration, gate bridging, and replay in one change.
