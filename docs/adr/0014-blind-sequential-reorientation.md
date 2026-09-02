# ADR 0014 Blind sequential reorientation

- **Status**: Accepted
- **Date**: 2026-09-01
- **Supersedes**: ADR 0012's multi-root forest and failure terminal
- **Amends**: ADR 0013's observation-derived successor generation and resource-frozen goal
- **Preserves**: ADR 0006 through ADR 0009 and ADR 0013's verified strong-result terminal

## Context

The adaptive search path still derives the next strategy from the failed node.
The Professor receives the failure, writes successor candidates, and can route
an empty frontier into an alternative-root flow. The next generation call is
therefore anchored to the direction that just failed. Hiding a few prompt fields
cannot fix this dependency because the dependency exists in the state and tool
flow.

The current goal also contains a concrete data snapshot. The harness freezes
that snapshot before a direction states its evidence needs. A later direction
cannot acquire a better public source without changing the object that defines
success.

The strong-result verifier is not the problem. It already prevents a weak result
from becoming `goal_achieved`. The missing part is a search policy that changes
direction without weakening that verifier or treating an empty frontier as a
scientific result.

## Decision

The harness will use one blind sequential reorientation engine. The engine owns
an immutable `GoalContract`, a tagged `ReorientationState`, and an
append-only ledger of closed attempts. Exactly one `DirectionAttempt` can be
active.

`advance_research` is the public control operation. A node can run only when it
is the sole nonlegacy node bound to the active `awaiting_evidence` attempt.
Legacy queued, resumable, or promoted nodes are audit-only and cannot outrank
that binding. The operation reserves one step, performs external work outside
the adaptive writer lock, and commits the result only if the reservation and
revision still match.

The operation returns one of these results:

- `direction_ready`
- `acquisition_running`
- `checkpointed`
- `goal_achieved`
- `hard_external_block`

No result or state contains `not_found`. Budget exhaustion creates a checkpoint.
The supervisor resumes download, request, wall-time, and cost checkpoints in a
later cycle. A resume carries the expected revision and checkpoint ID. A stale
resume cannot advance a replacement phase, and an unchanged cursor is not
replayed in a tight loop. While a physical checkpoint remains authoritative, a
stale legacy expansion pause cannot stop the supervisor. Only a verified strong
result completes the scientific goal.

### Freeze success before direction generation

The candidate-blind compiler creates `GoalContract` before the first
direction. Its input contains the operator problem, grilling record, feasibility
scope, baseline dossier, safety limits, and holdout requirements. Its signature
does not accept a direction, strategy, node, failure, lesson, or resource
snapshot.

The contract contains the question, success criteria, and evidence requirements.
It has no accepted intake-claim field, connector alternative, prior direction,
failure, lesson, concrete adapter snapshot, or acquired file. A derived
`ResearchGoal` projection keeps the existing strong-result checks compatible.
The contract digest is the only source of truth for the success bar. This
structural boundary does not prove that retained free text is semantically
strategy-free. A separate goal-only intake projection must enforce that property.

The compiler accepts only a `real_holdout` registered by the operator or
supervisor bootstrap. A `cross_generator_transfer` result remains readable as a
legacy screen, but it cannot author a `GoalContract` or permit
`goal_achieved`. The compiler does not add a `none` falsifier kind.

### Make blindness a data-flow property

The direction generator receives one `GenerationRequest`. That request has
exactly two fields:

```text
GenerationRequest
  goal_contract
  random_perspective
```

The generator cannot receive the attempt ledger, prior fingerprints, worker
reports, failure prose, private lessons, adapter inventory, or acquisition
history. The harness selects the random perspective. The model does not select
the perspective that looks most relevant.

The engine evaluates generated drafts after the generator returns. The novelty
and safety gates can read prior fingerprints and private lessons. Rejection
details stay inside those gates and never enter the next generation request.

### Represent one active direction in the state shape

`ReorientationState.phase` is a tagged union:

- `seeking`
- `generation_reserved`
- `acquisition_reserved`
- `awaiting_evidence`
- `checkpointed`
- `goal_achieved`
- `hard_external_block`

Only the phases that need an active attempt contain one. A checkpoint contains
the exact continuation payload for its prior phase. The parser rejects a second
active attempt and any checkpoint that drops its active attempt.

Each accepted direction has a six-axis `DirectionFingerprint`:

- mechanism
- intervention
- observables and data
- analysis unit
- timescale
- system boundary

After one negative attempt, the next direction changes at least two axes. One
changed axis must be mechanism or intervention. After two negative attempts,
both mechanism and intervention differ from every closed negative attempt.
After three negative attempts, the direction also uses a system boundary that
does not appear in the closed negative ledger. A config may require the boundary
change earlier. A generator cannot weaken these rules.

### Close attempts only from harness evidence

Professor prose does not close an attempt. The engine derives one evidence
verdict from persisted runner, metric, baseline, critic, falsifier, construct,
and rebuttal artifacts:

- `conclusive_failure`
- `needs_more_evidence`
- `needs_data`
- `strong_candidate`

Only `conclusive_failure` closes an attempt as negative and adds a private
lesson. Runner errors, missing data, malformed output, and timeouts do not count
as scientific failure. `strong_candidate` continues through the existing
strong-result verifier. Only its verified receipt permits `goal_achieved`.

A clean failed `real_holdout` result also closes its bound attempt. The result
must name the current contract, attempt, direction, node, and acquisition
manifest. The Professor decision does not commit if the harness cannot persist
the private failure record. The next direction generator receives neither the
record nor the failed falsifier.

### Acquire data after a direction states its needs

An accepted direction declares typed data needs. The acquisition boundary may
use registered adapters, public APIs, public pages, and robots-compliant crawling.
It may use a preconfigured credential reference. It cannot create an account,
spend money, bypass access control, or bypass a paywall.

Every response records its source URI, retrieval time, robots decision,
rate-limit events, credential profile name without secrets, license evidence,
byte count, checksum, schema result, and missingness result. Redirects are
checked again under the destination origin policy. A failed source causes a
lawful substitute search before an external block.

Fetched bytes enter a content-addressed cache through a temporary file, file
sync, and atomic rename. Each executed node receives one immutable
`PinnedNodeManifest`. The manifest records exact cache objects and validation
reports. The worker cannot replace the manifest after execution starts.

The closed `HardExternalBlockCode` set is:

- `auth_required`
- `lawful_access_unavailable`
- `legal_access_denied`
- `storage_unavailable`
- `operator_scope_conflict`

Robots denial, a rate limit, or one paywalled source is not a hard block while a
lawful substitute remains. Download, request, wall-time, and cost budgets create
a checkpoint with a resumable cursor.

The connector runs two intake searches. It first searches unrelated fields for
candidate methods without exposing the operator problem. It then runs public
market research against the operator problem and stores the source-backed
baseline dossier. A connector session aborts when the baseline search retrieves
no admissible paper. Production cannot start from invented baseline provenance.

### Persist external work with reservations

Model calls, HTTP requests, downloads, and schema scans do not run under
`_exclusive_adaptive_writer`. Each external step uses three transactions:

1. `plan_advance` validates the expected revision and writes a content-addressed
   reservation under the writer lock.
2. The worker performs the reserved external operation without the lock.
3. `commit_advance_result` reopens the lock and commits only when the reservation
   ID, expected phase, and revision still match.

A repeated command returns its existing command receipt. A crash before commit
leaves a reconcilable reservation. Cache digests, acquisition cursors, node
manifest IDs, and command receipts make retries converge on the same state.

### Commit strong completion through the active attempt

The adaptive strong-result receipt includes the immutable contract ID and the
active attempt, direction, node, and acquisition manifest IDs. The terminal
verifier re-derives the frozen goal from `GoalContract`, checks every
evidence artifact and runner digest, and confirms the durable node-attempt
index. Receipt labels alone cannot complete the research.

The passing falsifier result must contain the same five binding IDs. The
referent ledger rejects a stale result before it can set the adaptive state to
`goal_achieved`. The selector also runs the terminal verifier before it routes a
completed state to paper rendering. A stale adaptive flag becomes an operator
scope conflict instead of a publication result.

Strong attestation first writes a prepared terminal receipt that contains the
full attestation and strong receipt. Under the shared writer lock, it then
rechecks the active binding, pinned manifest, runner artifacts, and evidence
digests before committing the adaptive receipt. The tagged state advances to
`goal_achieved` only after this second verification.

If the process exits between these writes, the supervisor replays the prepared
payload with its expected strong receipt digest. The replay either converges on
the same `goal_achieved` state or remains nonterminal. A rendered paper is
terminal only when its reorientation state contains the same strong receipt
digest. Replaying the same attestation returns the committed result without
creating another terminal state.

### Migrate without two live policies

The migration keeps every legacy node as audit history. It marks every legacy
node-attempt binding `legacy_audit_only=true`, then generates and materializes
one new blind direction. No legacy root or subtree becomes the active attempt.
When a validated intake handoff exists, its accepted baselines, success criteria,
and disproof conditions override provisional grilling extraction before the
immutable contract is compiled. Its proposed claim does not enter the contract.

If frozen thread artifacts disagree on the success bar, migration returns
`operator_scope_conflict`. Multiple legacy roots that share the same bar do not
block migration. Unexecuted observation-derived successors do not gain
authority in the new engine.

After callers move to `advance_research`, the same implementation wave deletes
the live paths for `prepare_strategy_expansion`, adaptive follow-up children,
forest seeding and selection, alternative-root proposal and selection,
root revision, automatic resolver pivots, root terminal snapshots, and the
negative paper terminal. `design_initial_claim_contract` now persists only the
validated handoff. Historical artifacts remain readable.

A fresh thread does not need a pre-existing `search_state.json`. The engine
compiles the contract and materializes one blind direction from the intake
artifacts. A missing required intake artifact is an `operator_scope_conflict`,
not a storage failure. A historical `honest_failure.html` remains readable but
does not mark publication complete or disable supervisor retry.

## Implementation sequence

Each unit ends in a runnable state:

1. Add `GoalContract`, the tagged state, durable schemas, and a verification
   command. Do not change live selection yet.
2. Add the blind generator boundary, the six-axis gate, and harness-derived
   evidence verdicts. Verify that recorded generator requests contain only the
   contract and one random perspective.
3. Add the public acquisition policy, content cache, validation reports, and
   pinned node manifests. Use fake transports before enabling live HTTP.
4. Add legacy migration parsing, reservation, execution, and conditional commit
   to the MCP boundary. Route the no-candidate selector branch into
   `advance_research`. Make the supervisor resume checkpoints.
5. Migrate remaining callers and delete the X-aware successor and alternative
   root paths. Run the complete regression suite and a restart replay.

All five units are implemented on `main`. The restart replay covers two closed
failures and proves that the third generation request contains only the frozen
contract and a newly sampled perspective.

## Consequences

The harness can leave a failed direction without asking the generator to reason
from that failure. Prior evidence still protects cost and safety through private
gates. It does not anchor the next proposal.

The search remains physically bounded per cycle and logically unbounded across
cycles. Missing resources become acquisition work or an explicit external
block. They do not become a negative scientific claim.

Serial exploration costs more wall time than a forest. It gives each result one
causal direction and removes concurrent writes to shared research state. Public
acquisition adds provenance and policy work before experiments. It also lets a
later direction request evidence that did not exist in the initial snapshot.

The strong gate does not weaken. Persistent search changes how the harness seeks
an answer, not what counts as an answer.

## Rejected alternatives

### Hide failure text in the prompt

Rejected because the successor object still originates from the failed node and
the same observation-derived tool flow.

### Keep a forest and mark one root active

Rejected because the state still contains multiple live alternatives and keeps
the alternative-root selection policy as a second source of truth.

### Run crawling under the adaptive writer lock

Rejected because a slow or stalled source would block every state transition and
make crash recovery depend on a process-held lock.

### Replace the JSON state with event sourcing

Rejected for this migration. The current command receipts, atomic JSON write,
content digests, and reservations can provide convergence without a second
persistence system.
