# Candidate B: manifest-first dataset binding

## Problem

The active connector-forest pipeline names data by an unchecked adapter ID, while executable acquisition lives in the legacy `dataset_spec` materializers. The two lifetimes are different. Production must probe data before it advertises feasibility, then deliver the selected immutable snapshot when a node runs. This candidate uses one versioned `DatasetManifest` as the authority for both lifetimes. It does not add a binding receipt, plugin loader, or second fetcher framework.

## Usage (caller's view)

The operator registers canonical dataset specs. Project entries load first; operator entries replace project entries with the same `dataset_spec.id`.

```json
{
  "data_adapters": {
    "registered": [
      {
        "dataset_spec": {
          "id": "arxiv_category_taxonomy",
          "type": "raw_data",
          "role": "evaluation",
          "source": "file:///abs/research_harness/research_harness/connector/data/arxiv_categories.json"
        },
        "provenance": "Frozen arXiv category taxonomy shipped with research_harness"
      }
    ]
  }
}
```

There is no `kind`, `module`, or parallel adapter ID. `dataset_spec.id` is the anchor. The one-time settings migration rewrites the OKX declaration as `type="raw_data"`, `role="other"`, and an explicit `file://` source. Old entry shapes fail validation instead of entering a compatibility path.

### Call site 1: capability bootstrap

`thread_supervisor.bootstrap_envelope_if_missing()` creates or loads the thread manifest before it writes the feasibility envelope.

```python
manifest = ensure_thread_dataset_manifest(repo, thread_dir)
selection = manifest.choose(preferred_id=data_source_anchor)

envelope = build_feasibility_envelope(
    dataset_manifest_id=manifest.manifest_id,
    data_sources_available=manifest.capabilities(),
    operator_data_source_anchor=selection.dataset_id,
    target_scope=target_scope,
    # existing oracle, compute, baseline, and falsifier fields stay unchanged
)
```

`watch --data-source-anchor ID` supplies the operator choice. If omitted, `choose()` accepts the only ready dataset. Zero or multiple ready datasets produce a concrete error with the available IDs. The envelope contains `dataset_manifest_id`, and `operator_intent.data_source_anchor` names one ready entry.

### Call site 2: connector forest seeding

`handle_seed_forest_from_connector()` reads the envelope and the same manifest. It stamps every connector root before draft creation.

```python
manifest = load_thread_dataset_manifest(thread_dir)
anchor = manifest.require(
    manifest_id=envelope["dataset_manifest_id"],
    dataset_id=envelope["operator_intent"]["data_source_anchor"],
)

state = build_forest_search_state(
    grilling,
    claims,
    search_id=f"s_{tid}",
    policy=policy,
    deploy_grade_scope=envelope["operator_intent"]["target_deploy_grade_scope"],
    data_source_anchor=anchor.dataset_id,
)
```

`_build_forest_root()` writes both fields into `claim_contract`. Draft and successor creation already deep-copies the parent, so descendants keep the same anchor unless the existing revision tool validates and replaces it.

### Call site 3: plan, job, and runner

`handle_execute_node_experiment()` stages the exact thread manifest in the node workspace before changing the node to `running`.

```python
dataset_input = stage_dataset_input(
    thread_dir=thread_dir,
    dataset_id=node["claim_contract"]["data_source_anchor"],
    workspace=node_workspace,
)

plan, template = build_experiment_plan_for_node(
    repo,
    node,
    run_dir,
    settings=settings,
    dataset_input=dataset_input,
)
job = build_job_manifest_from_experiment_plan(node, plan, run_dir)
runner_result = LocalRunner(run_dir, settings=settings).execute(job)
```

The harness, not the Professor, stamps this typed plan and job input:

```json
{
  "dataset_manifest": {
    "path": "inputs/dataset_manifest.json",
    "manifest_id": "dsm_sha256_...",
    "dataset_ids": ["arxiv_category_taxonomy"]
  }
}
```

The runner validates the manifest and selected content identity, then exposes two generic variables:

```text
RESEARCH_HARNESS_DATASET_MANIFEST=/abs/node/workspace/inputs/dataset_manifest.json
RESEARCH_HARNESS_DATASET_ID=arxiv_category_taxonomy
```

Experiment code reads the selected entry and opens its `materialized_path`. No topic-specific environment variable is needed.

## Shape

### Types

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, NewType

DatasetId = NewType("DatasetId", str)
ManifestId = NewType("ManifestId", str)
ContentDigest = NewType("ContentDigest", str)
DatasetType = Literal[
    "raw_data", "benchmark", "model_weights",
    "factor_set", "synthetic", "custom",
]
DatasetRole = Literal[
    "training", "evaluation", "baseline_reference",
    "baseline_implementation", "other",
]
SettingsScope = Literal["project", "operator"]


@dataclass(frozen=True)
class LocalDatasetSpec:
    id: DatasetId
    type: Literal["raw_data", "benchmark", "model_weights", "factor_set", "custom"]
    role: DatasetRole
    source: Path                 # parsed absolute path


@dataclass(frozen=True)
class HuggingFaceDatasetSpec:
    id: DatasetId
    type: Literal["raw_data", "benchmark", "model_weights"]
    role: DatasetRole
    repo_id: str
    split: str | None
    revision: str | None


@dataclass(frozen=True)
class SyntheticDatasetSpec:
    id: DatasetId
    type: Literal["synthetic"]
    role: DatasetRole
    recipe: Mapping[str, object]


DatasetSpec = LocalDatasetSpec | HuggingFaceDatasetSpec | SyntheticDatasetSpec


@dataclass(frozen=True)
class RegisteredDataset:
    spec: DatasetSpec
    provenance: str
    source_scope: SettingsScope


@dataclass(frozen=True)
class ContentIdentity:
    algorithm: Literal["sha256-file-v1", "sha256-tree-v1"]
    digest: ContentDigest
    size_bytes: int
    file_count: int


@dataclass(frozen=True)
class ManifestDataset:
    spec: DatasetSpec
    provenance: str
    source_scope: SettingsScope
    materialized_path: Path       # required, absolute, readable
    fetcher_type: str
    content: ContentIdentity


@dataclass(frozen=True)
class ProbeFailure:
    dataset_id: DatasetId
    status: Literal[
        "failed", "needs_credential", "needs_user_input", "unsupported_type"
    ]
    error: str


@dataclass(frozen=True)
class DatasetManifest:
    schema_version: Literal[2]
    manifest_id: ManifestId
    created_at: str
    datasets: Mapping[DatasetId, ManifestDataset]
    unavailable: Mapping[DatasetId, ProbeFailure]

    def capabilities(self) -> list[dict[str, str]]:
        raise NotImplementedError

    def choose(self, preferred_id: str | None) -> ManifestDataset:
        raise NotImplementedError

    def require(self, *, manifest_id: str, dataset_id: str) -> ManifestDataset:
        raise NotImplementedError


@dataclass(frozen=True)
class DatasetInput:
    manifest_path: str            # workspace-relative
    manifest_id: ManifestId
    dataset_ids: tuple[DatasetId, ...]


@dataclass(frozen=True)
class DatasetInputEvidence:
    manifest_id: ManifestId
    dataset_ids: tuple[DatasetId, ...]
    content_digests: Mapping[DatasetId, ContentDigest]
```

The persisted `DatasetManifest` remains close to the existing schema. Version 2 adds `manifest_id`, canonical registered-spec snapshots, provenance and scope, required content identity, and `unavailable` probe results. An `ok` result with no concrete path becomes `unavailable`, never a ready entry.

`manifest_id` is SHA-256 over canonical JSON containing the sorted ready entries' specs, provenance, scope, fetcher type, and content identities. It excludes timestamps and absolute cache locations. Two paths containing the same declared snapshot produce the same semantic identity.

Files use `sha256-file-v1`. Directories use `sha256-tree-v1`, calculated over sorted records of relative path, file size, and file digest. The walker rejects symlinks. `LocalPathMaterializer` uses the same digest in its cache path:

```text
.dataset_cache/<type>/<dataset-id>/<content-digest>/<basename>
```

This is the only required materializer change. The dispatcher and the Hugging Face and synthetic materializers remain the acquisition mechanism. The manifest builder rejects any successful materializer result without a readable path and fingerprints every accepted path.

### Signatures

```python
# research_harness/datasets/manifest.py

def resolve_registered_datasets(repo_root: Path) -> tuple[RegisteredDataset, ...]:
    """Parse canonical project and operator entries; operator wins by dataset ID."""
    raise NotImplementedError


def ensure_thread_dataset_manifest(
    repo_root: Path,
    thread_dir: Path,
) -> DatasetManifest:
    """Load an existing immutable manifest or probe once and atomically create it."""
    raise NotImplementedError


def load_thread_dataset_manifest(thread_dir: Path) -> DatasetManifest:
    """Parse, validate, and recompute manifest_id at the persistence boundary."""
    raise NotImplementedError


def stage_dataset_input(
    *,
    thread_dir: Path,
    dataset_id: str,
    workspace: Path,
) -> DatasetInput:
    """Verify the selected entry and atomically copy the exact manifest into workspace."""
    raise NotImplementedError


def fingerprint_path(path: Path) -> ContentIdentity:
    """Return deterministic file or tree identity; reject symlinks and special files."""
    raise NotImplementedError


# research_harness/runner/dataset_inputs.py

def validate_dataset_input(
    raw_inputs: object,
    *,
    workspace: Path,
) -> tuple[DatasetInput, DatasetInputEvidence]:
    """Parse typed job input, contain its manifest path, and verify selected bytes."""
    raise NotImplementedError


def dataset_environment(
    dataset_input: DatasetInput,
    *,
    workspace: Path,
) -> dict[str, str]:
    """Return only the two stable dataset variables added to the child environment."""
    raise NotImplementedError
```

`LocalRunner.validate_or_raise()` calls `validate_dataset_input()`. `execute()` merges `dataset_environment()` into a sanitized child environment. `_runner_result()` adds required `dataset_input_evidence`; `build_worker_report_from_runner_evidence()` carries the manifest path as an artifact and copies the evidence object into the worker report. Existing runner and worker schemas gain the same typed evidence shape.

### Module map

```text
research_harness/datasets/manifest.py
  Owns canonical DatasetSpec parsing, project/operator precedence,
  materializer adaptation, content identity, manifest persistence, selection,
  and workspace staging. This is the deep public module.

research_harness/runner/dataset_inputs.py
  Owns the subprocess boundary: typed input parsing, workspace containment,
  manifest/content verification, generic environment, and evidence echo.

research_harness/datasets/local_path.py
  Keeps acquisition ownership; changes only to content-addressed cache targets.

research_harness/thread_supervisor.py
  Calls ensure_thread_dataset_manifest and derives envelope capabilities from it.

research_harness/connector/forest.py
  Accepts one validated scope and anchor and stamps every root before drafts exist.

research_harness/mcp_server.py
  Reads the manifest for forest seeding and stages it for node execution.

research_harness/orchestrator/experiment_plan.py
  Accepts harness-owned DatasetInput. Professor and user metadata cannot author it.

schemas/
  Version dataset_manifest; type feasibility manifest reference, plan/job inputs,
  runner evidence, and worker evidence.
```

The public dataset interface hides settings precedence, wire parsing, materializer dictionaries, content hashing, atomic persistence, and manifest selection. Callers see one immutable manifest and one typed job input. This is a deep enough interface for the work it performs, per `boundary-discipline` and `minimize-reader-load`.

The manifest is the single source of truth. The feasibility envelope stores only its ID and ready dataset IDs. Claims store one selected dataset ID. Plans and jobs store a workspace-relative manifest reference plus that ID. Runner and worker evidence echo identities but do not become authorities. No model-authored field can replace a probed entry, per `encode-lessons-in-structure`.

Thread manifests are write-once. Retry loads the existing manifest. Job staging writes an exact atomic copy and verifies the selected content before every execution. A changed or missing cache path fails before the node enters `running`. This makes retries converge on the same snapshot, per `make-operations-idempotent`.

## Synthesis decision

This is arena candidate B, the manifest-first alternative. It deliberately rejects a separate adapter binder plus receipt because that would create another persisted representation beside `dataset_manifest.json`. The arena synthesizer should compare this candidate's single-artifact authority against candidates that keep catalog and job receipts separate. No cross-candidate graft has happened yet.

## Tradeoffs accepted

- We accept hashing every newly probed dataset and rechecking the selected dataset at job start in exchange for real content identity. Large remote snapshots may make this expensive.
- We accept one thread-wide manifest copied into each node workspace in exchange for one artifact format and simple retry semantics. The typed `dataset_ids` field limits the declared selection, but the current runner is not an OS sandbox.
- We accept a one-time settings migration and rejection of legacy adapter rows in exchange for removing `kind`, `module`, and adapter-ID translation rules.
- We accept one data anchor for every root in a connector forest in exchange for deterministic grounding. Per-root data selection can be added later only if a real use case requires it.

## Alternatives considered

- A registry resolver that returns a new per-job binding receipt lost because it creates a second authority beside `DatasetManifest` and makes callers reconcile receipt, plan inputs, and dataset manifest.
- Passing absolute paths directly in `ExperimentPlan.inputs` lost because it exposes cache layout, omits content identity, and gives the runner nothing stable to verify.
- Loading Python modules named in adapter settings lost because the current materializers already own acquisition. A plugin contract would expose more interface while solving no acceptance requirement.

## Open questions and risks

- Is full content hashing acceptable for the largest Hugging Face model snapshots, or should those require an immutable upstream revision plus a cheaper verified snapshot identity?
- Should the runner merely validate and expose materialized paths, as proposed, or must a later security project add an OS sandbox with read-only mounts?
- Should changing the selected adapter after forest seeding be forbidden, or permitted only through `revise_root_after_reject` with a new manifest-backed anchor?

## Next implementation step

Implement `research_harness/datasets/manifest.py` with canonical settings parsing, per-ID project/operator precedence, content identity, and an idempotent thread manifest before changing the envelope or runner.
