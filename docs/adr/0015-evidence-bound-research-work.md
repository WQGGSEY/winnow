# ADR 0015 Evidence-bound research work

- Status: Accepted
- Date: 2026-09-06
- Amends: ADR 0014's universally blind generation boundary and session-long execution
- Preserves: frozen GoalContract, one active DirectionAttempt, acquisition provenance, independent baseline qualification, strong-result and publication checks

## Problem

Preparation execution was not connected to research decisions. The Pacman thread
spent repeated runs debugging or training an unqualified baseline while its claim
remained unchanged. A live Codex process and a different source file did not imply
new evidence. Rephrasing a hypothesis revision also restarted its draft rather
than resuming its critique; that bug was fixed separately in a54c888.

## Decision

One `ResearchWork` is the next bounded experiment or diagnostic, not a scientific
claim. It records an uncertainty, cited development evidence, competing
explanations and predictions, the decision each result changes, a test, an
inconclusive-result action, and a runtime limit. `plan_research_work` uses Sol low
to choose this work; Luna max or Sol low executes it through the existing tools.
The planner does not supply a completed learner or change the research goal.

Work transitions are `planned -> running -> completed`. Work identity includes
the decision and the evidence packet. Execution binds the node, scope and plan
digest. An outdated plan, mismatched work ID or runtime exceeding the work budget
cannot dispatch a new experiment. Completed preflight receipts remain replayable.

Each work selects one decision-changing uncertainty and explicitly defers other
questions. Deferred items cannot become requirements of its implementation review.
Source interpretation, definitions and questions answerable from existing records
use `kind=analysis` and `resolve_research_work`. The independent reader records a
sourced answer with limitations or identifies the missing evidence. No runner
report or new empirical observation is fabricated for such an analysis. Its
completion is replayable and triggers the same automatic next-work checkpoint.
Planning policy revisions invalidate stale planned work at dispatch as well as
at selection; input correction cannot bypass an evidence-driven replan.

Both baseline preparation and formal claim experiments review the implementation
before persisting a job manifest. Rejection keeps the claim and work unchanged
and routes formal experiments back to template revision. A corrected dispatch
clears the previous rejection outcome while retaining the review record.

The planner and implementation reviewer receive the registered protocol,
including its notes. A work binds its protocol digest so a plan cannot execute
against a different registration. This addresses the live run's conflict between
a QMIX-only protocol and later MAC-IAICC preparation.

`protocol_revision` routes a prospective development amendment to
`revise_evaluation_protocol`. It can replace notes only. Structured resources,
goal and predicate remain identical. Independent review must also establish that
endpoint meanings, thresholds, partition and fair comparison rules remain intact
and that the reserved holdout was untouched. Missing telemetry is not proof of
no access. Qualification, promotion or recorded final evaluation closes this
amendment window. Uncertain access history requires a new study.

Approved amendments preserve the previous protocol, rationale and review. The
paper writer receives them, the methods section must anchor each amendment, and
the publication receipt covers the amendment records. An amendment grants no
scientific approval and cannot turn prior development into preregistered evidence.

A request rejected before a job manifest exists returns to `planned` with the
same work identity. Its rejection reason and the absolute path of its saved
dispatch request remain available for correction. No execution checkpoint is
issued. The Pacman E2E exposed why this distinction matters: input-snapshot and
duplicated success-criteria errors were being mistaken for completed work, so
the next planner changed the scientific test despite receiving no observation.

Saved requests can be revised through path/value updates in the execution tool;
the execution agent's shell remains read-only. After structural validation and
before starting a new preparation job, an independent Sol low review checks that
the proposed code implements the selected test. Its receipt binds the exact work
decision and plan. A rewritten surrogate cannot stand in for an audit explicitly
requiring the actual collector. Rejection keeps the work planned and provides
required changes; it neither refutes the claim nor requests human approval.
Review objections are themselves fallible. Required changes must follow from the
selected test, declared objective or sourced method, rather than a reviewer's
preferred representation. The E2E reviewer incorrectly imposed a primitive-time
discount convention before that unit had been established. Review policy versions
invalidate cached judgments after such corrections. A restart before dispatch
also preserves the same planned work instead of inventing a new research result.

`finish_work` records the actual runner/report observation and whether its
measurement/error signature differs from the preceding observations. This is
called `new_observation`, never scientific progress or qualification. Source-code
changes, elapsed runtime and node IDs do not by themselves change that signature.
The next planner interprets the outcome with its original predictions. It must
cite real evidence IDs. Execution errors, interrupted execution and identical
non-replication observations require a diagnostic work unit. Replication is not
rejected merely because measurements agree.

Runner completion and measurement validity are separate observations. Invalid
metric output carries its validation error and no fallback runtime metrics as
research data. Completed, validated measurement details are supplied as bounded,
explicitly truncated excerpts so aggregate differences can be traced to fields.

The public planning and execution operations share the existing cross-process
writer lock. A reserved work encountered by a new planning call is reconciled
from its persisted report or recorded as interrupted, without calling it a
scientific failure. Per-work history remains under `production/research_control`.
These records never directly promote or prune the active claim.

The allowlisted development packet reads baseline preflight reports and formal
node reports bound through ResearchWork. It does not read external-falsifier or
final holdout result files. Report content remains unverified scientific support:
a successful process or a numeric metric can still be methodologically invalid.
This boundary cannot prove that arbitrary agent-authored code never accessed a
held-out sample; the existing input and evaluation integrity checks still matter.

New direction reservations include the development packet when available. Every
fourth draw omits it and explores the independently sampled perspective. This is
an initial explicit exploration allocation, not an empirically optimized ratio.
The complete generation request is persisted and hashed, so replay uses the same
observations even if later work changes disk state. Old two-field reservations
remain readable. The private closed-attempt ledger and final holdout are not
injected into generation. Novelty checks and one-active-attempt binding remain.

Supervisor sessions yield after an execution tool returns a durable work checkpoint, or after 16 completed
MCP calls with no pending MCP call. They never yield in the middle of an execution.
The result is written to the event log before termination. A dedicated internal
exit code distinguishes an intentional work checkpoint from rate limits or a
crash. The next supervisor cycle skips the idle gate and interprets durable state.
This is an automatic scheduling boundary, not a request for human decisions.

The graph shows the work's uncertainty, selected test, status and whether a new
observation was recorded independently of claim count.

During production, `get_research_state` defaults to the current work, execution
constraints, node summaries and absolute artifact paths. Full historical content
is available with `view=full`; it is no longer injected before every decision.
The live Pacman agent repeatedly fetched an unchanged response of about 70,000
characters, including historical hypotheses, while trying to locate one error.

## What this does not assert

The controller does not guarantee an informative experiment merely because the
planner wrote different predictions. Budget, identity and evidence references are
checked in code; the scientific adequacy of the test still needs independent
measurement and review. The current word-based interpretation is an explicit,
recorded proposal, not an established lesson.

Existing frozen placeholder clauses are not silently replaced. The positive-only
scientific terminal and theoretical-result contracts are not changed by this ADR.
The broader research-progress-control proposal describes those remaining design
questions. Publication-level E2E completion must still be demonstrated separately.

## Verification

Tests use real LocalRunner execution to show that an implementation exception
produces a diagnostic successor with the bound error evidence, without creating
or refuting a scientific claim. They cover work replay, runtime refusal, restart
reconciliation, formal-node input integrity, and withholding final holdout files.
A subprocess integration test confirms that a completed experiment yields without
waiting for the stall watchdog. Direction tests verify observation-bearing
reservations and idempotent replay; existing independent exploration tests remain.
