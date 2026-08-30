# Data adapter grounding

## Exit condition

The work stops only when both conditions hold.

1. A registered local adapter is parsed, probed, selected by `claim_contract.data_source_anchor`, carried through `ExperimentPlan.inputs` and `JobManifest.inputs`, and exposed to a real `LocalRunner` subprocess through a workspace-local manifest.
2. A fresh non-OKX thread started through the normal frontend path reaches a persisted terminal outcome recognized by `thread_supervisor.is_terminal`, or the run produces a concrete external blocker that cannot be repaired inside this repository.

The acceptance topic will use the frozen arXiv category taxonomy in `research_harness/connector/data/arxiv_categories.json`. The experiment question is whether a character n-gram classifier predicts the top-level arXiv archive from category names better than word-unigram, majority-class, and label-shuffle baselines on a fixed stratified holdout. This is small, CPU-only, non-financial, and exercises a real local adapter.

## Current flow

```text
settings.json or settings.local.json
  data_adapters.registered[]
        |
        | identity-only projection
        v
feasibility_envelope.data_sources_available[]
        |
        | string membership
        v
claim_contract.data_source_anchor

dataset_spec
        |
        | type-keyed materialize()
        v
dataset_manifest.json             Professor-generated ExperimentPlan.inputs={}
        |                                           |
        | no active bridge                          | blind pass-through
        x                                           v
                                         JobManifest.inputs={}
                                                   |
                                                   | ignored
                                                   v
                                              LocalRunner
```

## Confirmed constraints

- `frontend/datasets.py` validates the adapter ID, a purpose-like `kind`, the source string shape, and provenance. It does not prove the source exists or can be read.
- The UI merges project and operator adapters by ID. `ResolvedSettings` replaces the whole project list with the operator list. Supervisor bootstrap and MCP startup read only `settings.json`. These are three incompatible resolution policies.
- The checked-in OKX entry has no `source`. Its effective path is separately injected as `COIN_DATA_DIR` into the MCP server environment.
- Supervisor bootstrap stamps any project dictionary with an `id` as a `real_adapter`, regardless of source readiness or executable capability.
- `data_source_anchor` is persisted on the claim, but no active code resolves it to a dataset materializer or runtime input.
- `design_experiment_template` can enforce only that marker text occurs in submitted source. That check does not demonstrate that the experiment loads the adapter.
- The legacy refiner can materialize a complete `dataset_spec`, but the active frontend pipeline bypasses the refiner. Its manifest is not copied into a node workspace.
- `ExperimentPlan.inputs` and `JobManifest.inputs` already provide an unused transport seam. `LocalRunner` ignores it and writes only source files.
- The existing `DatasetMaterializer` and `MaterializeResult` types are useful acquisition boundaries. Local-path materialization already returns a stable cached path and a size receipt.
- A fresh directional run can terminate honestly as `accept_with_unverified_screen`. A positive deployment-grade goal would additionally require an operator-owned falsifier and is outside this acceptance run.

## Design requirements

- One parser and resolver must be the source of truth for project and operator adapter entries.
- Registration is not readiness. Only a successful probe may enter a feasibility envelope or runtime binding.
- A claim's real `data_source_anchor` must resolve before the experiment starts. Missing, invalid, unreadable, or mismatched adapters must fail at that boundary.
- Existing `DatasetMaterializer` implementations should remain the acquisition mechanism. Do not create a second fetcher framework.
- The runner must receive a workspace-local, machine-readable inputs manifest through a stable interface. Experiment code must not depend on topic-specific environment variables.
- Binding and manifest creation must be idempotent across retries.
- The smallest useful shape wins. No dynamic plugin loader, arbitrary Python adapter module, or generalized data catalog is required for this task.

## Pre-change evidence

- Relevant baseline suite passed with 112 tests in 4.16 seconds.
- The full Codex migration suite passed earlier with 764 tests and 17 subtests.
- The repository worktree already contains the user's uncommitted Codex migration. New changes must remain separable and preserve that work.
