# Research Harness

Production-track research harness inspired by Sakana AI Scientist-v2, with
Claude Code subscription workers treated as bounded runtime tools rather than
plain completion API calls. The current generation adds a user-facing entry
flow: a grilling agent, a market research agent (PDF retrieval and dossier
generation), a root-node generator, and a periodic lesson distillation agent.

References:

- Sakana AI Scientist-v2 repository: https://github.com/SakanaAI/AI-Scientist-v2
- Sakana AI Scientist-v2 BFTS config: https://github.com/SakanaAI/AI-Scientist-v2/blob/main/bfts_config.yaml

## Pipeline Overview

```text
user goal
  -> grilling agent (multi-turn, sonnet) -> grilling_session.json
  -> market research agent (arXiv + Google Scholar, agent-level not worker)
        -> reference_papers/ (PDFs + metadata)
        -> baseline_dossier (memory/baseline_dossiers/)
        -> baseline_analysis.md
  -> research_refiner agent (3-action: ASK | PROPOSE_DATASET | DONE)
        -> refined_research_plan.json (validation_procedure + dataset_specs
           + sota_reconciliations + acknowledged_limitations)
        -> dataset_manifest.json (per-node, type-keyed materializer dispatch)
  -> root node generator -> root_node.json (reads refined plan when present)
  -> production runner (preflight, mock tree search, rebuttal/AC, publish)
        -> publication artifacts (interactive_html, slides_html, markdown paper)
```

See [docs/adr/0001-research-refiner-and-dataset-cache.md](docs/adr/0001-research-refiner-and-dataset-cache.md)
for the design rationale (research_refiner role, 3-action protocol,
dataset materializer Protocol, per-node manifest pattern).

`research_harness.research_runner` is the high-level CLI that drives this. Live
Claude invocations remain behind explicit billing + execution acknowledgements.

## Included

- Claim-centered node schema and runtime envelope.
- Baseline dossier structure with current-best, naive, and random/null roles.
- Always-included one-line lesson memory.
- Deterministic critic folder routing.
- Rebuttal packet, rebuttal critic stage, AC decision.
- Worker-task contracts that restrict Claude worker output to source patches or
  observed results; the harness derives worker reports.
- Deterministic experiment-plan contracts, local runner manifest validation,
  bounded execution, baseline-evidence checks, and source/metrics-evidence
  ingestion into worker reports.
- Live Claude smoke runner + operator-controlled live node dispatch with
  reduction/memory/rebuttal modules behind approval gates.
- Production runner that chains preflight, mock tree search, rebuttal/AC, and a
  publish dispatcher that only renders on AC `accept` and follows
  `settings.publishing.default_outputs` (`interactive_html`, `slides_html`,
  optionally `markdown_paper`).
- **Grilling agent** (`research_harness.agents.grilling`): multi-turn sonnet
  loop driven by the harness; outputs schema-valid `grilling_session.json`.
- **Market research agent** (`research_harness.agents.market_research`):
  agent-level (tools allowed, not a worker). arXiv API + Google Scholar
  scraping; downloads PDFs; produces a baseline_dossier candidate satisfying
  full dossier invariants.
- **Root node generator** (`research_harness.orchestrator.root_node_from_grilling`):
  deterministic conversion of grilling output into a schema-valid root node;
  placeholder baseline_dossier_id is replaced after market research.
- **Lesson distillation agent** (`research_harness.memory.lesson_distillation`):
  deterministic byte-threshold trigger; live sonnet compresses lessons; old
  `lessons.yaml` archived under `memory/lessons/archive/`; approval gate
  required before writing.
- **research_refiner agent** (`research_harness.agents.research_refiner`):
  multi-turn live sonnet between market_research and root_node. 3-action
  protocol (`ASK | PROPOSE_DATASET | DONE`). PROPOSE_DATASET silently
  dispatches to the dataset materializer registry and injects the result
  into the next round. Forces SOTA reconciliation against the market
  research analysis. Emits `refined_research_plan.json` + `dataset_manifest.json`.
- **Dataset materializers** (`research_harness.datasets.*`): one Protocol,
  one class per type. HuggingFace (raw_data / benchmark / model_weights),
  LocalPath (factor_set / custom), Synthetic (synthetic with generator
  recipe). Optional dependencies degrade gracefully.

## Still Open

- Sakana-v2 BFTS parity for cross-node search heuristics.
- Real long-running training runner (slurm/container path).
- Final TeX renderer (disabled by default per token policy).
- Richer tree-visualization drilldown.

## Operator Frontend (local UI)

Localhost single-user web UI in front of the harness CLI. Replaces
terminal interaction for grilling / refiner chat and exposes per-phase
artifacts in a Claude.ai-style sidebar + accordion. See
[docs/adr/0002-operator-frontend-in-process.md](docs/adr/0002-operator-frontend-in-process.md)
and [docs/adr/0003-operator-frontend-htmx-stack.md](docs/adr/0003-operator-frontend-htmx-stack.md)
for design rationale, and `CONTEXT.md` (operator_frontend,
research_thread, thread_id, thread.json, single_active_run,
phase_accordion, multi_turn_session_persistence, live_acks) for the
domain language.

```bash
# install the optional frontend dependencies
pip install -e ".[frontend]"

# start the local server (bound to 127.0.0.1 only)
python -m research_harness.frontend --port 8765

# open http://127.0.0.1:8765 in a browser
```

What you get:

- **Sidebar** lists every `research_thread` under `runs/threads/<thread_id>/`.
  Each row shows the current phase × status + outcome badge. Click `+ New`
  to mint a `thread_<8hex>` and start a grilling session.
- **Accordion** per thread: Grilling / Market research / Refiner / Production.
  The current phase auto-expands; completed phases collapse with a one-line
  summary. Each panel shows curated cards plus a raw-JSON `<details>` fallback.
- **Live chat** (SSE) for the multi-turn agents (grilling, refiner). The
  agent's ASK arrives as a streamed message; replies POST back through the
  in-process `input_provider`.
- **Per-round persistence**: every grilling / refiner round is flushed to
  `grilling_session.json` / `refined_research_plan.json` with status
  `in_progress` immediately after the user reply is appended. On server
  restart, in-flight threads are demoted to `awaiting_input` and gain a
  Resume button.
- **single_active_run lock**: at most one live phase executes at a time
  (single-GPU constraint). A second launch attempt is refused, not queued.
- **Settings panel**: grant `subscription_ack` once (persistent in
  `settings.local.json`, gitignored). Toggle `full_auto_mode` to skip the
  per-phase `execute_ack` modal — a persistent `● AUTO` badge in the top bar
  makes the mode hard to forget.

The frontend does not change harness CLI behavior. You can keep using
`python -m research_harness.research_runner grill ...` from the terminal
and the resulting `runs/grilling/...` artifacts are unaffected. Threads
created through the UI live under `runs/threads/` and are completely
separate from any legacy single-phase runs.

## Run The Full Research Pipeline

```bash
# 1. grilling — multi-turn live sonnet interview
RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE=subscription_ack \
RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE=live_smoke_ack \
python -B -m research_harness.research_runner grill \
  --user-goal "your research goal" \
  --billing-ack --execute-ack \
  --run-dir runs/grilling/<id>

# 2. research — market research + refiner + root node + production
#    refiner is live too; pass refiner acks. add --skip-refine for fast prototyping.
RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE=subscription_ack \
RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE=live_smoke_ack \
python -B -m research_harness.research_runner research \
  --grilling-session runs/grilling/<id>/grilling_session.json \
  --refiner-billing-ack --refiner-execute-ack \
  --run-dir runs/research/<id>

# 2b. refine only (when you want to iterate on the refiner alone)
python -B -m research_harness.research_runner refine \
  --grilling-session runs/grilling/<id>/grilling_session.json \
  --market-research-brief runs/research/<id>/market_research/market_research_brief.json \
  --billing-ack --execute-ack

# 3. distill — periodic deterministic distillation (triggered by lesson byte total)
RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE=subscription_ack \
RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE=live_smoke_ack \
python -B -m research_harness.research_runner distill --approve
```

`grill` runs a bounded multi-turn loop against Claude sonnet (each round emits
either `{"action":"ASK","question":...}` or `{"action":"DONE","extracted":...}`);
the harness asks the user via stdin between turns. The session is force-extracted
on max_rounds and persisted as schema-valid `grilling_session.json`.

`research` runs the agent-level market research pass: arXiv API + best-effort
Google Scholar scrape, downloads PDFs into `reference_papers/`, writes a
baseline dossier candidate into `memory/baseline_dossiers/`, derives the root
node, and chains into the production pipeline.

`distill` only triggers when total active-lesson bytes exceed
`settings.memory.lessons.distillation_token_threshold_bytes`. It calls live
sonnet, archives the existing `lessons.yaml` under
`memory/lessons/archive/lessons_<utc>.yaml`, and only rewrites `lessons.yaml`
after `--approve`.

## Run The Production Pipeline (root node provided)

```bash
python -B -m research_harness.production_runner
```

Chains preflight, mock tree search, rebuttal/AC, and the gated publish
dispatcher in one command. Without a custom root node, uses the demo node.
Writes `runs/production_run/production_run_summary.json` and, when AC accepts,
renders the publication artifacts listed in `settings.publishing.default_outputs`.
Live Claude execution stays outside this chain; operators still drive live nodes
through `research_harness.orchestrator.live_dispatch` and the existing
approval-gated modules.

## Experiment Plan Templates (per-domain, directory-based)

Real experiment code lives in user-owned template directories listed in
`settings.json`:

```json
"experiment_plan_templates": {
  "directories": ["experiment_plan_templates", "my_other_templates"]
}
```

Each directory contains **one subdirectory per domain**, which is a real
Python project — not a single file:

```
experiment_plan_templates/
  retrieval/
    plan.json                 # metadata only
    src/                      # actual project tree, materialized verbatim
      __init__.py
      experiment.py           # entrypoint
      data.py
      proposed.py
      baselines/
        current_best.py
        naive.py
        random_baseline.py
      eval/
        ndcg.py
```

The router
(`research_harness.orchestrator.experiment_plan.build_experiment_plan_for_node`)
searches the configured directories in order, loads `<domain>/plan.json`,
walks `<domain>/src/` and embeds every allowed source file into the
materialized experiment plan, then validates against
`experiment_plan.schema.json`. The whole `src/` tree is copied into the
node workspace before the runner executes the entrypoint, so normal
multi-module Python imports work.

If no directory matches the node's domain, the router falls back to the
deterministic demo plan and the publish dispatcher refuses to render
artifacts (`evidence_is_fake` guard).

See `experiment_plan_templates/README.md` for the full authoring contract.

`production_run_summary.json` includes:

```json
"templates_used": {"n_xxx": "retrieval" | "_fallback_demo" | ...},
"fallback_node_ids": ["n_yyy", ...],
"evidence_is_fake": true | false
```

So you can immediately see whether the metrics you are reading came from
your real evaluation script or from the demo fallback.

### Grilling agent only emits registered domain names

`research_runner grill` injects the list of available domains (gathered from
your configured template directories) into the grilling system prompt as a
required enum. The agent must pick exactly one of them — it cannot invent a
new domain name. If the LLM ever returns an off-enum domain, the harness
rejects the session with a clear error rather than silently falling back to
fake evidence.

### Market research writes a real analysis brief

`research_runner research` runs the market research agent which:

1. Searches arXiv (and optionally Google Scholar) for the grilled query.
2. Downloads PDFs into `<run>/reference_papers/`.
3. Writes a deterministic baseline-analysis markdown (or, with
   `enable_sonnet_analysis=True` + billing/execution ack, an LLM-written
   markdown brief) at `<run>/baseline_analysis.md`. The markdown identifies
   the most likely current-best / naive / random baselines with reported
   metrics and citations.
4. The path is recorded on the root node under
   `lineage.inherited_assumptions` so downstream orchestrator / template
   code can find it.

A starter template for the `retrieval` domain (toy nDCG@10 evaluation) is
included as an example. See `experiment_plan_templates/README.md` for the
full authoring contract.

## Per-Agent Model Selection

`settings.json` exposes per-role model selection for every live-Claude
agent. Defaults to `sonnet` everywhere; override one role at a time:

```json
"runtime": {
  "agent_models": {
    "default": "sonnet",
    "grilling_agent": "opus",
    "market_research_agent": "sonnet",
    "lesson_distillation_agent": "haiku"
  }
}
```

Resolution order (per role): explicit role entry → `default` → live
backend's `model` → literal `"sonnet"`. All three agents
(`research_harness.agents.grilling`,
`research_harness.agents.market_research`,
`research_harness.memory.lesson_distillation`) call
`research_harness.config.resolve_agent_model(settings, role)` when
constructing the live CLI invocation, so changing settings is the only
thing you need to do.

## Critic Personas

`critics/` is organised by routing (`always/`, `by_node_type/<type>/`,
`by_domain/<domain>/`, `by_stage/<stage>/`). Current personas:

- `always/invariants_v1` — constitutional rule check.
- `always/runtime_safety_v1` — runtime envelope and permission policy.
- `always/senior_quant_researcher_v1` — quantitative rigor: variance,
  significance, baseline parity. Flags within-noise improvements as
  non-blocking objections.
- `always/reproducibility_auditor_v1` — seed / snapshot / artifact
  reproducibility. Flags missing JSON metrics.
- `by_node_type/capability/capability_strict_v1` — capability claim depth.
- `by_node_type/capability/experimental_methodologist_v1` — design controls
  the variable the claim names.
- `by_node_type/necessity/...`, `by_node_type/mechanism/...`,
  `by_node_type/validity/...` — type-specific rigor.
- `by_stage/rebuttal/whole_case_validity_v1`,
  `by_stage/rebuttal/overclaim_detector_v1`,
  `by_stage/rebuttal/publication_readiness_v1`,
  `by_stage/rebuttal/claim_skeptic_v1` — rebuttal-stage challenges.

Add a persona by dropping a markdown file in the right routing directory
with `critic_profile_id` frontmatter.

## Run The Demo

```bash
python -m research_harness
```

The demo writes:

```text
runs/demo_run/node.json
runs/demo_run/experiment_plan.json
runs/demo_run/nodes/n_demo_001/workspace/worker_task.json
runs/demo_run/job_manifest.json
runs/demo_run/nodes/n_demo_001/workspace/experiment.py
runs/demo_run/nodes/n_demo_001/workspace/artifacts/metrics.json
runs/demo_run/nodes/n_demo_001/workspace/runner_result.json
runs/demo_run/worker_report.json
runs/demo_run/critic_review_bundle.json
runs/demo_run/orchestrator_reduction.json
runs/demo_run/rebuttal_packet.md
runs/demo_run/orchestrator_rebuttal.md
runs/demo_run/rebuttal_critic_bundle.json
runs/demo_run/ac_decision.json
runs/demo_run/research_state_bundle.json
runs/demo_run/interactive_summary.html
```

## Run Tests

```bash
python -B -m unittest discover -s tests -v
```

## Run Mock Tree Search

```bash
python -B -m unittest tests.test_tree_search -v
```

```bash
python -B -m research_harness.orchestrator.tree_search
```

The staged tree-search loop currently accepts only dry-run/local backends. Each
node first writes an orchestrator-owned `experiment_plan.json`, materializes
declared workspace-local source files from that plan, derives `job_manifest.json`,
writes `workspace/runner_result.json` and declared metrics files, then builds
`worker_report.json` only from that runner evidence. The experiment plan declares
which metric must beat current-best, naive, and random/null baselines. If a
worker reports a supported result but the mandatory baseline comparison fails,
the claim is downgraded to a non-promotable `negative_result`. Runner failures,
missing metrics, or missing baseline evidence become non-promotable worker
reports. Live Claude Code remains behind `research_harness.workers.live_gate`
and is not called by tree search.

## Run Pre-Live Local Preflight

```bash
python -B -m research_harness.local_preflight
```

This checks config/profile loading, node invariants, deterministic critic
routing, dry-run Claude invocation envelope generation, worker-report schema
validation, experiment-plan contract validation, deterministic runner
manifest/result validation, baseline dossier validation, tree-search runner
artifact validation, rebuttal/AC gating, and interactive HTML generation without
calling Claude Code.

## Build Manual Live Smoke Plan

```bash
python -B -m research_harness.workers.live_gate
```

This still does not call Claude Code. It writes a schema-valid manual smoke
plan and runbook under `runs/manual_live_smoke/`, checks subscription auth
precedence, records the exact manual command, records the expected raw stdout
path, and records the ingest command. By default, the plan is blocked by the
billing guard; live Claude Code requires an explicit
`RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE=subscription_ack` acknowledgement. The auth
probe accepts only Claude.ai subscription auth status and stores no email,
organization id, or token fields. Claude live output is constrained to
`worker_task_result`; `claude_stdout_ingest` requires the matching
`manual_live_smoke_plan.json` for live backend stdout and writes
`worker_report.json` only after gate, schema, and scope validation.

For a repeatable one-node smoke after reviewing the generated plan, use the
explicit runner:

```bash
RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE=subscription_ack \
RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE=live_smoke_ack \
python -B -m research_harness.workers.live_smoke_runner
```

The runner still keeps `execution_enabled: false` in the plan. It only runs the
single smoke command after the second execution acknowledgement, unsets
`ANTHROPIC_API_KEY`, captures stdout/stderr, ingests stdout through the same
live gate, and writes `live_smoke_run_summary.json` with token/cost estimates.
Internally the same runner path now supports a single arbitrary node through
`run_live_node_once(...)`, but tree search still rejects `claude_code_live` so
the live worker cannot expand into autonomous search execution.

To dispatch the next queued `search_state` node without letting tree search call
the live backend directly:

```bash
python -B -m research_harness.orchestrator.live_dispatch \
  --search-state runs/<tree_run>/search_state.json
```

Add `--execute --billing-ack --execute-ack` only after reviewing the generated
`live_node_dispatch.json` and live plan. The dispatch layer selects one queued
ready node, preserves the search state unchanged, and records that tree-search
mutation is forbidden.

## Core Files

```text
settings.json                          Runtime, memory, publishing, publication-gate, distillation knobs.
configs/harness.yaml                   Sakana-like search config plus harness_semantics.
research_profile.md                    Positive research taste and branch-generation priors.
lessons.yaml                           Active one-line lessons always included in orchestration.
critics/                               Deterministic critic personas.
memory/                                Failure, lesson, and baseline dossier indexes.
research_harness/agents/grilling.py    Multi-turn grilling agent (sonnet).
research_harness/agents/market_research.py  Agent-level paper search and dossier candidate generation.
research_harness/memory/lesson_distillation.py  Periodic deterministic-trigger distillation.
research_harness/orchestrator/         Tree search, reduction, live dispatch, root node generator.
research_harness/production_runner.py  preflight + tree search + rebuttal/AC + publish chain.
research_harness/research_runner.py    User-facing high-level CLI (grill / research / distill).
```

## Non-Overridable Invariants

- Workers do not own search policy.
- Workers write only node-local artifacts.
- **Agents** (grilling, market research, distillation) are conceptually
  elevated above workers: tools allowed and multi-turn allowed, but still
  bounded by schema contracts, billing/execution gates, and (for memory-mutating
  agents) an approval gate.
- Critics are read-only and selected by deterministic governance.
- Experiments require claim contracts.
- Missing mandatory baselines make a claim not evaluable or confounded.
- Claude Code subscription auth is runtime preflight responsibility.
- Long-running jobs belong to deterministic runners, not agent sessions.
- Publication requires rebuttal and AC gating when enabled.
- `lessons.yaml` and `memory/baseline_dossiers/` mutations require the
  appropriate approval gate; the existing file is archived before being replaced.
