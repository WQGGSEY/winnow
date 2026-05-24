# Grilling Log

This file is a structured reconstruction of the design grilling session that
created this research harness. It is not a verbatim transcript. It preserves the
decisions, rationale, user taste, open questions, and prompt-relevant operating
principles that should be reused in future orchestrator, worker, critic, and
publication design.

## How To Use This Log

- Treat this file as long-form design memory.
- Use `research_profile.md` as the compact active profile.
- Use `docs/DECISIONS.md` as the canonical short decision record.
- When future design choices conflict with this log, check whether the conflict
  is about a core invariant or a tunable parameter.
- Core framework invariants should remain stable. Settings may tune operational
  details, but should not override the philosophy below.
- Future grilling sessions should append new dated entries instead of silently
  rewriting prior decisions.

## Core Objective

The goal is to build a research harness inspired by Sakana AI Scientist-v2 while
using Claude Code subscription OAuth instead of plain metered API calls whenever
possible. The user wants to avoid pay-as-you-go API costs and also wants the
harness to encode their own research taste, philosophy, and framework.

This is not a naive port from `llm.generate(prompt)` to Claude Code. Claude Code
is an agent runtime, not a completion API. It can read files, run commands,
consume multiple turns, ask for permissions, and plan internally. Therefore every
live invocation must be wrapped in explicit scope, role, schema, turn, timeout,
workspace, and failure handling constraints.

## Primary Risk: Nested Agency

Sakana-v2 already performs agentic tree search. Claude Code also contains an
agent loop. Naively nesting them creates this failure shape:

```text
outer agent tree search
  -> inner Claude Code autonomous agent
       -> internal planning / editing / tool calls / permission requests
```

The harness must prevent cost and variance explosion by making Claude workers
bounded tools. The worker reports only experimental results or bounded artifacts.
Search expansion, mechanism branches, baseline selection, critic routing,
failure memory, and publication decisions belong to the outer orchestrator.

## Runtime And Auth Decisions

- Claude Code subscription OAuth is the intended default LLM backend.
- Plain API calls are optional fallback, not the goal.
- `ANTHROPIC_API_KEY` must be unset for subscription mode because it takes
  precedence and may trigger API billing.
- Pro, Max, Team, and Enterprise subscription OAuth are acceptable auth modes.
- `claude setup-token` can be used for script/CI-style long-lived OAuth tokens.
- Live Claude Code calls require an explicit billing acknowledgement.
- Live execution requires a second execution acknowledgement.
- Claude Code output may include `total_cost_usd` metadata even when using
  subscription OAuth. Treat it as usage-risk evidence until billing is verified.
- Actual billing status must be checked in Claude Console usage after live runs.

## Worker Model

- Workers are tools of the orchestrator, not autonomous researchers.
- Workers must not expand scope, create branches, change the claim, change
  baselines, write shared memory, or decide publication readiness.
- Workers write only inside isolated per-node workspaces.
- Shared failure files and one-line lessons are written only by
  orchestrator-owned memory code after validation.
- Workers may report unexpected observations, but they may not pursue them.
- Worker output must satisfy a strict JSON schema.
- The harness, not the worker, derives trusted `worker_report.json`.
- Permission denial, timeout, budget exhaustion, invalid output, and scope
  violation are non-promotable states.
- Live workers should run with tools disabled unless a future design explicitly
  introduces a more controlled tool regime.

## Timeout And Long Jobs

Timeouts must be task-class dependent. A short smoke timeout is useful, but real
experiments may involve training runs that take about an hour. Scale-requiring
experiments should not be made impossible by overly short fixed timeouts.

Long-running jobs should be split:

```text
prepare_experiment_node -> deterministic_runner_job -> analyze_result_node
```

Claude Code prepares and analyzes; deterministic runners own training,
timeouts, retries, logs, and raw execution.

## Orchestrator Ownership

- The orchestrator owns the search tree.
- The orchestrator chooses whether a worker observation becomes a child branch,
  lesson, failure record, discarded note, or publication argument.
- The orchestrator must check whether a taste rejection applies to only a
  spawned research direction or to a subset of the original user goal.
- If a taste rejection is a subset of the original goal, tree branching may be
  needed rather than global rejection.
- Orchestrator-controlled state mutation must be explicit and approval-gated
  when applying live results.

Current implementation note: the orchestrator is deterministic Python logic, not
yet an LLM prompt-driven agent. A future LLM orchestrator should propose
candidates only; deterministic harness code should validate and apply.

## Research Philosophy

### Claim First

Research success is not just an improved metric. A result is successful only
when the claim under test survives required baselines, disproof conditions, and
goal-relative tradeoffs.

### Capability Before Mechanism

First establish whether the system can do the thing. Mechanism explanation is
then opened as a follow-up branch when useful. Capability and mechanism are not
separate disconnected objects; they are priority-ordered. Mechanism work can
include ablations, boundary checks, and analysis explaining how or why a
capability result holds.

### Necessity Matters

Ask: why should this method be used at all?

A method that works but is matched by simpler or stronger baselines has not
justified itself. Motivation, differentiation from prior work, and why this is
better are part of the research claim, not a decorative writeup step.

Example: if a contrastive-learning experiment gets good metrics, but a simple
supervised baseline solves the same problem similarly or better, this must be
recorded as a failure or confounded result for the contrastive-learning claim.
The contrastive method has not justified necessity.

### Baselines

Default baseline roles:

1. Current best-known method, resolved by web fetch / source review.
2. Naive approach.
3. Random or null hypothesis baseline.

Baseline selection must leave tags and reasons. Missing required baselines make
a claim not evaluable or confounded.

### Goal-Relative Success

Whether a tradeoff counts as success depends on the user goal.

- If the user goal includes efficiency, lower cost, lower compute, or practical
  usability, then a slightly weaker result may still matter.
- If the user explicitly asks for better performance or better generalizability,
  then a result that is merely cheaper or simpler should fail that claim.
- Do not over-restrict inferred goals. Inferred facets are priors, not hard
  pruning rules.
- When the user does not rank performance, efficiency, simplicity,
  interpretability, and robustness, keep parallel branches until evidence,
  budget, or user choice resolves them.

### Negative Results Should Be Learnable

Rejected hypotheses should produce lessons when those lessons prevent repeated
mistakes or improve future branch generation. A failed hypothesis can still be
valuable if it sharpens the search.

Lessons are always included as compact one-line priors. Each lesson must remain
one line.

## Failure Memory And Lessons

Failure records and lessons are both useful, but they serve different purposes.

- Failure files are detailed audit/evidence artifacts.
- Lessons are compact priors that are always included.
- Not every failure needs a fail file.
- Fail files are required when a failure contradicts a claim, affects
  promotion, produces a reusable lesson, repeats a known pattern, breaks
  necessity, or comes from an expensive experiment.
- Failure records should be category- and tag-indexed so future branch design can
  read only relevant files rather than wasting tokens on all failures.
- Lessons should be one line and included broadly.
- Lessons should guide search but not become hard gates.
- If an experiment has high success probability, it may still be worth running
  even if lessons warn about similar past failures.

Open tagging issue: exact failure tag taxonomy still needs additional design.
The contrastive-learning-vs-supervised-baseline example is a key case that must
be representable.

## Sunk Cost And Progress

Sunk cost decisions are difficult and should depend on progress. The harness
should not blindly continue because effort was already spent, but it also should
not abandon a branch before evaluating whether partial progress increases the
probability of success or lesson value.

## Critic Governance

- Critics are read-only.
- Critics write structured reviews.
- The orchestrator must not choose critics ad hoc.
- Critic routing should be deterministic from folders such as `always`,
  `by_node_type`, `by_domain`, and `by_stage`.
- All markdown files in the relevant critic directories become independent
  critic personas.
- `critic.md` or critic persona files can tune what the critic cares about.
- If the base framework and critic markdown conflict on taste emphasis, the
  markdown may override taste-level priorities.
- Critic markdown must not override core invariants.
- The critic process should be separated from the orchestrator so the
  orchestrator cannot game critic selection.

The user used a minimax analogy for critic/orchestrator tension, but later
clarified that the final design should follow the Sakana-v2 structure rather
than overfitting to literal minimax terminology.

## Rebuttal And Publication Gate

Before publication:

1. Orchestrator summarizes the full process into a reviewable packet.
2. Rebuttal-stage critics review the whole case.
3. The orchestrator may run limited-depth, objection-linked experiments or give
   reasoned rebuttals.
4. Critics score the result.
5. An AC agent decides accept, revise, or reject.
6. If accepted, camera-ready artifacts are generated according to settings.
7. If rejected, search continues with blocker-linked branches.

Publication outputs should be configurable:

- paper-style artifact,
- HTML slides,
- interactive HTML explanation,
- optional TeX only when needed.

Slides can be HTML. TeX is not preferred by default because it can waste tokens.

## Deep Interview Preference

The user does not want only option-selection interviews. To elicit research
framework and taste, the assistant should use bottleneck scenarios and ask how
the user would act. A good pattern is:

1. Basic option-selection interview to locate the broad preference.
2. Several rounds of open questions with concrete bottleneck examples.
3. Convert answers into invariant, default policy, tunable setting, or open
   question.

Open questions should be marked explicitly. Some apparent open questions may be
better represented as lower-priority branches or ablations rather than separate
top-level objects.

## Current Implementation Commitments

The repository currently implements many of these decisions as deterministic
scaffold code:

- Bounded worker task contracts.
- Live Claude Code gate and stdout ingest.
- Operator-controlled live dispatch.
- Live reduction ingest bundle.
- Approved live reduction apply.
- Approved live memory update.
- Live rebuttal session.
- Deterministic critic routing.
- Failure memory and one-line lessons.
- Rebuttal and AC publication gate scaffolding.

The missing major prompt artifact is an explicit orchestrator prompt contract.
Future work should define the orchestrator as a proposal-generating agent whose
outputs are validated and applied by deterministic harness code.

## Open Questions

- Exact failure tag taxonomy.
- Exact rules for when a claim should open mechanism branches.
- Exact procedure for orchestrator-led claim opening.
- How to represent user goals that mix performance, generalizability,
  efficiency, simplicity, and interpretability.
- How to calibrate task-class timeouts for real training experiments.
- How much live Claude Code autonomy, if any, should be allowed beyond
  tool-disabled JSON worker mode.
- How to build a real research-goal-to-root-node generator.
- How to connect live web baseline resolution without letting workers browse or
  mutate shared state.
- How to make the final orchestrator prompt preserve this framework without
  turning the orchestrator into an unbounded inner agent.

