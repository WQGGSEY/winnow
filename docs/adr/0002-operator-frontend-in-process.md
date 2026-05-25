# ADR 0002 — operator_frontend runs harness in-process

- **Status**: Accepted
- **Date**: 2026-05-25
- **Supersedes**: none
- **Relates to**: `CONTEXT.md` (operator_frontend, research_thread, thread_id, thread.json, single_active_run)

## Context

We are adding a local single-user web UI ([[operator_frontend]]) in front
of the existing CLI (`research_runner`, `production_runner`,
`live_dispatch`). The dominant unknown is how the frontend server
actually drives the harness. Three shapes were on the table:

- **A. Subprocess** — the frontend spawns `python -m
  research_harness.research_runner …` and talks to it over stdin/stdout.
- **B. In-process** — the frontend imports `research_harness` and calls
  agent entry points directly, passing an async `input_provider`
  callable.
- **C. Hybrid** — multi-turn agents in-process, heavy phases (market,
  production) subprocessed.

The decision matters because multi-turn agents (grilling,
research_refiner) are the central UX surface and their existing contract
is a Python `input_provider` callable (see `grilling.py:128` and
`research_refiner.py:157`). The choice also propagates to streaming
transport, the [[single_active_run]] lock implementation, error
propagation, and stop/cancel semantics.

## Decision

The frontend server **imports `research_harness` and drives it
in-process** (option B). Concretely:

- Each phase entry point is invoked as an async-aware function call from
  the FastAPI server; the `input_provider` becomes an asyncio-backed
  callable that pushes ASKs over SSE and resolves when the UI submits a
  reply.
- The [[single_active_run]] lock is an in-process asyncio mutex held for
  the duration of a phase call.
- Cancellation is cooperative: grilling and research_refiner gain a
  cancel-check between rounds. Live `claude` CLI subprocesses spawned
  inside the agent loops are killed by tracking their PID and signalling
  on cancel.
- The frontend server and the harness share one Python environment and
  one process; deployment is a single `uvicorn` command.

## Consequences

**Accepted trade-offs**

- The frontend is tightly coupled to harness internal APIs. This is
  acceptable because both are owned by the same operator and the agent
  entry-point shapes (`run_grilling`, `run_research_refiner`, …) are
  already stable enough to serve as that contract.
- A bug in the agent loop can crash the frontend server. Mitigated by
  the fact that the actually-risky code paths are already isolated as
  child processes by the harness: the live `claude` CLI runs as a child,
  and `production_runner` template execution runs as a child. The
  in-process Python is orchestration code authored in this repo.
- Stop/cancel requires explicit cancellation points in
  `agents/grilling.py` and `agents/research_refiner.py` (one check
  between rounds) plus child-PID tracking for live Claude invocations.
  This is a small additive change, not a rewrite.

**Rejected: A (subprocess)**

- Demotes `input_provider` from a Python callable to a stdin pipe,
  reverting the multi-turn agent design choice that already exists in
  the codebase.
- Forces stdout-scraping to stream rounds to the UI; the CLI currently
  prints human-readable text, not structured events.
- Provides isolation that is not needed — the workloads worth isolating
  are already isolated as child processes.

**Rejected: C (hybrid)**

- Maintains two code paths for no real isolation gain: market_research
  is pure Python plus PDF download, and production's heavy template
  execution is already child-process isolated by `production_runner`.
- Cost (two transport implementations, two error models) without
  proportional benefit.

## Operator usage note

Crash recovery / re-attach across frontend server restarts **is**
supported via per-round persistence of multi-turn agent sessions; see
`CONTEXT.md` → [[multi_turn_session_persistence]]. The in-process
choice in this ADR is what makes that recovery cheap: the persisted
`rounds[]` plugs straight back into the agent loop's existing
`rounds=rounds` parameter on resume.
