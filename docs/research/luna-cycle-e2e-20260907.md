# Luna max research-cycle E2E, 2026-09-07

The run resumed Pacman thread `thread_07b175cc` at completed diagnostic work
`82f68a4c0213439ad51f43d85334f251762a826c08493b7ed0e5178654ae65ee`.
Its aim was to observe result interpretation, next-work choice and execution without
root-authored scientific advice. It did not reach a new accepted research work.

## Limits and artifacts

`runs/e2e/lab3-game-only/luna-cycle-e2e-20260907-052505` contains the
controller, before/after work, budget ledger, streamed events and cleanup receipt.
Limits: Luna max, 20 minutes, 10 model launches, four supervisor cycles,
1,000,000 aggregate initial prompt bytes and 280,000 per call. These are not billed
token limits. A transport failure checkpoints the run instead of buying an automatic
retry. Four launches were reserved across the original attempt and corrected resume.

## Observed failures

The first planner call was rejected by the provider in about four seconds:
`invalid_json_schema`, because `uniqueItems` was not permitted on the new
`alternatives.items.properties.required_observations` field. It never entered
research reasoning. This was introduced by our previous schema change and had not
been caught by the offline thought experiment.

The unsupported keyword was removed from the two new response fields. Uniqueness
is now explicitly checked by the host for work-level count names, per-prediction
dependencies and observation citations. The old failed ledger was retained as
`schema-rejection-budget.json`; the recovery and its reason were recorded while
retaining the original deadline, call history and prompt limits.

The corrected planner received 84,847 initial prompt bytes, including instructions.
Its local tools were disabled. The provider started the turn without the earlier
schema error, but the call hit its existing 240-second timeout without a final
response. There were no planner tool calls, terminal usage record, or persisted
new decision. The incomplete trace does not show which reasoning task consumed
the time. It cannot establish that a longer call would also fail.

The coordinator correctly resumed planning rather than rerunning the completed
experiment. Its interim description conflated planning with independent review;
the recorded call label was `research work decision`. No scientific inference
should be drawn from coordinator progress prose.

The overall run stopped about 366.5 seconds after its original start, including
the intervening schema fix. The 20-minute global deadline and 10-call count were
not exhausted; the single planner timeout triggered the bounded failure checkpoint.
All processes carrying this run's exact budget environment were absent at cleanup.

## Research outcome

No new ResearchWork, experiment, intervention, comparison or paper was produced.
The previous completed diagnostic and its insufficient-support boundary remain
unchanged. The offline projection and partial-interpretation guards did not yet
translate into observed Luna research progress. Planning latency remains unresolved.
Actual total token usage is unknown because terminal usage was not emitted for
interrupted calls.

## Verification of the schema fix

37 focused research-control/review checks passed, including a duplicated
observation-citation rejection. `git diff --check` passed. The corrected live
provider call no longer returned the schema rejection, but did not finish research
planning. These facts are distinct from E2E research success.
