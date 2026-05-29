# ADR 0005 — Three-scope settings with per-phase snapshot resolution

- **Status**: Accepted
- **Date**: 2026-05-28
- **Supersedes**: none
- **Relates to**: ADR 0002 (operator_frontend in-process), ADR 0003
  (operator_frontend stack), `CONTEXT.md` (setting_scope,
  setting_classification, field_registry, resolved_settings,
  resolved_settings_snapshot, thread_settings_file, settings_validation,
  settings_ui)

## Context

The harness's configuration surface has outgrown a single `settings.json`
file. Three observations forced the rethink:

1. **The same field has different "natural homes" for different values.**
   `runtime.llm_orchestrator.mcp.default_model` should have a project
   default (committed) and a per-thread override (chosen by the operator
   when starting a specific research arc). `data_adapters.registered`
   describes what is installed on *this* machine, not what every clone
   of the repo can see. `frontend.subscription_ack_at` is a consent
   timestamp meaningful only for one user on one machine. Forcing all
   three into one file blurs git-tracked vs gitignored vs per-thread
   semantics.
2. **No first-class read path exists for thread-scoped configuration.**
   `thread.json` snapshots `title` / `domain` / `user_goal` at grilling
   completion, but never records "this thread chose Opus 4.7" or "this
   thread tightened the AC reproducibility threshold." When the harness
   produces a publication artifact, the reproducibility section cannot
   honestly answer "which model produced this phase's output?" because
   that information was never written down.
3. **The frontend cannot drive what it cannot read.** `settings.json`
   is touched directly by ~22 files via `settings.get("runtime",
   {}).get(...)` chains, each with its own ad-hoc fallback logic
   (`resolve_agent_model`, `resolve_agent_budget`,
   `resolve_agent_max_rounds`). Exposing settings to the operator
   frontend without a unified resolver would either fork the validation
   surface or require touching all 22 sites at once.

The operator's stated goal is to expose **every** setting in the
frontend and to make explicit which settings apply globally vs which
apply per-research-thread.

## Decision

The harness adopts a **three-scope settings system** with a
**registry-driven resolver** and **per-phase resolution snapshots**.

### 1. Three disjoint scopes

| Scope | File | Git | Question it answers |
|---|---|---|---|
| `project` | `settings.json` | tracked | What kind of research does this harness recognise? |
| `operator` | `settings.local.json` | gitignored | What can *this machine* / *this user* actually do? |
| `thread` | `runs/threads/<thread_id>/thread_settings.json` | per-repo policy on `runs/` | What did *this research arc* choose? |

Resolution order is **project → operator → thread**, with later scopes
overriding earlier ones for the same field. Arrays are whole-replace
(no concat). Each field declares which scopes are allowed to write it
via a `writable_at` permission list.

Classification principle: `project = structural`, `operator =
environmental`, `thread = experimental`. Tightening safety gates at a
narrower scope is always allowed; loosening is never.

### 2. FIELD_REGISTRY as single source of truth

A single in-code dictionary `FIELD_REGISTRY: dict[str, FieldSpec]` keys
**dotted paths** (e.g. `"runtime.llm_orchestrator.mcp.default_model"`)
to `FieldSpec` records carrying `writable_at`, `ui_editable_in`, the
field's type and validation rules, optional `enum_source` (for
cross-referenced enum values like the MCP model dropdown), and an
optional `custom_widget` name. Glob keys
(`"runtime.agent_max_rounds.*"`) cover dynamically-keyed families.

Registration is **whole-surface**: every leaf in `settings.json` and
`settings.local.json` gets an entry, including project-only fields,
which appear in the UI as read-only. Population is incremental — until
a field is registered, the existing `load_settings()` path remains the
fallback, so adoption ships in slices.

The JSON Schema exported by `export_json_schema(FIELD_REGISTRY)` is
what the frontend consumes to render forms. There is no second source
of truth.

### 3. Per-phase resolution snapshot

Thread-scoped settings remain **mutable between phases** but a phase's
view of the settings is **frozen at phase entry**. At phase start the
supervisor (or frontend phase launcher) constructs a `ResolvedSettings`
object — a `Mapping` view over the resolved values — and writes
`runs/threads/<thread_id>/<phase>/resolved_settings_snapshot.json`
recording each field as `{value, source}` where `source ∈ {project,
operator, thread, schema_default}`. The same object is passed
downstream as the `settings` argument that existing call sites already
accept (`dict[str, Any]` interface preserved via `Mapping`).

Retroactive edits to past snapshots are disallowed. Re-running a phase
under different settings creates a sibling attempt directory
(`<phase>.attempt2/`), each attempt carrying its own snapshot.

### 4. Two-layer validation, no silent fallback

- **HTTP boundary**: `POST /settings/{scope}` runs the field's registry
  schema (type / range / enum / regex) and rejects with 400 on
  violation.
- **Resolution time**: when `ResolvedSettings` is constructed at phase
  entry, a cross-field validator runs (e.g. `mcp.default_model ∈
  mcp.allowed_models`; `deployment` scope claims require ≥1
  `data_adapters.registered` entry). Violations refuse phase launch
  and surface in the UI with the offending field highlighted.
- **Silent fallback on bad values is forbidden** — invalid input raises
  explicitly, matching the existing `ConfigError`-fast pattern.

### 5. Thread settings live in a separate file

Thread-scope overrides live in `runs/threads/<thread_id>/thread_settings.json`,
**not** inside `thread.json`. `thread.json` is rewritten by the
supervisor on every phase transition (status, `execute_acks[]`); the
frontend writes to it would race. Splitting the files lets each side
use plain tempfile + atomic-rename writes without locks.

Shape is a flat dotted-path map matching `FIELD_REGISTRY` keys:

```json
{
  "runtime.llm_orchestrator.mcp.default_model": "claude-opus-4-7",
  "runtime.agent_max_rounds.grilling_agent": 12
}
```

### 6. Settings UI: schema-driven hybrid

Three tabs (`Project` / `Operator` / `Thread`) in the operator
frontend, rendering forms from `FIELD_REGISTRY` via Jinja2 macros for
the common types (scalar string, enum, integer, float, boolean,
role-keyed dict). Complex fields (`data_adapters.registered`,
`mcp.allowed_models` editor) opt into a named `custom_widget` template.
The `Thread` tab shows **only currently-overridden fields** plus an
"Add override…" picker — not the full tree — because most thread
settings fall through to project default.

`ui_editable_in` (subset of `writable_at`) gates *frontend* edits;
structural fields like `auth_policy.*` and
`mcp.persona_enforcement.*` are visible but read-only in the UI even
though they live at project scope.

## Consequences

**Accepted trade-offs**

- One config decision now writes to **three files** instead of one.
  This is the intended cost of separating git-tracked from
  machine-local from per-thread semantics.
- The resolved value of a field depends on which thread is active.
  Code that reads settings must either accept a `ResolvedSettings`
  argument (the long-term shape) or continue calling `load_settings()`
  to get project-only behaviour (the transitional shape). Both
  coexist during incremental migration.
- The supervisor / frontend phase launcher / MCP server gain a new
  invariant: every phase entry must construct a `ResolvedSettings`
  and write its snapshot before any agent code runs. This is the
  load-bearing step that makes the audit trail trustworthy.
- The MCP server is stateless across threads, so its tool handlers
  resolve on demand using the `thread_id` already in their request
  payload — no server-wide settings cache.

**Migration path**

- Phase 1: introduce `FIELD_REGISTRY`, `ResolvedSettings`,
  `resolved_settings_snapshot.json` writes. Keep existing
  `load_settings()` and the three ad-hoc resolvers
  (`resolve_agent_model`, `resolve_agent_budget`,
  `resolve_agent_max_rounds`) untouched.
- Phase 2: rewire `resolve_agent_*` to delegate to
  `resolved.get("runtime.agent_*.<role>")` — same fallback chain,
  same return type, now scope-aware.
- Phase 3: introduce the settings UI tabs in the frontend.
- Phase 4: opportunistically migrate the remaining call sites from
  `settings.get(...)` chains to dotted-path access as files are
  touched for other reasons.

There is **no big-bang cutover**. Existing threads (e.g.
`thread_3ae5ec30`) work unchanged because the absence of
`thread_settings.json` resolves to "no thread overrides" and the system
falls through to project/operator just as today.

## Considered Options

**Rejected: single `settings.json` with `_meta` blocks**

- Extending the existing `_comment`/`_example` pattern bloats the
  human-edited file and produces noisy git diffs. Schema metadata
  ends up interleaved with values at every nesting level.

**Rejected: separate `settings.schema.json` (JSON Schema standard)**

- A hand-written JSON Schema covering the full surface is more verbose
  than a Python dict, and the two files inevitably drift. Auto-exported
  JSON Schema from `FIELD_REGISTRY` retains the standard's UI benefits
  without the maintenance cost.

**Rejected: Pydantic / typed settings model**

- Cleanest from a static-typing perspective but requires migrating
  every `settings: dict[str, Any]` consumer in one go. Defers without
  precluding: `FIELD_REGISTRY` → Pydantic graduation is a mechanical
  one-to-one conversion if the static-typing benefit later outweighs
  the cost.

**Rejected: partition fields by scope (each field lives in exactly one
file)**

- Forces the operator to pick a model for every new thread (no project
  default to inherit from). Eliminates the "tighten threshold for this
  thread only" pattern. Mismatches the existing
  `mcp.default_model._comment` that already advertises per-thread
  override.

**Rejected: thread settings inside `thread.json`**

- Supervisor rewrites of `thread.json` during phase transitions race
  with operator edits from the frontend. File-level isolation
  eliminates the race; the cost is one extra file per thread.

**Rejected: lock thread settings at thread creation (immutable
thereafter)**

- Safer for reproducibility but eliminates mid-flight tuning, which
  is a common operator move ("this thread needs a bigger budget").
  Per-phase snapshots recover the reproducibility guarantee without
  the UX cost.

**Rejected: silently fall back to default on invalid values**

- Hides the operator's intent. If the UI accepts an edit and then the
  resolver discards it without warning, the operator cannot tell what
  the system is actually running with. Explicit raise is consistent
  with the rest of the harness (`ConfigError` at boot).
