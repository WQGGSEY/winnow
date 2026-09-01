# Pipeline (current)

Authoritative end-to-end flow for **new threads**. Source of truth:
`frontend/threads.py: PHASES`. This supersedes the phase diagram in
`SAKANA_MIRROR_DESIGN.md §2` (pre-connector) and the
`intake / refine / tree_search / rebuttal / publish` vocabulary in older docs.

```
grilling  →  connector  →  SolutionContract  →  one blind direction at a time
                                                →  verified strong result
```

`PHASES = ("grilling", "connector", "production")`. `market` is a **legacy**
token (`LEGACY_PHASES`) kept only so pre-connector threads still validate.
The connector now performs both P-blind far-method research and P-aware
baseline research before production.
`refine` / `research_refiner` is **removed** (`frontend/server.py`: "refine
phase removed; Professor replaces placeholders at production entry").

---

## 1. grilling

Multi-turn live agent grills the funder's problem **P** into a structured
`grilling_session.json`. The **frozen question** is pinned here
(`pin_frozen_question`, ADR 0008) — the immutable referent a construct
adversary is later funded against. The construction can never restate or
modify the referent it is judged by.

## 2. connector  (ADR 0012 — vagueness-driven diverse claim generation)

The creative **generation front-end** (replaces `refine`). It researches
far-framed ways to understand P and records candidate claim contracts. Code:
`connector/orchestrator.py: run_domain_connector` → `connector_session.json`.

```
abstraction (P-visible):  P → vague, de-domained skeleton; a firewall scans
                          for leaked domain terms (firewall_clean)
   ⟂ firewall — below here P-BLIND, abstraction text only ⟂
for each randomly-sampled external field
      (uniform, no replacement, seed = hash(grilling_session_id)):
   reading   (P-blind):   read the skeleton THROUGH that field
                          → field_method + emergent_claim
   prune-1   (P-blind):   cheap lenient coherence triage; fail → skip field
   far market (P-blind):  research that field's real methods   ← old "market"
                          (far_method_market.py)                  lives here now
   reduction (P-AWARE):   bring P back → construct a real claim_contract; keep
stop at quota (default 6) / max_fields_tried (default 40) / namespace exhausted
       (recorded in stopped_reason — never silently swallowed)
P-aware baseline research: retrieve papers, download available sources, and
                           write the baseline dossier used by SolutionContract
```

**Why the firewall.** The borrowed method is forced from a *random* field and
researched *blind to P*, so it cannot be bent to fit P prematurely
(co-optimization). Diversity = vagueness (admits readings) × random external
field-forcing. The loop is best-effort and may emit garbage — **reliability is
the production gate's job, not this loop's.** Generation and judging are
separated by design (ADR 0012 thesis: *generation + gate*).

It is a **live** step (Codex CLI calls), gated by
`RESEARCH_HARNESS_ALLOW_CODEX_LIVE` / `RESEARCH_HARNESS_EXECUTE_CODEX_LIVE`.

## 3. production (blind sequential reorientation)

Thread setup freezes the accepted intake handoff, feasibility envelope,
success criteria, baselines, disproof conditions, safety limits, and real
holdout into one immutable `SolutionContract`. Connector alternatives remain
audit context and never become live roots.

`advance_research` is the only direction-control operation. Exactly one
`DirectionAttempt` may be active. A generation request contains only the
frozen contract and one harness-sampled random perspective. It never contains
the failed node, failure prose, private lessons, acquired resources, or prior
directions. After generation, private novelty and safety gates compare the
six-axis fingerprint against closed attempts.

```
advance_research
  → direction_ready
  → acquisition_running | checkpointed
  → awaiting_evidence
  → design_experiment_template
  → submit_grad_student_review
  → execute_node_experiment
  → run_critic_reviews
  → submit_professor_decision (promoted | pruned)
  → harness evidence verdict
       conclusive_failure → close privately → advance_research
       needs_more_evidence → continue the same attempt
       needs_data → acquire or checkpoint
       strong_candidate → construct + falsifier + rebuttal gates
```

Public acquisition may use registered adapters, public APIs, public pages, and
robots-compliant crawling. It records provenance and resumes bounded requests,
downloads, wall time, and cost from checkpoints. It cannot create accounts,
spend money, bypass access control, or bypass paywalls.

Publication still requires construct-adversary evidence, a passing harness-owned
real-holdout falsifier, rebuttal acceptance, and a verified user-goal
attestation. Only that receipt can commit `goal_achieved` and allow
`render_final_paper`. Negative evidence has no publication terminal.

The production supervisor starts a fresh Codex JSONL session for each cycle.
It passes the repository MCP command, arguments, and environment through
per-invocation `-c mcp_servers.research_harness...` options, so production does
not depend on global MCP registration. Codex uses `--approve-for-me` on this
path because the observed `never` policy rejects MCP calls. Supervisor retry,
lock, terminal, watchdog, and process-group cleanup policy remains local to
`thread_supervisor.py`.

---

## Verdict-strength ceiling (ADR 0008)

A verdict's strength is bounded by the strongest referent its falsifier was
actually run (and harness-checked) against. The harness *derives* it from the
referent ledger (`verdict_strength.py`); the LLM never authors it.

```
internally_valid   <   construct_valid        <   transfer_valid
run's own metrics      funded construct-           real-referent falsifier
(floor)                adversary survived           ran and passed
                       air-gapped: earnable        air-gapped: UNREACHABLE
```

`transfer_valid` (= `goal_achieved`, i.e. usable for the user's *real*
decision) is **unconstructable air-gapped** — the system is constitutionally
silent about the gap it cannot measure. The only entrance is a **single real
scalar the operator registers**: `compute_falsifier_result(evidence={observed})`
with a `real_holdout` falsifier whose `holdout_source_id` is a registered
`real_adapter` (`settings.json.data_adapters.registered`). The harness trusts
that scalar (air-gapped — it cannot verify it) and owns only the frozen
predicate and the deterministic pass/fail. See ADR 0006, 0007, 0008.
