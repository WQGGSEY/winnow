# Live Smoke Observations

This file records manual Claude Code observations that affect billing, quota,
or runtime safety. Do not run additional live smoke tests unless the billing
guard is explicitly acknowledged.

## 2026-05-23

Environment:

- `ANTHROPIC_API_KEY` was unset for the live command.
- Claude Code auth preflight reported subscription OAuth mode.
- A non-inference `claude auth status --json` probe later confirmed
  `authMethod: claude.ai`, `apiProvider: firstParty`, and
  `subscriptionType: max`. Email and organization identifiers are not persisted
  in harness artifacts.
- The generated command used `--permission-mode dontAsk` and JSON output.

Observed runs:

- Run 1 completed with `subtype: success`, `modelUsage: claude-opus-4-6[1m]`,
  `num_turns: 11`, and `total_cost_usd: 0.29686075`.
- Run 1 produced no schema-valid `worker_report.json`; Claude returned a
  markdown-fenced custom JSON report on stdout and recorded permission denials
  for reads, shell, glob, and workspace write.
- Run 2 used Sonnet, disabled tools, disabled session persistence, and set
  `--max-budget-usd 0.05`. It stopped with `subtype: error_max_budget_usd` and
  `total_cost_usd: 0.11899725000000001`.
- A third run was interrupted by the operator before output files were written.
- Later smoke hardening produced schema-valid `worker_task_result` JSON with
  `status: completed`, `repaired: false`, and no permission denials.
- Replacing Claude Code's default system prompt with a minimal bounded JSON
  system prompt, disabling slash commands, using strict MCP config, and
  compacting the worker prompt reduced the successful smoke estimate from
  `cache_creation_input_tokens: 32756`, `output_tokens: 1295`, and
  `total_cost_usd: 0.142266` to `cache_creation_input_tokens: 1799`,
  `output_tokens: 165`, and `total_cost_usd: 0.00922725`.

Interpretation:

- The CLI cost fields are enough to trigger `--max-budget-usd`, so the harness
  must treat them as usage-risk evidence.
- Anthropic documentation says Pro/Max subscription users have usage included
  in the subscription, and session dollar figures are local estimates rather
  than authoritative billing. However, Claude Code activity still counts against
  subscription usage limits.
- If `ANTHROPIC_API_KEY` is present, Claude Code prioritizes it over the
  subscription and may incur API pay-as-you-go charges.

Current policy:

- `claude_code_live` is blocked by default.
- Live gate probes `claude auth status --json` and blocks if the auth method is
  not `claude.ai` or the subscription type is not one of the allowed
  subscription plans.
- Claude workers do not write report files directly. The harness ingests Claude
  CLI JSON stdout, rejects runtime blockers first, extracts only the nested
  `result` as worker output, and writes `worker_report.json` after validation.
- Claude workers run with tools disabled. The orchestrator embeds active
  lessons, baseline dossier summaries, and failure retrieval indexes directly in
  the prompt instead of expecting worker-side file reads.
- Failure candidates are only proposals. Shared failure files and one-line
  lessons are written by orchestrator-owned memory code after validation.
- `research_harness.workers.live_gate` returns `blocked_by_billing_guard`
  unless `RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE=subscription_ack` is explicitly
  present or a test passes `billing_ack=True`.
- `research_harness.workers.live_smoke_runner` may execute only the single
  gated smoke command and requires a second acknowledgement:
  `RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE=live_smoke_ack` or `--execute-ack`.
- The same runner module exposes `run_live_node_once(...)` for a single
  arbitrary node. It is still an explicit operator-controlled path, not a tree
  search backend.
- Tree search must not execute live Claude workers directly.
