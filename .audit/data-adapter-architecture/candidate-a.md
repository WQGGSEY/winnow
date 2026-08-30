# Candidate A: typed catalog with thread snapshot and job binding

## Problem

The repository has an adapter settings record, a feasibility-envelope ID, and a `DatasetMaterializer`, but no contract connects them. The active connector forest also drops `deploy_grade_scope` and `data_source_anchor`, while Professor-owned `inputs` pass through the plan and job schemas untyped and are ignored by `LocalRunner`. The design must make a registered local source executable without adding dynamic plugins or a second fetcher framework. It must also respect two lifetimes: a thread decides which adapters are ready before claims exist, while a job materializes one already-selected adapter into a workspace immediately before execution.

## Usage (caller's view)

The public adapter API has one constructor and two state transitions:

```python
from research_harness.adapters.catalog import AdapterCatalog, AdapterId

catalog = AdapterCatalog.open(repo_root)
snapshot = catalog.probe_thread(thread_dir)
runtime_inputs = catalog.bind_job(
    thread_dir=thread_dir,
    adapter_id=AdapterId(node["claim_contract"]["data_source_anchor"]),
    workspace=workspace,
)
```

`open()` is the only project/operator parser and per-ID resolver. Operator entries shadow project entries; an invalid operator override disables that ID instead of silently falling back. `probe_thread()` writes or reopens one immutable `production/adapter_snapshot.json`. `bind_job()` resolves only against that snapshot, calls the existing `research_harness.datasets` materializer, stages the resulting bytes under the job workspace, and atomically writes one runtime manifest.

### Call site 1: supervisor bootstrap

```python
def bootstrap_envelope_if_missing(repo: Path, tid: str, *, target_scope: str):
    thread_dir = _thread_dir(repo, tid)
    adapters = AdapterCatalog.open(repo).probe_thread(thread_dir)
    envelope = build_envelope(
        adapter_snapshot_id=adapters.snapshot_id,
        data_sources_available=[
            {
                "kind": "real_adapter",
                "id": str(item.adapter_id),
                "scope_note": item.provenance,
            }
            for item in adapters.ready.values()
        ],
        target_scope=target_scope,
    )
    write_envelope_atomically(thread_dir, envelope)
```

Only successfully probed entries enter the envelope. An existing snapshot is reused on supervisor retries; settings changes affect new threads, not an in-flight thread.

### Call site 2: connector-to-forest handoff

```python
def handle_seed_forest_from_connector(args: dict[str, object]) -> dict[str, object]:
    selection = ClaimResourceSelection.parse(
        deploy_grade_scope=args["deploy_grade_scope"],
        data_source_anchor=args["data_source_anchor"],
    )
    snapshot = AdapterCatalog.open(repo).probe_thread(thread_dir)
    validate_selection_against_envelope(selection, envelope, snapshot.ready.keys())
    state = build_forest_search_state(
        grilling,
        claims,
        search_id=f"s_{tid}",
        policy=policy,
        resource_selection=selection,
    )
```

`seed_forest_from_connector` gains required scope and anchor arguments. `_build_forest_root()` stamps both fields on every root claim contract; draft children already deep-copy that contract. The connector session remains about claim generation and does not guess data resources. A missing or unready real anchor rejects the handoff before `search_state.json` is written.

### Call site 3: active experiment execution and runner

```python
workspace = planned_workspace(run_dir, node_id)
anchor = parse_data_source_anchor(node["claim_contract"]["data_source_anchor"])
runtime_inputs = (
    catalog.bind_job(
        thread_dir=_thread_dir(tid),
        adapter_id=anchor.adapter_id,
        workspace=workspace,
    )
    if isinstance(anchor, RealAdapterAnchor)
    else NoAdapterInputs()
)
plan, template_used = build_experiment_plan_for_node(
    repo, node, run_dir, settings=settings, runtime_inputs=runtime_inputs
)
manifest = build_job_manifest_from_experiment_plan(node, plan, run_dir)
runner_result = LocalRunner(run_dir, settings=settings).execute(manifest)
```

Binding happens before the node transitions from `ready` to `running`. Professor metadata cannot supply or override `inputs`. `LocalRunner` validates the workspace-local manifest and its data digest, exports `RESEARCH_HARNESS_INPUT_MANIFEST`, and copies the verified binding identity into `runner_result.input_receipt`.

## Shape

### Core types and signatures

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, NewType

AdapterId = NewType("AdapterId", str)
SnapshotId = NewType("SnapshotId", str)
BindingId = NewType("BindingId", str)
Sha256 = NewType("Sha256", str)
WorkspacePath = NewType("WorkspacePath", str)  # validated relative path

AdapterPurpose = Literal[
    "raw_data", "benchmark", "model_weights", "factor_set", "custom", "real_panel"
]

@dataclass(frozen=True)
class LocalAdapterDeclaration:
    adapter_id: AdapterId
    purpose: AdapterPurpose       # settings `kind`; descriptive, never dispatches code
    source: Path                  # absolute path parsed at the settings boundary
    provenance: str
    scope: Literal["project", "operator"]
    declared_sha256: Sha256 | None

@dataclass(frozen=True)
class ProbedAdapter:
    adapter_id: AdapterId
    purpose: AdapterPurpose
    source: Path
    provenance: str
    content_sha256: Sha256
    size_bytes: int

@dataclass(frozen=True)
class ThreadAdapterSnapshot:
    schema_version: Literal[1]
    snapshot_id: SnapshotId
    ready: Mapping[AdapterId, ProbedAdapter]
    rejected: Mapping[str, str]

@dataclass(frozen=True)
class AdapterInputs:
    kind: Literal["adapter"]
    binding_id: BindingId
    manifest_path: WorkspacePath

@dataclass(frozen=True)
class NoAdapterInputs:
    kind: Literal["none"] = "none"

RuntimeInputs = AdapterInputs | NoAdapterInputs

@dataclass(frozen=True)
class RunnerInputReceipt:
    kind: Literal["adapter"]
    binding_id: BindingId
    snapshot_id: SnapshotId
    adapter_id: AdapterId
    manifest_path: WorkspacePath
    manifest_sha256: Sha256
    data_sha256: Sha256

class AdapterCatalog:
    @classmethod
    def open(cls, repo_root: Path) -> "AdapterCatalog": ...

    def probe_thread(self, thread_dir: Path) -> ThreadAdapterSnapshot: ...

    def bind_job(
        self, *, thread_dir: Path, adapter_id: AdapterId, workspace: Path
    ) -> AdapterInputs: ...
```

`LocalAdapterDeclaration` deliberately separates semantic `purpose` from executable dispatch. Every registered adapter supported by this design is a local file or directory and is converted privately to a `dataset_spec` with `type="custom"`, `role="primary"`, and a materializer ID derived from `(adapter_id, content_sha256)`. The existing `materialize()` dispatcher therefore selects `LocalPathMaterializer`; the settings `module` string is removed and never imported. Remote datasets and synthetic recipes remain on their existing dataset-spec path until a concrete registered-adapter requirement exists.

`probe_thread()` validates IDs and absolute sources, checks readability, hashes file contents (or a canonical directory tree), and atomically persists the snapshot. Its ID is a hash of the canonical ready and rejected records. This makes registration, readiness, and a thread snapshot distinct states, per boundary discipline.

`bind_job()` loads the persisted snapshot, rejects an absent anchor or changed source hash, and builds a private spec whose content-derived materializer ID avoids `LocalPathMaterializer`'s stale `<type>/<id>/<basename>` cache collision. It requires `MaterializeResult.status == "ok"` and a non-null path, verifies the returned content hash, stages it at `workspace/inputs/data/<binding-id>/`, and writes `workspace/inputs/adapter-manifest.json` atomically. Repeating the call validates and returns the same binding; a partial or mismatched binding fails closed.

The runtime manifest is the canonical job receipt, not a second dataset manifest:

```json
{
  "schema_version": 1,
  "binding_id": "sha256:...",
  "snapshot_id": "sha256:...",
  "adapter_id": "arxiv-category-taxonomy",
  "purpose": "custom",
  "data_path": "inputs/data/<binding-id>/arxiv_categories.json",
  "data_sha256": "sha256:...",
  "size_bytes": 1234,
  "provenance": "frozen arXiv category taxonomy",
  "materializer": {"dataset_type": "custom", "fetcher_type": "local_path"}
}
```

`ExperimentPlan.inputs` and `JobManifest.inputs` become the closed `RuntimeInputs` union. The execution coordinator, not Professor output, constructs it. `LocalRunner` owns wire validation, containment checks, digest verification, environment exposure, and `RunnerInputReceipt`; internal callers receive typed values, per boundary discipline. The receipt proves which binding the runner validated and delivered, not that arbitrary generated code semantically used every record.

The interface is deep: callers choose only a thread directory, an adapter ID, and a workspace. Settings precedence, legacy-field rejection, hashing, snapshot persistence, conversion to the existing materializer spec, cache-key versioning, staging, and manifest encoding remain private. The two methods correspond to two externally meaningful state transitions, not a sequence of exposed implementation stages.

### Data flow

```text
settings.json + settings.local.json
        | AdapterCatalog.open (parse + per-ID precedence)
        v
probe_thread --> immutable adapter_snapshot.json --> feasibility envelope
                                                      |
connector handoff stamps scope + anchor on every root-+--> child inheritance
                                                      |
node anchor + same thread snapshot                    v
        +-------------------------> bind_job --> DatasetMaterializer
                                                  |
                                                  v
                              workspace input manifest + staged data
                                                  |
                         ExperimentPlan.inputs -> JobManifest.inputs
                                                  |
                                      LocalRunner validates + exposes
                                                  |
                                      runner_result.input_receipt
```

### Module map

| Location | Ownership |
|---|---|
| `research_harness/adapters/catalog.py` | New deep module: canonical project/operator parsing, precedence, local probe, immutable snapshot, private dataset-spec adaptation, binding, staging, runtime-manifest persistence. |
| `research_harness/frontend/datasets.py` | Persists canonical `{id, kind, source, provenance, _upload}` declarations and uses the catalog parser for display/readiness errors; stops writing `module`. |
| `research_harness/thread_supervisor.py` | Calls `probe_thread()` before envelope construction and derives real availability only from `snapshot.ready`. |
| `research_harness/connector/forest.py` and `research_harness/mcp_server.py` | Make connector handoff accept, validate, and stamp one explicit `ClaimResourceSelection` on every root. |
| `research_harness/orchestrator/experiment_plan.py` | Accepts harness-owned `RuntimeInputs`; no longer copies Professor metadata inputs. |
| `research_harness/runner/local_runner.py` | Validates the runtime manifest/data, exports its path, and persists `RunnerInputReceipt`. |
| `research_harness/schemas/{feasibility_envelope,experiment_plan,job_manifest,runner_result,adapter_snapshot,runtime_input_manifest}.schema.json` | Encode snapshot identity, the closed runtime-input union, and the runner receipt at persistence boundaries. |
| `research_harness/datasets/` | Remains the only acquisition implementation; no adapter plugin loader is added. |

## Synthesis decision

Candidate A uses the caller-first two-stage catalog as its base. It incorporates the critiques' strongest constraints: thread readiness and job materialization have distinct lifetimes; connector forest roots need explicit resource assignment; and runtime inputs must be harness-authored and receipted. It rejects a general plugin/catalog framework, a post-claim-only binder, and a second fetcher abstraction because each exposes more policy or duplicates existing acquisition machinery without helping the local arXiv acceptance path.

## Tradeoffs accepted

- We accept hashing local inputs during thread bootstrap in exchange for an immutable identity that detects drift and makes retries auditable.
- We accept copying or linking materialized data into each workspace in exchange for a workspace-relative runtime contract with no topic-specific host path.
- We accept supporting only registered local sources in this catalog in exchange for a small, executable contract; remote and synthetic acquisition keep their existing paths.
- We accept that the runner receipt proves validated delivery, not semantic consumption, in exchange for an honest boundary that does not claim to sandbox or interpret arbitrary experiment code.

## Alternatives considered

- A single post-claim binder lost because it cannot determine what belongs in the pre-claim feasibility envelope and leaves connector roots anchorless.
- Making each settings entry name an importable Python adapter lost because it exposes loading and trust policy to configuration, duplicates the hard-coded materializer registry, and adds a plugin system with no second concrete need.
- Passing the materialized absolute path directly in `JobManifest.inputs` lost because every caller would need to understand host paths, containment, hashing, and provenance; the workspace manifest hides those decisions behind one reference.

## Open questions and risks

- Should directory adapters be copied into each workspace or exposed through a read-only link once the platform has an explicit sandbox model?
- Should existing threads with an envelope but no `adapter_snapshot.json` fail closed or receive a one-time migration snapshot?
- Should the checked-in OKX declaration be migrated to an explicit local `source`, or intentionally remain rejected until its ambient `COIN_DATA_DIR` dependency is retired?

## Next implementation step

Define the domain types plus `adapter_snapshot` and `runtime_input_manifest` schemas, then implement `AdapterCatalog.open()` and `probe_thread()` against the arXiv local-file declaration before changing any production caller.
