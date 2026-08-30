# Feature throughput checkpoint

## Product

- The production start interaction lets the operator select one ready registered adapter.
- A source-less or invalid adapter remains visible as unavailable and cannot enter the feasibility envelope.
- The arXiv acceptance run uses a real local adapter and deployment scope, so data binding cannot be bypassed by a directional-only path.
- The scientific terminal result may still be `accept_with_unverified_screen` because no independent external falsifier is being added.

## Technical

- One parser/resolver owns project and operator adapter semantics with per-ID, fail-closed operator precedence.
- Thread bootstrap creates immutable content-addressed snapshots without acquiring or staging every source.
- One selected snapshot ID flows through the envelope, forest root, plan, job, runtime manifest, runner result, and worker report.
- Job binding reuses the existing local materializer, verifies bytes, stages a workspace-local copy, and is retry-idempotent.
- Binding finishes before the node becomes `running`.

## Interaction

- Keep the existing dataset page and production controls. Add only canonical declaration fields, readiness feedback, and one adapter selector.
- When exactly one adapter is ready, the system may preselect it. Ambiguous or unavailable selection fails with a specific operator-facing error.
- Resume uses the persisted selection. It never silently follows later settings changes.

## Dependency

- Add no package.
- Use dataclasses, pathlib, hashlib, JSON, atomic file replacement, and existing schema/materializer helpers.
- Preserve the current Codex migration and unrelated dirty worktree changes.

## Verification bar

- Focused unit and integration tests cover parsing, precedence, snapshot identity, selection, idempotent binding, path/hash validation, active connector propagation, runner environment, and evidence propagation.
- The relevant baseline remains `112 passed` before implementation.
- After implementation, run the focused suite, the full suite, and a fresh frontend-driven arXiv thread.
- Completion requires a persisted terminal state or a concrete repeated external blocker. The live artifact audit must correlate the same adapter and snapshot identity across all stages.
