# Context

Project glossary. Definitions only — no implementation details, no spec, no
roadmap. Update inline when a term is resolved during a grilling session.

## GoalContract

The immutable problem-level question, success bar and final evidence requirements.
It is distinct from a development procedure and from a proposed explanation.
Current production requires a real holdout; a Lean artifact alone does not create
a theory-only terminal.

## DirectionAttempt

One active research direction bound to a formal claim node. Its lifecycle is
separate from the many development jobs needed to investigate it.

## ResearchWork

One bounded analysis, protocol revision or empirical procedure with competing
predictions. Before the next work, the planner interprets the previous completed
work against those predictions. This development judgment is not claim approval.
Empirical work declares the eligible-observation counts necessary to interpret
its selected test. Nonempty counts do not establish statistical power or validity.

## working research brief

A projection of work, claim links, evidence dependencies and result
interpretations, available before publication gates. The default prompt contains
a bounded recent view; full records remain available for targeted reading.
Historical records without interpretations are explicitly marked uninterpreted,
not retroactively summarized as scientific knowledge.

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

**STATUS — removed from the live (frontend) pipeline** (`frontend/server.py`
comment: "research_refiner removed from the frontend pipeline"; `frontend/threads.py`
`PHASES = ("grilling", "market", "production")`). In the live flow the **Professor
designs the claim in production** (`design_initial_claim_contract` →
`build_root_node_from_grilling`, reading grilling directly). This entry — and the
[[refined_research_plan]] / [[validation_procedure]] / [[dataset materializer]] /
[[dataset_manifest]] / [[sota_reconciliation]] entries below — describe the removed
refine phase (legacy CLI `research_runner` only).

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
`grilling -> market -> production` (frontend `threads.py` `PHASES`; the
former `research_refiner` step was removed — see [[research_refiner (agent)]]). A
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
  "current_phase": "grilling | market | production",
  "phase_status": "idle | running | awaiting_input | complete | failed",
  "outcome": "accept | reject | inconclusive | null",
  "domain": "alpha_factor_combo",
  "user_goal": "<first-grilling user_goal, cached for sidebar tooltip>"
}
```

State model is **two-axis**: `current_phase` × `phase_status`, not a
single flat enum. The 1:1 mapping is "the [[single_active_run]] lock is
held by exactly the thread whose `phase_status === "running"`". The
`awaiting_input` value applies only to the multi-turn grilling phase;
other phases never enter it. `outcome` stays `null` until
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
is selected in the sidebar, its three phases (grilling, market,
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

## setting_scope

The tier at which a configuration value lives. The harness recognises **three
disjoint scopes**, surfaced and editable through [[operator_frontend]]:

- **`project`** — checked-in defaults shared by everyone who clones the repo.
  Lives in `settings.json` (git-tracked). Answers "what is this harness's
  baseline behaviour?".
- **`operator`** — single-machine, single-user choices that should *not*
  travel with the code. Lives in `settings.local.json` (gitignored). Answers
  "what has *this* operator on *this* machine consented to / installed?".
  Examples: `frontend.full_auto_mode`, `frontend.subscription_ack_at`, the
  set of data adapters this machine actually has on disk.
- **`thread`** — choices that belong to one [[research_thread]] and should
  not leak across threads. Lives inside `runs/threads/<thread_id>/thread.json`
  (or a sibling override file). Answers "what did the operator decide for
  *this* research arc specifically?". Example: MCP model selection, agent
  max_rounds for this thread, which registered data adapter this thread
  anchors against.

Resolution order is **project → operator → thread**, with later scopes
overriding earlier ones for the same field. Each field declares a
`writable_at` permission list (subset of `{project, operator, thread}`);
scopes outside that list cannot set the field. A field's resolved value
is the highest-priority scope that has *any* value, falling through to
the next scope only when absent — not deep-merged. Array fields follow
the same rule: whole-array replace, no concat. (Concat-by-id semantics
are deferred until a concrete use case demands them.)

## setting_classification

Working principle for assigning each settings field its
[[setting_scope]] `writable_at` permission list:

- **`project` = structural** — what kind of research this harness recognises
  (auth policy, persona enforcement rules, schemas, publishing format,
  backend definitions, the memory/lesson system's shape).
- **`operator` = environmental** — what *this machine* / *this user* can do
  and has consented to (data adapters actually installed on disk, subscription
  consent, the active runtime backend choice, rate-limit backoff tuning).
- **`thread` = experimental** — what *this research arc* chose (which MCP /
  agent model, per-agent budgets and max_rounds, runner timeouts, AC accept
  thresholds, rebuttal policy, production milestone count, memory
  retrieval breadth, publication-gate on/off, optional `full_auto_mode`
  *tightening*).

Cross-cutting rules:

- Tightening safety gates is always allowed at a narrower scope. Loosening
  is never allowed at a narrower scope — e.g. `full_auto_mode` may be
  *disabled* at thread scope even if operator enabled it, but cannot be
  *enabled* at thread scope when operator left it off.
- The `runtime.default_backend` / `llm_orchestrator.backend` choice does
  not descend to thread scope: a thread-level backend swap would silently
  break the reproducibility of that thread's phase artifacts.
- The lesson / failure-distillation memory parameters stay project-scoped:
  the system's purpose is cross-thread learning, so per-thread overrides
  defeat the design.

## resolved_settings_snapshot

Per-phase immutable record of the resolved [[setting_scope]] values that a
given phase actually saw. Written into the phase's artifact directory at
phase start (e.g. `runs/threads/<tid>/grilling/resolved_settings_snapshot.json`,
`market/...`, `refine/...`, `production/...`). Allows thread-level
settings to remain mutable between phases without sacrificing the
reproducibility audit trail.

Shape per field: `{value, source}` where `source ∈ {project, operator,
thread, schema_default}`. Recording the source — not just the value —
makes it possible to answer "why did this phase use X instead of the
default?" without re-resolving from scratch.

Retroactive editing of past phase snapshots is disallowed. Re-running
a phase under different settings goes into a sibling attempt directory
(`<phase>.attempt2/`, mirroring the existing
`production.attempt_reject_<ts>/` pattern), each attempt carrying its
own snapshot.

## field_registry

Single in-code dictionary that drives the entire [[setting_scope]]
system. Each entry keys a **dotted path** (e.g.
`"runtime.llm_orchestrator.mcp.default_model"`) to its `FieldSpec`,
which carries: `writable_at` (subset of `{project, operator, thread}`),
the field's type and validation rules (enum, min/max, regex), and an
optional `enum_source` dotted path for enums whose allowed values live
elsewhere in settings (e.g. the MCP model dropdown reads its options
from `runtime.llm_orchestrator.mcp.allowed_models`).

Glob keys (e.g. `"runtime.agent_max_rounds.*"`) cover families of
dynamically-keyed fields without one entry per role.

Registration policy is **whole-surface (D-2)**: every leaf in
`settings.json` plus `settings.local.json` plus thread-overridable
fields gets a registry entry. Fields whose `writable_at` is `["project"]`
appear in [[operator_frontend]] as read-only — the goal is "one screen
shows the full configuration of this harness", not just "the editable
parts". Population of the registry is incremental; until a field is
registered it falls back to direct settings.json reads (project-only
behaviour), so adoption can land in slices.

Resolved values, ad-hoc resolvers (`resolve_agent_model`,
`resolve_agent_budget`, `resolve_agent_max_rounds`) and the JSON Schema
that [[operator_frontend]] consumes to auto-generate forms all derive
from this single registry. No second source of truth.

## resolved_settings

In-memory dict-like view of every [[field_registry]] entry's value
after [[setting_scope]] resolution for one (thread, phase) context.
Constructed once at phase entry, written verbatim to
[[resolved_settings_snapshot]] on disk, and passed downstream in place
of the raw `settings: dict[str, Any]` argument every existing call site
takes today.

Implements `Mapping`, so existing `settings.get(...)` /
`settings["runtime"]["agent_max_rounds"]` call sites keep working
unchanged. New call sites prefer the dotted-path API
(`resolved["runtime.agent_max_rounds.grilling_agent"]`) and the
`source_of(path)` accessor for "did this come from thread, operator,
project, or default?".

`to_dict()` serialises to plain JSON for subprocess hand-off (LocalRunner,
experiment template entrypoints), so downstream code that runs in a
spawned Python interpreter does not need any new code paths.

The MCP server is stateless across threads, so it does *not* hold a
ResolvedSettings instance; every tool handler that needs settings
resolves on demand using the `thread_id` argument already present in
its request payload.

## thread_settings_file

Per-thread file `runs/threads/<thread_id>/thread_settings.json` holding
the thread-scope override values for that [[research_thread]]. Kept
separate from `thread.json` to isolate user-driven edits from
supervisor-driven runtime state — the supervisor rewrites `thread.json`
(phase status, `execute_acks[]`) on every phase transition, while the
operator may edit settings at any time through [[operator_frontend]];
splitting the files removes the race entirely and lets each side use
plain tempfile + atomic-rename writes without locks.

Shape: a flat dotted-path → value map, matching the [[field_registry]]
key shape, so write paths can patch-update a single key without
round-tripping nested dicts:

```json
{
  "runtime.llm_orchestrator.mcp.default_model": "claude-opus-4-7",
  "runtime.agent_max_rounds.grilling_agent": 12
}
```

Only fields whose `writable_at` includes `thread` may appear in this
file; any other key fails resolution-time validation.

## settings_validation

Two-layer validation for [[field_registry]] writes:

- **HTTP boundary** — the frontend `POST /settings/...` handler runs the
  field's registry schema (type / range / enum / regex) over the
  incoming payload and rejects with 400 on violation. Single-field;
  cheap; bounces obvious errors before disk touch.
- **Resolution time** — when [[resolved_settings]] is built at phase
  entry, a cross-field validator runs (e.g. `mcp.default_model` must
  lie inside `mcp.allowed_models`; `deployment`-scope claims require
  at least one `data_adapters.registered` entry). Violations refuse
  phase launch and surface in [[operator_frontend]] with the offending
  field + scope highlighted.

Silent fallback on bad values is **not** allowed — every invalid value
raises explicitly. Quietly substituting a default would hide why the
operator's edit did not take effect.

`ui_editable_in` on a FieldSpec is a subset of `writable_at` and gates
*frontend* edits only — values may still be set by hand-editing the
underlying file. Defaults to equal to `writable_at` when unspecified;
narrowed only for structurally-sensitive fields (auth policy, persona
enforcement, schema definitions) where a one-click toggle would be a
foot-gun.

## settings_ui

[[operator_frontend]] surface for editing every [[field_registry]]
entry. Three tabs — `Project` / `Operator` / `Thread` — corresponding
1:1 with [[setting_scope]] tiers. The `Thread` tab is enabled only
when a [[research_thread]] is selected from the sidebar.

Rendering strategy is **schema-driven hybrid**: a small set of Jinja2
auto-widgets (scalar string, enum, integer, float, boolean,
role-keyed dict) covers most fields by reading `FieldSpec.type`
directly. Complex fields (e.g. `data_adapters.registered`, the
`mcp.allowed_models` list editor) opt into a named `custom_widget`
template under `templates/settings/widgets/`. Adding a new field is a
one-line registry edit by default; the custom widget is a per-field
override, not the norm.

Cross-tab affordances:

- **Project tab git badge** — a small `M` indicator appears when
  `settings.json` has uncommitted changes, reminding the operator
  that other clones won't see the edit until it's committed.
- **Read-only fields** — entries whose `ui_editable_in` excludes the
  current scope render as a greyed box with a "structural — edit in
  source / git" note.
- **Thread tab inheritance model** — `Thread` shows **only the fields
  currently overridden**, not the full settings tree. A separate
  "Add override…" affordance opens a searchable field picker. This
  keeps the thread page short (95% of fields stay at project default)
  and makes "what is special about this thread?" instantly legible.
- **Resolution-time violations** — when a phase launch is refused
  due to cross-field invariant failure (see [[settings_validation]]),
  the offending field is highlighted with the violation message in a
  sticky banner that links directly to the field.

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

## external_falsifier

The harness-owned check that goal-achievement is graded against, recorded
on the `feasibility_envelope`. Its defining property (ADR 0006) is that it
is **registered before the claim is designed and owned by something other
than the production tree-search that is trying to win** — the operator, or
the supervisor's auto-bootstrap acting as the operator's stand-in. The
production run, which is the success-seeking generator, must not author its
own falsifier; that is why `registered_by` has no `worker` value.

Three kinds:

- `real_holdout` — the strong falsifier: a predicate evaluated on a
  registered `real_adapter` data source. The only kind that fully closes
  the generator-judges-itself loop.
- `cross_generator_transfer` — the weak air-gapped falsifier: the worker's
  pipeline ranking under generator A must be preserved on a held-out
  **distinct** generator B (Spearman ρ ≥ threshold). Raises the cost of
  gaming without eliminating it, because air-gapped even generator B is
  ultimately LLM-touched.
- `none` — no falsifier; the envelope's [[max_attestable_status]] is
  capped at `unverified_screen` and `achieved=true` becomes structurally
  impossible.

## max_attestable_status

Harness-**stamped** field on the `feasibility_envelope` (never
operator-writable) that records the ceiling on goal achievement for the
thread. Derived purely from [[external_falsifier]]: `goal_achieved` when a
falsifier with `kind != none` is registered, otherwise `unverified_screen`.
Derived fresh at the gate even for hand-written envelopes, so the operator
cannot widen the ceiling by stamping the field directly. The attestation
gate refuses `achieved=true` whenever this is `unverified_screen`.

## falsifier_result

The outcome of evaluating the [[external_falsifier]] predicate over
held-out data, produced by a harness module (`research_harness/falsifier.py`)
and **not** by worker experiment code. Carries `{passed, observed,
predicate, holdout_source_id, produced_by}`. The `passed` boolean is
derived by the harness from a deterministic comparison — the worker cannot
stamp it. A `passed=true` result computed against the envelope-registered
holdout (matching `holdout_source_id` and `kind`) is the **only** path to
`user_goal_attestation.achieved=true`.

## attested_status

First-class status on the `user_goal_attestation`, harness-stamped (the LLM
does not assert it):

- `goal_achieved` — `achieved=true`, backed by a passing
  [[external_falsifier]].
- `unverified_screen` — a coherent screening result with no falsifier
  registrable: the **honest air-gapped ceiling**. A recorded terminal
  outcome (`is_terminal` → `accept_with_unverified_screen`), NOT a failure
  to retry forever, and distinct from a mid-loop honest_failure.
- `not_achieved` — a falsifier is registrable but the goal is not met;
  drives more research or root diversification.

## scope_attainment

Record on the `user_goal_attestation` (ADR 0006 incentive integrity) of how
far the attested claim narrowed relative to the **frozen seed**:
`{seed_target_scope, attested_scope, narrowed}`. The seed is the
`feasibility_envelope.operator_intent.target_deploy_grade_scope`; a
`narrowed=true` win is meant to rank below a full-scope honest attempt, so
narrowing-to-win is visible rather than laundered into an indistinguishable
accept.

## premature_termination

The single disease the harness fights (ADR 0007). The system avoids the
expensive middle of research (digging) and bolts for an exit. It has two
exits, and they are the *same* disease, not two: **declare victory and quit**
(fake strength — scope-narrow, reorder a metric, manufacture a recovered
sub-claim → [[external_falsifier]] gate locks this door) and **declare
weakness and quit** (lazy honesty → the [[investigation_depth]] gate locks
this door). Locking only one door pushes the system out the other; RSTF is
what happens when the weakness door was the only one shut. The reward axis is
therefore **earned depth, not polarity**: shallow-positive and
shallow-negative both score low; deep-positive and deep-negative both score
high.

## investigation_depth

Historical audit measure of how much genuine digging a legacy thread did.
`{distinct_attempts, tree_distinct_attempts, narrowing_pivots,
killed_hypotheses, archived_attempts}` remains readable, but it no longer gates
a negative terminal. New research continues through blind sequential
reorientation until a verified strong result.

## distinct_attempt

A genuine, non-relabel research attempt that counts toward
[[investigation_depth]]. A node that *ran* (status produced evidence) and is
**not** a scope-narrowing relabel of its parent. Crucially, a narrowing pivot
(a `feasibility_narrowed` formulation, a `deploy_grade_scope` weaker than the
root's, or an `acf_feasibility` node) is **excluded** and surfaced as
`narrowing_pivots`. Collapsed-branch auto-narrowing is repackaging the same
failure smaller, not digging. The live auto-resolver path has been removed.

## load_bearing_mechanism

Field on the `user_goal_attestation` (ADR 0007): the earned explanation of why
the direction fails or what would have to be true to succeed. The harness may
retain it as a private lesson for post-generation gates. It is never a negative
terminal or an input to the next direction generator.

## adversarial_dominant_aggregation

ADR 0007 lever 0 (free): a grounded critic kill
(`critic_review.verdict_candidate == "contradicted"` or `blocking == true`)
**dominates** aggregation — it freezes the claim. A positive
`orchestrator_reduction.final_verdict` or AC `accept` is refused while any
kill is *undefeated*. A kill is **defeated on merits** only when
`orchestrator_reduction.blocking_objections` names its `critic_id` with
`defeated=true` and a substantive `defeat_rebuttal` that engages the
objection's grounds — relabeling / scope-narrowing does not count. The
harness's persona pack already generates kills; this stops them being
reconciled into a "provides" synthesis, recovering decorrelation already paid
for at zero model cost.

## verdict_strength

The referent-keyed strength of an attestation (ADR 0008), the type that makes
reality-closeness *unsayable* air-gapped. A ladder —
`internally_valid < construct_valid < transfer_valid` — where a verdict's
maximum is bounded by the strongest referent its falsifier was harness-checked
against: internal metrics → `internally_valid`; a survived funded
[[construct_adversary]] against the [[frozen_question]] → `construct_valid`; a
passing falsifier against a real referent → `transfer_valid`. The harness
DERIVES it from the [[referent_ledger]] and stamps it; the LLM cannot author
it. A verdict above the available referent is **unconstructable** — not
rejected after the fact, absent from the reachable vocabulary. `achieved=true`
requires `transfer_valid`, so it is refused on every air-gapped thread until
the operator registers a real referent.

## referent_ledger

The harness-computed record `{has_real_referent, has_construct_referent,
max_reachable_verdict}` from which [[verdict_strength]] is derived. Built only
from executed, harness-checked evidence (a passing `real_holdout`
falsifier_result; a survived [[construct_adversary]] report) — never from a
proposer assertion. This is the `unverified` typing meta-invariant in force:
proposer-controlled inputs do not raise strength.

## frozen_question

The formal question object the [[construct_adversary]] is funded against
(ADR 0008 Axis 1). Its defining property is **authorship separation**: pinned
(`pin_frozen_question`) from a pre-construction artifact (grilling) with a
provenance hash, and **immutable** — the construction cannot restate or modify
the referent it is judged against. Carries `formal_statement` and `true_iff`
(the condition under which the answer is YES, used to label each tested
world's true answer independently of whether the construction's measurement
passes).

## construct_adversary

The funded adversary that earns `construct_valid` (ADR 0008 Axis 1). Authored
to BREAK (referent = the [[frozen_question]], not the proposer's reasoning) and
with no accept authority. It searches the construction's **pass_but_wrong
region** — worlds where the measurement passes yet the frozen answer is NO —
and submits an enumerated search (`submit_construct_adversary_report`). The
verdict is harness-RE-DERIVED (`construct_adversary.py`): `survived` (→
construct_valid) only on a **funded failure** — budget spent, ≥ min distinct
worlds tested, and no breaking world; `broken` if any enumerated world is
pass-but-wrong (a self-reported `survived` that lists a break is overruled);
`invalid` if budget-0 / shallow (a certification that did not search proves
nothing). Construct validity is **necessary, not sufficient** — it says
nothing about reality.

## creativity

The target of the diversity-generation front-end under design. A research
**claim** counts as creative only when it is BOTH (a) **non-obvious** — far from
the problem's home framing, reached by the vagueness-driven diversity mechanism —
AND (b) **earned** — it survives the existing claim-gate (validity / mechanism /
necessity / falsifier / [[construct_adversary]] / AC). Generation-diversity alone
is **not** creativity: a far-but-unearned claim that fails the gate is noise.
Judgement is harness-derived (the gate), never asserted — the same "earned, not
declared" rule as [[premature_termination]]. In short: **creativity = diverse
generation × earned gate-survival.**

## abstraction (de-domained)

The single de-domained restatement of the problem **P** the diversity front-end
works from. Strips source-domain vocabulary ("stock", "market") while
**preserving P's structural skeleton** (its relational form), and is made
deliberately **vague** — under-specified enough that the skeleton admits many
readings. **Single, not multi-grain**: varying the abstraction's vagueness level
across several `A_i` was considered and dropped — diversity comes from the
**readings** of the one vague abstraction, not from the number of abstractions;
a mis-set vagueness level is caught downstream (too-vague readings fail reduction
to P, too-concrete ones fail the non-obvious bar). One skeleton per P (P's
fundamental structure is preserved, not re-framed into alternative skeletons).

## reading (field-forced)

How the diversity front-end *realizes* the diversity the vague
[[abstraction (de-domained)]] merely permits. The LLM is **forced to read the
one vague abstraction through a specific field**, and the field is chosen by
**random sampling** from a large external list — never by the LLM (asking it to
"be diverse" mode-collapses to obvious neighbours) and never by picking the
"best" field (also collapses). Each forced read yields a reading, from which a
method and a research claim emerge (**reading-first**: the field seeds the read,
the read is the LLM's act — not field-first shoehorning). Vagueness is necessary
but **not sufficient** — it lets a randomly-assigned field "take"; the random
forcing is what spreads the readings. So **diversity = vagueness (permits) ×
random field-forcing (realizes).** Random sampling continues until a target
number of readings clear [[prune-1]].

## prune-1

Cheap **P-blind** triage at the front of the diversity pipeline (before
[[reduction]] — P still firewalled). Judges a [[reading (field-forced)]] only
for **coherence against the [[abstraction (de-domained)]]**: an **independent**
LLM call must **construct the correspondence skeleton** between the forced field
and the abstraction — *demonstration*, never a self-reported confidence (a
declared value the harness rejects). **Binary + lenient**: any constructible
skeleton passes (near or far); only blatant nonsense is cut; random forcing
resamples until a quota clears it (quota = a compute budget, not statistical).
**Unreliable by acceptance**: prune-1's own LLM may hallucinate a skeleton for
nonsense — tolerated, because its errors are biased to *false-pass* (nonsense
slips to the gate, costing only compute) over *false-cut* (which would lose a
creative reading). Reliability is **not** stacked here via more LLM judges; it
lives in the executed gate. Best-effort garbage reduction — if it cuts almost
nothing, drop it and let the production validity stage triage instead.

## reduction

The mandatory **P-aware** step that turns a coherent [[reading (field-forced)]]
into a testable claim — the **construction-worker**. Given the far-domain
reading, its field↔abstraction skeleton, and **P** (the firewall lifts here), it
builds a **P-claim_contract**: maps the field's method onto P's actual
observables (a correspondence table), states `claim_under_test` in P's terms,
plus baselines / success / disproof. **Faithful, not creative** — it
*translates* the far method onto P and must preserve its far-ness (no flattening
to a generic P-approach, or the creativity evaporates); the creativity already
happened in the [[reading (field-forced)]]. **Implicit filter**: readings whose
correspondence breaks once P's specifics return die here. **Honesty (same pattern
as [[prune-1]])**: the reduction LLM can force a hollow correspondence — its
reliability is not propped up by more LLM judging; the resulting P-claim goes to
the executed gate, where a hollow mapping fails validity / mechanism / necessity
(a forced mapping has no real mechanism and won't reproduce). The professor stays
a judge; construction is separated from judgement.

## market research (per-reading, far-method)

The existing market_research role, **extended** from once-per-thread to **once
per coherent [[reading (field-forced)]]**: for each reading that clears
[[prune-1]] it researches the **far-domain method in its own field** — whether
the method exists, its canonical form, known results — so [[reduction]] has
material and the claim does not reinvent the wheel. **P-blind** (a distinct
invocation from the legacy P-aware market): the field was already chosen behind
the firewall, so showing P here would bias the method-research toward
P-convenient findings. Distinct from the P-claim's **baselines** (current_best /
naive / random for the capability gate), which are resolved later in the
production validity stage (P-aware).

## research forest + single output

After [[reduction]], each P-claim becomes the root of its own claim-tree, and
the N trees run the **existing per-claim gate** unchanged (typed decomposition
validity→capability→mechanism→necessity→boundary, critic axes, falsifier,
[[construct_adversary]], AC, [[verdict_strength]]). The multi-root **forest** is
operationally NEW — real threads have only ever been single-root; the code
scaffold for multiple roots (`seed_drafts_from_root` `root_id`, `alternative_root`
tools) exists but is unused, so it must be activated. **The final output is ONE,
not a set** ("there are no scattered papers"): of the claims that survive the
gate, the harness **selects the single strongest-earned one** — by
[[verdict_strength]] then [[investigation_depth]], a claim-native choice (not a
metric). This introduces no multiple-comparison problem: each survivor already
cleared the full gate (earned, not lucky), and a lucky-shallow survivor scores
low on earned strength so it is not chosen. Zero survivors → honest-failure; the
other survivors are search by-products, not separate papers.

## firewall (P-withholding)

The boundary that keeps diversity generation from collapsing onto P's home
framing. **P is visible** to grilling (captures P), abstraction-generation (must
see P to strip its domain), and [[reduction]] + production (P re-enters).
**P-blind** are the [[reading (field-forced)]], [[prune-1]], and
[[market research (per-reading, far-method)]] steps — they see only the
[[abstraction (de-domained)]] text. Enforced two ways: **structural** — those are
separate LLM calls whose context contains the abstraction only, never P — and a
**de-domaining scan** of the abstraction (regenerate if P's source-domain
vocabulary leaks; behavioral, not a self-report). Unlike [[prune-1]], the
firewall **must be reliable**: a prune-1 miss is absorbed by the gate, but a
firewall leak (P's domain seeping in → readings collapse to P-adjacent) is **not**
recoverable — the gate only filters, it cannot regenerate lost diversity.
