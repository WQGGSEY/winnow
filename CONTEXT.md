# Context

Project glossary. Definitions only — no implementation details, no spec, no
roadmap. Update inline when a term is resolved during a grilling session.

## dataset

Any **external artifact a research experiment depends on**, not just data.

Includes raw training corpora, evaluation benchmarks, pretrained model
weights (e.g. ResNet checkpoints), quant alpha / factor sets, and proprietary
internal artifacts.

A dataset always carries a `type` tag drawn from the enum
`raw_data | benchmark | model_weights | factor_set | synthetic | custom`.
Fetcher dispatch, reproducibility-auditor checks, and
senior_quant_researcher verification all depend on the type. `synthetic`
carries a `synthetic_recipe` sub-object (shape, dimensions,
distributions, correlations, seed, generator_module) instead of an
external `source`.

A dataset may live in a public source (HuggingFace, torchvision, arXiv data
release, etc.) or in an internal location (S3 team bucket, on-prem warehouse,
proprietary feed). Internal locations are resolved through a grilling
follow-up where the user supplies the source path, access method, schema,
universe / time range, and any other metadata the fetcher needs.

## research_refiner (agent)

Agent-level role (not a worker) that runs **after market_research** and
**before root_node generation**. Multi-turn live interview with the user
to sharpen the research plan into something the orchestrator can act on.

Operator usage note: the user originally called this `finetuner`, but in
this codebase `finetune` is reserved for model-weight fine-tuning. The
canonical name is `research_refiner`.

Responsibilities:

- Sharpen the original grilling claim into a **validation-ready** plan
  (concrete metric, threshold, splits, statistical test, decision rule).
- Reconcile conflicts between the user's stated goal and what
  market_research found is current SOTA — surface the conflict, force
  the user to either narrow the claim, change the baseline, or accept
  the discrepancy in writing.
- **Decide every dataset** the experiment depends on (`type`, `source`,
  `role`, `split`, plus any internal-source metadata the user supplies).
  This decision is mandatory; the agent does not exit with unresolved
  dataset slots.

Dataset materialization (the actual download) is a deterministic step
the refiner calls into, not a separate agent. The agent owns the
**decision**; the fetcher just executes the chosen spec.

Skippable: production runs go through the refiner; quick prototyping may
skip it by going directly from market_research to root_node generation.

## refined_research_plan

Immutable artifact the refiner emits. Carries the final claim, the
validation procedure (metric + threshold + splits + statistical test +
seeds + decision rule), the list of `dataset_specs`, and a record of
any SOTA conflicts that were reconciled with the user. Downstream
`root_node` generator reads this instead of `grilling_session.extracted`
when refiner ran; otherwise falls back to grilling directly.

## validation_procedure

Sub-object of `refined_research_plan` that defines how the claim will
actually be tested: primary metric, comparison operator, threshold,
train/eval splits, statistical test, number of seeds, and the decision
rule (e.g. "all baselines beaten AND p < 0.05").

## dataset materializer

Deterministic, type-keyed fetcher invoked by the refiner during a
`PROPOSE_DATASET` action. Conforms to a single Protocol
(`try_materialize(spec, cache_root) -> MaterializeResult`) so each
type dispatches to its own implementation without leaking type-specific
branches into the refiner. HuggingFace and Torch Hub fetchers reuse
their native caches (`~/.cache/huggingface`, `~/.cache/torch/hub`); the
local / S3 / synthetic fetchers write into the repo-local
`.dataset_cache/<type>/<id>/`. Heavy fetchers (huggingface_hub, boto3,
torch) are optional dependencies; missing imports degrade gracefully
to `MaterializeResult.status="failed"` with a clear error.

## dataset_manifest

Per-node-workspace JSON written by the refiner after all PROPOSE_DATASET
rounds succeed. Maps each `dataset_spec.id` to its `materialized_path`
plus the original spec. User template `src/` code reads this manifest at
runtime to locate datasets, rather than the harness copying multi-GB
data into every node workspace.

## sota_reconciliation

Record of a conflict the refiner found between the user's grilled claim
and the SOTA evidence in `market_research_brief.baseline_analysis_md`,
plus how the user chose to resolve it. Resolution is one of three named
paths: `narrow_claim`, `change_baseline`, `acknowledge_limitation`. Each
entry carries the conflict text, the evidence paper URL, the chosen
path, the post-resolution wording, and the round it was resolved in.
Stored as a list on `refined_research_plan`.

## acknowledged_limitation

A discrepancy or scope restriction the user explicitly accepts rather
than fixing. Distinct from a `disproof_condition` (which would still
fail the claim if hit) — an acknowledged limitation lives outside the
claim altogether and is surfaced unchanged in publication artifacts so
the writeup cannot quietly drop it.

## operator_frontend

Local single-user web UI sitting in front of the existing CLI surface
(`research_runner`, `production_runner`, `live_dispatch`). Binds to
localhost only; no auth, no multi-user session model — the filesystem
under `runs/` remains the source of truth and the same `--billing-ack` /
`--execute-ack` / approval-gate semantics that apply to the CLI apply
unchanged to the frontend. The frontend exists to replace terminal
interaction for the same single operator, not to expose the harness as a
shared service. Team-mode is explicitly out of scope; if it is ever
added it will be layered on top, not designed in up front.

Lives at `research_harness/frontend/` as a sub-package of the harness,
with FastAPI / uvicorn / Jinja2 declared as `[frontend]` optional
dependencies so headless installs are unaffected. Top-level frontend
package imports are lazy — server-side imports of FastAPI happen only
inside the `__main__` entry point, never at package import time.

UX shape: Claude.ai-style left sidebar enumerates runs / research
threads; main pane shows the currently-selected one (chat for live
multi-turn agents, artifact viewer for completed runs). Multiple runs
may coexist for browsing, but **at most one live run executes at a
time** — see [[single_active_run]].

## live_acks (frontend)

[[operator_frontend]] surface for the two distinct CLI acknowledgements
that gate live Claude execution. They are kept semantically separate
because they answer different questions:

- **`subscription_ack`** — "I consent to using my Claude subscription
  for live calls in this app." Persistent, granted **once** during
  onboarding, stored in `settings.local.json` under
  `frontend.subscription_ack_at`. Equivalent to the env
  `RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE=subscription_ack`. Revocable
  through the Settings panel.
- **`execute_ack`** — "Run this specific live phase now." Per-phase,
  prompted by a modal at phase launch ("Execute <phase> for thread
  <title>? Estimated cost: ~$X"). Equivalent to the env
  `RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE=live_smoke_ack` plus
  `--execute-ack`. Audit-logged into `thread.json.execute_acks[]`
  (`{phase, at, mode: "manual" | "auto"}`).

### Full-auto mode

Operator escape hatch: `settings.local.json.frontend.full_auto_mode:
true` suppresses the per-phase `execute_ack` modal — phases launch
immediately when the user clicks "Advance" or starts the thread.
`subscription_ack` is **not** bypassed (still required once).

Safety affordances baked in:

- Enabling full-auto requires a one-time explicit confirmation modal
  that lists what is being bypassed.
- A persistent badge in the top bar (`● AUTO`) is shown whenever the
  mode is on, so the operator cannot silently forget that gates are
  off.
- Auto-launched phases still write `execute_acks[{mode: "auto"}]` to
  `thread.json` — the audit log is preserved regardless of mode.
- Disabling full-auto is one click in Settings; no confirmation needed
  to *tighten* gates.

## research_thread

First-class entity owning one full research arc — the chain
`grilling -> market_research -> research_refiner -> production`. A
thread is the unit the [[operator_frontend]] sidebar enumerates (one
sidebar row = one thread), the unit the pipeline wizard advances, and
the unit the [[single_active_run]] lock is acquired against.

Layout on disk (the `dogfood_alpha_e2e/` run already follows this
shape and is the prototype):

```
runs/threads/<thread_id>/
  thread.json                  # title, created_at, current_phase, status
  grilling/grilling_session.json
  market/market_research_brief.json, baseline_analysis.md, reference_papers/
  refine/refined_research_plan.json, dataset_manifest.json
  production/production_run_summary.json, interactive_summary.html, ...
```

Pre-existing flat directories under `runs/` (smoke runs, legacy
single-phase outputs) are **not** threads; they are surfaced separately
(or not at all) by the frontend and are out of scope for the thread
sidebar.

A thread has exactly one in-flight live phase at a time and at most one
thread holds the global execution lock at any moment.

## thread_id

Immutable opaque identifier for a [[research_thread]], shaped
`thread_<8-hex>` (e.g. `thread_3f9a2b1c`). Minted by the
[[operator_frontend]] at thread creation, **before** any phase runs.
Carries no semantics — the human-readable title lives in `thread.json`
and is renamable, while `thread_id` is the unchanging key used in URLs,
the [[single_active_run]] lock, log lines, and the on-disk path
`runs/threads/<thread_id>/`.

Phase-internal identifiers (`grilling_session.session_id`,
market_research run metadata, etc.) keep their own IDs untouched and
sit **inside** the thread directory — `thread_id` is the outer
container, not a replacement for them. This makes it safe to re-run a
single phase (e.g. retry grilling) under the same thread without
losing identity.

## thread.json

Denormalized index file at `runs/threads/<thread_id>/thread.json` that
the [[operator_frontend]] sidebar reads directly without parsing inner
phase artifacts. Schema:

```json
{
  "thread_id": "thread_3f9a2b1c",
  "title": "Alpha factor combo ensemble Sharpe",
  "created_at": "2026-05-25T08:14:00Z",
  "updated_at": "2026-05-25T09:02:11Z",
  "current_phase": "grilling | market | refine | production",
  "phase_status": "idle | running | awaiting_input | complete | failed",
  "outcome": "accept | reject | inconclusive | null",
  "domain": "alpha_factor_combo",
  "user_goal": "<first-grilling user_goal, cached for sidebar tooltip>"
}
```

State model is **two-axis**: `current_phase` × `phase_status`, not a
single flat enum. The 1:1 mapping is "the [[single_active_run]] lock is
held by exactly the thread whose `phase_status === "running"`". The
`awaiting_input` value applies only to multi-turn phases (grilling,
refine); other phases never enter it. `outcome` stays `null` until
production reaches an AC decision; it is intentionally separate from
`phase_status` because a `reject` production run is still a `complete`
phase. Event-log–derived status is deliberately deferred — if audit /
approval-queue work ever needs it, an `events[]` field can be added
non-breakingly.

`title`, `domain`, and `user_goal` are snapshotted into `thread.json`
once when grilling completes; they are derived from immutable grilling
output so they do not go stale.

## phase_accordion

Main-pane layout of [[operator_frontend]]: when a [[research_thread]]
is selected in the sidebar, its four phases (grilling, market, refine,
production) are rendered as a vertical accordion in fixed pipeline
order. Top-to-bottom order mirrors the strict phase dependency; tabs
were rejected because tabs imply peers, not a chain.

Auto-expand policy:

- `current_phase` && `phase_status` ∈ {`running`, `awaiting_input`} →
  expanded, scrolled into view on thread select.
- `current_phase` && `phase_status === "complete"` → expanded, with an
  "Advance to <next phase>" action visible inside the panel.
- Prior phases (`complete`, before `current_phase`) → collapsed by
  default, header shows a one-line summary (e.g.
  `grilling · domain=alpha_factor_combo · 12 rounds · DONE`).
- Phases after `current_phase` → disabled header (`not yet run`).

Collapsed panels do not fetch their inner artifacts; expansion is the
lazy-load trigger. This keeps the page light for threads with large
production summaries.

Inside each panel, the UI renders **curated cards for headline data
plus a raw-JSON `<details>` fallback**. The fallback is the schema
decoupling guarantee: when a phase artifact gains a new field (a
common occurrence when the harness evolves — see ADR 0001's
`refined_research_plan` growth), the field is immediately visible via
the raw view, and the curated card can catch up at the frontend's own
pace without blocking the user.

## multi_turn_session_persistence

Persistence policy for in-flight multi-turn agent sessions (grilling,
research_refiner) used by [[operator_frontend]]. Each round's
`rounds[]` entry is flushed to the on-disk session JSON
(`grilling_session.json`, `refined_research_plan.json` working file)
**immediately after the round is appended** — meaning the user's reply
is persisted before the next live Claude call fires, so a crash mid-LLM
never loses already-submitted user input.

Flush state during the loop carries `status: "in_progress"` (final
states `done | error | max_rounds_reached` are written only when the
loop terminates).

On server boot, threads whose `thread.json.phase_status === "running"`
are transitioned to `awaiting_input` and surfaced in the sidebar with a
"Resume" affordance. Resume re-enters the same agent loop seeded with
the persisted `rounds[]` — the existing `_call_claude_round(...,
rounds=rounds, ...)` signature already accepts prior rounds as context,
so no protocol changes are needed.

Abandoning a stuck thread is allowed: the user can mark
`phase_status: "failed"`, the partial session JSON is kept for
inspection, and a retry of the same phase under the same
[[thread_id]] is written into a sibling directory
(`<phase>.attempt2/`).

## single_active_run

Hard invariant for [[operator_frontend]]: at any given moment at most
one live run is executing across the whole harness. Motivation is the
single-GPU constraint (training / live Claude rounds cannot
meaningfully share the box), not authorization. Browsing, replaying,
and reading completed runs is always allowed in parallel; only the
"execute this step" action is mutually exclusive. The frontend enforces
this via a single server-side execution lock — attempts to start a
second live action while one is in flight are refused, not queued
(queueing is deferred until we have a real need for it).

## domain_scaffolding

Sub-mode of the grilling phase that fires **only when no registered
domain matches the user_goal**. The grilling agent's normal job is to
pick exactly one domain from the enum produced by
[[experiment_plan_templates]] discovery; when nothing fits, instead of
forcing a bad fit the agent enters a deeper interview that collects
enough information for the harness to materialize a **complete new
domain folder** — `plan.json` plus the full `src/` Python tree
(entrypoint, proposed, baselines/{current_best,naive,random}, eval).

This is option C in the design grilling: AI-generated executable
Python. The operator accepts that AI-generated code may be incorrect
(fake evidence risk); the mitigation is the mandatory deep interview
(no shortcut path lets a new domain be created with shallow input) plus
the existing `evidence_is_fake` guard at publication time.

UI visibility: while grilling is in progress the [[research_thread]]'s
domain badge is **null** (no domain decided yet); once grilling
completes the badge flips to either the matched existing domain or the
newly scaffolded name. The transition is the user-visible signal that
domain selection / scaffolding finished.

## grilling action

Action tokens the grilling LLM emits each round. Extends the original
2-action protocol with a third action used **only when
[[domain_scaffolding]] is active** (no existing domain matches):

- `ASK` — question for the user; harness reads reply.
- `PROPOSE_FILE` — single file (path + content) the agent wants to add
  to the new domain scaffold. Harness validates (syntax for `.py`,
  schema for `plan.json`), injects result into the next round, and
  stages the file under a per-thread scaffold workspace. No user
  round-trip.
- `DONE` — emit the final grilling extract. If a new domain was
  scaffolded, the harness performs a final dry-import of the staged
  `src/` tree; only on success does it move staging → real location
  `experiment_plan_templates/<new_domain>/` and set
  `extracted.domain = "<new_name>"`. A failed dry-import rejects the
  DONE and the agent must emit more `PROPOSE_FILE` rounds to fix.

This mirrors [[refiner action]]'s 3-action protocol intentionally —
the dataset materializer feedback loop already proved this pattern
works for AI-generated artifacts with strict validation.

## refiner action

One of three protocol tokens the research_refiner LLM emits each round:

- `ASK` — question for the user; harness reads stdin reply.
- `PROPOSE_DATASET` — candidate `dataset_spec`; harness silently calls
  the deterministic fetcher and injects the success / failure result
  into the next round's transcript. No user round-trip.
- `DONE` — emit the final `refined_research_plan`. May only be issued
  after every `dataset_spec` it lists has been verified by a successful
  `PROPOSE_DATASET` in this session (or explicitly marked
  `unresolved_dataset_specs` when max_rounds was hit).
