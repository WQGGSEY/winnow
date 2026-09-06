# Development review continuation, 2026-09-07

The target remains an autonomous strong-result paper. These changes address the
pre-execution review bottleneck; approval and successful measurement alone do not
establish scientific improvement or publication readiness.

## Cause and change

The previous reviewer received roughly 371 KB and could reread its own live
trace and serialized request. A one-line output repair triggered broad inspection
of the method, runner and historical work. A first correction separated historical
inventories into addressable evidence files, removed duplicate source from the
initial input, and denied reads of the invocation's own trace/request. That reduced
initial input to 160,619 bytes, but a real Luna max run still timed out after
300 seconds. Input compaction and change metadata did not override the reviewer's
whole-method audit instructions.

The final implementation uses a bounded revision review when the verified prior
approval request has the same protocol history, registered protocol, input manifest
and execution-plan fields except node identity/workspace and proposed source.
It provides the complete proposed source, exact source diffs and prior assessment,
with no local tools. The independent reviewer must decide the revision and its
dependencies, including any newly found reachable defect. Prior approval is never
transferred automatically. Changed experimental conditions retain the general
review route. Bulky historical inventories remain addressable in that route.

The host compares source content from the prior request, verifies the prior request
digest, and verifies current materialized source against the proposed bytes before
and after review. Scientific adequacy of the revision remains the independent
reviewer's responsibility. An unchanged dependency may reuse an earlier finding;
this is not a semantic proof of correctness.

Work reservation now records `execution_phase=implementation_review`. Only the
actual launch after approval records `experiment_running`; completion records
`finished`. The graph and coordinator instructions use these distinctions.

## Observed execution

- `runs/e2e/lab3-game-only/luna-review-fix-20260907-000111`: first correction,
  351.1 seconds, two calls; review timed out. No experiment. No owned processes
  remained at cleanup.
- `runs/e2e/lab3-game-only/luna-revision-review-20260907-000903`: final bounded
  revision review received 240,452 bytes including full source, made no tool calls,
  and approved. The real diagnostic then ran in 41.01439 seconds. The first cycle
  stopped after 248.0 seconds at a durable research-work checkpoint.
- The same run budget was resumed in `interpretation/` to observe the next planner
  decision, without a new budget or root-authored research advice.

The completed diagnostic reports 216 eligible feed-forward groups and zero eligible
recurrent groups, both with zero alias signal. Zero recurrent denominator is
explicitly recorded as an unexpected observation, not absence of a scientific effect.
The selected work's three named count metrics are missing at the top level, so
`measurement_support.evaluable` is false even though the payload validated. Game
and record counts exist inside family details. The reviewer approved without
resolving this observable-output mismatch; the host correctly did not promote the
work to sufficient scientific evidence. Resolving this handoff is remaining work.

The successful reviewer reported 81,715 input tokens and 7,985 output tokens,
including 7,768 reasoning tokens. These are only that review's usage, not the total
run or failed-run usage. Interrupted coordinator/reviewer calls may lack terminal
usage. Prompt bytes do not measure billed tokens or bound repeated tool output.

## Local checks

127 existing/focused checks and 3 subtests passed across research review/control,
CLI adapter, supervisor and frontend supervisor. Regressions cover preserving full
evidence, invalidating prior evidence on digest/condition changes, supplying full
source without tools for bounded revisions, and review-phase state. A real
`codex sandbox` subprocess could not read its explicitly denied events file.
JavaScript syntax checking, focused pyflakes and `git diff --check` passed.
These checks support implementation correctness only; they do not establish the
research success criterion.

## Interpretation continuation result

The follow-up planner received 117,100 initial prompt bytes and timed out after
its 240-second call limit without a new persisted work. The combined successful
review/experiment run and interpretation continuation used four model launches
within the same budget, stopping at 567.4 seconds from the original start.
No owned processes remained. The completed experiment and its checkpoint are
retained; no additional experiment, intervention, comparative improvement or paper
was produced. Interpretation latency and the selected-count output mismatch are
remaining bottlenecks. The revision-review fix does not establish an end-to-end
research solution. No root-authored learner or scientific intervention was added.
