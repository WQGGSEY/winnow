# Pipeline (current)

Authoritative end-to-end flow for **new threads**. Source of truth:
`frontend/threads.py: PHASES`. This supersedes the phase diagram in
`SAKANA_MIRROR_DESIGN.md §2` (pre-connector) and the
`intake / refine / tree_search / rebuttal / publish` vocabulary in older docs.

```
grilling  →  connector  →  production (forest)  →  strongest survivor
```

`PHASES = ("grilling", "connector", "production")`. `market` is a **legacy**
token (`LEGACY_PHASES`) kept only so pre-connector threads still validate —
**the connector folds market in** (per-reading far-method research, P-blind).
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

The creative **generation front-end** (replaces `refine`). Turns P into N
diverse, far-framed `claim_contract`s — the forest seed. Code:
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
```

**Why the firewall.** The borrowed method is forced from a *random* field and
researched *blind to P*, so it cannot be bent to fit P prematurely
(co-optimization). Diversity = vagueness (admits readings) × random external
field-forcing. The loop is best-effort and may emit garbage — **reliability is
the production gate's job, not this loop's.** Generation and judging are
separated by design (ADR 0012 thesis: *generation + gate*).

It is a **live** step (subscription `claude` calls), gated by
`RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE` / `RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE`.

## 3. production  (forest)

`seed_forest_from_connector` (MCP) turns the connector's N claims into an
**N-root forest** (multi-root `search_state`). Each root runs the same
per-node claim gate; `select_strongest_survivor` picks the single earned
output once every survivor root is snapshotted (`snapshot_root_terminal`).
Single-root threads never call select.

Thread-level setup (once): `submit_feasibility_envelope` (pre-claim,
immutable; declares `data_sources_available`, `llm_oracles_available`,
`compute_budget`, `operator_intent` scope, and the `external_falsifier` —
harness stamps `max_attestable_status`, ADR 0006) and the pinned frozen
question. Then per root:

```
design_initial_claim_contract        claim_under_test + mandatory_baselines
                                      + measurable success + hittable disproof
loop (one node at a time, deterministic get_next_admissible_node), through
     claim-typed stages scope_pinning → baseline_evidence →
     mechanism_or_necessity → boundary_ablation:
   design_experiment_template → submit_grad_student_review →
   execute_node_experiment → run_critic_reviews → submit_professor_decision
                                                   (promote / branch / prune)
submit_construct_adversary_report     funded adversary vs the frozen question;
                                      survives → construct_valid (ADR 0008)
compute_falsifier_result              harness-owned predicate over held-out
                                      evidence (the worker cannot stamp passed)
submit_professor_user_goal_attestation  achieved=true refused unless a passing
                                      real_holdout result exists
snapshot_root_terminal
```

Publication gate: `prepare_rebuttal_packet → submit_rebuttal_critic_review →
submit_orchestrator_reduction → submit_ac_decision` → `render_final_paper`
or `render_honest_failure_paper`.

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
