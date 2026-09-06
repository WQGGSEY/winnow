# Luna bounded constructive-research continuation

2026-09-06. Thread `thread_07b175cc`, existing Pacman development state.
This was a resumed-state check, not fresh intake and not publication validation.

## Outcome

The bounded continuation did not reach a new accepted ResearchWork or execute a
new experiment. The current work remained the completed policy-16 work
`fd87d2ed6bff75cfd6a4ae6789f25c40f288f9fd2aac997985247530b3069817`.
No qualified learner, strong result, confirmation or paper was produced.

The model generated a solution_path linking aliasing diagnosis to possible
representation interventions. That response was rejected: it marked the old
operational-invalidity alternative supported despite an execution_failure.
The current contract requires all prediction effects unresolved on execution
failure, including mixed legacy alternatives. The correction prompt now explains
that result_kind carries the operational finding. This does not permit a failed
measurement to support a scientific mechanism.

The response also repeated the earlier PosixPath failure, whereas the latest
outcome records `unexpected_observations payload is invalid: metrics.json`.
Thus a populated solution_path did not demonstrate a sound explanation update.
The second planning call was interrupted at the shared wall-clock deadline.

## Limits and observed cost

- Luna max was verified in supervisor and nested planner command lines.
- Maximum three supervisor cycles, eight adapter invocations and 2,000,000
  initial prompt bytes; ten minutes shared across the paid attempts.
- A setup-only start failed because the existing data-source anchor was omitted;
  no model call occurred in that attempt.
- The first paid attempt spawned a native CLI subagent via a host research skill.
  It was stopped. Bounded calls now disable native multi-agent features and host
  skill discovery. The subagent was outside the original adapter ledger.
- The next attempt exposed missing budget propagation through MCP. It was stopped;
  the budget environment is now explicitly included in MCP configuration.
- The retry reused the original deadline and ledger. An interrupted unmetered
  planner was conservatively accounted from its submitted request plus 30,000
  instruction bytes, not claimed as exact usage.
- The final ledger recorded six reservations and 1,533,292 prompt bytes. These are
  not total API requests or billed tokens; CLI internal turns and the early native
  subagent are not represented completely.
- One completed planner invocation reported 146,063 input tokens, zero cached
  input tokens, and 7,486 output tokens, including 5,696 reasoning output tokens.
  Interrupted-call usage was unavailable. Do not present this as total run cost.
- The controller stopped at 601.5 seconds including cleanup. No process carrying
  this bounded-run budget remained when inspected. The viewing frontend remains.

Artifacts are under `runs/e2e/lab3-game-only/luna-insight-20260906-222659`;
the shared budget is in `luna-insight-20260906-222325/call_budget.json`.
The controller source, offsets, before-work snapshot and stop receipt were saved.

## Changes after stopping, not live-validated

Planning policy 20 retains large secondary context as content-hashed files for
targeted reads. It also surfaces authoritative_previous_result and removes the
previous work's duplicate source_observations. Protocol history remains available;
unchanged earlier requirements are not retired by moving them out of the prompt.

Applying this projection to the same saved submitted request reduced JSON UTF-8
bytes from 451,549 to 120,233, or 73.4 percent. This is a static comparison, not a
measured token saving. Necessary retrieval may consume some or all of the saving.
The corrected plan behavior and retrieval quality still need live verification.

Related offline checks: 160 passed and three subtests across eight existing test
files; diff whitespace and changed-module pyflakes checks. These establish only
local contracts, not research success. No extra paid run was started after the
budget expired.
