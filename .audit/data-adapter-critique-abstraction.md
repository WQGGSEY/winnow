## Findings

### 1. [structural] The proposed seam spans two different lifetimes

**Components**: `thread_supervisor.bootstrap_envelope_if_missing`, `validate_claim_fits_envelope`, proposed adapter-binding boundary

**Finding**: The explanation places one adapter-binding boundary after claim validation but also assigns that boundary responsibility for deciding whether an adapter is ready enough to advertise as feasible. Those decisions occur at different lifetimes. Feasibility-envelope construction is project/session bootstrap state; materialization and receipt creation are node-execution state. Treating both as one late boundary leaves the earlier envelope dependent on the existing declaration-only bootstrap, while moving the whole boundary earlier would require it to manufacture node-specific paths and receipts before a node exists. The explanation therefore names one component for two distinct transitions without defining the reusable contract between them.

**Evidence**: `/home/hsj68/research_harness/research_harness/thread_supervisor.py:303-354` builds the feasibility envelope from `settings.json` before a claim is selected. `/home/hsj68/research_harness/research_harness/persona_validator.py:709-892` validates a selected claim against that already-built envelope. The proposed flow in `/home/hsj68/research_harness/.audit/data-adapter-explanation.md:94-105` places binding after claim validation, yet step 3 says to probe readiness before treating the adapter as feasible.

**Impact**: The declared feasibility set can still contain an unavailable adapter, so the claim may pass validation and fail only at execution. It also concentrates settings precedence, readiness policy, acquisition, cache behavior, and runtime packaging in a component whose state lifetime is unclear, making the abstraction harder to test and cache correctly.

### 2. [structural] The active connector handoff has no anchor for the proposed binder

**Components**: connector reduction, connector session schema, forest root construction, `handle_seed_forest_from_connector`

**Finding**: The explanation treats `design_initial_claim_contract.data_source_anchor` as the universal input to binding, but the active connector-to-forest path neither represents nor validates that field. It creates production roots directly from a four-field claim contract and explicitly replaces the design-initial contract. A binder placed after anchor selection therefore has no declared adapter identity on this path.

**Evidence**: `/home/hsj68/research_harness/research_harness/connector/reduction.py:124-161` emits only `claim`, `baselines`, `success_criterion`, and `disproof_criterion`. `/home/hsj68/research_harness/research_harness/schemas/connector_session.schema.json:92-128` rejects additional claim-contract properties, so it cannot carry `data_source_anchor`. `/home/hsj68/research_harness/research_harness/connector/forest.py:44-64` copies only those four fields into each forest root. `/home/hsj68/research_harness/research_harness/mcp_server.py:4436-4492` says connector seeding replaces `design_initial_claim_contract` and builds the forest without the feasibility/registered-adapter checks used by `/home/hsj68/research_harness/research_harness/mcp_server.py:2148-2225`.

**Impact**: The proposed seam cannot cover all active production roots without either changing the connector contract or inferring an adapter later from topic text or environment state. That omission preserves an ungrounded production path and would force adapter-specific special cases into an otherwise explicit binding boundary.

### 3. [structural] Harness-owned binding state is not separated from model-authored inputs

**Components**: Professor template coercion, experiment-plan construction, experiment/job input schemas, LocalRunner

**Finding**: The explanation says to store the binding under `ExperimentPlan.inputs`, but it does not define who owns that field or when the harness overwrites it. Today `inputs` is optional, untyped metadata authored through the Professor/template path. The same opaque object is copied into the job manifest. Placing an execution authority there without a harness-owned typed substructure and stamping point mixes declarative model output with verified runtime state.

**Evidence**: `/home/hsj68/research_harness/research_harness/professor.py:947-956` currently coerces Professor output to empty `inputs`. `/home/hsj68/research_harness/research_harness/orchestrator/experiment_plan.py:249-327` accepts metadata inputs or defaults to `{datasets: [], snapshots: []}`. `/home/hsj68/research_harness/research_harness/schemas/user_experiment_plan_metadata.schema.json:49` and `/home/hsj68/research_harness/research_harness/schemas/experiment_plan.schema.json:6-24,77-78` constrain `inputs` only as an object. `/home/hsj68/research_harness/research_harness/orchestrator/experiment_plan.py:703-726` passes the object unchanged into the job manifest, whose schema is likewise opaque at `/home/hsj68/research_harness/research_harness/schemas/job_manifest.schema.json:6-20,63-64`.

**Impact**: A generated plan can omit, replace, or redirect the purported binding unless a later harness step is explicitly authoritative. Reviewers cannot tell from the proposed architecture whether `inputs` is user intent, model suggestion, verified data receipt, or all three, so the grounding invariant remains bypassable.

### 4. [concern] “Convert the declaration into a dataset spec” hides an unresolved taxonomy mapping

**Components**: frontend registry entry, refined-plan dataset spec, materializer registry

**Finding**: The proposed conversion is not a shape-preserving parse. The registry and materializer use different category systems and require different facts. A frontend declaration may say `kind: real_panel` and supply a path, while the executable spec requires a supported `type` and a `role`; the hard-coded materializer registry has no `real_panel` entry. The explanation does not identify an authoritative mapping or which boundary owns values absent from the declaration.

**Evidence**: `/home/hsj68/research_harness/research_harness/frontend/datasets.py:51-53,246-310` admits `real_panel` alongside materializer-like kinds and writes `{id, kind, source, provenance, module}`. `/home/hsj68/research_harness/research_harness/schemas/refined_research_plan.schema.json:102-145` requires dataset `id`, `type`, and `role`, with a type enum that excludes `real_panel`. `/home/hsj68/research_harness/research_harness/datasets/registry.py:23-37` dispatches only `raw_data`, `benchmark`, `model_weights`, `factor_set`, `custom`, and `synthetic`. The checked-in OKX declaration at `/home/hsj68/research_harness/settings.json:2-17` does not even contain the frontend's `source` or `module` fields.

**Impact**: The binding layer would need inference rules or adapter-specific switches for `type`, `role`, source, and revision. That makes “adapter binding” a semantic translation layer whose outputs can vary by caller, rather than a narrow resolution of a declared executable contract.

### 5. [concern] Existing materialization does not provide the content identity the proposed receipt promises

**Components**: `LocalPathMaterializer`, `MaterializeResult`, proposed binding receipt

**Finding**: The explanation relies on the existing materializer while promising a stable, idempotent binding receipt, but local-path cache identity is based only on type, ID, and basename. An existing target suppresses copying without checking source contents, source metadata, or a digest. Reusing an adapter ID after changing the source can therefore return stale bytes as a successful materialization.

**Evidence**: `/home/hsj68/research_harness/research_harness/datasets/local_path.py:19-66` computes `.dataset_cache/<spec_type>/<id>/<basename>` and copies only when that target does not exist. `/home/hsj68/research_harness/research_harness/datasets/protocol.py:17-58` returns path, verification, size, and fetcher name but no content digest or revision identity.

**Impact**: Adapter identity, materialization identity, and byte identity collapse into one mutable cache key. A receipt can be stable while referring to stale content, undermining reproducibility and making probe success insufficient evidence that the experiment received the declared dataset version.

### 6. [concern] The proposal adds another persistence representation without naming the authority

**Components**: registry declaration, dataset spec, dataset manifest, proposed binding receipt, experiment/job manifests

**Finding**: The proposed flow creates a new “binding receipt” and a workspace-local manifest reference even though the repository already persists a `dataset_manifest.json` containing the spec and materialization result. It does not state whether that existing manifest is extended, replaced, or subordinated, nor which artifact is authoritative when paths or provenance disagree. The flow therefore accumulates representations rather than defining one canonical transition artifact.

**Evidence**: `/home/hsj68/research_harness/research_harness/agents/research_refiner.py:263-280,829-835` materializes dataset specs and writes a dataset manifest. `/home/hsj68/research_harness/research_harness/datasets/protocol.py:17-58` already defines the persisted materialization result shape. The proposed artifacts appear at `/home/hsj68/research_harness/.audit/data-adapter-explanation.md:55-63,101-105`, followed by another opaque copy through `/home/hsj68/research_harness/research_harness/orchestrator/experiment_plan.py:703-726`.

**Impact**: Registry provenance, dataset-manifest provenance, binding-receipt provenance, and plan/job inputs can drift independently. The extra artifact has not yet earned its complexity because its unique invariant and authority relative to the existing manifest are unspecified.
