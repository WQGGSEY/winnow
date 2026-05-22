# Pre-Live Claude Code Checklist

Run this checklist before replacing the mock backend with a live Claude Code
backend.

## Required Local Gates

- `python -B -m unittest discover -v`
- `python -B -m research_harness.local_preflight`
- `python -B -m research_harness.workers.live_gate`
- `python -B -m research_harness`

## Evidence Required

- `runs/prelive_preflight/research_state_bundle.json` contains a schema-valid
  `invocation_envelope`.
- `runs/prelive_preflight/job_manifest.json` is schema-valid and accepted by
  the deterministic runner validator.
- `runs/prelive_preflight/baseline_resolution.md` identifies current-best,
  naive, and random/null baselines without executing webfetch.
- `runs/prelive_preflight/nodes/*/workspace/prompt.md` states the worker role,
  claim, baselines, success criteria, disproof conditions, no scope expansion,
  active lessons, baseline dossier summary, failure retrieval index, and strict
  JSON output.
- `ANTHROPIC_API_KEY` preflight fails in subscription mode.
- Live auth status must be checked through `claude auth status --json`; only
  `authMethod: claude.ai` with an allowed subscription type may pass. Persist no
  email, org id, or token fields.
- Critic routing includes `critics/always`, node-type, domain, and rebuttal
  stage critics where applicable.
- Output repair accepts only deterministic format repair and rejects
  schema-invalid content.
- Runner manifests reject non-allowlisted executables, shell control tokens,
  output paths outside the workspace, and missing claim-contract fields.
- Baseline dossiers reject missing selected candidates, missing naive/null
  candidates, missing detail files, non-http sources, and invalid dates.
- `runs/manual_live_smoke/manual_live_smoke_plan.json` is schema-valid, has
  `execution_enabled: false`, and records the exact manual command plus
  `stdin_path` for a later operator-controlled live smoke.
- Live smoke planning must return `blocked_by_billing_guard` unless the operator
  explicitly sets `RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE=subscription_ack`.
- Captured Claude CLI stdout must be ingested through
  `python -B -m research_harness.workers.claude_stdout_ingest --stdout <captured_stdout> --envelope <live_invocation_envelope>`;
  workers must not be trusted to write `worker_report.json` directly.
- Live workers run with tools disabled; any lessons, baselines, or failure
  retrieval context needed by a worker must be embedded by the orchestrator in
  the prompt rather than read by the worker.
- Failure retrieval must read only category-indexed files selected by explicit
  `selected_fail_files` or compact tag/lesson overlap, up to
  `settings.memory.failure_retrieval_top_k`.
- Failure candidates from worker reports must be promoted by orchestrator-owned
  memory code
  (`python -B -m research_harness.memory.failure_memory --node <node.json> --worker-report <worker_report.json>`);
  it writes category-named failure files and one-line lessons. Workers must not
  write shared memory.

## Live Backend Still Not Allowed

The current code may construct dry-run and live-smoke command plans, but
`execution_enabled` must remain `false`. Live Claude execution should not be
enabled until a human explicitly accepts subscription/API usage risk and runs
the generated manual command after reviewing the plan.

## First Live Smoke Observation

On 2026-05-23, the first manual smoke used subscription OAuth with
`ANTHROPIC_API_KEY` unset. Claude Code still reported `total_cost_usd` fields in
its JSON output. Treat those values as billing-risk evidence, not as harmless
metadata, until verified against the user's Claude billing/usage settings.
