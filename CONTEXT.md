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
