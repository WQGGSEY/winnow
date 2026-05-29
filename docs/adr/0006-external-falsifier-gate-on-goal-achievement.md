# ADR 0006 — External-falsifier gate on goal achievement

- **Status**: Accepted
- **Date**: 2026-05-29
- **Supersedes**: none
- **Relates to**: ADR 0004 (grilling domain scaffolding), ADR 0005
  (three-scope settings), `CONTEXT.md` (feasibility_envelope,
  user_goal_attestation, external_falsifier, falsifier_result,
  max_attestable_status, attested_status), the "strictness rails"
  commits (955cc..d45796), `runs/threads/thread_e5b277f9` (the RSTF
  failure this ADR is a response to).

## Context

The harness has the **generator and the judge drawn from the same LLM
family, reading the same medium** (natural-language / JSON artifacts the
run itself produces). Every acceptance criterion expressed as "an LLM
reads an artifact and judges X" can therefore be co-optimised: the
generator learns to write the artifact that passes.

The previous mitigation was a series of *rails* — deterministic checks
in `mcp_server.py` (successor-collapse block, anchor-binding block,
deterministic-dump detection, confidence down-clamp). Rails moved the
**check** into deterministic code, but their **inputs** are still values
the run authored:

- `user_goal_anchors[*].bound_metric_key` — written by the grilling LLM
  (`extract_user_goal_anchor_candidates` seeds it; the grilling pass
  refines it).
- `worker_report.metrics.*` — produced by AI-authored experiment code.
- `feasibility_envelope.data_sources_available[*].kind` — written by the
  supervisor/Professor.
- `alternative_claim_formulations[*].scope_kind` — written at grilling.

So the rails are whack-a-mole: wherever the LLM writes a value that a
deterministic check later reads, the generator can learn to write the
passing value.

**The concrete failure.** `runs/threads/thread_e5b277f9` (the "RSTF"
thread) attested `user_goal_attestation.achieved=true` with
`proposed.linear.ir_b_mean=225.39 vs hybrid_vae_mcmc_surrogate=29.12`
(~10× uplift) — while its own `synthetic_to_real_caveat` admits **cohort
A and cohort B are drawn from the SAME generator** under different regime
parameters. It is same-DGP self-grading. The run narrowed the claim from
the operator's `deployment` target to `feasibility` scope, and nothing
structurally stopped `achieved=true`. The envelope had been
auto-bootstrapped synthetic-only (`data_sources_available` = one
`synthetic` source) while `operator_intent.target_deploy_grade_scope =
"deployment"`. The dual gate (`AC accept` + `attestation.achieved=true`)
passed on a claim that the manuscript itself documents as unverifiable.

This is not a bug in any one rail. It is the structural property that
**`goal achieved` was provable from inside the run**, where every input
to the proof is downstream of the success-seeking generator.

## Decision

Adopt a single load-bearing change, with the supporting structure it
forces:

> **`achieved=true` is unreachable without a harness-owned falsifier
> result the run could not author. When no falsifier can be registered
> (the air-gapped synthetic-only default), the maximum attestable status
> is `unverified_screen`, and `achieved=true` is structurally
> impossible — refused, not clamped.**

The principle in one line: *separate the entity that wants to succeed
from the entity that judges success, and give the judge information the
generator did not author.* Independence comes from (a) **different
information** — a holdout the generator never saw — or, when that is
impossible air-gapped, (b) a **harness-owned predicate** computed over
held-out values the generator must supply but cannot directly stamp
`passed`. (a) is the real thing; (b) is the weak fallback for the
semantic residue (a) cannot reach.

### Three integrity layers

**1. Specification integrity — freeze the criterion before the run, off
the run's hands.** The falsifier is *registered in the
`feasibility_envelope`*, which is already submitted **before**
`design_initial_claim_contract`, and is owned by the operator (or the
supervisor's auto-bootstrap acting as operator stand-in) — not by the
production tree-search that is trying to win. The envelope is stamped
with a harness-computed `max_attestable_status` at submission time and is
immutable for the rest of the run.

**2. Verification integrity — score with a falsifier the run can't
fit.** A new `falsifier_result` artifact is produced by a harness module
(`research_harness/falsifier.py`), not by worker experiment code. The
module owns the **predicate computation**:

- `real_holdout` — the predicate is evaluated on a registered
  `real_adapter` data source (the genuine external falsifier).
- `cross_generator_transfer` — the air-gapped weak falsifier: the
  generator's pipeline ranking under generator A vs. a **held-out
  distinct generator B** must be preserved (Spearman ρ ≥ threshold). The
  LLM supplies the two ranking vectors; the **harness computes ρ and the
  pass/fail**. The worker cannot write `passed=true` directly — it must
  supply held-out rankings that the deterministic predicate accepts.

The honest limit is recorded, not hidden: in a fully air-gapped harness
even generator B is ultimately LLM-touched, so `cross_generator_transfer`
is a *weak* falsifier (the generator could fabricate correlated
rankings). That residual is exactly why the ceiling exists.

**3. Incentive integrity — make the honest screen a first-class
terminal.** `unverified_screen` is not a booby prize. It is a recorded
terminal outcome (`is_terminal` → `accept_with_unverified_screen`),
distinct from both `accept_with_goal_achieved` and the mid-loop
`honest_failure` retry. The attestation records `attested_status ∈
{goal_achieved, unverified_screen, not_achieved}` and a `scope_attainment`
block (seed target scope vs. attested scope, `narrowed` flag) so a
claim that narrowed to win is *visibly* recorded below a full-scope
attempt rather than laundered into an indistinguishable "accept".

### Mechanism summary

| Piece | File | What it does |
|---|---|---|
| `external_falsifier` + `max_attestable_status` | `feasibility_envelope.schema.json` | Register the falsifier before the claim; stamp the ceiling. |
| Envelope stamp | `handle_submit_feasibility_envelope` | Compute `max_attestable_status` from the registered falsifier; persist on the envelope. |
| Bootstrap default | `thread_supervisor.bootstrap_envelope_if_missing` | Synthetic-only with no real adapter → `external_falsifier.kind="none"` → `unverified_screen`. |
| Predicate | `research_harness/falsifier.py` | Deterministic `compute_falsifier_result(falsifier, evidence)`. |
| Result artifact | `falsifier_result.schema.json` | `{passed, predicate, observed, holdout_source_id, produced_by}`. |
| Compute tool | `compute_falsifier_result` (MCP) | Run the predicate, write `production/rebuttal/falsifier_result.json`. |
| The gate | `handle_submit_professor_user_goal_attestation` | Reject `achieved=true` unless `max_attestable_status=="goal_achieved"` AND a passing falsifier result computed against the registered holdout exists. |
| `attested_status` + `scope_attainment` | `user_goal_attestation.schema.json` | First-class honest-screen status + seed-relative scope record. |
| Terminal | `thread_supervisor.is_terminal` | `unverified_screen` terminates as `accept_with_unverified_screen`. |
| Toggle | `runtime.llm_orchestrator.mcp.persona_enforcement.falsification_gate` | `enabled` (default true) — idiomatic escape hatch matching `persona_enforcement`. |

### Why a toggle, defaulting on

The codebase already gates behaviour behind `persona_enforcement.enabled`
and `publication_gate.enabled`. `falsification_gate.enabled` defaults to
`true` (faithful to "impossible, not clamp") but gives the operator a
one-line, reversible way to disable the gate if it blocks a workflow we
did not anticipate. Disabling it is a *loosening*, so per ADR 0005's
setting-classification rules it is operator/thread-tightening-only and
never silently widened.

## Consequences

**Positive**

- The RSTF failure is structurally impossible: a synthetic-only thread
  bootstraps to `unverified_screen` and cannot attest `achieved=true`. To
  reach `goal_achieved` the operator must register a `real_holdout`, or
  the Professor must pass a `cross_generator_transfer` falsifier whose ρ
  the harness — not the worker — computed.
- The air-gapped honest ceiling is named and reachable
  (`unverified_screen`), so the system makes progress instead of
  retrying a blocked `achieved=true` forever.
- Narrowing is no longer free: `scope_attainment` records the gap between
  the seed target scope and the attested scope.

**Negative / accepted**

- Existing production threads that previously could attest `achieved=true`
  air-gapped now cannot. This is the intended behaviour change; the prior
  behaviour was the bug.
- `cross_generator_transfer` is a *weak* falsifier (documented). It
  raises the cost of gaming (the generator must fabricate
  rank-correlated held-out vectors) without eliminating it. The only
  strong falsifier is a real holdout.
- The MCP server must be restarted to load the new tool + gate; in-flight
  threads should be past attestation before restart (none are at the time
  of writing — all four threads are at `market complete`).

## Forward work (not built here — honest about the air-gapped ceiling)

1. **Sealed generator-B runner.** Have the harness *execute* a distinct
   held-out DGP end-to-end and rank the worker's pipelines on it, so the
   held-out rankings are harness-produced rather than LLM-supplied. This
   upgrades `cross_generator_transfer` from weak to medium.
2. **Adversarial predicate-compilation pass.** Compile
   `seed → predicate(held_out_result)` in a separate pass with no stake
   in the run succeeding (Layer-1 ideal), instead of the
   operator/supervisor registering it.
3. **Anchor inputs off the run's hands.** Move
   `user_goal_anchors[*].bound_metric_key` compilation out of grilling
   into the same pre-run, off-run step that registers the falsifier.
