# Codex adapter synthesis

## Base

Candidate A is the base. It gives callers provider-neutral completion and agent-session contracts. One private Codex boundary owns argument ordering, prompt composition, JSONL parsing, usage extraction, authentication, and per-invocation MCP configuration.

## Grafts

- Candidate B's `TokenUsage` fields cover every observed Codex counter, including cached, cache-write, and reasoning tokens.
- Candidate B's pure command builder and JSONL parser remain directly testable.
- The migration checklist must reject stale Claude imports, backend names, schema fields, model IDs, and test fixtures on the active path.
- MCP environment values live at project scope in `settings.json`. The supervisor and per-invocation MCP config read the same map.

## Rejections

- Do not add a provider registry. The target has one active provider.
- Do not add a compatibility re-export module or legacy tuple API.
- Do not report `cost_usd=0`. Codex does not provide that value.
- Do not move thread terminal policy, retries, locks, or experiment watchdog rules into the adapter.
- Do not rely on a global Codex MCP registration.

## Implementation shape

- `research_harness/agent_runtime.py` owns provider-neutral request, result, usage, authentication, and event data.
- `research_harness/adapters/codex_cli.py` owns Codex commands, authentication probing, one-shot execution, JSONL parsing, and the production session process wrapper.
- Grilling and connector pass prompts into the completion contract.
- The supervisor keeps lifecycle policy and consumes normalized session events.
- The bounded-worker envelope keeps its research result schema but persists Codex backend identity.
- Settings and frontend copy describe Codex and ChatGPT authentication.

## Verification order

1. Unit-test command construction, auth parsing, JSONL parsing, missing-final-message errors, and usage extraction.
2. Migrate grilling and connector. Run their tests.
3. Migrate the supervisor and per-invocation MCP configuration. Run supervisor tests and a live read-only MCP probe.
4. Migrate the bounded-worker envelope, gate, and ingest. Run the live-worker tests and one schema-constrained Codex probe.
5. Update settings, UI text, and operational docs. Run settings and frontend tests.
6. Run the full pytest suite and scan the active pipeline for Claude executable or model references.

## Verification result

The design matches the observed Codex 0.150.1 CLI. Candidate A and the cross-judge agreed on the base. Candidate B supplied usage and migration-risk details. Implementation is not yet verified.
