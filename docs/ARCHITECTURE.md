# Architecture

## Design Boundary

The harness is not a direct fork-port of Sakana AI Scientist-v2. It is a
greenfield-compatible skeleton that keeps Sakana-v2-style search mechanics and
vocabulary while replacing node semantics and runtime integration with stricter
contracts.

Sakana-v2-style mechanics:

- Ideation and experimentation are separate phases.
- Progressive tree search is guided by an experiment manager/orchestrator.
- Multiple workers can explore nodes in parallel.
- Debug, retry, abandon, logs, artifacts, and tree visualization remain core
  concepts.
- Writeup/review exists as a later stage.

Harness additions:

- Claim-centered nodes.
- Baseline dossiers.
- Runtime envelopes for Claude Code subscription workers.
- Deterministic runner split for long-running jobs.
- Critic governance independent of the orchestrator.
- Failure memory and always-included lesson memory.
- Rebuttal session, AC decision, and configurable publication renderers.

## Control Ownership

```text
orchestrator:
  claim map, branch generation, search frontier, final reduction

worker:
  bounded execution and raw report only

runner:
  long-running deterministic jobs

critic governance:
  deterministic critic routing and independent reviews

memory maintenance:
  lesson distillation and memory hygiene

publishing:
  research_state_bundle to paper/slides/interactive artifacts
```

## Worker Invocation

Claude Code is an agent runtime, not a completion API. Each invocation must be
wrapped by a runtime envelope:

```yaml
role: experiment_worker
workspace: /runs/{run_id}/nodes/{node_id}/workspace
allowed_write:
  - node workspace
  - node artifacts
denied_write:
  - canonical repo
  - global memory
permission_mode: non_interactive_or_fail
scope_policy: no_self_expansion
output_schema: strict_json
```

Workers may report unexpected observations, but they may not pursue them.
Orchestrator reduction decides whether an observation becomes a child branch,
lesson, or discarded note.

Failure memory is a non-binding branch prior. Retrieved one-line lessons and
failure summaries add risk controls to future child branches, but they do not
block promotion by themselves.

Search depth, debug depth, sunk-cost behavior, and scale-up requirements are
read from `configs/harness.yaml`. Scale-up is disallowed by default unless the
configured policy and root goal allow it.

The staged tree-search loop only accepts dry-run/local backends. Live Claude
Code remains outside the loop behind `live_gate`, explicit billing
acknowledgement, auth-status verification, and stdout ingestion.

## Long Jobs

Long training jobs are split:

```text
prepare_experiment_node -> run_job_node -> analyze_result_node
```

Claude Code prepares manifests and analyzes artifacts. The deterministic runner
owns execution, timeouts, log collection, and retries.

## Critic Governance

The orchestrator does not choose critics. The governance layer routes by folder:

```text
critics/always/*.md
critics/by_node_type/{node.type}/*.md
critics/by_domain/{node.domain}/*.md
critics/by_stage/{node.stage}/*.md
```

Each critic runs independently and writes a structured review. Orchestrator
reduction consumes the review bundle but cannot erase the routing trace.

## Publication Gate

Promotion to publication goes through:

```text
promotion candidate
  -> rebuttal_packet.md
  -> rebuttal critics
  -> orchestrator_rebuttal.md
  -> final critic scoring
  -> AC decision
  -> camera-ready artifact generation or continued search
```

`revise` returns to publishing. `reject` returns to search with blocker-linked
nodes. `accept` triggers the renderers selected in `settings.json`.
