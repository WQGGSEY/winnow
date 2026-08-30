from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from research_harness.data_adapters import probe_registered_adapters
from research_harness.orchestrator.demo import _demo_node
from research_harness.runner.evidence import build_worker_report_from_runner_evidence
from research_harness.runner.job_manifest import build_demo_job_manifest
from research_harness.runner.local_runner import LocalRunner
from research_harness.runtime_inputs import (
    RUNTIME_INPUT_ENV,
    RuntimeInputError,
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


def test_manifest_digest_covers_persisted_bytes(tmp_path):
    snapshot, _ = _snapshot(tmp_path)
    workspace = tmp_path / "workspace"
    reference = bind_runtime_input(snapshot=snapshot, workspace=workspace, repo_root=tmp_path)

    evidence = validate_runtime_input_reference(reference, workspace=workspace)

    assert evidence["manifest_sha256"] == hashlib.sha256(
        (workspace / "runtime_inputs.json").read_bytes()
    ).hexdigest()


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
