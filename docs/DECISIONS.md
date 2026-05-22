# Decisions

This file records the agreed design decisions from the grill session.

## Search Skeleton

- Follow Sakana-v2-style search mechanics by default.
- Use a greenfield-compatible implementation, not a naive fork-port.
- Keep Sakana-like config keys, with `harness_semantics` extensions.

## Worker Model

- Claude Code subscription is the primary intended LLM backend.
- Plain API calls are optional fallback, not the default goal.
- Worker invocations are bounded tools, not autonomous researchers.
- Worker writes are limited to isolated per-node workspaces and artifacts.
- Worker cannot self-authorize new baselines, new scope, or research direction.
- Interactive permission escalation is disabled for automated workers.
- Resume uses a fresh invocation plus structured artifacts, not the same agent
  session.

## Auth

- Subscription OAuth is a runtime preflight concern.
- `ANTHROPIC_API_KEY` must not silently override subscription mode.
- Workers may report auth state but may not repair auth.

## Claims

- The harness is claim-first.
- Capability claims come before mechanism claims.
- Mechanism follow-up is required when results are strong, surprising, complex,
  story-critical, regime-specific, or taste-risky.
- Necessity is checked before deep mechanism work.
- Necessity uses current best-known, naive, and random/null baselines.
- Web baseline resolution belongs to orchestrator support nodes, not workers or
  critics.

## Goal Facets

- Root goals are decomposed into explicit, inferred, and unknown facets.
- Explicit facets are strong constraints.
- Inferred facets are priors, not hard pruning rules.
- Unresolved tradeoffs remain parallel branches until evidence, budget, or user
  choice resolves them.

## Failure And Lessons

- Failure records are selective evidence/audit artifacts.
- Lessons are always-included one-line scoped priors.
- Not every failure gets a fail file.
- Fail files are required when a failure contradicts a claim, affects promotion,
  produces a reusable lesson, repeats a pattern, breaks necessity, or comes from
  an expensive experiment.
- Lesson format should be scoped: `In [regime], [principle], unless/when [condition].`
- Lesson distillation is owned by memory maintenance, checked by critics, and
  accepted by the orchestrator.

## Critic Model

- Critics are read-only and write only structured reviews.
- `critic.md` or critic personas can tune research emphasis but cannot override
  core invariants.
- Critic selection is deterministic folder routing, independent from the
  orchestrator.
- Rebuttal-stage critics are separate from node-level critics.

## Publication

- Publishing consumes `research_state_bundle`; raw logs are drilldown only.
- Paper, slides, and interactive HTML are renderers over the same state bundle.
- TeX is optional and disabled by default.
- Publication gate includes rebuttal and AC decision by default.
- Rebuttal experiments are limited-depth, objection-linked evidence repairs.
- AC outputs `accept`, `revise`, or `reject` with scores and next actions.

