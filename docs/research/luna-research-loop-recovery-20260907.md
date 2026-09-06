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

The recovered synthesis completed and was persisted as `analysis_completed`. It
reported recoverable counts, a fixed-grid feed-forward no-signal result, and an
unevaluable recurrent result because all groups represented single primitive
states. Its usage was 50,752 input and 7,439 output tokens, with 6,732 reasoning
tokens included in output. The next planner used this finding without rerunning
the aliasing experiment.

## Planner completion and citation contract

The next 240-second planner timed out. In run
`luna-cycle-e2e-20260907-060835`, the same saved research input and output contract
were submitted with a 600-second limit. Input equality was checked. It completed
in approximately 333 seconds, using 44,751 input and 18,218 output tokens, of
which 16,563 were reasoning tokens. This completed call required more than the
old limit. The earlier 140.90-second completion also shows substantial variability.
The production planner limit is now 600 seconds, still bounded by the run's
remaining global deadline; this avoids imposing the known inadequate 240-second
cap on every expensive reasoning attempt.

The model selected a matched task-feasibility/reward diagnostic rather than
repairing the recurrent denominator again. However, it put raw artifact pointers
into `prediction_updates.observation_ids` while interpreting source analysis.
That field accepts declared observation names; analysis declares none. The host
correctly rejected it but the generated schema had failed to constrain the field
when no measurement-support record existed. The schema now binds observation
citations for every previous prediction, including empty citation arrays for
source analysis. Existing source/analysis receipt citations remain available.

The focused reproduction also found that the local JSON-schema subset ignored
`maxItems`. It now enforces array upper bounds, so local validation respects the
same empty-array contract. 38 research-control/review checks and 23 existing
schema checks passed. These checks do not substitute for a completed experiment.

Supervisor guidance also still said repeated identical observations should always
lead to causal discrimination. It now follows the planner's exploration,
discrimination or intervention choice and allows replacing diagnostics that do
not change the next intervention. The original goal, matched comparisons and
unverified status of intuition remain explicit.

## Execution handoff and historical evidence loss

The corrected planner registered work
`95de6a19a52f95f4d1b06ba80f698c46a7dd596f7031491f3620aa16a08c373a`.
Its first dispatch had no experiment_plan because a new unimplemented work pointed
straight to execution. New empirical work now points to source preparation;
preparation preserves the same scientific work and then points to preflight.
The live agent subsequently authored its control program and explicit count
bindings. The root did not author the experiment.

The first implementation review timed out at 300 seconds after ten successful
read-only outputs. Recovery now supports execution reviews as well as analysis.
The coordinator independently found a real source discrepancy during resumption:
`testCapture` remained in the copied source where the selected work specified
`bloxCapture`. It changed that line. Consequently the input digest changed and
old inspection was correctly not reused for the changed source. The new source
entered a fresh independent review. This is a code correction, not evidence of
an RL intervention or a successful control result.

A more consequential memory defect was also found. `development_evidence` admitted
only the newest eight reports, and the selector then saw only the latest two.
The earlier completed collect-and-return feasibility control was absent both
from its visible results and its available evidence IDs. The selector could not
know that this related question had already been measured. The earlier control
and the new matched reward-alignment diagnostic are not asserted to be equivalent;
the omission prevented an informed comparison of their scope in the first place.

A historical result index now retains all completed development measurements
with declared objectives, observed metrics, receipt paths and artifact digests.
For this thread it contains 69 measurements in approximately 44 KB, rather than
full source and tool histories. These entries are discoverable and citable by the
selector and selected source-analysis role. The index explicitly requires checking
protocol and implementation differences before reuse. It does not qualify any
result or expose final-falsifier files. Recent execution reconciliation still uses
its existing bounded window.

The historical-window regression retains the oldest of ten measurements and
leaves a final-falsifier sentinel unread. The combined control/review checks pass
40 cases. A transport checkpoint handoff now gives unchanged saved-request
arguments, distinguishing retry from an implementation objection and discouraging
coordinator rereads of reviewer logs.

## Correcting inspection retention

The changed-source review exhausted the remaining global deadline after roughly
230 seconds. A fresh bounded run, `luna-cycle-e2e-20260907-063918`, attempted
synthesis from that exact request. Inspection of the recovery revealed a defect
in the new retention logic: retaining the most recent 80 KB favored later harness
implementation reads and omitted the experiment source read. The root interrupted
that synthesis before any assessment was accepted and recorded a transport
checkpoint. This interrupted call's billed usage is unknown.

Recovery now prioritizes commands reading the bound experiment source, followed
by its workspace inputs, ahead of unrelated framework reads. The full hash-checked
proposed source is also supplied inline to a tool-free execution reviewer. Output
omissions remain explicit; missing evidence does not grant approval. A targeted
case confirms that a 4 KB source read survives a later 77 KB framework read.
The corrected continuation `source-retention-recovery` retains the same run
ledger/deadline. Its initial review prompt is 262,965 bytes, below the 280 KB
per-call ceiling. 41 control/review checks passed. Actual review and experiment
outcomes are still pending at this checkpoint.
