# ADR 0001 — research_refiner agent and dataset manifest cache

- **Status**: Accepted
- **Date**: 2026-05-25
- **Supersedes**: none
- **Relates to**: `CONTEXT.md` (dataset, research_refiner, refined_research_plan, dataset materializer, dataset_manifest, sota_reconciliation, acknowledged_limitation, refiner action)

## Context

The pipeline before this decision ran:

```
grilling → market_research → root_node_from_grilling → production_runner
```

`grilling` produced a broad claim contract; `market_research` retrieved
relevant papers and a baseline dossier candidate. The `root_node` was then
built directly from the grilling output and handed to the production runner.

Three gaps surfaced during operator review:

1. **No validation-procedure layer.** The grilled claim was sharp enough to
   route critics but not sharp enough to test. The metric, threshold,
   evaluation splits, statistical test, number of seeds, and decision rule
   were never explicitly chosen — they came out as boilerplate in the demo
   template and were not interrogated against the user's intent.
2. **No SOTA reconciliation.** `market_research` produced a baseline analysis
   but nothing forced the user's stated goal to be compared against it. A
   user who claimed novelty over a baseline that was already beaten in a
   recent paper would not find that out until rebuttal-stage critics.
3. **Dataset assumed, not resolved.** "Dataset" was implicitly the local
   toy data baked into the template. Real research depends on a wider class
   of external artifacts — training corpora, evaluation benchmarks,
   pretrained model weights (e.g. ResNet), quant alpha / factor sets,
   internal team-bucket artifacts. The harness had no machinery to decide
   which datasets a node needs, to fetch them, or to fall back to synthetic
   data when nothing public matches.

Without filling these three gaps, every production run risks publishing a
plausible-looking artifact that either compares against the wrong baseline
or rests on data that the user never confirmed.

## Decision

Introduce a new agent and an artifact / cache pattern between
`market_research` and `root_node`.

1. **New agent: `research_refiner`.** Agent-level (tools allowed, not bound
   by worker invariants). Runs after `market_research`, before `root_node`
   generation. Skippable in prototyping via a CLI flag; required by default
   in the production runner.
2. **New artifact: `refined_research_plan`.** Immutable. Downstream
   `root_node` reads this when present, falls back to `grilling_session`
   when refiner was skipped. Carries:
   - the sharpened claim and the `validation_procedure`
     (primary metric, operator, threshold, splits, statistical test, seeds,
     decision rule);
   - `dataset_specs[]` — every external artifact the experiment depends on,
     typed (`raw_data | benchmark | model_weights | factor_set | synthetic
     | custom`);
   - `sota_reconciliations[]` — every conflict found against the market
     research baseline analysis, plus the user-chosen resolution
     (`narrow_claim | change_baseline | acknowledge_limitation`);
   - `acknowledged_limitations[]` — explicit scope restrictions the user
     accepts rather than fixing.
3. **3-action interview protocol** (`ASK | PROPOSE_DATASET | DONE`).
   `PROPOSE_DATASET` is silent: harness invokes the deterministic
   materializer, injects the success / failure into the next round's
   transcript without a user round-trip. `DONE` is only valid when every
   listed `dataset_spec` has been verified by a successful
   `PROPOSE_DATASET` in this session (or explicitly recorded under
   `unresolved_dataset_specs` after max_rounds).
4. **Dataset materializer Protocol + per-type fetchers.** One Protocol
   (`try_materialize(spec, cache_root) -> MaterializeResult`), one fetcher
   per type, optional dependencies. HuggingFace and Torch Hub reuse their
   own caches; local / S3 / synthetic write to repo-local
   `.dataset_cache/<type>/<id>/`.
5. **Per-node `dataset_manifest.json`.** The node workspace receives only
   a manifest mapping each `dataset_spec.id` to its materialized path. The
   template `src/` code reads the manifest at runtime. Multi-GB datasets
   are never duplicated across node workspaces.
6. **SOTA conflict detection is LLM-driven.** Refiner's system prompt
   includes the `baseline_analysis_md` and instructs it to compare the
   grilled claim against every SOTA mention, surface each conflict via
   `ASK`, and offer the three named reconciliation paths. The critic layer
   (`senior_quant_researcher`, `claim_skeptic`) then verifies that
   `sota_reconciliations` is non-empty when the analysis contains strong
   SOTA evidence.

## Consequences

### Positive

- The harness owns the **decision** layer (claim sharpening, baseline
  reconciliation, dataset choice) explicitly and auditable, rather than
  leaking those decisions into the template or the publication artifact.
- `evidence_is_fake` guard now extends naturally to dataset coverage: a
  template that runs without a materialized manifest is detectable.
- Adding a new dataset type means writing one materializer class — the
  refiner protocol and downstream consumers do not change.
- Synthetic data is first-class: a `dataset_spec` with `type="synthetic"`
  carries the distribution / dimensions / seed that critics need to assess
  reproducibility, instead of those parameters living unverified inside
  template code.
- Quick-prototype mode (skip refiner) is preserved.

### Negative

- One more multi-turn live-sonnet step in the production path. Cost goes
  up by one agent's worth per research run. Mitigated by `agent_models`
  per-role selection — operator can pick a cheaper model for the refiner.
- `dataset_manifest.json` uses absolute `materialized_path`. Moving a node
  workspace to a different machine breaks the references. Acceptable
  because the deterministic runner is already single-machine; revisit when
  a distributed runner is added.
- Template `src/` code now has a hard contract with the manifest format —
  changing the manifest schema is a breaking change for every template.

### Neutral

- The original `dataset_loader` name (operator's first phrasing) was
  retired in favor of `research_refiner` because the role is broader than
  loading and because `*_finetuner` would have collided with model
  fine-tuning vocabulary.
- The fetcher Protocol intentionally does not include a `download_async`
  variant. Datasets are large but the refiner's loop is naturally
  sequential (one PROPOSE_DATASET per round). Revisit if a benchmark suite
  requires dozens of small datasets in parallel.

## Alternatives considered

- **Skip the agent, extend `market_research` to also sharpen the plan.**
  Rejected: `market_research` already mixes paper search with dossier
  generation; adding interview and dataset decision would make it three
  unrelated jobs in one module.
- **Per-node `dataset_loader` triggered inside tree search.** Rejected:
  reintroduces nested agency, which the original grilling-log decision
  explicitly forbids.
- **Mutate `grilling_session.extracted` in place to record refinement.**
  Rejected: destroys the audit trail. The session must remain immutable.
- **Two-action protocol (ASK / DONE), separate fetch phase after DONE.**
  Rejected: a failed fetch after DONE forces an outer loop back to
  interview phase, which is harder to reason about than interleaving fetch
  verification with the interview.
- **Single global dataset cache under `~/.research_harness_datasets/`.**
  Rejected: would re-copy what HuggingFace and Torch Hub already cache
  natively, and would couple the harness to the user's home directory.
- **Synthetic data as a separate `synthetic_data_spec` artifact.**
  Rejected: doubles the spec inventory and forces every downstream
  consumer (critics, manifest, template code) to handle two shapes for the
  same conceptual role.
