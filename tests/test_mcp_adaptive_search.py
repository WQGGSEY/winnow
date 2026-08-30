from __future__ import annotations

import json
from pathlib import Path

import pytest

import research_harness.mcp_server as mcp
from research_harness.orchestrator.adaptive_search import experiment_fingerprint


def _node(*, status: str = "critic_reviewed") -> dict:
    return {
        "id": "n_parent",
        "type": "capability",
        "status": status,
        "domain": "test_domain",
        "stage": "experimentation",
        "parent": None,
        "lineage": {
            "root_goal_id": "rg_test",
            "covers_goal_facets": [],
            "inherited_assumptions": [],
            "introduced_assumptions": [],
            "taste_constraints_applied": [],
        },
        "claim_contract": {
            "claim_under_test": "A character model beats the word baseline.",
            "mandatory_baselines": ["word baseline"],
            "success_criteria": ["accuracy improves", "macro-F1 improves"],
            "disproof_conditions": ["either metric does not improve"],
            "data_source_anchor": "taxonomy",
        },
        "baseline_refs": [
            {
                "baseline_dossier_id": "bd_test",
                "candidate_ids": ["word"],
                "roles": ["current_best_known"],
            }
        ],
        "runtime_profile": {
            "worker_type": "experiment_worker",
            "timeout_policy": "task_class_dependent",
            "turn_budget": 6,
        },
        "failure_retrieval": {
            "query_tags": ["classification"],
            "selected_fail_files": [],
        },
        "outputs": {"artifacts": [], "verdict": None},
    }


def _state(*, status: str = "critic_reviewed") -> dict:
    node = _node(status=status)
    return {
        "search_id": "s_test",
        "status": "running",
        "max_depth": 5,
        "max_debug_depth": 2,
        "sunk_cost_policy": "progress_gated",
        "scaleup_policy": "disallow_by_default",
        "frontier": [
            {
                "node_id": node["id"],
                "parent": None,
                "depth": 0,
                "priority": 1.0,
                "stage": "experimentation",
                "status": "done" if status != "ready" else "queued",
                "reason": "test",
            }
        ],
        "nodes": [node],
        "completed_node_ids": [node["id"]] if status != "ready" else [],
        "promoted_node_ids": [],
        "pruned_node_ids": [],
        "transitions": [],
    }


def _write_thread(tmp_path: Path, tid: str, state: dict) -> Path:
    thread_dir = tmp_path / tid
    tree = thread_dir / "production" / "tree"
    node_dir = tree / "nodes" / "n_parent"
    node_dir.mkdir(parents=True)
    (tree / "search_state.json").write_text(json.dumps(state), encoding="utf-8")
    (node_dir / "worker_report.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "claim_verdict_candidate": "contradicted",
                "metrics": {"accuracy": 0.4, "macro_f1": 0.3},
                "baselines": {"word_accuracy": 0.4, "word_macro_f1": 0.4},
                "baseline_evidence_status": {"overall": "failed", "results": []},
                "disproof_conditions_hit": ["accuracy did not improve"],
                "input_evidence": {"adapter_id": "taxonomy"},
            }
        ),
        encoding="utf-8",
    )
    (thread_dir / "thread.json").write_text(
        json.dumps({"user_goal": "Beat the word baseline on the held-out taxonomy."}),
        encoding="utf-8",
    )
    grilling_dir = thread_dir / "grilling"
    grilling_dir.mkdir()
    (grilling_dir / "grilling_session.json").write_text(
        json.dumps(
            {
                "user_goal": "Beat the word baseline on the held-out taxonomy.",
                "extracted": {
                    "claim_under_test": "A character model beats the word baseline.",
                    "mandatory_baselines": ["word baseline"],
                    "success_criteria": ["accuracy improves", "macro-F1 improves"],
                    "disproof_conditions": ["either metric does not improve"],
                    "taste_constraints": ["CPU-only", "fixed split"],
                },
            }
        ),
        encoding="utf-8",
    )
    (thread_dir / "production" / "feasibility_envelope.json").write_text(
        json.dumps(
            {
                "operator_intent": {
                    "target_deploy_grade_scope": "deployment",
                    "data_source_anchor": "taxonomy",
                },
                "data_sources_available": [{"kind": "real_adapter", "id": "taxonomy"}],
                "llm_oracles_available": [{"kind": "subscription_codex"}],
                "external_falsifier": {"kind": "real_holdout"},
            }
        ),
        encoding="utf-8",
    )
    return thread_dir


def _candidate(
    family: str,
    mechanism: str,
    intervention: str,
    gap: str,
    *,
    cost: float,
) -> dict:
    return {
        "type": "mechanism",
        "successor_claim": f"Test {family} against the same frozen bar.",
        "rationale": f"The negative observation implicates {mechanism}.",
        "strategy_family": family,
        "mechanism": mechanism,
        "intervention": intervention,
        "information_target": f"Whether {mechanism} caused the failure.",
        "predicted_outcomes": [
            "The intervention improves the failed comparison.",
            "The failed comparison remains unchanged.",
        ],
        "tests_bar_gaps": [gap],
        "required_capabilities": ["data:taxonomy"],
        "estimated_cost": cost,
    }


def _negative_args(tid: str) -> dict:
    return {
        "thread_id": tid,
        "node_id": "n_parent",
        "next_transition": "needs_child_branch",
        "final_verdict": "accuracy tied and macro-F1 lost to the word baseline",
        "command_id": "decision:n_parent:v1",
        "expected_revision": 0,
        "follow_up_children": [
            _candidate(
                "boundary-aware features",
                "word boundaries erase discriminative fragments",
                "add boundary-crossing character features",
                "accuracy improves",
                cost=0.1,
            ),
            _candidate(
                "archive-conditioned smoothing",
                "rare archives are over-smoothed",
                "condition smoothing on archive support",
                "macro-F1 improves",
                cost=0.8,
            ),
        ],
    }


def test_negative_decision_atomically_materializes_distinct_strategies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_negative"
    thread_dir = _write_thread(tmp_path, tid, _state())
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)

    response = mcp.handle_submit_professor_decision(_negative_args(tid), settings={})
    retry = mcp.handle_submit_professor_decision(_negative_args(tid), settings={})
    stale_args = _negative_args(tid)
    stale_args["command_id"] = "decision:n_parent:stale"
    stale = mcp.handle_submit_professor_decision(stale_args, settings={})

    assert response == retry
    assert stale["reason"] == "stale_adaptive_revision"
    assert stale["expected_revision"] == 1
    assert response["status"] == "accepted"
    assert len(response["created_child_ids"]) == 2
    assert response["search_disposition"] == "continue"

    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    children = [node for node in persisted["nodes"] if node.get("parent") == "n_parent"]
    assert len(children) == 2
    assert len({node["strategy"]["id"] for node in children}) == 2
    frozen_bar = persisted["adaptive"]["goal"]["bar"]
    assert all(
        node["claim_contract"]["success_criteria"]
        == frozen_bar["success_criteria"]
        for node in children
    )
    assert all(
        node["claim_contract"]["disproof_conditions"]
        == frozen_bar["disproof_conditions"]
        for node in children
    )
    assert len(persisted["adaptive"]["observations"]) == 1
    assert persisted["adaptive"]["revision"] == 1

    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})
    assert selected["status"] == "ok"
    assert selected["node_id"] == response["created_child_ids"][0]
    assert selected["priority_components"]["evidence_basis"]
    assert "evidence-derived" in selected["_selection_policy"]


def test_negative_decision_rejects_duplicate_strategy_before_state_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_duplicate"
    thread_dir = _write_thread(tmp_path, tid, _state())
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    args = _negative_args(tid)
    args["follow_up_children"][1] = dict(args["follow_up_children"][0])

    response = mcp.handle_submit_professor_decision(args, settings={})

    assert response["status"] == "rejected"
    assert "distinct" in response["reason"]
    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["nodes"] == _state()["nodes"]


def test_promotion_rejects_worker_report_without_supported_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_unsupported_promotion"
    _write_thread(tmp_path, tid, _state())
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)

    response = mcp.handle_submit_professor_decision(
        {
            "thread_id": tid,
            "node_id": "n_parent",
            "next_transition": "promoted",
            "final_verdict": "supported",
            "follow_up_children": [],
        },
        settings={},
    )

    assert response["status"] == "rejected"
    assert response["reason"].startswith(
        "promotion_requires_supported_execution_evidence"
    )


def test_supported_adaptive_promotion_preempts_remaining_frontier_for_rebuttal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_strong_terminal"
    state = _state(status="promoted")
    state["promoted_node_ids"] = ["n_parent"]
    state["nodes"][0]["strategy"] = {"id": "strategy_verified"}
    thread_dir = _write_thread(tmp_path, tid, state)
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    adaptive = mcp._ensure_adaptive_state(tid, state)
    adaptive["strategies"] = [{"id": "strategy_verified"}]
    (thread_dir / "production" / "tree" / "search_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    node_dir = thread_dir / "production" / "tree" / "nodes" / "n_parent"
    worker = json.loads((node_dir / "worker_report.json").read_text())
    worker.update(
        claim_verdict_candidate="supported",
        baseline_evidence_status={"overall": "passed", "results": []},
        disproof_conditions_hit=[],
    )
    (node_dir / "worker_report.json").write_text(
        json.dumps(worker), encoding="utf-8"
    )
    rebuttal = thread_dir / "production" / "rebuttal"
    rebuttal.mkdir()
    (rebuttal / "falsifier_result.json").write_text(
        json.dumps({"kind": "real_holdout", "passed": True, "observed": 0.2}),
        encoding="utf-8",
    )

    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert selected["status"] == "strong_candidate_ready"
    assert selected["node_id"] == "n_parent"
    assert selected["next_tool_to_call"] == "decide_publication_readiness"

    (thread_dir / "production" / "tree" / "mcp_readiness.json").write_text(
        json.dumps({"submit": True}), encoding="utf-8"
    )
    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})
    assert selected["status"] == "rebuttal_ready"
    assert selected["next_tool_to_call"] == "prepare_rebuttal_packet"

    state["adaptive"]["disposition"] = "goal_achieved"
    (thread_dir / "production" / "tree" / "search_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    (rebuttal / "user_goal_attestation.json").write_text(
        json.dumps({"achieved": True, "promoted_node_id": "n_parent"}),
        encoding="utf-8",
    )
    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})
    assert selected["status"] == "goal_achieved_render_pending"
    assert selected["next_tool_to_call"] == "prepare_paper_writing_context"


def test_negative_without_strategies_persists_resumable_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_negative_pause"
    thread_dir = _write_thread(tmp_path, tid, _state())
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    args = _negative_args(tid)
    args["follow_up_children"] = []

    response = mcp.handle_submit_professor_decision(args, settings={})
    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert response["status"] == "paused_needs_expansion"
    assert selected["status"] == "paused_needs_expansion"
    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["adaptive"]["observations"]
    assert persisted["adaptive"]["pause"]["reason"] == "needs_strategy_expansion"


def test_empty_frontier_persists_resumable_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_pause"
    state = _state(status="pruned")
    state["status"] = "completed"
    state["pruned_node_ids"] = ["n_parent"]
    thread_dir = _write_thread(tmp_path, tid, state)
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)

    response = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert response["status"] == "paused_needs_expansion"
    assert response["search_disposition"] == "paused_needs_expansion"
    assert response["next_tool_to_call"] == "propose_alternative_root_directions"
    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["status"] == "blocked"
    assert persisted["adaptive"]["pause"]["reason"] == "needs_strategy_expansion"


def test_unavailable_strategy_capability_pauses_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_missing_capability"
    thread_dir = _write_thread(tmp_path, tid, _state())
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    args = _negative_args(tid)
    for candidate in args["follow_up_children"]:
        candidate["required_capabilities"] = ["data:private_holdout"]

    decision = mcp.handle_submit_professor_decision(args, settings={})
    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert decision["status"] == "accepted"
    assert selected["status"] == "paused_needs_expansion"
    assert selected["pause"]["reason"] == "missing_capability"
    assert selected["pause"]["missing_capabilities"] == [
        "data:private_holdout"
    ]
    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["status"] == "blocked"
    assert all(item["status"] == "queued" for item in persisted["frontier"][1:])

    envelope_path = thread_dir / "production" / "feasibility_envelope.json"
    envelope = json.loads(envelope_path.read_text())
    envelope["data_sources_available"].append(
        {"kind": "real_adapter", "id": "private_holdout"}
    )
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")

    resumed = mcp.handle_get_next_admissible_node({"thread_id": tid})
    assert resumed["status"] == "ok"
    resumed_state = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert resumed_state["status"] == "running"
    assert resumed_state["adaptive"]["pause"] is None
    assert resumed_state["adaptive"]["disposition"] == "continue"


def test_first_selection_persists_adaptive_state_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_first_selection"
    thread_dir = _write_thread(tmp_path, tid, _state(status="ready"))
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)

    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert selected["status"] == "ok"
    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["adaptive"]["goal"]["source"] == "pre_generation"


def test_legacy_migration_pauses_when_root_bars_disagree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_legacy_conflict"
    state = _state(status="ready")
    conflicting = json.loads(json.dumps(state["nodes"][0]))
    conflicting["id"] = "n_other_root"
    conflicting["claim_contract"]["success_criteria"] = ["different bar"]
    state["nodes"].append(conflicting)
    state["frontier"].append(
        {
            "node_id": conflicting["id"],
            "parent": None,
            "depth": 0,
            "priority": 1.0,
            "stage": "experimentation",
            "status": "queued",
            "reason": "test",
        }
    )
    thread_dir = _write_thread(tmp_path, tid, state)
    (thread_dir / "thread.json").write_text("{}", encoding="utf-8")
    (thread_dir / "grilling" / "grilling_session.json").write_text(
        "{}", encoding="utf-8"
    )
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)

    response = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert response["status"] == "paused_needs_expansion"
    assert "claim contracts disagree" in response["reason"]


def test_duplicate_experiment_is_rejected_before_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_experiment_duplicate"
    state = _state(status="ready")
    state["nodes"][0]["claim_contract"].pop("data_source_anchor")
    plan = {
        "node_id": "n_parent",
        "plan_id": "plan_parent",
        "objective": "Run the test",
        "task_class": "eval",
        "entrypoint": {"command": ["python3"], "args": ["src/experiment.py"]},
        "source_files": [
            {"path": "src/experiment.py", "content": "print(1)", "purpose": "test"}
        ],
        "inputs": {},
        "expected_outputs": {},
        "baseline_evidence_requirements": [],
        "resources": {"timeout_sec": 10},
        "reproducibility": {"seed": 7},
    }
    fingerprint = experiment_fingerprint(plan)
    state["adaptive"] = {
        "version": 1,
        "revision": 1,
        "goal": {
            "id": "goal_" + "1" * 64,
            "question": "q",
            "bar": {
                "claim_under_test": "q",
                "mandatory_baselines": ["b"],
                "success_criteria": ["s"],
                "disproof_conditions": ["d"],
                "operator_requirements": [],
                "target_scope": "directional",
                "data_source_anchor": None,
                "data_source_snapshot_id": None,
                "external_falsifier": {},
            },
            "bar_digest": "sha256:" + "2" * 64,
        },
        "strategies": [],
        "observations": [],
        "experiments": [
            {"id": fingerprint, "node_id": "n_other", "status": "executed"}
        ],
        "duplicate_rejections": [],
        "command_receipts": {},
        "disposition": "continue",
        "pause": None,
    }
    _write_thread(tmp_path, tid, state)
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    monkeypatch.setattr(
        "research_harness.orchestrator.experiment_plan.build_experiment_plan_for_node",
        lambda *_args, **_kwargs: (plan, True),
    )
    monkeypatch.setattr(
        "research_harness.orchestrator.experiment_plan.validate_experiment_plan",
        lambda *_args, **_kwargs: None,
    )

    def fail_if_run(*_args, **_kwargs):
        raise AssertionError("runner consumed duplicate work")

    monkeypatch.setattr(
        "research_harness.runner.local_runner.LocalRunner.execute", fail_if_run
    )

    response = mcp.handle_execute_node_experiment(
        {"thread_id": tid, "node_id": "n_parent"}
    )

    assert response["status"] == "rejected"
    assert response["reason"] == "duplicate_experiment"
    assert response["duplicate_of"] == "n_other"
