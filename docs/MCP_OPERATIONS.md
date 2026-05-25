# MCP-Mode Operations

The default backend for the research harness is **MCP**: Claude Code
interactive (your subscription) drives reasoning, the harness exposes
state + persona-enforced tools via a stdio JSON-RPC server. This document
is the operator quickstart.

## Why MCP

- **Free in the subscription pool.** No per-call API charges. Unaffected
  by the 2026-06-15 billing change because `claude` interactive sessions
  stay in the interactive pool while `claude -p`, Agent SDK, and direct
  API calls migrate to the new agent credit pool.
- **Persona enforced.** Every tool call goes through
  `persona_validator` at the MCP boundary. Lazy / safe / placeholder
  claims, idealized setups, and parent-restating follow-ups are rejected
  with explicit retry instructions Claude Code rewrites against.
- **One source of truth on disk.** Frontend and Claude Code share the
  same `runs/threads/<tid>/` tree; the frontend visualizes whatever
  Claude Code has committed so far.

## One-time setup

> macOS Terminal mangles multi-line paste — type each command on its own
> line. Do not paste blocks.

Register the MCP server with your Claude Code installation (once per
machine):

```
claude mcp add research_harness -- python -m research_harness.mcp_server --repo-root $(pwd)
```

If you run the harness from a fixed checkout, replace `$(pwd)` with the
absolute path so future shells find it.

## Daily operation

1. Start the frontend (one terminal):

   ```
   python -m research_harness.frontend
   ```

2. In the browser, create the thread, run grilling, then market research
   (same flow as before).

3. In the thread header, pick the model from the dropdown. Default is
   `claude-opus-4-7`; full list under
   `settings.runtime.llm_orchestrator.mcp.allowed_models`.

4. Click **Advance to production →**. The modal shows three single-line
   steps. Type each one separately:

   - **Step ②** in a separate terminal:

     ```
     claude --model claude-opus-4-7
     ```

   - **Step ③** as a single chat message inside Claude Code (NOT a slash
     command — this is plain English text):

     > Start the research_harness production run for thread &lt;thread_id&gt;.
     > Use the MCP tools: call get_research_state once, then loop —
     > get_next_admissible_node, design_experiment_template,
     > submit_grad_student_review, execute_node_experiment,
     > run_critic_reviews, submit_professor_decision — one node at a time.
     > When decide_publication_readiness returns submit=true, call
     > run_rebuttal_and_publish. If AC rejects, revise_root_after_reject
     > and restart.

   Claude Code reads the MCP tool descriptions and invokes them
   automatically from the prompt. No slash commands.

5. Leave the Claude Code terminal open. The browser updates live as
   Claude Code commits each decision (look for the "MCP — recent Claude
   Code decisions" panel in production).

## Tool catalog (what Claude Code calls)

All tools enforce persona at the boundary. Rejections come back as
machine-readable JSON with an explicit `reason` so Claude Code retries
without operator intervention.

| Tool | What it does |
| --- | --- |
| `get_research_state` | Full snapshot: thread, grilling, market brief, dossier YAML, paper filenames, current search_state, dialog log, operator model preference. |
| `get_next_admissible_node` | **Deterministic** selector — picks the single next node by stage + type weight (validity < capability < necessity < mechanism < boundary < constraint < operational < taste). One node at a time. |
| `design_initial_claim_contract` | Convert the funder's problem into a strong + honest claim. Rejects restatements, placeholder baselines, vague success, unhittable disproof, and baselines not grounded in the market output. |
| `design_experiment_template` | Persist Professor-authored code (`_lib/*` shared modules + per-node `experiment.py`) to disk. |
| `execute_node_experiment` | Build the experiment_plan, materialize the workspace, run LocalRunner, persist worker_report. Advances the node's state. |
| `run_critic_reviews` | Run the deterministic critic pack (`critics/always`, `by_node_type`, `by_domain`, `by_stage`). |
| `submit_grad_student_review` | Pre-run skeptical review. Rejects pure-agreement commentary. |
| `submit_professor_decision` | Promote / branch / prune. Mutates search_state, spawns follow-up children. Out-of-order submissions are rejected; the response names the correct next node. |
| `run_rebuttal_and_publish` | Rebuttal pack → AC decision → renderers (paper.html / interactive / slides). |
| `revise_root_after_reject` | AC rejected: archive the attempt and propose a new root claim. Same persona enforcement. |
| `decide_publication_readiness` | Operator-visible "submit or keep working" decision. |

## Persona rules (deterministic)

Configured under `settings.runtime.llm_orchestrator.mcp.persona_enforcement`:

- `reject_problem_restatement` — token overlap > 70% with the funder's
  problem statement → reject. Unicode-aware tokenizer covers Korean.
- `reject_placeholder_baselines` — any "TBD", "various", "any reasonable"
  / too-short baseline → reject.
- `reject_missing_measurable_success` — no numeric threshold in
  `success_criteria` → reject.
- `reject_unhittable_disproof` — disproof too abstract to ever trigger →
  reject.
- `require_market_grounding` — baselines must reference the dossier
  YAML, baseline analysis, or a downloaded paper filename. Free-styling
  on top of the market step is a violation.
- `require_grad_student_challenge` — grad student commentary that is
  pure agreement (no "잠깐", "wait", "concern", "however", etc., and no
  explicit concerns list) → reject.
- Follow-up children: if every successor claim is a near-restatement of
  the parent → reject.

Tune any rule by editing `settings.json`; set the boolean to `false` to
disable, or adjust `min_baseline_role_specificity_chars` to change the
length floor.

## Per-thread model

`thread.json.mcp_model` overrides `settings.runtime.llm_orchestrator.mcp.default_model`.
The frontend dropdown writes through `POST /api/threads/{tid}/mcp_model`,
which validates against `allowed_models`. Free-form input is intentionally
disabled — typos would silently send Claude Code a nonexistent model
string. To add a new model, edit `settings.json`.

## When something breaks

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `production launch is disabled in mcp mode` 409 | The frontend's "Advance to production" tried to launch directly. | This is correct behavior — use the modal's `/research start` command. |
| Selector returns the same node repeatedly | `submit_professor_decision` not committing search_state. | Confirm the tool returned `status: "accepted"`. If `rejected`, read the `reason`. |
| `baselines_ignore_market_output` reject | Professor designed baselines without reading the market dossier. | Have Claude Code call `get_research_state` first, then redesign with the dossier names verbatim. |
| Paper never appears | `run_rebuttal_and_publish` was not called. | Have Claude Code call it after `decide_publication_readiness` returns submit=true. |
| `production_run_summary.json` missing | Same as above. | `run_rebuttal_and_publish` is what writes the summary. |

## Other backends (still in the codebase)

`settings.runtime.llm_orchestrator.backend` can also be:

- `mock` — deterministic, fast, for tests / CI / demos.
- `claude_cli` — calls `claude -p` subprocess. Free until 2026-06-15;
  after the policy change moves to the agent credit pool at API rates.
- `anthropic` — anthropic SDK + `ANTHROPIC_API_KEY`. Always API-rate.

Switching backends is just one settings edit. The frontend MCP-specific
affordances (handoff modal, model dropdown, mcp_progress panel) only
show up while `backend == "mcp"`.
