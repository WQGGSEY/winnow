from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from research_harness.acquisition import (
    AcquisitionBudget,
    AcquisitionComplete,
    NeedPlan,
    PublicAcquisition,
    RegisteredSource,
    make_acquisition_command,
)
from research_harness.data_adapters import probe_registered_adapters
from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.direction_generation import (
    DataNeed,
    make_direction_draft,
    make_direction_fingerprint,
)
from research_harness.runner.evidence import build_worker_report_from_runner_evidence
from research_harness.runner.job_manifest import build_demo_job_manifest
from research_harness.runner.local_runner import LocalRunner
from research_harness.runtime_inputs import (
    RUNTIME_INPUT_ENV,
    RuntimeInputError,
    bind_acquisition_manifest,
    bind_runtime_input,
    runtime_input_environment,
    validate_runtime_input_reference,
)


def _snapshot(repo, content="dataset"):
    source = repo / "source.json"
    source.write_text(content, encoding="utf-8")
    (repo / "settings.json").write_text(
        json.dumps(
            {
                "data_adapters": {
                    "registered": [
                        {
                            "id": "local_data",
                            "materializer_type": "benchmark",
                            "role": "evaluation",
                            "source": str(source),
                            "provenance": "fixture",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return probe_registered_adapters(repo)["snapshots"][0], source


def test_binding_is_idempotent_and_validates_evidence(tmp_path):
    snapshot, _ = _snapshot(tmp_path)
    workspace = tmp_path / "run" / "nodes" / "n1" / "workspace"

    first = bind_runtime_input(snapshot=snapshot, workspace=workspace, repo_root=tmp_path)
    second = bind_runtime_input(snapshot=snapshot, workspace=workspace, repo_root=tmp_path)
    evidence = validate_runtime_input_reference(first, workspace=workspace)

    assert second == first
    assert evidence["adapter_id"] == "local_data"
    assert evidence["snapshot_id"] == snapshot["snapshot_id"]
    assert runtime_input_environment(evidence, workspace=workspace) == {
        RUNTIME_INPUT_ENV: str(workspace / "runtime_inputs.json")
    }


def test_binding_rejects_source_drift(tmp_path):
    snapshot, source = _snapshot(tmp_path)
    source.write_text("changed", encoding="utf-8")

    with pytest.raises(RuntimeInputError, match="hash mismatch"):
        bind_runtime_input(
            snapshot=snapshot,
            workspace=tmp_path / "workspace",
            repo_root=tmp_path,
        )


def test_runner_validation_rejects_tampered_staged_bytes(tmp_path):
    snapshot, _ = _snapshot(tmp_path)
    workspace = tmp_path / "workspace"
    reference = bind_runtime_input(snapshot=snapshot, workspace=workspace, repo_root=tmp_path)
    manifest = json.loads((workspace / "runtime_inputs.json").read_text(encoding="utf-8"))
    staged = workspace / manifest["primary_dataset"]["relative_path"]
    staged.write_text("tampered", encoding="utf-8")

    with pytest.raises(RuntimeInputError, match="hash mismatch"):
        validate_runtime_input_reference(reference, workspace=workspace)


def test_binding_repairs_tampered_staged_bytes_for_retry(tmp_path):
    snapshot, _ = _snapshot(tmp_path)
    workspace = tmp_path / "workspace"
    reference = bind_runtime_input(
        snapshot=snapshot,
        workspace=workspace,
        repo_root=tmp_path,
    )
    manifest = json.loads((workspace / "runtime_inputs.json").read_text())
    staged = workspace / manifest["primary_dataset"]["relative_path"]
    staged.write_text("tampered", encoding="utf-8")

    rebound = bind_runtime_input(
        snapshot=snapshot,
        workspace=workspace,
        repo_root=tmp_path,
    )

    assert rebound == reference
    assert validate_runtime_input_reference(rebound, workspace=workspace)


def test_binding_repairs_staged_symlink_without_writing_outside(tmp_path):
    snapshot, _ = _snapshot(tmp_path)
    workspace = tmp_path / "workspace"
    reference = bind_runtime_input(
        snapshot=snapshot,
        workspace=workspace,
        repo_root=tmp_path,
    )
    manifest = json.loads((workspace / "runtime_inputs.json").read_text())
    staged = workspace / manifest["primary_dataset"]["relative_path"]
    outside = tmp_path / "outside"
    staged.unlink()
    staged.symlink_to(outside)

    rebound = bind_runtime_input(
        snapshot=snapshot,
        workspace=workspace,
        repo_root=tmp_path,
    )

    assert rebound == reference
    assert not outside.exists()
    assert validate_runtime_input_reference(rebound, workspace=workspace)


def test_manifest_digest_covers_persisted_bytes(tmp_path):
    snapshot, _ = _snapshot(tmp_path)
    workspace = tmp_path / "workspace"
    reference = bind_runtime_input(snapshot=snapshot, workspace=workspace, repo_root=tmp_path)

    evidence = validate_runtime_input_reference(reference, workspace=workspace)

    assert evidence["manifest_sha256"] == hashlib.sha256(
        (workspace / "runtime_inputs.json").read_bytes()
    ).hexdigest()


def _acquisition_fixture(tmp_path):
    source = tmp_path / "public.csv"
    source.write_text("day,value\n1,7\n", encoding="utf-8")
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    need = DataNeed(kind="registered_adapter", description="Daily values")
    direction = make_direction_draft(
        claim="Adaptive scheduling improves utility.",
        fingerprint=make_direction_fingerprint(
            mechanism="load feedback",
            intervention="adaptive scheduling",
            observables_and_data="daily utility",
            analysis_unit="daily cohort",
            timescale="four weeks",
            system_boundary="regional service",
        ),
        experiment_objective="Compare utility against baselines.",
        data_needs=(need,),
        predicted_outcomes=("margin clears", "margin misses"),
    )
    registered = RegisteredSource(
        adapter_id="events",
        snapshot_id="as_" + "1" * 64,
        path=str(source),
        content_sha256="sha256:" + digest,
        size_bytes=len(content),
        entry_count=1,
        provenance="operator fixture",
        retrieved_at="thread snapshot",
    )
    command = make_acquisition_command(
        reservation_id="reservation_" + "2" * 64,
        node_id="n_blind_runtime",
        attempt_id="attempt_runtime",
        direction=direction,
        needs=(NeedPlan(0, need, (registered,)),),
        budget=AcquisitionBudget(0, 0, 30),
    )
    cache_root = tmp_path / "cache"
    outcome = PublicAcquisition(cache_root).acquire(command)
    assert isinstance(outcome, AcquisitionComplete)
    return outcome.manifest, cache_root


def test_pinned_acquisition_manifest_binds_as_one_runtime_dataset(tmp_path):
    manifest, cache_root = _acquisition_fixture(tmp_path)
    workspace = tmp_path / "workspace"

    first = bind_acquisition_manifest(
        manifest=manifest,
        cache_root=cache_root,
        workspace=workspace,
    )
    second = bind_acquisition_manifest(
        manifest=manifest,
        cache_root=cache_root,
        workspace=workspace,
    )
    evidence = validate_runtime_input_reference(first, workspace=workspace)
    runtime_manifest = json.loads(
        (workspace / "runtime_inputs.json").read_text(encoding="utf-8")
    )
    aggregate = workspace / runtime_manifest["primary_dataset"]["relative_path"]
    index = json.loads((aggregate / "index.json").read_text(encoding="utf-8"))

    assert second == first
    assert evidence["adapter_id"] == "acquisition"
    assert evidence["snapshot_id"] == "as_" + manifest.manifest_id[12:]
    assert index["manifest_id"] == manifest.manifest_id
    assert index["needs"][0]["description"] == "Daily values"


def test_acquisition_binding_repairs_derived_documents(tmp_path):
    manifest, cache_root = _acquisition_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    reference = bind_acquisition_manifest(
        manifest=manifest,
        cache_root=cache_root,
        workspace=workspace,
    )
    runtime_manifest = json.loads(
        (workspace / "runtime_inputs.json").read_text(encoding="utf-8")
    )
    aggregate = workspace / runtime_manifest["primary_dataset"]["relative_path"]
    index_path = aggregate / "index.json"
    manifest_path = workspace / "runtime_inputs.json"
    index_path.write_text("tampered", encoding="utf-8")
    manifest_path.write_text("tampered", encoding="utf-8")

    repaired = bind_acquisition_manifest(
        manifest=manifest,
        cache_root=cache_root,
        workspace=workspace,
    )

    assert repaired == reference
    assert validate_runtime_input_reference(repaired, workspace=workspace)


def test_acquisition_binding_ignores_predictable_legacy_temporaries(tmp_path):
    manifest, cache_root = _acquisition_fixture(tmp_path)
    hostile_workspace = tmp_path / "hostile_workspace"
    hostile_aggregate = (
        hostile_workspace
        / "inputs"
        / ("as_" + manifest.manifest_id.removeprefix("acqmanifest_"))
    )
    hostile_aggregate.mkdir(parents=True)
    outside_staged = tmp_path / "outside_staged"
    outside_index = tmp_path / "outside_index"
    outside_manifest = tmp_path / "outside_manifest"
    (hostile_aggregate / "need_000.csv.staging").symlink_to(outside_staged)
    (hostile_aggregate / "index.json.tmp").symlink_to(outside_index)
    (hostile_workspace / "runtime_inputs.json.tmp").symlink_to(outside_manifest)

    bind_acquisition_manifest(
        manifest=manifest,
        cache_root=cache_root,
        workspace=hostile_workspace,
    )

    assert not outside_staged.exists()
    assert not outside_index.exists()
    assert not outside_manifest.exists()


def test_acquisition_binding_rejects_parent_symlink_escape(tmp_path):
    manifest, cache_root = _acquisition_fixture(tmp_path)
    escaped_workspace = tmp_path / "escaped_workspace"
    escaped_workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (escaped_workspace / "inputs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeInputError, match="must stay under"):
        bind_acquisition_manifest(
            manifest=manifest,
            cache_root=cache_root,
            workspace=escaped_workspace,
        )
    assert list(outside.iterdir()) == []


def test_local_runner_sets_environment_and_propagates_input_evidence(tmp_path):
    snapshot, _ = _snapshot(tmp_path)
    run_dir = tmp_path / "run"
    node = _demo_node()
    manifest = build_demo_job_manifest(node, run_dir)
    workspace = (run_dir / "nodes" / node["id"] / "workspace").resolve()
    manifest["inputs"] = bind_runtime_input(
        snapshot=snapshot, workspace=workspace, repo_root=tmp_path
    )
    (workspace / "experiment.py").write_text(
        "import os\nprint(os.environ['RESEARCH_HARNESS_INPUT_MANIFEST'])\n",
        encoding="utf-8",
    )

    runner_result = LocalRunner(run_dir).execute(manifest)
    report = build_worker_report_from_runner_evidence(
        node, manifest, runner_result, run_dir
    ).worker_report

    assert Path(runner_result["stdout_path"]).read_text(encoding="utf-8").strip() == str(
        workspace / "runtime_inputs.json"
    )
    assert runner_result["input_evidence"]["snapshot_id"] == snapshot["snapshot_id"]
    assert report["input_evidence"] == runner_result["input_evidence"]
    assert "nodes/n_demo_001/workspace/runtime_inputs.json" in report["artifacts"]
