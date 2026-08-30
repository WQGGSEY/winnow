# Data adapter architecture synthesis

## Decision

Use candidate C's functional, immutable snapshot pipeline as the base.

The public lifecycle has three transitions:

1. `snapshot_registered_adapters` resolves project and operator declarations, probes local bytes, and persists content-addressed thread capabilities.
2. Supervisor bootstrap resolves the operator's adapter ID to one exact snapshot and stores `{adapter_id, snapshot_id}` in the feasibility envelope. Connector forest seeding validates and copies that authority into every root.
3. `bind_runtime_input` loads only the persisted snapshot, reuses the existing materializer, verifies and stages bytes inside the node workspace, and writes the runtime input manifest before `ready -> running`.

No stateful catalog, dynamic loader, generalized remote catalog, or revived refiner is introduced.

## Grafts

### Fail-closed operator precedence from candidate A

Resolution must preserve invalid operator rows as keyed unavailable results. If an operator row overrides a project adapter ID but is malformed or unready, that ID stays unavailable. It must not fall back to the project declaration.

The resolved per-ID state is either a ready immutable snapshot or an unavailable problem. This is the shared policy used by the frontend and supervisor.

### Operator-owned selection from candidate B

Selection belongs to supervisor bootstrap, not a model-authored MCP argument. The frontend/CLI accepts a stable human adapter ID. Bootstrap resolves it to the probed snapshot and persists both IDs in `feasibility_envelope.operator_intent`. If no ID is supplied and exactly one adapter is ready, selecting that adapter is deterministic and allowed. Multiple ready adapters require an explicit selection.

Connector seeding reads and validates this harness-owned selection. A deployment root cannot be created without it.

### End-to-end evidence from candidate B

The runner's typed input evidence is copied into the worker report. The evidence includes adapter ID, snapshot ID, content digest, manifest digest, and workspace-relative manifest path. This is a receipt, not a second authority.

## Product boundary

Version one supports registered local files and directories through existing local materializers. Canonical declarations contain:

- `id`
- `materializer_type`
- `role`
- `source`
- `provenance`

The legacy `real_panel` value is not guessed into a materializer type. The current source-less OKX declaration is reported unavailable and is not used by the acceptance run.

The acceptance adapter is `arxiv_category_taxonomy`, backed by the frozen repository file `research_harness/connector/data/arxiv_categories.json`.

## Authority and artifacts

- Settings declarations are mutable operator input.
- `production/adapter_snapshots.json` is immutable thread capability evidence.
- `feasibility_envelope.operator_intent` is the thread's selected adapter authority.
- The root claim contract copies adapter ID, snapshot ID, and deployment scope.
- `runtime_inputs.json` is the job's staged input authority.
- `runner_result.json` and `worker_report.json` are delivery evidence.

Jobs never reinterpret live settings. Experiment-model output cannot replace harness-owned runtime inputs.

## Runtime contract

The staged manifest exposes one primary dataset at a workspace-relative path. `LocalRunner` validates containment, snapshot agreement, and content hash, then sets `RESEARCH_HARNESS_INPUT_MANIFEST` for the child process. Plan and job inputs contain only the manifest reference and immutable snapshot ID.

This proves which bytes the harness delivered. It does not prove that an unsandboxed child process read no other host path, and the acceptance report must not claim otherwise.

## Scope cuts

- No Hugging Face or synthetic adapter registry expansion.
- No eager materialization of all adapters at bootstrap.
- No arbitrary Python adapter modules.
- No global change to settings list-merge semantics outside data adapters.
- No runner sandbox rewrite.
- No removal of the old OKX configuration or topic-specific compatibility environment in this slice.

## Arena rationale

Candidate C won both head-to-head comparisons involving it. Its content-addressed per-adapter identity, explicit thread/job lifetime split, workspace-local delivery, and narrow functional surface best fit the active path. Candidate A's service object adds lifecycle coordination without durable state and its proposed role/type conversion is invalid. Candidate B supplies useful operator selection and evidence ideas but otherwise broadens the system into a multi-backend catalog and performs acquisition at the wrong lifetime.

No fatal red flag remains after adding fail-closed precedence and bootstrap-owned selection. The filesystem claim remains deliberately limited to verified delivery evidence.
