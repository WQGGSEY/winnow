# Experiment Plan Templates

Each subdirectory here is **one research domain** the operator has wired
up. The router in
`research_harness/orchestrator/experiment_plan.py::build_experiment_plan_for_node`
looks for `<this directory>/<node.domain>/plan.json` and a sibling `src/`
directory; the entire `src/` tree is materialized into the node workspace
before the deterministic runner executes the entrypoint.

If a node's domain has no matching directory the router falls back to the
demo plan and the production runner refuses to publish anything (every
`production_run_summary.json` records `evidence_is_fake: true` and lists
which nodes fell back).

The list of directories the router scans is configured in `settings.json`:

```json
"experiment_plan_templates": {
  "directories": ["experiment_plan_templates", "my_other_templates"]
}
```

## Directory shape

```
<this directory>/
  <domain_name>/                # e.g. retrieval, rag, summarization_factuality
    plan.json                   # metadata only (no source code)
    src/                        # the actual project — copied verbatim
      __init__.py
      experiment.py             # entrypoint
      model.py
      data.py
      eval/
        ndcg.py
      baselines/
        bm25.py
        random_baseline.py
```

Rules:

- The domain directory name must be a valid Python identifier
  (`[A-Za-z_][A-Za-z0-9_]*`). The grilling agent's allowed-domain enum is
  built from these names — so the directory name is also the contract.
- `plan.json` must satisfy
  `research_harness/schemas/user_experiment_plan_metadata.schema.json`.
- `src/` must contain at least one file with an allowed extension
  (`.py .yaml .yml .json .txt .md .toml .cfg .sh`). Per-file max 200KB,
  whole tree max 5MB. Binaries / data files do not belong under `src/`.
- The entrypoint is expressed relative to the materialized workspace.
  Use `"args": ["-m", "src.experiment"]` for module-style execution
  (recommended; lets you keep normal Python imports across the tree) or
  `"args": ["src/experiment.py"]` for direct script execution.

## plan.json contract

You write only the metadata. The harness fills in `plan_id`, `node_id`,
`claim_under_test`, `mandatory_baselines`, `success_criteria`,
`disproof_conditions`, `workspace`, and `source_files` from the node and
the materialized source tree.

Required fields:

- `task_class` — one of `smoke_test | ablation | training | eval | analysis`.
- `objective` — one-sentence description.
- `entrypoint.command` — list of strings, e.g. `["python"]`.
- `entrypoint.args` — list of strings.
- `resources.timeout_sec` — integer seconds.
- `expected_outputs` — must include `metrics_files` pointing at the JSON
  file your script writes (e.g. `artifacts/metrics.json`).
- `baseline_evidence_requirements` — must cover all three roles
  (`current_best_known`, `naive`, `random_or_null`). The harness uses
  these to decide whether the worker report supports the claim.

Optional fields with safe defaults:

- `plan_id_suffix`, `inputs`, `guardrails`, `failure_index_hints`,
  `reproducibility`.

## What your script must write

The runner reads `expected_outputs.metrics_files[0]` from the workspace.
The JSON object at that path must have the shape:

```json
{
  "metrics": { "<your metric key>": <number> },
  "baselines": {
    "<baseline_key matching your plan.json>": <number>
  },
  "claim_verdict_candidate": "supported" | "contradicted" | "inconclusive" | "not_evaluable",
  "disproof_conditions_hit": ["<string>", "..."],
  "unexpected_observations": [
    {"observation": "...", "evidence": "...", "scope_relation": "within_claim"}
  ]
}
```

The harness compares `metrics[<metric_key>]` against `baselines[<baseline_key>]`
using the `operator` you declared in `baseline_evidence_requirements`. A
node is only promoted when every required baseline comparison passes.

## See also

- Reference template: `retrieval/` (toy nDCG@10 evaluation).
- Full pipeline contract: `research_harness/schemas/experiment_plan.schema.json`
- Operator subset: `research_harness/schemas/user_experiment_plan_metadata.schema.json`
