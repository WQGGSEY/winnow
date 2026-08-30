# Cross-judge: candidate A vs candidate B

## Verdict

**Winner: candidate A.** It reaches the active connector-forest path, keeps the implementation local to the registered-local-adapter requirement, stages only the selected data into the node workspace, and exposes a smaller caller surface. Candidate B has stronger evidence propagation and a cleaner canonical dataset-spec vocabulary, but its manifest-first implementation expands into remote and synthetic acquisition, materializes every registered source before selection, and leaves runtime data at absolute cache paths.

Scores use a five-point scale.

| Criterion | Candidate A | Candidate B | Judgment |
|---|---:|---:|---|
| Correctness | 4.4 | 3.7 | Both distinguish registration, readiness, and execution. A binds one thread-snapshotted adapter and verifies its bytes. B's copied workspace manifest still points experiments to absolute materializer paths and can expose more than the selected dataset. |
| Active-path reachability | 4.6 | 4.2 | Both repair `seed_forest_from_connector` and stamp roots before drafts. A makes scope and anchor explicit at the handoff. B moves selection into supervisor startup and needs an additional frontend/CLI choice when more than one source is ready. |
| Interface depth | 4.5 | 4.1 | A's `open -> probe_thread -> bind_job` surface hides settings precedence, hashing, materializer adaptation, staging, and receipts behind two meaningful transitions. B has a coherent deep module, but exposes manifest methods plus resolve, ensure, load, stage, fingerprint, runner validation, and environment functions. |
| Simplicity | 4.6 | 3.1 | A intentionally supports registered local sources and privately adapts them to the existing local materializer. B designs local, Hugging Face, and synthetic spec variants, versions the legacy manifest, changes cache layout, and propagates evidence through worker schemas before those broader cases are required. |
| Idempotence | 4.5 | 4.6 | Both use immutable thread artifacts, content-derived identities, atomic writes, and pre-run hash checks. B's single manifest authority is especially clear; A's content-derived materializer and binding IDs avoid the existing stale cache key equally well for the required local path. |
| Verification surface | 4.1 | 4.7 | A records a runner input receipt. B additionally carries typed input evidence into the worker report, giving downstream critics and publication artifacts a direct grounding trail. |

**Total:** candidate A 26.7/30; candidate B 24.4/30.

## Why candidate A is the better base

The grounding asks for one registered local adapter to pass through the normal frontend, connector forest, experiment plan, job manifest, and real runner. Candidate A follows that exact slice. Its connector handoff fixes the fatal omission identified by the critiques: active roots receive scope and anchor before the search state is written. Binding then occurs before `ready -> running`, so an unavailable adapter cannot strand a node in a half-started execution state.

Its `AdapterCatalog` earns its interface despite the name. `probe_thread` and `bind_job` are not pass-through methods; each owns a substantial invariant-changing transition with a different lifetime. The class remains acceptable only if `bind_job` treats the persisted thread snapshot as its sole authority and does not consult newly parsed live settings.

Candidate B's best idea is its single-authority manifest, but the implementation overreaches the acceptance requirement. `ensure_thread_dataset_manifest` probes/materializes every resolved dataset, including Hugging Face and synthetic specs, before the operator has selected one. This makes supervisor bootstrap potentially expensive, credential-dependent, or capable of invoking the existing synthetic-module path. It also copies the manifest rather than selected bytes into the workspace, so generated code still follows an absolute `materialized_path` outside the workspace. Those choices weaken both minimality and the claimed runtime boundary.

## Strongest graft from candidate B

Graft **end-to-end input evidence propagation** into candidate A. Keep A's selected, workspace-local binding and `RunnerInputReceipt`, then copy its typed identity into `worker_report` and the worker schema as B proposes:

- snapshot ID and binding ID;
- adapter ID;
- manifest digest and data digest;
- workspace-relative manifest path.

This makes the acceptance run verifiable from the same worker artifact consumed by critics and later publication stages, instead of requiring an auditor to correlate `runner_result.json` separately. It adds evidence, not another authority: the thread snapshot remains capability authority and the workspace runtime manifest remains job authority.

Do not graft B's multi-backend registered-spec union, eager materialization of all sources, or absolute-path runtime manifest.

## Fatal red flags

### Candidate A

No fatal red flag for the required local-adapter slice. Before implementation, tighten two points:

- `AdapterCatalog.bind_job` must load only the immutable thread snapshot, despite being called on an object created with `open(repo)`.
- The envelope should carry the snapshot ID and the runner receipt should be propagated to the worker report; otherwise later artifacts can identify only the mutable adapter ID.

### Candidate B

Candidate B is not safe to adopt unchanged:

- Supporting `SyntheticDatasetSpec` in an eager bootstrap manifest can execute the existing synthetic materializer's arbitrary module path while merely deciding feasibility.
- Materializing all registered datasets before selection makes frontend supervisor startup scale with every local, remote, credentialed, or model-weight entry rather than the chosen acceptance resource.
- Copying a manifest whose entries retain absolute cache paths does not deliver the selected bytes through the proposed workspace boundary and exposes non-selected ready entries to the child.

These are shape-level issues, not local fixes. Removing them converges candidate B toward candidate A's selected local binding, which is why A should be the base.
