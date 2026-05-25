# ADR 0004 — grilling-time domain scaffolding (AI-generated templates)

- **Status**: Accepted
- **Date**: 2026-05-25
- **Supersedes**: none
- **Relates to**: ADR 0001 (research_refiner 3-action protocol),
  `CONTEXT.md` (domain_scaffolding, grilling action,
  experiment_plan_templates, research_thread)

## Context

The harness routes every research to a domain-specific
`experiment_plan_templates/<domain>/` folder that holds `plan.json`
plus a `src/` Python tree (entrypoint, proposed, baselines, eval). The
grilling agent picks a domain from the enum produced by
`list_available_domains()` and is forbidden from inventing one
(`_coerce_extracted` raises if the extracted domain is off-enum).

This is the right safety choice when the operator has already authored
every domain they care about. It breaks down when the operator wants to
explore a new research area: they would have to (1) drop out of the
frontend, (2) hand-author a complete domain folder including
`baseline_evidence_requirements` and a working entrypoint, (3) return
to the frontend and restart grilling. The user-facing promise of "the
grilling agent decides everything" leaks at exactly this moment.

The operator asked the harness to scaffold a new domain
*during* grilling when no existing domain matches. Two axes of choice
mattered:

1. **How complete is the scaffolded output?** Empty stub (operator
   fills in later), `plan.json` only, or full Python tree?
2. **How does the agent emit it?** One large DONE payload, an iterative
   action loop with per-file validation, or a separate scaffolder
   agent?

## Decision

The grilling agent generates a **complete domain** (option C in the
design grilling) — `plan.json` plus the full `src/` tree — via a new
**`PROPOSE_FILE`** action that follows the same iterative-validation
pattern ADR 0001 introduced for refiner's `PROPOSE_DATASET`.

Concretely:

- The existing grilling protocol expands from `ASK | DONE` to
  `ASK | PROPOSE_FILE | DONE`.
- A new "deep interview" sub-mode of grilling activates *only* when the
  agent (with explicit user opt-in) decides no enum domain matches.
  Sub-mode is gated on user confirmation; the agent cannot enter it
  unilaterally.
- In deep mode, the agent walks a structured checklist (domain_slug,
  task_class, data_source, proposed_method_spec, baseline_specs,
  metric_spec, resource_budget, expected_outputs) before proposing any
  files. Checklist guidance lives in the system prompt; the only
  hard-enforced post-checklist invariant is the **manifest** of 10
  required files (`plan.json` + `src/__init__.py` +
  `src/experiment.py` + `src/proposed.py` + `src/data.py` +
  `src/baselines/{__init__,current_best,naive,random_baseline}.py` +
  `src/eval/{__init__,<metric>}.py`).
- Each `PROPOSE_FILE` is staged under
  `runs/threads/<thread_id>/grilling/scaffold_staging/<slug>/` and
  validated immediately: `ast.parse` for Python, JSON-schema
  (`user_experiment_plan_metadata`) for `plan.json`. The result
  (`ok | failed`, error detail, manifest progress) is injected into
  the next round so the agent can fix in place.
- `DONE` triggers a final `importlib`-based dry-import of the staged
  `src/` tree. Only if every required manifest entry is present **and**
  the dry-import succeeds does the harness `os.rename` the staging
  directory to `experiment_plan_templates/<slug>/` and finalize the
  grilling session with `extracted.domain = "<slug>"`. A failed
  dry-import rejects the DONE; the agent emits more `PROPOSE_FILE`
  rounds to repair.
- `max_rounds` is auto-promoted from 8 to 30 when deep mode activates,
  reflecting the higher round budget needed for ~10 manifest files
  plus retries.
- Every scaffolded domain carries a `.scaffold_origin.json` marker file
  so future audits can distinguish AI-generated from operator-authored
  templates. `list_available_domains` ignores the marker — routing is
  unaffected.

The operator-facing UI exposes the mode transition transparently: the
[[research_thread]]'s domain badge stays `null` (rendered as
`✎ scaffolding` italic) for the duration of the deep interview and
flips atomically to the new slug on successful finalize.

## Consequences

**Accepted trade-offs**

- AI-generated Python can be semantically wrong even when it parses and
  imports — a fake-metric implementation can produce supported-looking
  evidence. The mitigations:
  1. **Mandatory deep interview** — option C cannot be reached without
     the agent collecting the full checklist; shortcut path is
     impossible by design.
  2. **Manifest enforcement** — the harness rejects any DONE that
     doesn't carry exactly the canonical file set; the agent can't
     "forget" a baseline.
  3. **Dry-import gate** — fails closed (no finalize) on broken
     imports. Catches structural breakage, not semantic.
  4. **Existing `evidence_is_fake` guard at publication** — already
     refuses to publish when the production run falls back to
     `_fallback_demo`. A scaffolded domain that misbehaves at run time
     will still trigger that gate via `baseline_evidence_requirements`
     failures.
- The grilling agent's responsibility surface grows. We considered a
  separate `domain_scaffolder` agent (option C in Q2) and rejected it:
  the user's mental model is "grilling builds the domain", an extra
  agent would mean another system prompt + ack gate + CLI + tests for
  marginal SRP benefit on a single-operator project.

**Atomicity**

- Staging→final is a single `os.rename` on the same filesystem
  (the repo). On the rare cross-mount case we fall back to
  `copytree → rename → rmtree`; only the inner `rename` is atomic so
  no partially-populated final directory is ever observable by
  `list_available_domains`.
- Mid-flight crashes leave the staging directory intact and
  `grilling_session.json` with a `scaffold_state` that lists every
  validated file. Resume seeds the agent with the prior rounds and
  continues from the last validated file.
- A `scaffold_failed` thread retains the staging directory for the
  operator to inspect; the harness does not auto-clean.

**Rejected alternatives**

- **Option A (empty scaffold)** — punts the work back to the operator.
  Defeats the user-facing automation promise.
- **Option B (plan.json only)** — same punt for `src/`. Real value comes
  from getting to a runnable Python tree.
- **Single-shot DONE with all files** — no per-file feedback loop.
  AI emits 10 files, one syntax error, harness rejects DONE wholesale,
  agent has no localized error to fix. Refiner's `PROPOSE_DATASET`
  pattern already demonstrated the per-step value.
- **Separate `domain_scaffolder` agent** — duplicated infrastructure
  (system prompt, ack gates, CLI surface, tests) without SRP benefit
  on a single-operator project.

## Operator usage note

`/abandon` on a scaffolding thread cancels the agent, releases the
[[single_active_run]] lock, marks `scaffold_state: scaffold_failed`,
and **keeps the staging directory** under `runs/threads/<tid>/`. The
operator can inspect the partial output and either hand-finish the
domain or delete the staging dir to start over.
