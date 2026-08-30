# Codex adapter grounding

## Goal

Replace the Claude runtime used by the active new-topic pipeline with Codex CLI. A new thread must use Codex for grilling, connector JSON calls, the production MCP-driving supervisor, and the bounded live-worker acceptance path.

## Done predicate

- `codex login status` passes with ChatGPT authentication.
- One-shot Codex calls return normalized final text and usage from JSONL.
- Grilling and connector consume that normalized result without Claude envelopes.
- The supervisor launches Codex with the repository MCP server supplied per invocation.
- Codex completes a real `research_harness` MCP tool call.
- The live-worker envelope names Codex and validates a schema-constrained result.
- No active new-thread path requires the `claude` executable or Claude model IDs.
- Targeted tests and the full pytest suite pass.

## Observed CLI contracts

- Installed binary is `codex-cli 0.150.1`.
- `codex login status` returns exit 0 and `Logged in using ChatGPT`.
- Approval options belong before the `exec` subcommand.
- `codex exec --json` emits `thread.started`, `turn.started`, `item.completed`, and `turn.completed` JSONL events.
- The final answer is `item.completed.item.type=agent_message` with text in `item.text`.
- Usage is in `turn.completed.usage`.
- `--output-schema` enforced a test schema in a real call.
- `approval_policy=never` refused a local MCP call.
- `--approve-for-me` completed `research_harness.get_research_state` through a per-invocation MCP config.
- Codex has no `--system-prompt`, `--tools ""`, or `--max-budget-usd` equivalent.

## Existing ownership

- `agents/grilling.py` owns multi-round intake and currently builds Claude commands.
- `connector/claude_call.py` owns every connector completion subprocess.
- `thread_supervisor.py` owns production process lifecycle and stream logging.
- `mcp_server.py` owns provider-neutral tool validation and persisted research state.
- `workers/claude_code_invoker.py` and live gate modules own the bounded-worker acceptance path.
- `settings.json`, `settings_scoped.py`, frontend templates, and schemas persist Claude-specific provider names.

## Constraints

- Keep research-domain schemas unchanged unless they persist provider identity.
- Preserve injected command runners used by tests.
- Do not pretend the removed USD budget flag still enforces a limit. Timeouts and round limits remain real.
- Do not depend on a global Codex MCP registration. Pass the repository MCP command in each supervisor invocation.
- Read MCP environment from harness settings, not `~/.claude.json` or another client's private config.
- The active pipeline is `grilling -> connector -> production` from `docs/PIPELINE.md`.
- Dormant market and refiner phases are outside the required slice.

## Design rubric

1. The caller sees provider-neutral completion and agent-session contracts.
2. One source builds Codex commands and parses Codex JSONL.
3. The design removes active Claude coupling rather than adding a compatibility shim.
4. The MCP server remains client-neutral and receives its command and environment from repository settings.
5. The migration stays small enough to verify in ordered units.
6. The bounded-worker path can enforce its existing output schema with Codex.
