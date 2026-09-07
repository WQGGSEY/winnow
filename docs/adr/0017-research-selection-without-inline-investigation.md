# ADR 0017: Separate work selection from investigation

- Status: Accepted
- Date: 2026-09-06
- Amends: ADR 0015's read-only tool access during work selection
- Preserves: ResearchWork provenance, GoalContract, independent implementation review, prospective protocol approval, strong-result publication checks

## Failure

The Pacman continuation did not return a work decision within 240 seconds. The
previous input compaction replaced useful findings with file references while the
planner's instructions still required inspecting source and accumulated protocol
history. This made work selection an open-ended source investigation inside an
MCP call. Reducing initial prompt bytes did not bound the agent's subsequent work.
The discarded timeout output prevents attributing the measured latency to one
specific internal tool or document; that part remains unknown.

## Decision

The planner is a decision-only role. It receives current observations, previous
predictions, explanation lineage, findings with limitations, the registered goal
and protocol, and references to detailed evidence. It returns one provisional
ResearchWork without local tools, subagent delegation or host skill discovery.
Its prose is bounded at generation; the execution agent still writes the program.

Analysis conclusions remain inline even when the underlying packet is large.
Code and detailed protocol history remain available to the execution agent and
independent reviewer. The planner must not claim to have read referenced material.
If a source question changes which scientific test should run, it selects an
explicit analysis work. Routine implementation inspection or compliance review
must not become a separate scientific work solely because the planner cannot
inspect files. An operational failure can lead to a corrected bounded measurement
of the same scientific question, without pretending that failure supported a
scientific prediction.

Selection does not grant permission to execute. The existing implementation and
protocol reviewers still receive source, full protocol history and provenance;
rejection preserves the selected work or identifies a necessary prospective
amendment. No final endpoint, qualification condition or publication bar changes.

## Adjacent state failures

A planned work replaced because its policy or model changed is marked superseded,
not left as another apparently active work. A premature confirmation decision is
rejected before its response is cached, so the next attempt sees actionable
rejection feedback rather than replaying a permanently invalid cached response.

## Limits

This change bounds the planner's responsibility, not the difficulty of research.
Analysis and implementation review may still be expensive. Removing their access
to required evidence would weaken validity and is not part of this decision.
A fast planned work is not an experimental result or publication success.

The scheduling unit does not define the scope of a study-design amendment. In the
2026-09-07 continuation, a completed one-off diagnostic consumed its permission;
the planner prepared another diagnostic before scope review rejected it, then
selected another one-off amendment. Repeating this pattern spends planning,
implementation and protocol review on each development step. Policy 26 allows the
planner to consider a finite adaptive development procedure instead. It must name
the earlier restrictions it replaces, permitted data and changes, aggregate trial
and compute limits, continuation and stopping rules, and the final freeze.
Approval still precedes any changed scope. Every empirical work retains its source
and observation review. Development adaptation does not qualify a method, lower
the original success bar, or permit outcome-selected confirmation retries. This
guidance does not expand an existing protocol or establish scientific progress.

On failed execution/measurement, selection is scoped to recovery of the previous
scientific measurement. The packet retains that question, actual failure, recent
relevant findings and fixed constraints rather than reopening the entire source
and hypothesis library. Detailed evidence remains archived and the packet binds
a digest of the full context. This does not convert invalid data into evidence or
permit an unreviewed rerun.

CLI events for planning are written from process start, not only upon return.
A five-second shutdown margin permits failure recording before the outer bounded
controller terminates the session. The execution subprocess cannot read host
agent skill directories; its role is defined by this harness's prompts and tools.

A planned repair exposes an execution handoff through both research-state views.
It supplies a draft from the actual predecessor execution, with fresh node identity
and source references checked against recorded bytes. It does not choose a code
patch or execute anything. A saved current dispatch takes precedence over the
predecessor draft, so retrying does not discard the agent's prepared repair.

Bounded budget exhaustion before review is an operational checkpoint, not an input
rejection. The current work and saved request remain resumable without an instruction
to change code. This distinction also applies when the work has already been bound
but no job manifest exists.
