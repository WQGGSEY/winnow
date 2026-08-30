# Strong strategy search grounding

## Goal

The harness must search for a strategy that clears the operator's frozen claim
bar. A negative or unverified paper is not a successful scientific terminal.
When the current budget or capability cannot produce another useful experiment,
the harness must persist a resumable pause with the missing capability and the
best next experiment.

The redesign cannot guarantee that an arbitrary research problem has a winning
strategy. It must guarantee that failed evidence changes the next strategy,
that duplicate work is rejected, and that only a strong result completes the
research program.

## Baseline

Run this command:

```bash
venv/bin/python scripts/evaluate_search_trace.py runs/threads/thread_7f170f15
```

The current arXiv run produced these values:

- 24 negative decisions.
- 0 negative decisions with follow-up strategies.
- 0 adaptive children.
- 5 unique evidence payloads from 24 worker reports.
- 79.1667 percent duplicate evidence.
- 0 represented strategy families.

The persisted scorecard is `.audit/strong-strategy-baseline.json`.

## Active runtime path

The production frontend starts Codex sessions through `thread_supervisor.py`.
Codex drives the file-backed handlers in `mcp_server.py`. The in-process
`AgentManager`, `ParallelAgent`, and class-based `Professor` are not the active
production controller.

The active path has these defects:

1. `handle_get_next_admissible_node` orders queued work by a fixed stage, a
   claim-type weight, depth, and ID. It ignores frontier priority and evidence.
2. `handle_submit_professor_decision` moves a negative node to
   `needs_child_branch` without materializing the proposed branch. The automated
   and approved-live paths do materialize children.
3. The six connector roots in the baseline run each received three drafts that
   prefixed the same claim with a verification-axis label. These drafts did not
   represent different strategies.
4. Failure memory reaches the active path mainly during rebuttal and writing.
   It does not shape initial generation or the next production proposal. The
   active MCP failure writer also emits a format that loses lesson and reason
   text when the generic retriever parses it.
5. Empty queue, stage completion, depth exhaustion, publication readiness, and
   scientific termination are separate predicates. No domain object explains
   why the search can or cannot continue.
6. `unverified_screen` is a terminal in `is_terminal`. The supervisor therefore
   treats an internally valid negative screen as completed work.
7. ADR 0012 freezes the problem-level bar before generation. The connector
   implementation instead lets each reduced root replace the baselines, success
   criteria, and disproof conditions.

## Decisions that remain in force

- ADR 0006 prevents a success-seeking run from authoring its own success proof.
- ADR 0007 treats premature termination as the common cause of fake strength
  and lazy negative conclusions.
- ADR 0008 limits claim strength to the strongest executed external referent.
- ADR 0009 keeps declared values out of slots that require measured facts.
- ADR 0011 permanently rejects behavioral co-deployment distance as a domain
  connection mechanism.
- ADR 0012 keeps generation separate from earned gate survival. Its frozen
  problem-level bar must become true in code.

The redesign must not weaken the falsifier, construct-adversary, critic, AC, or
attestation gates.

## Required data model

The current node conflates four different concepts. The new design must keep
them separate:

- The immutable research goal and its strong success bar.
- A strategy family with a causal mechanism and lineage.
- A diagnostic experiment with predicted outcomes and a cost.
- An observation that updates explicit beliefs and strategy status.

The frontier must contain experiments chosen for expected progress. It must not
use verification-axis labels as strategy identities.

## Done predicate

The first vertical slice is verified only when all conditions hold:

1. A negative observation can create at least two structurally distinct strategy
   candidates that keep the same frozen bar.
2. The selector chooses a candidate from evidence-derived priority components.
3. A repeated strategy or evidence-equivalent experiment is rejected before it
   consumes a worker run.
4. Empty useful frontier becomes `paused_needs_expansion`, not a negative paper.
5. Only a candidate that clears the frozen bar can reach a scientific terminal.
6. The scorecard and focused tests reproduce the old behavior and show the new
   behavior.
7. A live or recorded-product replay exercises the active MCP path, not a legacy
   controller.

## Design task

Design the smallest coherent architecture that satisfies the done predicate.
Show the caller's usage first. Define the core types, pure policy functions,
MCP boundary, persistence ownership, crash and retry behavior, and the migration
from the current `search_state.json`. Prefer one active controller and remove or
isolate legacy policy copies. State which parts belong in the first vertical
slice and which parts wait for later work.
