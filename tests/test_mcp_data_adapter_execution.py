from __future__ import annotations

import json

import research_harness.mcp_server as mcp
from research_harness.acquisition import (
    AcquisitionBudget,
    AcquisitionComplete,
    NeedPlan,
    PublicAcquisition,
    RegisteredSource,
    make_acquisition_command,
)
from research_harness.data_adapters import ensure_thread_adapter_snapshots
from research_harness.orchestrator import experiment_plan as plans
from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.direction_generation import (
    DataNeed,
    make_direction_draft,
    make_direction_fingerprint,
)
from research_harness.orchestrator.search_state import initialize_search_state


_POLICY = {
    "max_depth": 5,
    "max_debug_depth": 2,
    "sunk_cost_policy": "progress_gated",
    "scaleup_policy": "disallow_by_default",
}


def _arxiv_baseline_requirements():
    return [
        {
            "role": "current_best_known",
            "metric_key": "accuracy",
            "baseline_key": "word",
            "operator": "greater_than",
            "margin": 0,
            "required": True,
        },
        {
            "role": "naive",
            "metric_key": "macro_f1",
            "baseline_key": "majority",
            "operator": "greater_than",
            "margin": 0,
            "required": True,
        },
        {
            "role": "random_or_null",
            "metric_key": "macro_f1",
            "baseline_key": "label_shuffle",
            "operator": "greater_than",
            "margin": 0,
            "required": True,
        },
    ]


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_repo_root", lambda: tmp_path)
    source = tmp_path / "dataset.json"
    source.write_text('{"label": "data"}', encoding="utf-8")
    (tmp_path / "settings.json").write_text(
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
                },
                "runtime": {"llm_orchestrator": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    thread_dir = tmp_path / "runs" / "threads" / "thread_bound"
    document = ensure_thread_adapter_snapshots(tmp_path, thread_dir)
    snapshot = document["snapshots"][0]
    node = _demo_node()
    node["claim_contract"].update(
        {
            "deploy_grade_scope": "deployment",
            "data_source_anchor": snapshot["adapter_id"],
            "data_source_snapshot_id": snapshot["snapshot_id"],
        }
    )
    state = initialize_search_state(
        search_id="s_bound", root_node=node, policy=_POLICY
    )
    state_path = thread_dir / "production" / "tree" / "search_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        mcp,
        "_authoritative_active_node_id",
        lambda _tid: node["id"],
    )

    original_builder = plans.build_experiment_plan_for_node

    def builder(*args, **kwargs):
        plan, used = original_builder(*args, **kwargs)
        plan["source_files"][0]["content"] += (
            "\nimport os\nimport json\nfrom pathlib import Path\n"
            "_manifest_path = Path(os.environ['RESEARCH_HARNESS_INPUT_MANIFEST'])\n"
            "_manifest = json.loads(_manifest_path.read_text())\n"
            "_dataset_path = _manifest_path.parent / "
            "_manifest['primary_dataset']['relative_path']\n"
            "(_dataset_path / 'index.json').read_text() "
            "if _dataset_path.is_dir() else _dataset_path.read_text()\n"
        )
        return plan, used

    monkeypatch.setattr(plans, "build_experiment_plan_for_node", builder)
    return thread_dir, state_path, source, snapshot, node


def test_execute_binds_before_running_and_propagates_identity(tmp_path, monkeypatch):
    thread_dir, _, _, snapshot, node = _setup(tmp_path, monkeypatch)

    result = mcp.handle_execute_node_experiment(
        {"thread_id": "thread_bound", "node_id": node["id"]}
    )

    assert result["status"] == "ok"
    node_dir = thread_dir / "production" / "tree" / "nodes" / node["id"]
    plan = json.loads((node_dir / "experiment_plan.json").read_text())
    job = json.loads((node_dir / "job_manifest.json").read_text())
    runner = json.loads((node_dir / "workspace" / "runner_result.json").read_text())
    worker = json.loads((node_dir / "worker_report.json").read_text())
    assert plan["inputs"] == job["inputs"]
    assert plan["inputs"]["snapshot_id"] == snapshot["snapshot_id"]
    assert runner["input_evidence"]["snapshot_id"] == snapshot["snapshot_id"]
    assert worker["input_evidence"] == runner["input_evidence"]


def test_execute_binds_verified_acquisition_manifest(tmp_path, monkeypatch):
    thread_dir, state_path, source, snapshot, node = _setup(tmp_path, monkeypatch)
    need = DataNeed(kind="registered_adapter", description="Evaluation dataset")
    direction = make_direction_draft(
        claim="The intervention improves the target metric.",
        fingerprint=make_direction_fingerprint(
            mechanism="closed-loop feedback",
            intervention="adaptive control",
            observables_and_data="held-out measurements",
            analysis_unit="evaluation cohort",
            timescale="one evaluation cycle",
            system_boundary="deployed service",
        ),
        experiment_objective="Compare against every mandatory baseline.",
        data_needs=(need,),
        predicted_outcomes=("the margin clears", "the margin misses"),
    )
    registered = RegisteredSource(
        adapter_id=snapshot["adapter_id"],
        snapshot_id=snapshot["snapshot_id"],
        path=str(source),
        content_sha256="sha256:" + snapshot["content_sha256"],
        size_bytes=snapshot["size_bytes"],
        entry_count=snapshot["entry_count"],
        provenance=snapshot["provenance"],
        retrieved_at="thread snapshot",
    )
    command = make_acquisition_command(
        reservation_id="reservation_" + "1" * 64,
        node_id=node["id"],
        attempt_id="attempt_execution",
        direction=direction,
        needs=(NeedPlan(0, need, (registered,)),),
        budget=AcquisitionBudget(0, 0, 30),
    )
    cache_root = (
        thread_dir / "production" / "reorientation" / "acquisition_cache"
    )
    outcome = PublicAcquisition(cache_root).acquire(command)
    assert isinstance(outcome, AcquisitionComplete)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    persisted_node = next(item for item in state["nodes"] if item["id"] == node["id"])
    manifest_id = outcome.manifest.manifest_id
    persisted_node["claim_contract"].update(
        {
            "data_source_anchor": f"acquisition_manifest:{manifest_id}",
            "data_source_snapshot_id": "as_" + manifest_id.removeprefix(
                "acqmanifest_"
            ),
        }
    )
    persisted_node["outputs"]["artifacts"] = [
        f"acquisition_manifest:{manifest_id}"
    ]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = mcp.handle_execute_node_experiment(
        {"thread_id": "thread_bound", "node_id": node["id"]}
    )

    assert result["status"] == "ok"
    node_dir = thread_dir / "production" / "tree" / "nodes" / node["id"]
    job = json.loads((node_dir / "job_manifest.json").read_text())
    runner = json.loads((node_dir / "workspace" / "runner_result.json").read_text())
    assert job["inputs"]["snapshot_id"] == "as_" + manifest_id.removeprefix(
        "acqmanifest_"
    )
    assert runner["input_evidence"]["adapter_id"] == "acquisition"


def test_binding_failure_leaves_node_ready(tmp_path, monkeypatch):
    _, state_path, source, _, node = _setup(tmp_path, monkeypatch)
    source.write_text("changed after snapshot", encoding="utf-8")

    result = mcp.handle_execute_node_experiment(
        {"thread_id": "thread_bound", "node_id": node["id"]}
    )

    assert result["status"] == "rejected"
    state = json.loads(state_path.read_text())
    persisted = next(item for item in state["nodes"] if item["id"] == node["id"])
    assert persisted["status"] == "ready"


def test_execute_rejects_runtime_input_modified_by_experiment(tmp_path, monkeypatch):
    thread_dir, state_path, _, _, node = _setup(tmp_path, monkeypatch)
    original_builder = plans.build_experiment_plan_for_node

    def mutating_builder(*args, **kwargs):
        plan, used = original_builder(*args, **kwargs)
        plan["source_files"][0]["content"] += (
            "\n_manifest_path = Path(os.environ['RESEARCH_HARNESS_INPUT_MANIFEST'])\n"
            "_manifest = json.loads(_manifest_path.read_text())\n"
            "_dataset_path = _manifest_path.parent / "
            "_manifest['primary_dataset']['relative_path']\n"
            "_dataset_path.write_text('tampered')\n"
        )
        return plan, used

    monkeypatch.setattr(plans, "build_experiment_plan_for_node", mutating_builder)

    result = mcp.handle_execute_node_experiment(
        {"thread_id": "thread_bound", "node_id": node["id"]}
    )

    assert result["status"] == "rejected"
    assert "runtime input changed" in result["reason"]
    state = json.loads(state_path.read_text())
    persisted = next(item for item in state["nodes"] if item["id"] == node["id"])
    assert persisted["status"] == "ready"
    node_dir = thread_dir / "production" / "tree" / "nodes" / node["id"]
    assert not (node_dir / "worker_report.json").exists()


def test_invalid_baseline_evidence_contract_requeues_node(tmp_path, monkeypatch):
    thread_dir, state_path, _, _, node = _setup(tmp_path, monkeypatch)
    original_builder = plans.build_experiment_plan_for_node

    def nested_metrics_builder(*args, **kwargs):
        plan, used = original_builder(*args, **kwargs)
        plan["baseline_evidence_requirements"] = _arxiv_baseline_requirements()
        plan["source_files"][0]["content"] = (
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "manifest_path = Path(os.environ['RESEARCH_HARNESS_INPUT_MANIFEST'])\n"
            "manifest = json.loads(manifest_path.read_text())\n"
            "(manifest_path.parent / manifest['primary_dataset']['relative_path']).read_text()\n"
            "Path('artifacts').mkdir(exist_ok=True)\n"
            "Path('artifacts/metrics.json').write_text(json.dumps({\n"
            "    'metrics': {\n"
                "        'char': {'accuracy': 0.4},\n"
                "        'word': {'accuracy': 0.5},\n"
                "        'majority': {'macro_f1': 0.2},\n"
                "        'label_shuffle': {'macro_f1': 0.1},\n"
            "    },\n"
            "}))\n"
        )
        return plan, used

    monkeypatch.setattr(
        plans, "build_experiment_plan_for_node", nested_metrics_builder
    )

    result = mcp.handle_execute_node_experiment(
        {"thread_id": "thread_bound", "node_id": node["id"]}
    )

    assert result["status"] == "rejected"
    assert result["baseline_evidence_overall"] == "not_evaluable"
    assert "metric key is missing: accuracy" in result["reason"]
    state = json.loads(state_path.read_text())
    persisted = next(item for item in state["nodes"] if item["id"] == node["id"])
    assert persisted["status"] == "ready"
    frontier = next(
        item for item in state["frontier"] if item["node_id"] == node["id"]
    )
    assert frontier["status"] == "queued"
    worker = json.loads(
        (
            thread_dir
            / "production"
            / "tree"
            / "nodes"
            / node["id"]
            / "worker_report.json"
        ).read_text()
    )
    assert worker["baseline_evidence_status"]["overall"] == "not_evaluable"

    def corrected_metrics_builder(*args, **kwargs):
        plan, used = original_builder(*args, **kwargs)
        plan["baseline_evidence_requirements"] = _arxiv_baseline_requirements()
        plan["source_files"][0]["content"] = (
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "manifest_path = Path(os.environ['RESEARCH_HARNESS_INPUT_MANIFEST'])\n"
            "manifest = json.loads(manifest_path.read_text())\n"
            "(manifest_path.parent / manifest['primary_dataset']['relative_path']).read_text()\n"
            "Path('artifacts').mkdir(exist_ok=True)\n"
            "Path('artifacts/metrics.json').write_text(json.dumps({\n"
            "    'metrics': {'accuracy': 0.4, 'macro_f1': 0.3},\n"
            "    'baselines': {\n"
            "        'word': 0.5,\n"
            "        'majority': 0.2,\n"
            "        'label_shuffle': 0.1,\n"
            "    },\n"
            "}))\n"
        )
        return plan, used

    monkeypatch.setattr(
        plans, "build_experiment_plan_for_node", corrected_metrics_builder
    )
    retry = mcp.handle_execute_node_experiment(
        {"thread_id": "thread_bound", "node_id": node["id"]}
    )

    assert retry["status"] == "ok"
    assert retry["baseline_evidence_overall"] == "failed"
    state = json.loads(state_path.read_text())
    persisted = next(item for item in state["nodes"] if item["id"] == node["id"])
    assert persisted["status"] == "completed_worker_report"
