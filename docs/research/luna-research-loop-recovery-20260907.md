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
