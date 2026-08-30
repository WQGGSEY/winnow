# Candidate C: Functional adapter snapshots

## Problem

The active new-thread path is `grilling -> connector -> production forest`, but its adapter story is split across mutable settings declarations, an identity-only feasibility envelope, connector roots that do not carry an anchor, opaque plan/job `inputs`, and a runner that never exposes those inputs. The design must make one registered local dataset usable by a fresh non-OKX thread without reviving the legacy refiner, inventing a dynamic plugin system, or hiding lifecycle state behind a service object. It must also respect two different times: the supervisor decides what data is honestly available before claim construction, while a node binds the chosen bytes immediately before execution.

## Usage (caller's view)

The operator registers one canonical local declaration through `/datasets`:

```json
{
  "id": "arxiv_category_taxonomy",
  "materializer_type": "benchmark",
  "role": "evaluation",
  "source": "file:///home/hsj68/research_harness/research_harness/connector/data/arxiv_categories.json",
  "provenance": "Frozen arXiv category taxonomy shipped with research_harness"
}
```

There is no `AdapterCatalog` to construct or retain. Three real callers compose functions over immutable values.

### Call site 1: supervisor bootstrap snapshots ready declarations

```python
from research_harness.data_adapters import snapshot_registered_adapters

snapshots, problems = snapshot_registered_adapters(
    project_entries=project_settings.get("data_adapters", {}).get("registered", []),
    operator_entries=operator_settings.get("data_adapters", {}).get("registered", []),
)
write_adapter_snapshots(thread_dir / "production" / "adapter_snapshots.json", snapshots, problems)

envelope["data_sources_available"] = [
    {
        "kind": "real_adapter",
        "id": snapshot.adapter_id,
        "snapshot_id": snapshot.snapshot_id,
        "scope_note": snapshot.provenance,
    }
    for snapshot in snapshots
] + [{"kind": "synthetic", "id": "synthetic_generator_default"}]
```

`bootstrap_envelope_if_missing()` performs this once for a fresh production directory. A resume reads the already-persisted thread snapshots; it does not reinterpret mutable settings.

### Call site 2: connector forest seeding selects one immutable snapshot

```python
from research_harness.data_adapters import select_snapshot

selection = select_snapshot(
    snapshots=read_adapter_snapshots(thread_dir / "production" / "adapter_snapshots.json"),
    envelope=read_feasibility_envelope(thread_id),
    selected_snapshot_id=args["selected_snapshot_id"],
    deploy_grade_scope=args["deploy_grade_scope"],
)
state = build_forest_search_state(
    grilling,
    connector_claims,
    search_id=f"s_{thread_id}",
    policy=policy,
    data_selection=selection,
)
```

`seed_forest_from_connector` requires an explicit `selected_snapshot_id` for a deployment claim. `build_forest_search_state` stamps both `data_source_anchor=selection.adapter_id` and `data_source_snapshot_id=selection.snapshot_id` onto every root before drafts are copied. It never infers an adapter from the topic, an environment variable, or the first registry row.

### Call site 3: node execution binds before entering `running`

```python
from research_harness.runtime_inputs import bind_runtime_input, plan_input_reference

snapshot = require_snapshot(
    read_adapter_snapshots(thread_dir / "production" / "adapter_snapshots.json"),
    node["claim_contract"]["data_source_snapshot_id"],
)
runtime_manifest = bind_runtime_input(
    snapshot=snapshot,
    node_id=node_id,
    workspace=node_workspace,
    repo_root=repo,
)

plan = build_experiment_plan_for_node(repo, node, run_dir, settings=settings)
plan["inputs"] = plan_input_reference(runtime_manifest)
manifest = build_job_manifest_from_experiment_plan(node, plan, run_dir)

# Only after binding, hashing, and manifest validation succeed:
transition_node(state, node_id, "running", event="mcp_dispatch", reason="runtime input bound")
runner_result = LocalRunner(run_dir, settings=settings).execute(manifest)
```

The generated experiment reads one stable interface:

```python
manifest_path = Path(os.environ["RESEARCH_HARNESS_INPUT_MANIFEST"])
runtime_input = json.loads(manifest_path.read_text())["primary_dataset"]
dataset_path = manifest_path.parent / runtime_input["relative_path"]
```

The runner records that it delivered the manifest, its hash, and its snapshot ID. It does not claim that an unsandboxed child could not read some other host path.

## Shape

### Data structures

The domain model is immutable records. JSON dictionaries exist only at settings, MCP, and persisted-artifact boundaries.

```python
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, NewType

AdapterId = NewType("AdapterId", str)
SnapshotId = NewType("SnapshotId", str)
Sha256 = NewType("Sha256", str)

AdapterScope = Literal["project", "operator"]
DatasetType = Literal[
    "raw_data", "benchmark", "model_weights", "factor_set", "custom"
]
DatasetRole = Literal[
    "training", "evaluation", "baseline_reference",
    "baseline_implementation", "other",
]
DeployGradeScope = Literal["deployment", "feasibility", "directional"]


@dataclass(frozen=True, slots=True)
class AdapterDeclaration:
    adapter_id: AdapterId
    materializer_type: DatasetType
    role: DatasetRole
    source: Path
    provenance: str
    scope: AdapterScope


@dataclass(frozen=True, slots=True)
class SourceObservation:
    resolved_source: Path
    content_sha256: Sha256
    size_bytes: int
    entry_count: int


@dataclass(frozen=True, slots=True)
class AdapterSnapshot:
    snapshot_id: SnapshotId
    adapter_id: AdapterId
    materializer_type: DatasetType
    role: DatasetRole
    source: Path
    provenance: str
    content_sha256: Sha256
    size_bytes: int
    entry_count: int


@dataclass(frozen=True, slots=True)
class AdapterProblem:
    adapter_id: str | None
    scope: AdapterScope
    code: Literal[
        "malformed", "duplicate", "unsupported_type",
        "missing_source", "unreadable_source",
    ]
    message: str


@dataclass(frozen=True, slots=True)
class DataSourceSelection:
    deploy_grade_scope: DeployGradeScope
    adapter_id: AdapterId
    snapshot_id: SnapshotId


@dataclass(frozen=True, slots=True)
class RuntimeDatasetInput:
    snapshot_id: SnapshotId
    adapter_id: AdapterId
    relative_path: PurePosixPath
    content_sha256: Sha256
    size_bytes: int
    fetcher_type: str
    provenance: str


@dataclass(frozen=True, slots=True)
class RuntimeInputManifest:
    schema_version: Literal[1]
    node_id: str
    primary_dataset: RuntimeDatasetInput
```

`snapshot_id` is `as_` plus SHA-256 of canonical JSON containing adapter ID, executable dataset type, role, provenance, and the observed content hash. It is safe as a path component and changes if either declared semantics or bytes change. The mutable adapter ID remains the human-facing name; the immutable snapshot ID is the execution identity.

The canonical declaration deliberately names `materializer_type` rather than overloading today's `kind`. A compatibility parser may accept legacy `kind` only when its value is already one of the supported materializer types. It must reject `real_panel` rather than silently guessing `custom`; the current OKX entry has no executable source and is honestly unready until explicitly migrated.

### Function signatures

`research_harness.data_adapters` contains records and stateless transformations:

```python
def parse_adapter_entries(
    raw_entries: object,
    *,
    scope: AdapterScope,
) -> tuple[tuple[AdapterDeclaration, ...], tuple[AdapterProblem, ...]]:
    """Parse wire dictionaries; validate IDs, fields, and contained local paths."""
    raise NotImplementedError


def merge_adapter_entries(
    project: tuple[AdapterDeclaration, ...],
    operator: tuple[AdapterDeclaration, ...],
) -> tuple[AdapterDeclaration, ...]:
    """Return one declaration per ID; operator scope wins, order is stable by ID."""
    raise NotImplementedError


def observe_local_source(source: Path) -> SourceObservation:
    """Read-only I/O shell: resolve, require readable file/tree, hash sorted contents."""
    raise NotImplementedError


def probe_adapter(
    declaration: AdapterDeclaration,
    observation: SourceObservation,
) -> AdapterSnapshot:
    """Purely validate an observation and derive the content-addressed snapshot."""
    raise NotImplementedError


def snapshot_registered_adapters(
    *,
    project_entries: object,
    operator_entries: object,
    observe: Callable[[Path], SourceObservation] = observe_local_source,
) -> tuple[tuple[AdapterSnapshot, ...], tuple[AdapterProblem, ...]]:
    """Compose parse -> per-ID merge -> observe -> probe; retain all failures."""
    raise NotImplementedError


def select_snapshot(
    *,
    snapshots: tuple[AdapterSnapshot, ...],
    envelope: Mapping[str, object],
    selected_snapshot_id: str,
    deploy_grade_scope: DeployGradeScope,
) -> DataSourceSelection:
    """Require the exact snapshot in both the thread snapshot and envelope projection."""
    raise NotImplementedError


def require_snapshot(
    snapshots: tuple[AdapterSnapshot, ...],
    snapshot_id: str,
) -> AdapterSnapshot:
    raise NotImplementedError
```

`observe_local_source` is the only read boundary in the snapshot pipeline. Parsing, merge precedence, probe validation, ID derivation, and selection are pure and deterministic. Tests pass a `SourceObservation`; they do not need a fake service or global registry. The convenience composer accepts the observer as a plain function, not an interface hierarchy.

`research_harness.runtime_inputs` owns the one impure transition from a selected snapshot to a runnable workspace input:

```python
RUNTIME_INPUT_MANIFEST = PurePosixPath("runtime_inputs.json")
RUNTIME_INPUT_ENV = "RESEARCH_HARNESS_INPUT_MANIFEST"


def dataset_spec_for_snapshot(snapshot: AdapterSnapshot) -> dict[str, object]:
    """Pure adapter from the domain record to the existing materializer wire shape."""
    raise NotImplementedError


def bind_runtime_input(
    *,
    snapshot: AdapterSnapshot,
    node_id: str,
    workspace: Path,
    repo_root: Path,
    materialize_fn: Callable[..., MaterializeResult] = materialize,
) -> RuntimeInputManifest:
    """Materialize, verify the snapshot hash, stage it locally, and atomically write one manifest."""
    raise NotImplementedError


def plan_input_reference(runtime: RuntimeInputManifest) -> dict[str, object]:
    return {
        "runtime_manifest": RUNTIME_INPUT_MANIFEST.as_posix(),
        "snapshot_id": str(runtime.primary_dataset.snapshot_id),
    }


def validate_runtime_input_reference(
    inputs: Mapping[str, object],
    workspace: Path,
) -> RuntimeInputManifest:
    """Runner boundary: parse the workspace manifest and verify path/hash/snapshot agreement."""
    raise NotImplementedError


def runtime_input_environment(
    manifest: RuntimeInputManifest,
    *,
    inherited: Mapping[str, str],
) -> dict[str, str]:
    """Return child environment with one workspace-relative manifest pointer."""
    raise NotImplementedError
```

`dataset_spec_for_snapshot` uses the snapshot ID, not the mutable adapter ID, as `dataset_spec.id`. That makes the existing `.dataset_cache/<type>/<id>/...` path content-addressed without changing the materializer protocol. `bind_runtime_input` calls the existing `research_harness.datasets.materialize()`, verifies the returned bytes against the snapshot, stages them at `workspace/inputs/<snapshot_id>/...`, and writes `runtime_inputs.json` by temp-file replacement. A repeat with the same snapshot verifies and returns the same manifest; a different snapshot in an occupied workspace fails instead of overwriting evidence.

### Persisted shapes

`production/adapter_snapshots.json` is the single thread-owned capability artifact:

```json
{
  "schema_version": 1,
  "snapshots": [
    {
      "snapshot_id": "as_<canonical-sha256>",
      "adapter_id": "arxiv_category_taxonomy",
      "materializer_type": "benchmark",
      "role": "evaluation",
      "source": "/absolute/operator-owned/source.json",
      "provenance": "Frozen arXiv category taxonomy shipped with research_harness",
      "content_sha256": "<data-sha256>",
      "size_bytes": 12345,
      "entry_count": 1
    }
  ],
  "problems": []
}
```

It is created before `feasibility_envelope.json`; the envelope projects adapter ID, snapshot ID, and scope note from it. Jobs never resolve the live settings again. There is no global mutable catalog and no second binding receipt.

The one workspace artifact is `runtime_inputs.json`:

```json
{
  "schema_version": 1,
  "node_id": "n_example",
  "primary_dataset": {
    "snapshot_id": "as_<canonical-sha256>",
    "adapter_id": "arxiv_category_taxonomy",
    "relative_path": "inputs/as_<canonical-sha256>/arxiv_categories.json",
    "content_sha256": "<data-sha256>",
    "size_bytes": 12345,
    "fetcher_type": "local_path",
    "provenance": "Frozen arXiv category taxonomy shipped with research_harness"
  }
}
```

The legacy `dataset_manifest.json` remains owned by the removed refiner path. The active pipeline does not copy, extend, or synchronize it.

### Module map

- `research_harness/data_adapters.py` is new. It owns the immutable records, canonical declaration parsing, per-ID operator precedence, source observation-to-snapshot conversion, JSON boundary codecs, and explicit snapshot selection. It owns no process, cache, lock, or mutable singleton.
- `research_harness/runtime_inputs.py` is new. It adapts one `AdapterSnapshot` to the existing materializer, verifies content identity, stages workspace-local bytes, writes/reads the sole runtime manifest, and builds the runner environment.
- `research_harness/frontend/datasets.py` keeps upload and settings persistence. Its write boundary emits the canonical `materializer_type` and `role` fields and calls the same parser before saving; its list view uses `parse_adapter_entries` plus `merge_adapter_entries` rather than a third merge policy.
- `research_harness/thread_supervisor.py` remains the production shell. Before envelope bootstrap it reads project and operator settings, calls `snapshot_registered_adapters`, atomically writes the thread snapshot artifact, and advertises only ready snapshots. An existing thread snapshot is immutable on resume.
- `research_harness/mcp_server.py` changes at two orchestration points only. `seed_forest_from_connector` parses `deploy_grade_scope` plus `selected_snapshot_id`, calls `select_snapshot`, and passes `DataSourceSelection` into the forest. `execute_node_experiment` loads the stamped snapshot and calls `bind_runtime_input` before moving the node to `running`.
- `research_harness/connector/forest.py` accepts `data_selection: DataSourceSelection` and stamps its two IDs and scope into every root. Connector generation and `connector_session.json` remain data-agnostic.
- `research_harness/orchestrator/experiment_plan.py` no longer accepts Professor-authored runtime authority. The harness stamps the validated `{runtime_manifest, snapshot_id}` input reference after plan construction; job-manifest construction preserves it.
- `research_harness/runner/local_runner.py` validates the manifest is inside the workspace, adds only `RESEARCH_HARNESS_INPUT_MANIFEST` to the inherited environment, and records `input_evidence={manifest_path, manifest_sha256, snapshot_id, adapter_id}` in `runner_result.json`.
- Schemas add `adapter_snapshots.schema.json` and `runtime_inputs.schema.json`; add `snapshot_id` to real envelope sources and `data_source_snapshot_id` to node claim contracts; replace opaque experiment/job `inputs` with `{runtime_manifest, snapshot_id}`; and add required runner input evidence when a runtime manifest is present. `user_experiment_plan_metadata.schema.json` drops `inputs` because it is harness-owned.

### Invariants and interface depth

- Only successfully observed and probed declarations become snapshots or envelope capabilities, per boundary discipline.
- Operator-over-project precedence is a pure per-ID rule used by both UI and runtime, so there is one source of truth.
- A forest root names both a mutable human adapter ID and an immutable execution snapshot ID. Child construction copies the claim contract; revisions may inherit the same selection or explicitly select and revalidate another snapshot, never edit IDs independently.
- Binding occurs before `ready -> running`; adapter failure returns a resource problem without leaving a half-running node.
- Materialization is reused, not reimplemented. The binding module adds the missing policy: content identity, workspace delivery, and evidence.
- The public operational surface is three calls: snapshot at bootstrap, select at forest seed, bind at execution. Each hides parsing, precedence, hashing, materializer adaptation, idempotence, and schema checks without introducing a service lifecycle. This is a deep functional module rather than a shallow service wrapper.
- The design does not sandbox generated Python, prove exclusive input consumption, fetch arbitrary URLs, load adapter modules dynamically, revive the refiner, or certify a real holdout. Those are separate trust boundaries.

For the acceptance run, register the frozen arXiv taxonomy, create a fresh thread through the frontend, complete grilling and connector, then start the supervisor with `target_scope=deployment`. The seed call must select the arXiv snapshot, which makes real binding mandatory. The absence of an external falsifier still caps the scientific result at `unverified_screen`; that is acceptable because the system test is proving adapter-grounded execution and a terminal lifecycle, not a positive research claim.

## Synthesis decision

Candidate C deliberately selects a functional, content-addressed pipeline as its whole-shape bet. It rejects a stateful catalog/service base because no caller needs long-lived queries, subscriptions, or mutation coordination: settings are read once, thread snapshots are immutable, and jobs bind one selected record. It incorporates the critics' two-lifetime correction by using the same `AdapterSnapshot` value at bootstrap and execution without pretending those operations happen in one service transaction. It also moves snapshot selection into the active connector-forest handoff and makes deployment-scope acceptance mandatory, addressing the two places where an otherwise-correct late binder would remain unexercised. Arena synthesis should retain this candidate only if the other candidates cannot demonstrate that a service object hides additional real policy rather than adding pass-through methods.

## Tradeoffs accepted

- We accept hashing local data at production bootstrap in exchange for an immutable execution identity and honest readiness evidence.
- We accept one primary real dataset per claim in exchange for a singular anchor and one small runtime-manifest contract; multiple inputs require a schema-versioned extension later.
- We accept rejecting the legacy source-less `real_panel` declaration in exchange for never guessing an executable materializer type or ambient path.
- We accept duplicating or hard-linking materialized bytes into a node workspace in exchange for a stable, workspace-relative child interface and reviewable evidence.
- We accept delivery evidence rather than proof of exclusive consumption in exchange for keeping runner sandboxing outside this adapter change.

## Alternatives considered

- A stateful `AdapterCatalog` with `load`, `probe`, `select`, and `bind` methods lost because callers would still need to coordinate those temporal methods, while the object would retain no legitimate mutable state after a thread snapshot is written. It exposes a larger lifecycle interface but hides no more policy than the three functional boundaries.
- A single late binder from `data_source_anchor` to `LocalRunner` lost because the feasibility envelope would continue advertising unprobed settings entries and the active connector forest still would not carry an anchor.
- Passing a resolved absolute source path directly through `ExperimentPlan.inputs` lost because it couples generated code to host layout, makes mutable settings the runtime authority, bypasses materializers, and gives retries no byte identity.
- Reviving `research_refiner` to produce its legacy `dataset_manifest.json` lost because it restores an inactive user-interview phase and a second plan authority merely to reach functionality that two focused functions can reuse directly.

## Open questions and risks

- Should v1 hash directory adapters recursively, or reject directories until there is a bounded tree-hash policy for very large sources?
- May operator deletion remove uploaded bytes referenced by an active thread snapshot, or should thread snapshots pin uploaded content until the thread is archived?
- Should a non-OKX deployment acceptance run expose exactly one ready adapter so Codex must select it, or should the frontend collect `selected_snapshot_id` alongside target scope?
- Is recording that the runner delivered a verified manifest sufficient for this iteration, or does the acceptance bar require a sandbox or experiment-written consumption receipt?
- Should old OKX threads retain their persisted envelope behavior while new source-less OKX declarations become unready, or is a one-time explicit OKX declaration migration required before rollout?

## Next implementation step

Add the frozen record types plus pure parse, per-ID merge, observation-to-snapshot, and selection tests in `research_harness/data_adapters.py`, then freeze the two new JSON schemas before wiring any caller.
