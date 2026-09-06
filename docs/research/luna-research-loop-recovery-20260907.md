# Pacman research-loop recovery, 2026-09-07

Scope: priorities 1–4, ending in a harness-authored intervention and an actual
controlled comparison. Strong-result confirmation and manuscript production are
subsequent work, not demonstrated by a diagnostic or passing software checks.

## Planner isolation

`runs/e2e/lab3-game-only/planner-time-isolation` preserves the original prompt,
packet and output schema, the live Luna max response, usage and host rejection.
Only the per-call timeout changed from 240 to 600 seconds. The call completed in
140.90 seconds with 43,498 input tokens and 7,494 output tokens, including 6,285
reasoning tokens. The prior run timed out at 240 seconds without terminal usage.
This single comparison does not establish that a higher timeout caused completion:
the completed response arrived inside the former limit. Latency variance and task
burden remain plausible contributors; timeout alone is not the demonstrated root
cause of all failures.

The completed response selected existing-source analysis of missing observation
bindings. Its scientific text was produced by Luna. The host rejected the entire
decision because `solution_path.parent_work_id` was null even though the preceding
work was already known. This was an unnecessary generation responsibility.

The response schema now leaves lineage assignment to the host. Canonical stored
work still requires and validates its exact parent. Raw model output is retained.
The saved real response was passed through the normal planner validation again,
with zero new model calls. The recovery asserted that research inputs were equal
apart from rejection feedback and that every scientific decision field was
unchanged. Only host-owned lineage changed. `recovered_result.json` records the
source response hash and accepted work
`126b48797f1c568c3b1ab9426e2a7451a8d42d9e636abb5b01550296697659d0`.

## Analysis responsibility

The analysis role now receives selected evidence and recorded JSON measurement
facts first. Other history remains in hash-addressed files for targeted inspection.
The full packet is retained for audit and validation. This does not qualify the
old diagnostic or reinterpret zero eligible groups as a scientific negative.

For the resumed work the full analysis packet occupied 396,749 bytes; the live
initial prompt, including instructions, occupied 29,493 bytes. The supervisor run
is `runs/e2e/lab3-game-only/luna-cycle-e2e-20260907-055131`. Its limits are 20 minutes,
10 calls, 1 MB aggregate initial prompts and 280 KB per call. Prompt-byte limits
are not billed-token limits.

## Outcome boundary

The recovered work has entered live source analysis. An intervention and controlled
comparison have not yet been established at this checkpoint. The root agent has
not supplied an RL learner or scientific intervention. 37 focused control/review
checks passed; these protect implementation behavior and do not demonstrate
research success.

## Interrupted inspection recovery

The focused analysis reached the metric producer and read the selected artifacts,
but timed out at 300 seconds before returning a final answer. Its event stream
contained 12 successful read-only command outputs, including the full metric
producer and computed summaries of the existing artifact. No accepted analysis
was persisted. Input compaction alone was therefore insufficient.

Analysis recovery now separates these completed observations from unfinished
interpretation. It retains successful tool outputs verbatim, their output hashes,
commands and original event-stream hash. Failed commands and incomplete event
lines do not become evidence. Duplicate outputs are removed and an 80 KB bound
on the serialized observations reports any omissions. All 12 outputs fit for this
run, with zero omissions. A resumed synthesis call gets those observations and
the selected question with local tools disabled. It must answer from that record
or explicitly identify missing evidence; it cannot buy another round of the same
inspection. The original events remain immutable and the synthesis cache key
includes the recovered observations.

The `inspection-recovery` continuation retains the original run's deadline and
call ledger. All processes belonging to the failed budget were absent before
resumption. The live synthesis initial prompt is 105,277 bytes. A focused
regression also exercises an interrupted trailing JSON event, exact observation
retention, disabled synthesis tools and reuse of the accepted analysis receipt.
38 control/review checks passed. The scientific result is still pending.
