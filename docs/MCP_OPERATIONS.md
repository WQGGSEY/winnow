# Codex MCP operations

The production supervisor runs Codex CLI sessions. Each session receives the
repository's `research_harness` MCP server through command-line configuration.
The harness does not require global MCP registration or `~/.claude.json`.

## Prerequisites

1. Install the Codex CLI and make `codex` available on `PATH`.
2. Sign in with ChatGPT:

   ```bash
   codex login status
   ```

3. Install the project and optional frontend dependencies:

   ```bash
   pip install -e ".[frontend]"
   ```

The auth preflight accepts ChatGPT login only. It records a redacted status and
auth method, never account identifiers or tokens.

## Start production

1. Start the frontend:

   ```bash
   python -m research_harness.frontend
   ```

2. Create a thread and complete grilling and connector phases.
3. Select an installed Codex model in the thread header. The default is
   `gpt-5.6-sol`; allowed IDs are in
   `settings.runtime.llm_orchestrator.mcp.allowed_models`.
4. Open the production phase and start the supervisor.

The supervisor starts `codex --approve-for-me exec` because the observed
`never` approval policy rejects MCP calls. It also supplies these settings for
every invocation:

- `mcp_servers.research_harness.command`
- `mcp_servers.research_harness.args`
- `mcp_servers.research_harness.env.*`

The configured command is `python -m research_harness.mcp_server --repo-root
<repo>`. Update `settings.runtime.llm_orchestrator.mcp.server_env` when the MCP
server needs another repository-specific environment value.

## Observe and stop

The production page streams `supervisor.log` and `codex_subprocess.log`. Stop
requests preserve the existing process-group watchdog behavior: the supervisor
sends `SIGTERM`, waits for the grace period, then escalates to `SIGKILL` if the
process remains alive.

The MCP boundary validates persona rules and operation order. A rejected call
returns a machine-readable reason so Codex can revise the request. Important
operations include `get_research_state`, `get_next_admissible_node`,
`design_experiment_template`, `execute_node_experiment`, `run_critic_reviews`,
`submit_professor_decision`, and the rebuttal and publication operations.

## Verify the adapter

Run the auth check and focused tests:

```bash
scripts/verify_codex_adapter.sh
```

The optional live probe is read-only and ephemeral:

```bash
RESEARCH_HARNESS_VERIFY_CODEX_LIVE=1 scripts/verify_codex_adapter.sh
```

No live sessions are persisted by the probe.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Auth preflight reports `non_chatgpt_auth` | Run `codex login`, choose ChatGPT login, and rerun the preflight. |
| MCP tools are unavailable | Check `server_command`, `server_args`, and `server_env` in `settings.json`; do not rely on global MCP registration. |
| Model selection is rejected | Choose an ID from `mcp.allowed_models` or add an installed Codex model to that list. |
| Supervisor appears idle | Inspect `supervisor.log` and `codex_subprocess.log` on the production page. |
| A tool call is rejected | Read the returned `reason`, correct the contract or operation order, and retry. |

The dormant `claude_cli` and `anthropic` implementations remain legacy code.
Their model settings are isolated under `legacy_claude_agent_models`; they do
not consume active Codex model IDs.
