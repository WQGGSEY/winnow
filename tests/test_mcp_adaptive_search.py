from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

import research_harness.mcp_server as mcp
from research_harness.acquisition import (
    AcquisitionBudget,
    make_acquisition_command,
    serialize_command,
)
from research_harness.orchestrator.adaptive_search import experiment_fingerprint
from research_harness.orchestrator.blind_reorientation import (
    AwaitingEvidence,
    DirectionAttemptRef,
    ReorientationState,
    serialize_reorientation_state,
)
from research_harness.orchestrator.direction_generation import (
    make_direction_draft,
    make_direction_fingerprint,
)


class _BlindEngine:
    def __init__(
        self,
        result: dict[str, object],
        *,
        before_advance: Callable[[], None] | None = None,
    ) -> None:
        self.result = result
        self.before_advance = before_advance
        self.calls: list[tuple[str, object]] = []

    def advance_research(self, *, command_id, acquisition_command=None):
        if self.before_advance is not None:
            self.before_advance()
        self.calls.append((command_id, acquisition_command))
        return dict(self.result)


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
    reorientation = thread_dir / "production" / "reorientation"
    reorientation.mkdir()
    attempt = DirectionAttemptRef(
        attempt_id="attempt_parent",
        direction_id="direction_" + "d" * 64,
        fingerprint=make_direction_fingerprint(
            mechanism="parent mechanism",
            intervention="parent intervention",
            observables_and_data="parent observations",
            analysis_unit="parent unit",
            timescale="parent timescale",
            system_boundary="parent boundary",
        ),
        ordinal=0,
    )
    reorientation_state = ReorientationState(
        version=2,
        contract_id="contract_" + "e" * 64,
        revision=1,
        closed_attempts=(),
        phase=AwaitingEvidence(active_attempt=attempt),
    )
    (reorientation / "state.json").write_text(
        json.dumps(serialize_reorientation_state(reorientation_state)),
        encoding="utf-8",
    )
    (reorientation / "node_attempts.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": {
                    "n_parent": {
                        "attempt_id": attempt.attempt_id,
                        "direction_id": attempt.direction_id,
                        "manifest_id": "acqmanifest_" + "f" * 64,
                        "legacy_audit_only": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (node_dir / "worker_report.json").write_text(
        json.dumps(
            {
                "node_id": "n_parent",
                "status": "completed",
                "claim_verdict_candidate": "contradicted",
                "metrics": {"accuracy": 0.4, "macro_f1": 0.3},
                "baselines": {"word_accuracy": 0.4, "word_macro_f1": 0.4},
                "baseline_evidence_status": {
                    "overall": "failed",
                    "results": [
                        {
                            "role": "current_best_known",
                            "metric_key": "accuracy",
                            "baseline_key": "word_accuracy",
                            "operator": "greater_than",
                            "margin": 0.0,
                            "required": True,
                            "metric_value": 0.4,
                            "baseline_value": 0.4,
                            "status": "failed",
                            "reason": "deterministic comparison result",
                        }
                    ],
                },
                "disproof_conditions_hit": ["accuracy did not improve"],
                "artifacts": [],
                "unexpected_observations": [],
                "failure_record_candidate": None,
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


def _drop_authoritative_binding(thread_dir: Path) -> None:
    reorientation = thread_dir / "production" / "reorientation"
    for path in reorientation.iterdir():
        path.unlink()
    reorientation.rmdir()


def test_authoritative_node_requires_one_nonlegacy_durable_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_authoritative_binding"
    thread_dir = tmp_path / tid
    reorientation = thread_dir / "production" / "reorientation"
    reorientation.mkdir(parents=True)
    fingerprint = make_direction_fingerprint(
        mechanism="mechanism",
        intervention="intervention",
        observables_and_data="observables",
        analysis_unit="analysis unit",
        timescale="timescale",
        system_boundary="system boundary",
    )
    attempt = DirectionAttemptRef(
        attempt_id="attempt_active",
        direction_id="direction_" + "a" * 64,
        fingerprint=fingerprint,
        ordinal=0,
    )
    state = ReorientationState(
        version=2,
        contract_id="contract_" + "b" * 64,
        revision=1,
        closed_attempts=(),
        phase=AwaitingEvidence(active_attempt=attempt),
    )
    (reorientation / "state.json").write_text(
        json.dumps(serialize_reorientation_state(state)), encoding="utf-8"
    )
    node_attempts = {
        "version": 1,
        "nodes": {
            "n_active": {
                "attempt_id": attempt.attempt_id,
                "direction_id": attempt.direction_id,
                "manifest_id": "acqmanifest_" + "c" * 64,
                "legacy_audit_only": False,
            }
        },
    }
    index_path = reorientation / "node_attempts.json"
    index_path.write_text(json.dumps(node_attempts), encoding="utf-8")
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)

    assert mcp._authoritative_active_node_id(tid) == "n_active"

    node_attempts["nodes"]["n_active"]["legacy_audit_only"] = True
    index_path.write_text(json.dumps(node_attempts), encoding="utf-8")
    assert mcp._authoritative_active_node_id(tid) is None

    node_attempts["nodes"]["n_active"]["legacy_audit_only"] = False
    node_attempts["nodes"]["n_duplicate"] = dict(
        node_attempts["nodes"]["n_active"]
    )
    index_path.write_text(json.dumps(node_attempts), encoding="utf-8")
    assert mcp._authoritative_active_node_id(tid) is None


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


def _prepared_acquisition_command():
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
        predicted_outcomes=("margin clears", "margin misses"),
    )
    return make_acquisition_command(
        reservation_id="reservation_" + "1" * 64,
        node_id="n_blind_test",
        attempt_id="attempt_test",
        direction=direction,
        needs=(),
        budget=AcquisitionBudget(0, 0, 30),
    )


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


def test_negative_decision_rejects_observation_derived_successors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_negative"
    thread_dir = _write_thread(tmp_path, tid, _state())
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)

    response = mcp.handle_submit_professor_decision(_negative_args(tid), settings={})

    assert response["status"] == "rejected"
    assert "follow_up_children is retired" in response["reason"]

    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["nodes"] == _state()["nodes"]


def test_pruned_decision_closes_direction_and_advances_blind_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_pruned"
    thread_dir = _write_thread(tmp_path, tid, _state())
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    lock_depth = 0

    @contextmanager
    def writer_lock():
        nonlocal lock_depth
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1

    def assert_unlocked() -> None:
        assert lock_depth == 0

    monkeypatch.setattr(mcp, "_exclusive_adaptive_writer", lambda _tid: writer_lock())
    engine = _BlindEngine(
        {
            "status": "checkpointed",
            "reason": "generation_retry",
            "next_tool_to_call": "advance_research",
        },
        before_advance=assert_unlocked,
    )
    monkeypatch.setattr(
        mcp,
        "_build_blind_research_engine",
        lambda _tid: engine,
    )
    args = _negative_args(tid)
    args["next_transition"] = "pruned"
    args.pop("follow_up_children")

    response = mcp.handle_submit_professor_decision(args, settings={})
    retry = mcp.handle_submit_professor_decision(args, settings={})

    assert response == retry
    assert response["status"] == "accepted"
    assert response["created_child_ids"] == []
    assert response["research_advance"]["status"] == "checkpointed"
    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["nodes"][0]["status"] == "pruned"
    assert len(persisted["nodes"]) == 1
    assert persisted["adaptive"]["observations"] == []
    assert len(engine.calls) == 1


def test_pruned_decision_retries_when_failure_lesson_cannot_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_failure_lesson"
    thread_dir = _write_thread(tmp_path, tid, _state())
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)

    def fail_failure_record(**_kwargs: object) -> None:
        raise OSError("read-only memory")

    monkeypatch.setattr(
        mcp,
        "_write_failure_record",
        fail_failure_record,
    )
    args = _negative_args(tid)
    args["next_transition"] = "pruned"
    args.pop("follow_up_children")

    response = mcp.handle_submit_professor_decision(args, settings={})

    assert response["status"] == "rejected"
    assert "failure lesson could not be persisted" in response["reason"]
    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["nodes"][0]["status"] == "critic_reviewed"


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
        },
        settings={},
    )

    assert response["status"] == "rejected"
    assert response["reason"].startswith(
        "promotion_requires_supported_execution_evidence"
    )


def test_prune_rejects_worker_report_without_conclusive_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_inconclusive_prune"
    thread_dir = _write_thread(tmp_path, tid, _state())
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    worker_path = (
        thread_dir
        / "production"
        / "tree"
        / "nodes"
        / "n_parent"
        / "worker_report.json"
    )
    worker = json.loads(worker_path.read_text())
    worker["status"] = "failed"
    worker_path.write_text(json.dumps(worker), encoding="utf-8")

    response = mcp.handle_submit_professor_decision(
        {
            "thread_id": tid,
            "node_id": "n_parent",
            "next_transition": "pruned",
            "final_verdict": "execution failed before a conclusion",
        },
        settings={},
    )

    assert response["status"] == "rejected"
    assert response["reason"].startswith("prune_requires_conclusive_failure")
    state = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert state["nodes"][0]["status"] == "critic_reviewed"


def test_selector_requires_experiment_revision_for_inconclusive_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_inconclusive_retry"
    initial_state = _state()
    initial_state["nodes"][0]["claim_contract"].pop("data_source_anchor")
    thread_dir = _write_thread(tmp_path, tid, initial_state)
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    node_dir = thread_dir / "production" / "tree" / "nodes" / "n_parent"
    worker_path = node_dir / "worker_report.json"
    worker = json.loads(worker_path.read_text())
    worker["status"] = "failed"
    worker_path.write_text(json.dumps(worker), encoding="utf-8")
    plan = {
        "node_id": "n_parent",
        "plan_id": "plan_retry",
        "objective": "Run the retryable experiment",
        "task_class": "eval",
        "entrypoint": {"command": ["python3"], "args": ["src/experiment.py"]},
        "source_files": [
            {
                "path": "src/experiment.py",
                "content": "print(1)",
                "purpose": "test",
            }
        ],
        "inputs": {},
        "expected_outputs": {},
        "baseline_evidence_requirements": [],
        "resources": {"timeout_sec": 10},
        "reproducibility": {"seed": 7},
    }
    (node_dir / "experiment_plan.json").write_text(
        json.dumps(plan),
        encoding="utf-8",
    )
    template_dir = (
        thread_dir / "production" / "professor_templates" / "n_parent"
    )
    (template_dir / "src").mkdir(parents=True)
    (template_dir / "src" / "experiment.py").write_text(
        "print(1)",
        encoding="utf-8",
    )
    (template_dir / "plan.json").write_text(
        json.dumps({"description": "original"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "research_harness.orchestrator.experiment_plan.build_experiment_plan_for_node",
        lambda *_args, **_kwargs: (dict(plan), True),
    )
    monkeypatch.setattr(
        "research_harness.orchestrator.experiment_plan.validate_experiment_plan",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mcp,
        "_authoritative_active_node_id",
        lambda _tid: "n_parent",
    )

    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})
    (template_dir / "plan.json").write_text(
        json.dumps({"description": "prose changed only"}),
        encoding="utf-8",
    )
    premature_run = mcp.handle_execute_node_experiment(
        {"thread_id": tid, "node_id": "n_parent"}
    )

    assert selected["status"] == "retry_evidence"
    assert selected["evidence_reason"] == "runner_did_not_complete"
    assert selected["next_tool_to_call"] == "design_experiment_template"
    assert premature_run["status"] == "rejected"
    assert "prose-only edits do not qualify" in premature_run["reason"]
    state = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert state["nodes"][0]["status"] == "ready"


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
    adaptive["disposition"] = "paused_needs_expansion"
    adaptive["pause"] = {"reason": "needs_strategy_expansion"}
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
    monkeypatch.setattr(
        mcp,
        "_authoritative_active_node_id",
        lambda _tid: "n_parent",
    )

    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert selected["status"] == "strong_candidate_ready"
    assert selected["node_id"] == "n_parent"
    assert selected["next_tool_to_call"] == "decide_publication_readiness"

    (thread_dir / "production" / "tree" / "mcp_readiness.json").write_text(
        json.dumps({"submit": True, "node_id": "n_parent"}), encoding="utf-8"
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
    import research_harness.thread_supervisor as supervisor

    monkeypatch.setattr(
        supervisor,
        "is_terminal",
        lambda *_args, **_kwargs: (False, None),
    )
    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})
    assert selected["status"] == "hard_external_block"
    assert selected["code"] == "operator_scope_conflict"
    monkeypatch.setattr(
        supervisor,
        "is_terminal",
        lambda *_args, **_kwargs: (True, "sha256:" + "a" * 64),
    )
    monkeypatch.setattr(
        mcp,
        "_authoritative_active_node_id",
        lambda _tid: pytest.fail("terminal render must precede the active-node gate"),
    )
    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})
    assert selected["status"] == "goal_achieved_render_pending"
    assert selected["next_tool_to_call"] == "prepare_paper_writing_context"


def test_node_mutation_rejects_non_authoritative_legacy_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp, "_thread_dir", lambda tid: tmp_path / tid)
    monkeypatch.setattr(
        mcp,
        "_authoritative_active_node_id",
        lambda _tid: "n_active",
    )

    result = mcp.handle_design_experiment_template(
        {
            "thread_id": "t_gate",
            "node_id": "n_legacy",
            "plan_metadata": {"source_files": []},
        }
    )

    assert result["status"] == "rejected"
    assert "not the authoritative blind node" in result["reason"]
    assert not (tmp_path / "t_gate" / "production").exists()


def test_template_design_rechecks_node_authority_under_writer_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager

    authoritative = "n_before_lock"
    monkeypatch.setattr(mcp, "_thread_dir", lambda tid: tmp_path / tid)
    monkeypatch.setattr(mcp, "_authoritative_active_node_id", lambda _tid: authoritative)
    checks = []

    @contextmanager
    def changed_authority(_tid):
        nonlocal authoritative
        checks.append("locked")
        authoritative = "n_after_lock"
        yield

    monkeypatch.setattr(mcp, "_exclusive_adaptive_writer", changed_authority)
    result = mcp.handle_design_experiment_template({
        "thread_id": "t_gate", "node_id": "n_before_lock", "plan_metadata": {"source_files": []},
    })

    assert checks == ["locked"]
    assert result["status"] == "rejected"
    assert "not the authoritative blind node" in result["reason"]
    assert not (tmp_path / "t_gate" / "production").exists()


def test_readiness_records_authoritative_candidate_despite_legacy_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp, "_thread_dir", lambda tid: tmp_path / tid)
    monkeypatch.setattr(
        mcp,
        "_authoritative_active_node_id",
        lambda _tid: "n_active",
    )

    result = mcp.handle_decide_publication_readiness(
        {
            "thread_id": "t_readiness",
            "node_id": "n_active",
            "submit": True,
        }
    )

    assert result == {"status": "recorded"}
    persisted = json.loads(
        (
            tmp_path
            / "t_readiness"
            / "production"
            / "tree"
            / "mcp_readiness.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted["node_id"] == "n_active"


def test_unbound_legacy_strong_candidate_cannot_enter_rebuttal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_unbound_legacy_strong"
    state = _state(status="promoted")
    state["promoted_node_ids"] = ["n_parent"]
    state["nodes"][0]["strategy"] = {"id": "strategy_legacy"}
    thread_dir = _write_thread(tmp_path, tid, state)
    _drop_authoritative_binding(thread_dir)
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    adaptive = mcp._ensure_adaptive_state(tid, state)
    adaptive["strategies"] = [{"id": "strategy_legacy"}]
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
    engine = _BlindEngine({"status": "direction_ready", "node_id": "n_blind"})
    monkeypatch.setattr(mcp, "_build_blind_research_engine", lambda _tid: engine)

    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert selected == {"status": "direction_ready", "node_id": "n_blind"}
    assert len(engine.calls) == 1


def test_empty_frontier_advances_blind_engine_outside_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_pause"
    state = _state(status="pruned")
    state["status"] = "completed"
    state["pruned_node_ids"] = ["n_parent"]
    thread_dir = _write_thread(tmp_path, tid, state)
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    lock_depth = 0

    @contextmanager
    def writer_lock():
        nonlocal lock_depth
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1

    def assert_unlocked() -> None:
        assert lock_depth == 0

    engine = _BlindEngine(
        {
            "status": "direction_ready",
            "direction": {"data_needs": []},
            "next_tool_to_call": "advance_research",
        },
        before_advance=assert_unlocked,
    )
    monkeypatch.setattr(mcp, "_exclusive_adaptive_writer", lambda _tid: writer_lock())
    monkeypatch.setattr(
        mcp,
        "_build_blind_research_engine",
        lambda _tid: engine,
    )

    response = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert response["status"] == "direction_ready"
    assert len(engine.calls) == 1
    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["status"] == "completed"


def test_advance_research_forwards_stable_command_to_blind_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _BlindEngine(
        {
            "status": "direction_ready",
            "direction": {"data_needs": []},
            "next_tool_to_call": "advance_research",
        }
    )
    monkeypatch.setattr(mcp, "_thread_dir", lambda tid: tmp_path / tid)
    monkeypatch.setattr(
        mcp,
        "_build_blind_research_engine",
        lambda _tid: engine,
    )

    response = mcp.handle_advance_research(
        {"thread_id": "t_direct", "command_id": "step_1"},
        settings={},
    )

    assert response["status"] == "direction_ready"
    assert engine.calls == [("step_1", None)]

    def fail_engine_construction(*_args, **_kwargs):
        raise AssertionError("committed receipt replay constructed an engine")

    monkeypatch.setattr(
        mcp,
        "_build_blind_research_engine",
        fail_engine_construction,
    )

    retry = mcp.handle_advance_research(
        {"thread_id": "t_direct", "command_id": "step_1"},
        settings={},
    )

    assert retry == response
    assert engine.calls == [("step_1", None)]


def test_advance_research_forwards_optimistic_concurrency_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RecordingEngine:
        def __init__(self):
            self.kwargs = None

        def advance_research(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "state_changed"}

    engine = _RecordingEngine()
    monkeypatch.setattr(mcp, "_thread_dir", lambda tid: tmp_path / tid)
    monkeypatch.setattr(
        mcp,
        "_build_blind_research_engine",
        lambda _tid: engine,
    )

    mcp.handle_advance_research(
        {
            "thread_id": "t_guarded",
            "command_id": "step_guarded",
            "expected_revision": 7,
            "expected_checkpoint_id": "checkpoint_fixture",
        },
        settings={},
    )

    assert engine.kwargs == {
        "command_id": "step_guarded",
        "acquisition_command": None,
        "expected_revision": 7,
        "expected_checkpoint_id": "checkpoint_fixture",
    }


def test_blind_engine_uses_thread_scoped_agent_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_harness.config import resolve_agent_model
    from research_harness.orchestrator import blind_mcp_adapter

    tid = "t_scoped_models"
    thread_dir = tmp_path / "runs" / "threads" / tid
    thread_dir.mkdir(parents=True)
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "runtime": {
                    "agent_models": {
                        "direction_generator": "project-direction",
                        "structural_assessor": "project-assessor",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (thread_dir / "thread_settings.json").write_text(
        json.dumps(
            {
                "runtime.agent_models.direction_generator": "thread-direction",
                "runtime.agent_models.structural_assessor": "thread-assessor",
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def capture_engine(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(mcp, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: thread_dir)
    monkeypatch.setattr(
        blind_mcp_adapter,
        "build_blind_research_engine",
        capture_engine,
    )

    mcp._build_blind_research_engine(tid)

    assert resolve_agent_model(captured["settings"], "direction_generator") == (
        "thread-direction"
    )
    assert resolve_agent_model(captured["settings"], "structural_assessor") == (
        "thread-assessor"
    )


def test_advance_research_rejects_command_id_reuse_with_different_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _BlindEngine({"status": "direction_ready"})
    monkeypatch.setattr(mcp, "_thread_dir", lambda tid: tmp_path / tid)
    monkeypatch.setattr(
        mcp,
        "_build_blind_research_engine",
        lambda _tid: engine,
    )
    first = {"thread_id": "t_collision", "command_id": "same"}

    assert mcp.handle_advance_research(first, settings={})["status"] == "direction_ready"
    collision = mcp.handle_advance_research(
        {**first, "acquisition": {"needs": []}},
        settings={},
    )

    assert collision["status"] == "rejected"
    assert "different advance request" in collision["reason"]
    assert len(engine.calls) == 1


def test_advance_research_recovers_prepared_acquisition_without_replanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_prepared"
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    args = {
        "thread_id": tid,
        "command_id": "acquire_once",
        "acquisition": {"needs": []},
    }
    command = _prepared_acquisition_command()
    receipt_path = mcp._advance_command_receipt_path(tid, args["command_id"])
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(
            {
                "version": 1,
                "command_id": args["command_id"],
                "input_digest": mcp._json_sha256(args),
                "status": "prepared",
                "acquisition_command": serialize_command(command),
            }
        ),
        encoding="utf-8",
    )
    engine = _BlindEngine({"status": "acquisition_running"})
    monkeypatch.setattr(
        mcp,
        "_build_blind_research_engine",
        lambda _tid: engine,
    )

    response = mcp.handle_advance_research(args, settings={})

    assert response["status"] == "acquisition_running"
    assert engine.calls == [("acquire_once", command)]


def test_advance_research_does_not_overwrite_malformed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_malformed_receipt"
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    receipt_path = mcp._advance_command_receipt_path(tid, "same")
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text("not json", encoding="utf-8")
    engine = _BlindEngine({"status": "direction_ready"})
    monkeypatch.setattr(
        mcp,
        "_build_blind_research_engine",
        lambda _tid: engine,
    )

    response = mcp.handle_advance_research(
        {"thread_id": tid, "command_id": "same"},
        settings={},
    )

    assert response == {
        "status": "rejected",
        "reason": "advance command receipt is malformed",
    }
    assert receipt_path.read_text() == "not json"
    assert engine.calls == []


def test_professor_command_id_reuse_with_changed_payload_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_professor_collision"
    _write_thread(tmp_path, tid, _state())
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    monkeypatch.setattr(
        mcp,
        "_build_blind_research_engine",
        lambda _tid: _BlindEngine({"status": "checkpointed"}),
    )
    args = _negative_args(tid)
    args["next_transition"] = "pruned"
    args.pop("follow_up_children")

    assert mcp.handle_submit_professor_decision(args, settings={})["status"] == "accepted"
    collision = mcp.handle_submit_professor_decision(
        {**args, "final_verdict": "a different decision payload"},
        settings={},
    )

    assert collision["status"] == "rejected"
    assert "different professor decision" in collision["reason"]


@pytest.mark.parametrize("legacy_command_id", ["legacy-explicit", "implicit:forged"])
def test_legacy_explicit_professor_receipt_requires_new_command_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_command_id: str,
) -> None:
    tid = "t_legacy_professor_receipt"
    state = _state()
    thread_dir = _write_thread(tmp_path, tid, state)
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    adaptive = mcp._ensure_adaptive_state(tid, state)
    adaptive["command_receipts"][legacy_command_id] = {
        "status": "accepted",
        "applied_transition": "pruned",
    }
    (thread_dir / "production" / "tree" / "search_state.json").write_text(
        json.dumps(state),
        encoding="utf-8",
    )

    response = mcp.handle_submit_professor_decision(
        {
            "thread_id": tid,
            "node_id": "n_parent",
            "next_transition": "pruned",
            "command_id": legacy_command_id,
        },
        settings={},
    )

    assert response["status"] == "rejected"
    assert "legacy professor receipt" in response["reason"]


def test_unavailable_legacy_strategy_routes_to_blind_reorientation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_missing_capability"
    state = _state(status="ready")
    thread_dir = _write_thread(tmp_path, tid, state)
    _drop_authoritative_binding(thread_dir)
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    adaptive = mcp._ensure_adaptive_state(tid, state)
    strategy = {
        "id": "strategy_" + "d" * 64,
        "goal_id": "goal_" + "b" * 64,
        "family": "private holdout strategy",
        "mechanism": "private signal feedback",
        "intervention": "private holdout calibration",
        "information_target": "whether private holdout calibration clears the bar",
        "predicted_outcomes": ["the bar clears", "the bar misses"],
        "tests_bar_gaps": ["accuracy improves"],
        "required_capabilities": ["data:private_holdout"],
        "estimated_cost": 0.5,
        "derived_from_direction_id": "direction_" + "c" * 64,
        "status": "candidate",
        "priority": {
            "bar_gap_closure": 1.0,
            "information_gain": 1.0,
            "mechanism_novelty": 1.0,
            "score": 9.0,
            "capability_fit": 0.0,
            "normalized_cost": 0.0,
            "evidence_basis": ["direction_" + "c" * 64],
        },
    }
    state["nodes"][0]["strategy"] = strategy
    state["frontier"][0]["priority_components"] = {
        "bar_gap_closure": 1.0,
        "information_gain": 1.0,
        "mechanism_novelty": 1.0,
        "score": 9.0,
        "capability_fit": 0.0,
        "normalized_cost": 0.0,
        "evidence_basis": ["observation_" + "a" * 64],
    }
    adaptive["strategies"] = [strategy]
    (thread_dir / "production" / "tree" / "search_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    engine = _BlindEngine(
        {
            "status": "checkpointed",
            "reason": "generation_retry",
            "next_tool_to_call": "advance_research",
        }
    )
    monkeypatch.setattr(
        mcp,
        "_build_blind_research_engine",
        lambda _tid: engine,
    )

    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert selected["status"] == "checkpointed"
    assert len(engine.calls) == 1
    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["nodes"][0]["status"] == "ready"
    assert persisted["frontier"][0]["status"] == "queued"


def test_legacy_ready_node_routes_to_blind_reorientation_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = "t_first_selection"
    thread_dir = _write_thread(tmp_path, tid, _state(status="ready"))
    _drop_authoritative_binding(thread_dir)
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)
    engine = _BlindEngine({"status": "direction_ready", "node_id": "n_blind"})
    monkeypatch.setattr(mcp, "_build_blind_research_engine", lambda _tid: engine)

    selected = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert selected == {"status": "direction_ready", "node_id": "n_blind"}
    assert len(engine.calls) == 1
    persisted = json.loads(
        (thread_dir / "production" / "tree" / "search_state.json").read_text()
    )
    assert persisted["nodes"][0]["status"] == "ready"
    assert "adaptive" not in persisted


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
    _drop_authoritative_binding(thread_dir)
    (thread_dir / "thread.json").write_text("{}", encoding="utf-8")
    (thread_dir / "grilling" / "grilling_session.json").write_text(
        "{}", encoding="utf-8"
    )
    monkeypatch.setattr(mcp, "_thread_dir", lambda _tid: tmp_path / _tid)

    response = mcp.handle_get_next_admissible_node({"thread_id": tid})

    assert response["status"] == "hard_external_block"
    assert response["code"] == "operator_scope_conflict"


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
