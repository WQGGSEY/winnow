# Sakana-v2 Mirror — Current-Model Synthesis

Written 2026-05-25 as the gate for structural mirroring of Sakana AI Scientist-v2's
BFTS package onto this harness. The synthesis records every load-bearing concept
captured during the line-by-line read of all schemas, critic markdowns, orchestrator
Python, workers/runner/publishing/memory Python, and the top-level docs. The mirror
work must preserve everything below.

## 1. The non-negotiable axis: claim-typed nodes, NOT metric values

Sakana keys node identity off `Node.metric` (a numeric score). This harness keys
identity off **node_type ∈ { capability, validity, necessity, boundary, mechanism,
constraint, taste, operational }** plus a `claim_contract` object containing
`claim_under_test`, `mandatory_baselines`, `success_criteria`, `disproof_conditions`.
That switch flows through every layer:

- `grilling_session.extracted.node_type` is set by the grilling agent and travels
  to the root node verbatim (`orchestrator/root_node_from_grilling.py`).
- Critics route by **node_type folder**, not by metric tier
  (`critics/governance.py` → `critics/by_node_type/<type>/*.md`).
- Child branches are typed (`orchestrator/child_nodes.py` →
  `VALID_CHILD_TYPES` enum) and the suggested type determines which critic
  pack reviews them next.
- The reduction layer never compares metrics across nodes; it labels each node's
  `final_verdict` ∈ { `supported_with_scope_narrowing`,
  `confounded_or_not_evaluable`, `contradicted`, `inconclusive`, ... } based on
  the worker's `claim_verdict_candidate` × critic blocking
  (`orchestrator/reduction.py`).

**Implication for the Sakana mirror:** stages cannot be re-mapped to
`stage1_preliminary / stage2_baseline_tuning / stage3_creative_research /
stage4_ablation_studies` because those are metric-improvement-stages. They must be
mapped to **claim-type-axis stages** (proposed below in §6).

## 2. The pipeline (frontend-visible phases)

```
intake → grilling → market → refine → tree_search → rebuttal → production → publish
                                        └── (live_dispatch optional, off-tree) ──┘
```

Stage-by-stage data contract:

| Phase | Producer | Schema | Persisted to |
|---|---|---|---|
| grilling | live multi-turn agent | `grilling_session.schema.json` | `runs/threads/<tid>/grilling/session.json` |
| market | sonnet + arxiv | `market_research_brief.schema.json`, `baseline_dossier.schema.json` | `memory/baselines/<dossier_id>/` |
| refine | live multi-turn agent | `refined_research_plan.schema.json`, `dataset_manifest.schema.json` | thread dir + node workspace |
| root_node | deterministic builder | `node.schema.json` | search_state.nodes[0] |
| tree_search | orchestrator loop | `search_state.schema.json` | `runs/<run>/search_state.json` |
| rebuttal | orchestrator + critics | `critic_review.schema.json` | `runs/<run>/rebuttal_*.{json,md}` |
| AC | orchestrator | `ac_decision.schema.json` | `runs/<run>/ac_decision.json` |
| publish | jinja renderers | publication_dispatch.json | `production/publication/*.html` |

## 3. The four actors and their write contracts

| Actor | May read | May write | May NOT |
|---|---|---|---|
| Orchestrator | everything | search_state, transitions, child nodes, failure memory, lessons.yaml | execute live Claude on the tree |
| Worker (Claude Code) | node workspace, claim_contract, baseline_refs (read-only) | node workspace ONLY (worker_task_result.json) | shared memory, repo, branch creation, claim mutation |
| Critic | node + worker_report | nothing (returns reviews) | mutate routing, choose itself in or out |
| Runner (LocalRunner) | job_manifest workspace | workspace stdout/stderr/result | escape workspace, run non-allowlisted executable |

Enforced by:
- `workers/worker_task.py: validate_worker_task` (scope_locks must equal node.claim_contract).
- `workers/workspace.py: ensure_path_inside` (every write path checked).
- `workers/claude_code_invoker.py` (envelope sets `scope_policy=no_self_expansion`,
  `denied_write_roots` includes `memory/`, `critics/`, repo root; unsets
  `ANTHROPIC_API_KEY` to force subscription OAuth).
- `critics/governance.py: FORBIDDEN_NODE_ROUTING_KEYS` (node JSON cannot request
  specific critics).
- `runner/local_runner.py: FORBIDDEN_TOKENS` (no shell metachars; allowlisted
  executables only).

## 4. The node state machine (11 statuses, ~10 transitions)

```
proposed → ready → running → completed_worker_report → critic_reviewed
                                         ↓
                                orchestrator_reduced
                                   ↓        ↓        ↓
                              promoted  needs_child_branch  pruned
                                            ↓
                                        (pruned)
```

Hard-coded in `orchestrator/search_state.py: ALLOWED_TRANSITIONS`. Each transition
is logged as a `node_transition` record and the frontier item's status is
updated to `queued | running | done | pruned`. Terminal statuses (`promoted`,
`pruned`, `blocked`, `failed`) cannot leave their state.

## 5. The current `run_mock_tree_search` loop (what we have today)

`orchestrator/tree_search.py: run_mock_tree_search` is a **single-threaded
sequential BFS** that:

1. Pops the highest-priority queued frontier item (priority desc, then depth, then id).
2. Marks node running, calls `LocalRunner.execute` on a job manifest derived
   from `experiment_plan_templates/<domain>/`.
3. Builds a `worker_report` from runner evidence (`runner/evidence.py`).
4. Selects critics by folder routing, runs them (read-only).
5. Builds `failure_branch_prior` from failure_memory similarity (`orchestrator/branch_prior.py`).
6. Reduces → `{promoted, needs_child_branch, pruned}`.
7. If branch, drafts typed children (parent.id + `_b01`, `_b02`, ...) and queues them.
8. Stops at `policy.max_depth + 1` steps.

**Gap vs Sakana:** Sakana runs 4 staged sub-loops with `num_workers` parallel
workers per stage and explicit per-stage iteration budgets (20/12/12/18). We
run one node per iteration, never in parallel, with no staged structure.

## 6. Proposed claim-typed staging (the mirror)

Sakana stage names → our claim-typed equivalents:

| Sakana stage | Our equivalent | Driving rule |
|---|---|---|
| 1. preliminary investigation | **scope_pinning** | Force a `validity` child if claim or baselines are underspecified, before any capability claim runs |
| 2. baseline tuning | **baseline_evidence** | Run the triad (current_best / naive / random) until `baseline_evidence_status.overall == passed` |
| 3. creative research | **mechanism_or_necessity** | Once capability is supported with scope narrowing, branch into `mechanism` (why) and `necessity` (against same-budget baselines) |
| 4. ablation studies | **boundary_ablation** | Once mechanism passes, branch into `boundary` and `constraint` children |

These stages live in an outer `AgentManager`-shaped wrapper around the existing
`run_mock_tree_search` body. The wrapper owns:

- A `Stage` dataclass `(name, claim_types_admitted, exit_predicate, max_iterations)`.
- Sub-stage transitions: `scope_pinning → baseline_evidence → mechanism_or_necessity
  → boundary_ablation`, each gated on a predicate over the search state
  (no metric thresholds; only verdict labels + child-coverage).
- Parallelism through `num_workers` queued frontier items dispatched
  concurrently within a stage. The frontier popper already sorts by priority,
  so concurrency = "pop up to N queued items, run them through `_execute_node_runner`
  in a thread pool".

## 7. Live dispatch is OFF-tree by design

`orchestrator/live_dispatch.py` + `live_reduction_ingest.py` +
`live_reduction_apply.py` form a three-step gated path:

1. **plan/execute_once**: pick one queued node, build a manual_live_smoke_plan
   (`tree_search_mutation: "forbidden"`), optionally run it manually through the
   subscription CLI.
2. **ingest**: build `live_reduction_bundle.json` from the worker_report —
   `state_mutation: "forbidden"`, `apply_required: true`.
3. **apply**: only with `--approve` flag, sha256-pin search_state, mutate state.

This must survive the Sakana mirror. The outer stage loop must dispatch only
through this gated path for live workers; never autonomously call live Claude.

## 8. The folder-routed critic pack

Every node goes through:
- `critics/always/*.md` (4 files: invariants, reproducibility_auditor, runtime_safety, senior_quant_researcher)
- `critics/by_node_type/<node.type>/*.md`
- `critics/by_domain/<node.domain>/*.md`
- `critics/by_stage/<node.stage>/*.md`

For the mirror, the new stage names (`scope_pinning`, `baseline_evidence`,
`mechanism_or_necessity`, `boundary_ablation`) need either:
- new folders under `critics/by_stage/` (preferred — folder routing is the
  invariant), or
- a deterministic alias map (e.g. `scope_pinning → by_stage/experimentation`)
  if we want to defer the new critic content.

## 9. Failure memory + branch prior

- Workers may propose `failure_record_candidate` in their report; only
  `memory/failure_memory.py: record_failure_candidate` writes it (with a
  category-validated relative path under `memory/failures/<category>/`).
- `memory/failure_retrieval.py` returns a tag-overlap-scored top-k.
- `orchestrator/branch_prior.py: build_failure_branch_prior` injects each
  retrieved failure as a `branch_suggestion(type="validity")` with a required
  control — this is how prior pain is forced into child branches.
- `orchestrator/reduction.py` accepts the branch_prior and merges it with
  worker observations + baseline failures into `child_branch_suggestions`.

In the Sakana mirror, this same mechanism becomes the source of `debug_prob`-style
"go back and fix what broke before" branches.

## 10. Search policy (configs/harness.yaml)

```yaml
agent:
  num_workers: 3           # NOT YET HONORED — current loop is sequential
  steps: 12
  num_seeds: 3
search:
  max_depth: 5
  max_debug_depth: 2
  debug_prob: 0.25          # NOT YET HONORED — no debug-vs-improve branching
  num_drafts: 3             # NOT YET HONORED — root has 1 draft
  sunk_cost_policy: progress_gated
  scaleup_policy: disallow_by_default
harness_semantics:
  require_claim_contract: true
  baseline_policy: web_current_best_naive_random
  worker_scope_policy: no_self_expansion
```

The mirror must wire `num_workers`, `debug_prob`, `num_drafts` into the new
stage loop. `worker_scope_policy: no_self_expansion` must remain inviolate
(already enforced by `invocation_envelope.scope_policy` enum).

## 11. AC gate (publication boundary)

`publishing/ac.py: decide_acceptance` is keyed on `critic_reviews` + settings
thresholds. Six axes: novelty, validity, necessity, clarity, reproducibility,
taste_alignment. Output decision ∈ { accept, revise, reject } with
`required_next_search_nodes` populated when blocking objections exist — these
are claim-typed child suggestions, so a `revise` decision feeds straight back
into the tree without metric comparisons.

## 12. What we are NOT changing

- Schemas (every JSON Schema file under `research_harness/schemas/`).
- The four-actor write contract.
- Folder-routed critic governance.
- The grilling → market → refine → root_node ingestion chain.
- Live-dispatch's three-step approval pipeline.
- Memory writes (only `memory/*.py` may write `memory/failures/`, `lessons.yaml`,
  `memory/baselines/`).
- Frontend semantics, retry endpoints, in-process FastAPI design.

## 13. Concrete next steps (after this synthesis is approved)

1. Create `research_harness/orchestrator/treesearch/` with `agent_manager.py`,
   `parallel_agent.py`, `journal.py`, `interpreter.py` mirroring Sakana's
   package layout — but each file owns claim-typed semantics, not metric-typed.
2. Move the current `run_mock_tree_search` body into
   `treesearch/parallel_agent.py: ParallelAgent.run_one_step` so a Stage can
   call it inside its iteration budget.
3. Add `Stage` dataclass + `AgentManager` outer loop with the four claim-typed
   stages and explicit exit predicates.
4. Wire `num_workers` via a `ThreadPoolExecutor` over the frontier popper.
5. Create new `critics/by_stage/{scope_pinning,baseline_evidence,
   mechanism_or_necessity,boundary_ablation}/` folders (or set an alias).
6. Make `production_runner` call the new manager; keep the mock backend as the
   sole executor (live remains gated).

Each step keeps every existing test green; the mirror is a structural
rearrangement plus a stage wrapper, not a rewrite.
