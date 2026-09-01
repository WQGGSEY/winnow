from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest

from research_harness.acquisition import (
    AcquisitionBlocked,
    AcquisitionBudget,
    AcquisitionCheckpoint,
    AcquisitionComplete,
    make_acquisition_command,
)
from research_harness.acquisition.model import make_cursor, make_manifest
from research_harness.orchestrator.blind_reorientation import (
    AcquisitionReserved,
    Checkpointed,
    GoalAchieved,
    HardExternalBlockCode,
    ReorientationState,
    Seeking,
    make_checkpoint,
    serialize_reorientation_state,
)
from research_harness.orchestrator.blind_sequential_research import (
    BlindSequentialResearch,
    BlindSequentialResearchError,
    VerifiedStrongResult,
    resolve_strong_result_binding,
)
from research_harness.orchestrator.direction_generation import (
    DirectionFingerprint,
    make_direction_draft,
    make_direction_fingerprint,
    make_structural_equivalence_assessment,
    parse_direction_draft,
    serialize_direction_draft,
)
from research_harness.orchestrator.solution_contract import (
    compile_solution_contract,
    serialize_solution_contract,
    solution_contract_input_from_artifacts,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _contract():
    source = solution_contract_input_from_artifacts(
        repo_root=REPO_ROOT,
        baseline_dossier_id="bd_agent_harness_20260523",
        operator_problem="Find a safe intervention that improves utility.",
        grilling_record={
            "rounds": [],
            "extracted": {
                "claim_under_test": "A deployable intervention beats the incumbent.",
                "mandatory_baselines": ["incumbent", "naive", "random/null"],
                "success_criteria": ["utility improves by at least 5%"],
                "disproof_conditions": ["utility improvement is below 5%"],
                "taste_constraints": ["no access-control bypass"],
            },
        },
        feasibility_envelope={
            "operator_intent": {
                "target_deploy_grade_scope": "deployment",
                "acceptable_alternative_scopes": ["deployment"],
            },
            "external_falsifier": {
                "kind": "real_holdout",
                "holdout_source_id": "operator_holdout",
                "predicate": {
                    "metric": "utility",
                    "op": ">=",
                    "threshold": 0.05,
                },
                "registered_by": "operator",
            },
        },
    )
    return compile_solution_contract(source)


def _fingerprint(label: str) -> DirectionFingerprint:
    return make_direction_fingerprint(
        mechanism=f"{label} mechanism",
        intervention=f"{label} intervention",
        observables_and_data=f"{label} observations",
        analysis_unit=f"{label} unit",
        timescale=f"{label} timescale",
        system_boundary=f"{label} boundary",
    )


def _draft(label: str):
    return make_direction_draft(
        claim=f"The {label} intervention improves utility.",
        fingerprint=_fingerprint(label),
        experiment_objective="Measure utility against every mandatory baseline.",
        predicted_outcomes=(
            "Utility clears the success threshold.",
            "Utility fails one mandatory baseline.",
        ),
    )


class _Generator:
    def __init__(self, *drafts) -> None:
        self._drafts = list(drafts)
        self.calls: list[dict[str, object]] = []
        self.after_call = None

    def generate(self, request):
        self.calls.append(deepcopy(request))
        result = serialize_direction_draft(self._drafts.pop(0))
        if self.after_call is not None:
            self.after_call(len(self.calls))
        return result


class _Assessor:
    def assess(self, candidate, reference):
        return make_structural_equivalence_assessment(
            candidate=candidate,
            reference=reference,
            mechanism="changed",
            intervention="changed",
            observables_and_data="changed",
            analysis_unit="changed",
            timescale="changed",
            system_boundary="changed",
            evidence_source_digest="sha256:" + "a" * 64,
        )


class _LockProbe:
    def __init__(self) -> None:
        self.held = False

    @contextmanager
    def acquire(self):
        assert not self.held
        self.held = True
        try:
            yield
        finally:
            self.held = False


class _LockAwareAssessor(_Assessor):
    def __init__(self, probe: _LockProbe) -> None:
        self.probe = probe

    def assess(self, candidate, reference):
        assert not self.probe.held
        return super().assess(candidate, reference)


class _Acquisition:
    def __init__(self) -> None:
        self.outcomes = []
        self.calls = []

    def acquire(self, command, *, cursor=None):
        self.calls.append((command, cursor))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, AcquisitionComplete):
            self.last_manifest = outcome.manifest
        return outcome

    def verify_manifest(self, manifest_id, *, node_id):
        assert self.last_manifest.manifest_id == manifest_id
        assert self.last_manifest.node_id == node_id
        return self.last_manifest


class _StrongVerifier:
    def __init__(self) -> None:
        self.bindings = []

    def verify(self, binding):
        self.bindings.append(binding)
        return VerifiedStrongResult(
            binding=binding,
            receipt_sha256="sha256:" + "f" * 64,
        )


class _RejectStrongVerifier:
    def verify(self, binding):
        return None


@contextmanager
def _writer_lock():
    yield


def _empty_search_state() -> dict[str, object]:
    return {
        "search_id": "s_blind",
        "status": "running",
        "max_depth": 5,
        "max_debug_depth": 2,
        "sunk_cost_policy": "progress_gated",
        "scaleup_policy": "disallow_by_default",
        "frontier": [],
        "nodes": [],
        "completed_node_ids": [],
        "promoted_node_ids": [],
        "pruned_node_ids": [],
        "transitions": [],
    }


def _legacy_node(
    node_id: str,
    claim: str,
    *,
    status: str,
    parent: str | None = None,
    target_scope: str | None = None,
) -> dict[str, object]:
    claim_contract: dict[str, object] = {
        "claim_under_test": claim,
        "mandatory_baselines": ["baseline"],
        "success_criteria": ["score >= 1"],
        "disproof_conditions": ["score < 1"],
    }
    if target_scope is not None:
        claim_contract["deploy_grade_scope"] = target_scope
    return {
        "id": node_id,
        "type": "mechanism",
        "status": status,
        "domain": "test",
        "stage": "experimentation",
        "parent": parent,
        "lineage": {
            "root_goal_id": node_id if parent is None else parent,
            "covers_goal_facets": [],
            "inherited_assumptions": [],
            "introduced_assumptions": [],
            "taste_constraints_applied": [],
        },
        "claim_contract": claim_contract,
        "baseline_refs": [
            {
                "baseline_dossier_id": "bd_test",
                "candidate_ids": [],
                "roles": ["current_best_known", "naive", "random_or_null"],
            }
        ],
        "runtime_profile": {
            "worker_type": "experiment_worker",
            "timeout_policy": "test",
            "turn_budget": 2,
        },
        "failure_retrieval": {"query_tags": [], "selected_fail_files": []},
        "outputs": {"artifacts": [], "verdict": None},
    }


def _engine(
    tmp_path: Path,
    generator: _Generator,
    acquisition: _Acquisition,
    *,
    writer_lock=_writer_lock,
    assessor=None,
    strong_result_verifier=None,
):
    thread_dir = tmp_path / "runs" / "threads" / "thread_test"
    tree = thread_dir / "production" / "tree"
    tree.mkdir(parents=True)
    (tree / "search_state.json").write_text(
        json.dumps(_empty_search_state()),
        encoding="utf-8",
    )
    paths = thread_dir / "production" / "reorientation"
    paths.mkdir(parents=True)
    (paths / "solution_contract.json").write_text(
        json.dumps(serialize_solution_contract(_contract())),
        encoding="utf-8",
    )
    return BlindSequentialResearch(
        repo_root=REPO_ROOT,
        thread_dir=thread_dir,
        writer_lock=writer_lock,
        direction_generator=generator,
        structural_assessor=assessor or _Assessor(),
        acquisition=acquisition,
        strong_result_verifier=strong_result_verifier,
        perspective_seed=73,
    )


def _acquisition_command(direction_result: dict[str, object]):
    direction = parse_direction_draft(direction_result["direction"])
    return make_acquisition_command(
        reservation_id=direction_result["reservation_id"],
        node_id=direction_result["node_id"],
        attempt_id=direction_result["attempt_id"],
        direction=direction,
        needs=(),
        budget=AcquisitionBudget(
            max_requests=2,
            max_download_bytes=1024,
            max_wall_seconds=10,
        ),
    )


def test_generation_is_blind_and_command_retry_is_idempotent(tmp_path: Path) -> None:
    generator = _Generator(_draft("first"))
    engine = _engine(tmp_path, generator, _Acquisition())

    first = engine.advance_research(command_id="cycle-1")
    repeated = engine.advance_research(command_id="cycle-1")

    assert first == repeated
    assert first["status"] == "direction_ready"
    assert len(generator.calls) == 1
    assert set(generator.calls[0]) == {"solution_contract", "random_perspective"}
    forbidden = {"failure", "lesson", "history", "resources", "closed_attempts"}
    assert forbidden.isdisjoint(generator.calls[0])
    state = engine.read_state()
    assert state is not None
    assert isinstance(state.phase, AcquisitionReserved)


def test_verified_strong_result_commits_active_binding_as_terminal(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    verifier = _StrongVerifier()
    engine = _engine(
        tmp_path,
        _Generator(_draft("first")),
        acquisition,
        strong_result_verifier=verifier,
    )
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    manifest = make_manifest(
        command_id=command.command_id,
        node_id=command.node_id,
        attempt_id=command.attempt_id,
        direction_id=command.direction.direction_id,
        acquired_needs=(),
    )
    acquisition.outcomes.append(AcquisitionComplete(manifest))
    engine.advance_research(
        command_id="cycle-2",
        acquisition_command=command,
    )

    result = engine.advance_research(command_id="cycle-3")

    state = engine.read_state()
    assert state is not None
    assert isinstance(state.phase, GoalAchieved)
    assert result == {
        "status": "goal_achieved",
        "attempt_id": command.attempt_id,
        "node_id": command.node_id,
        "strong_result_receipt_sha256": "sha256:" + "f" * 64,
    }
    assert verifier.bindings[0].contract_id == _contract().contract_id
    assert verifier.bindings[0].manifest_id == manifest.manifest_id


def test_requested_node_cannot_hide_a_second_active_attempt_node(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    engine = _engine(tmp_path, _Generator(_draft("first")), acquisition)
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    manifest = make_manifest(
        command_id=command.command_id,
        node_id=command.node_id,
        attempt_id=command.attempt_id,
        direction_id=command.direction.direction_id,
        acquired_needs=(),
    )
    acquisition.outcomes.append(AcquisitionComplete(manifest))
    engine.advance_research(
        command_id="cycle-2",
        acquisition_command=command,
    )
    state = engine.read_state()
    assert state is not None
    attempts = json.loads(engine.paths.node_attempts.read_text(encoding="utf-8"))
    attempts["nodes"]["n_duplicate"] = dict(
        attempts["nodes"][command.node_id]
    )

    with pytest.raises(
        BlindSequentialResearchError,
        match="exactly one materialized node",
    ):
        resolve_strong_result_binding(
            state,
            attempts,
            node_id=command.node_id,
        )

    del attempts["nodes"]["n_duplicate"]
    with pytest.raises(
        BlindSequentialResearchError,
        match="requested node is not",
    ):
        resolve_strong_result_binding(
            state,
            attempts,
            node_id="n_other",
        )


def test_expected_revision_prevents_stale_supervisor_advance(tmp_path: Path) -> None:
    generator = _Generator(_draft("unused"))
    engine = _engine(tmp_path, generator, _Acquisition())

    result = engine.advance_research(
        command_id="stale-supervisor",
        expected_revision=1,
    )

    assert result == {
        "status": "state_changed",
        "expected_revision": 1,
        "actual_revision": 0,
    }
    assert generator.calls == []


def test_terminal_only_advance_does_not_fall_through_after_evidence_race(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    engine = _engine(
        tmp_path,
        _Generator(_draft("first")),
        acquisition,
        strong_result_verifier=_RejectStrongVerifier(),
    )
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    manifest = make_manifest(
        command_id=command.command_id,
        node_id=command.node_id,
        attempt_id=command.attempt_id,
        direction_id=command.direction.direction_id,
        acquired_needs=(),
    )
    acquisition.outcomes.append(AcquisitionComplete(manifest))
    engine.advance_research(
        command_id="cycle-2",
        acquisition_command=command,
    )
    before = engine.read_state()

    result = engine.advance_research(
        command_id="terminal-race",
        expected_revision=before.revision,
        expected_strong_result_receipt_sha256="sha256:" + "f" * 64,
    )

    assert result == {"status": "strong_result_changed"}
    assert engine.read_state() == before


def test_acquisition_checkpoint_resumes_exact_cursor_and_materializes_root(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    engine = _engine(tmp_path, _Generator(_draft("first")), acquisition)
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    cursor = make_cursor(
        command_id=command.command_id,
        next_need_index=0,
        next_candidate_index=0,
        requests_used=1,
        download_bytes_used=17,
        completed=(),
    )
    manifest = make_manifest(
        command_id=command.command_id,
        node_id=command.node_id,
        attempt_id=command.attempt_id,
        direction_id=command.direction.direction_id,
        acquired_needs=(),
    )
    acquisition.outcomes.extend(
        [
            AcquisitionCheckpoint(reason="request_budget", cursor=cursor),
            AcquisitionComplete(manifest),
        ]
    )

    checkpoint = engine.advance_research(
        command_id="cycle-2",
        acquisition_command=command,
    )
    checkpoint_state = engine.read_state()
    assert checkpoint_state is not None
    assert isinstance(checkpoint_state.phase, Checkpointed)
    resumed = engine.advance_research(command_id="cycle-3")

    assert checkpoint["status"] == "checkpointed"
    assert resumed["status"] == "acquisition_running"
    assert acquisition.calls == [(command, None), (command, cursor)]
    search_state = json.loads(
        (
            engine.paths.root.parent
            / "tree"
            / "search_state.json"
        ).read_text(encoding="utf-8")
    )
    node = next(item for item in search_state["nodes"] if item["id"] == command.node_id)
    assert node["parent"] is None
    assert node["strategy"]["derived_from_direction_id"] == command.direction.direction_id
    assert "derived_from_observation_id" not in node["strategy"]
    assert search_state["adaptive"]["observations"] == []


def test_unchanged_checkpoint_id_still_advances_revision(tmp_path: Path) -> None:
    acquisition = _Acquisition()
    engine = _engine(tmp_path, _Generator(_draft("first")), acquisition)
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    cursor = make_cursor(
        command_id=command.command_id,
        next_need_index=0,
        next_candidate_index=0,
        requests_used=1,
        download_bytes_used=17,
        completed=(),
    )
    acquisition.outcomes.extend(
        [
            AcquisitionCheckpoint(reason="request_budget", cursor=cursor),
            AcquisitionCheckpoint(reason="request_budget", cursor=cursor),
        ]
    )
    engine.advance_research(
        command_id="cycle-2",
        acquisition_command=command,
    )
    checkpointed = engine.read_state()
    assert checkpointed is not None
    assert isinstance(checkpointed.phase, Checkpointed)

    repeated = engine.advance_research(
        command_id="cycle-3",
        expected_revision=checkpointed.revision,
        expected_checkpoint_id=checkpointed.phase.checkpoint.checkpoint_id,
    )

    assert repeated["checkpoint_id"] == checkpointed.phase.checkpoint.checkpoint_id
    assert repeated["resumed_from_checkpoint_id"] == repeated["checkpoint_id"]
    assert repeated["revision"] == checkpointed.revision + 1


def test_conclusive_failure_closes_attempt_without_leaking_into_next_request(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    generator = _Generator(_draft("first"), _draft("second"))
    probe = _LockProbe()
    engine = _engine(
        tmp_path,
        generator,
        acquisition,
        writer_lock=probe.acquire,
        assessor=_LockAwareAssessor(probe),
    )
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    manifest = make_manifest(
        command_id=command.command_id,
        node_id=command.node_id,
        attempt_id=command.attempt_id,
        direction_id=command.direction.direction_id,
        acquired_needs=(),
    )
    acquisition.outcomes.append(AcquisitionComplete(manifest))
    engine.advance_research(
        command_id="cycle-2",
        acquisition_command=command,
    )
    node_dir = (
        engine.paths.root.parent
        / "tree"
        / "nodes"
        / command.node_id
    )
    node_dir.mkdir(parents=True)
    worker_report = {
        "node_id": command.node_id,
        "status": "completed",
        "claim_verdict_candidate": "contradicted",
        "metrics": {"utility": 0.52},
        "baselines": {"incumbent": 0.50},
        "baseline_evidence_status": {
            "overall": "failed",
            "results": [
                {
                    "role": "current_best_known",
                    "metric_key": "utility",
                    "baseline_key": "incumbent",
                    "operator": "greater_equal",
                    "margin": 0.05,
                    "required": True,
                    "metric_value": 0.52,
                    "baseline_value": 0.50,
                    "status": "failed",
                    "reason": "deterministic comparison result",
                }
            ],
        },
        "disproof_conditions_hit": [],
        "artifacts": [],
        "unexpected_observations": [],
        "failure_record_candidate": None,
    }
    (node_dir / "worker_report.json").write_text(
        json.dumps(worker_report),
        encoding="utf-8",
    )

    closed = engine.advance_research(command_id="cycle-3")
    next_direction = engine.advance_research(command_id="cycle-4")

    assert closed["status"] == "checkpointed"
    assert next_direction["status"] == "direction_ready"
    assert len(engine.read_state().closed_attempts) == 1
    assert len(generator.calls) == 2
    assert set(generator.calls[1]) == {"solution_contract", "random_perspective"}
    assert generator.calls[1]["random_perspective"]["draw_index"] == 1


def test_checkpoint_state_is_typed_after_acquisition_budget(tmp_path: Path) -> None:
    acquisition = _Acquisition()
    engine = _engine(tmp_path, _Generator(_draft("first")), acquisition)
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    cursor = make_cursor(
        command_id=command.command_id,
        next_need_index=0,
        next_candidate_index=0,
        requests_used=1,
        download_bytes_used=0,
        completed=(),
    )
    acquisition.outcomes.append(
        AcquisitionCheckpoint(reason="request_budget", cursor=cursor)
    )

    engine.advance_research(
        command_id="cycle-2",
        acquisition_command=command,
    )

    state = engine.read_state()
    assert state is not None
    assert isinstance(state.phase, Checkpointed)
    assert isinstance(state.phase.continuation, AcquisitionReserved)
    assert state.phase.checkpoint.payload_digest is not None
    assert engine.paths.checkpoint_payload(
        state.phase.checkpoint.payload_digest
    ).exists()


def test_checkpoint_payload_tampering_is_rejected_before_resume(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    engine = _engine(tmp_path, _Generator(_draft("first")), acquisition)
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    cursor = make_cursor(
        command_id=command.command_id,
        next_need_index=0,
        next_candidate_index=0,
        requests_used=1,
        download_bytes_used=0,
        completed=(),
    )
    acquisition.outcomes.append(
        AcquisitionCheckpoint(reason="request_budget", cursor=cursor)
    )
    engine.advance_research(
        command_id="cycle-2",
        acquisition_command=command,
    )
    state = engine.read_state()
    assert state is not None
    assert isinstance(state.phase, Checkpointed)
    payload_digest = state.phase.checkpoint.payload_digest
    assert payload_digest is not None
    payload_path = engine.paths.checkpoint_payload(payload_digest)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["cursor"]["requests_used"] = 0
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        BlindSequentialResearchError,
        match="checkpoint payload digest does not match durable state",
    ):
        engine.advance_research(command_id="cycle-3")

    assert acquisition.calls == [(command, None)]


def test_checkpoint_rejects_cursor_from_another_command(tmp_path: Path) -> None:
    acquisition = _Acquisition()
    engine = _engine(tmp_path, _Generator(_draft("first")), acquisition)
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    other = make_acquisition_command(
        reservation_id=command.reservation_id,
        node_id=command.node_id,
        attempt_id=command.attempt_id,
        direction=command.direction,
        needs=(),
        budget=AcquisitionBudget(
            max_requests=3,
            max_download_bytes=1024,
            max_wall_seconds=10,
        ),
    )
    acquisition.outcomes.append(
        AcquisitionCheckpoint(
            reason="request_budget",
            cursor=make_cursor(
                command_id=other.command_id,
                next_need_index=0,
                next_candidate_index=0,
                requests_used=1,
                download_bytes_used=0,
                completed=(),
            ),
        )
    )

    with pytest.raises(BlindSequentialResearchError, match="different command"):
        engine.advance_research(
            command_id="cycle-2",
            acquisition_command=command,
        )

    assert isinstance(engine.read_state().phase, AcquisitionReserved)


def test_complete_rejects_manifest_from_another_command(tmp_path: Path) -> None:
    acquisition = _Acquisition()
    engine = _engine(tmp_path, _Generator(_draft("first")), acquisition)
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    other = make_acquisition_command(
        reservation_id=command.reservation_id,
        node_id=command.node_id,
        attempt_id=command.attempt_id,
        direction=command.direction,
        needs=(),
        budget=AcquisitionBudget(
            max_requests=3,
            max_download_bytes=1024,
            max_wall_seconds=10,
        ),
    )
    acquisition.outcomes.append(
        AcquisitionComplete(
            make_manifest(
                command_id=other.command_id,
                node_id=command.node_id,
                attempt_id=command.attempt_id,
                direction_id=command.direction.direction_id,
                acquired_needs=(),
            )
        )
    )

    with pytest.raises(
        BlindSequentialResearchError,
        match="not bound to the active attempt",
    ):
        engine.advance_research(
            command_id="cycle-2",
            acquisition_command=command,
        )

    assert isinstance(engine.read_state().phase, AcquisitionReserved)


def test_prepared_receipt_recovers_without_advancing_command_twice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    acquisition = _Acquisition()
    engine = _engine(tmp_path, _Generator(_draft("first")), acquisition)
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    cursor = make_cursor(
        command_id=command.command_id,
        next_need_index=0,
        next_candidate_index=0,
        requests_used=1,
        download_bytes_used=0,
        completed=(),
    )
    acquisition.outcomes.append(
        AcquisitionCheckpoint(reason="request_budget", cursor=cursor)
    )
    original = engine._write_command_receipt
    crashed = False

    def crash_once(command_id, input_digest, result):
        nonlocal crashed
        if command_id == "cycle-2" and not crashed:
            crashed = True
            raise OSError("simulated crash after state commit")
        return original(command_id, input_digest, result)

    monkeypatch.setattr(engine, "_write_command_receipt", crash_once)
    with pytest.raises(OSError, match="simulated crash"):
        engine.advance_research(
            command_id="cycle-2",
            acquisition_command=command,
        )
    monkeypatch.setattr(engine, "_write_command_receipt", original)

    recovered = engine.advance_research(
        command_id="cycle-2",
        acquisition_command=command,
    )

    assert recovered["status"] == "checkpointed"
    assert acquisition.calls == [(command, None)]


def test_external_block_can_retry_same_cursor_after_operator_action(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    engine = _engine(tmp_path, _Generator(_draft("first")), acquisition)
    direction_result = engine.advance_research(command_id="cycle-1")
    command = _acquisition_command(direction_result)
    cursor = make_cursor(
        command_id=command.command_id,
        next_need_index=0,
        next_candidate_index=0,
        requests_used=1,
        download_bytes_used=0,
        completed=(),
    )
    manifest = make_manifest(
        command_id=command.command_id,
        node_id=command.node_id,
        attempt_id=command.attempt_id,
        direction_id=command.direction.direction_id,
        acquired_needs=(),
    )
    acquisition.outcomes.extend(
        [
            AcquisitionBlocked(
                code=HardExternalBlockCode.AUTH_REQUIRED,
                required_external_action="Configure credential profile public-data.",
                cursor=cursor,
            ),
            AcquisitionComplete(manifest),
        ]
    )

    blocked = engine.advance_research(
        command_id="cycle-2",
        acquisition_command=command,
    )
    resumed = engine.advance_research(command_id="cycle-3")

    assert blocked["status"] == "hard_external_block"
    assert resumed["status"] == "acquisition_running"
    assert acquisition.calls == [(command, None), (command, cursor)]


def test_stale_generation_result_cannot_overwrite_newer_state(tmp_path: Path) -> None:
    generator = _Generator(_draft("stale"), _draft("committed"))
    engine = _engine(tmp_path, generator, _Acquisition())

    def advance_state(call_count: int) -> None:
        if call_count != 1:
            return
        state = engine.read_state()
        assert state is not None
        newer = ReorientationState(
            version=2,
            contract_id=state.contract_id,
            revision=state.revision + 1,
            closed_attempts=state.closed_attempts,
            phase=make_checkpoint(Seeking(), reason="generation_retry"),
        )
        engine.paths.state.write_text(
            json.dumps(serialize_reorientation_state(newer)),
            encoding="utf-8",
        )

    generator.after_call = advance_state

    result = engine.advance_research(command_id="concurrent-cycle")

    assert result["status"] == "direction_ready"
    assert result["direction"]["direction_id"] == _draft("committed").direction_id
    assert len(generator.calls) == 2
    assert not engine.paths.direction(_draft("stale").direction_id).exists()


def test_legacy_subtree_uses_one_attempt_identity() -> None:
    state = {
        "nodes": [
            {"id": "n_root", "parent": None},
            {"id": "n_child", "parent": "n_root"},
            {"id": "n_grandchild", "parent": "n_child"},
            {"id": "n_other", "parent": None},
        ]
    }

    bindings = BlindSequentialResearch._legacy_attempt_bindings(state)

    assert bindings["n_root"]["attempt_id"] == bindings["n_child"]["attempt_id"]
    assert bindings["n_child"]["attempt_id"] == bindings["n_grandchild"]["attempt_id"]
    assert bindings["n_other"]["attempt_id"] != bindings["n_root"]["attempt_id"]


def test_single_active_legacy_root_finishes_before_blind_migration(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    generator = _Generator(_draft("unused"))
    thread_dir = tmp_path / "runs" / "threads" / "thread_active_legacy"
    tree = thread_dir / "production" / "tree"
    tree.mkdir(parents=True)
    state = _empty_search_state()
    state["nodes"] = [
        _legacy_node("n_root", "Legacy claim", status="ready"),
        _legacy_node(
            "n_child",
            "Legacy child claim",
            status="proposed",
            parent="n_root",
        ),
    ]
    state["frontier"] = [
        {
            "node_id": "n_root",
            "parent": None,
            "depth": 0,
            "priority": 1.0,
            "stage": "experimentation",
            "status": "queued",
            "reason": "legacy root",
        }
    ]
    (tree / "search_state.json").write_text(json.dumps(state), encoding="utf-8")
    engine = BlindSequentialResearch(
        repo_root=REPO_ROOT,
        thread_dir=thread_dir,
        writer_lock=_writer_lock,
        direction_generator=generator,
        structural_assessor=_Assessor(),
        acquisition=acquisition,
    )

    result = engine.advance_research(command_id="migrate")
    repeated = engine.advance_research(command_id="migrate")

    assert result["status"] == "acquisition_running"
    assert repeated == result
    assert result["acquisition_status"] == "legacy_work_pending"
    assert result["next_tool_to_call"] == "get_next_admissible_node"
    attempts = json.loads(engine.paths.node_attempts.read_text(encoding="utf-8"))
    root = attempts["nodes"]["n_root"]
    child = attempts["nodes"]["n_child"]
    assert root["attempt_id"] == result["attempt_id"]
    assert child["attempt_id"] == result["attempt_id"]
    assert root["legacy_audit_only"] is False
    assert child["legacy_audit_only"] is False
    assert engine.read_contract() is None
    assert engine.read_state() is None
    assert generator.calls == []


def test_multiple_active_legacy_roots_require_operator_resolution(
    tmp_path: Path,
) -> None:
    thread_dir = tmp_path / "runs" / "threads" / "thread_active_conflict"
    tree = thread_dir / "production" / "tree"
    tree.mkdir(parents=True)
    state = _empty_search_state()
    state["nodes"] = [
        _legacy_node("n_a", "Same claim", status="ready"),
        _legacy_node("n_b", "Same claim", status="running"),
    ]
    state["frontier"] = [
        {
            "node_id": node_id,
            "parent": None,
            "depth": 0,
            "priority": 1.0,
            "stage": "experimentation",
            "status": frontier_status,
            "reason": "legacy root",
        }
        for node_id, frontier_status in (("n_a", "queued"), ("n_b", "running"))
    ]
    (tree / "search_state.json").write_text(json.dumps(state), encoding="utf-8")
    engine = BlindSequentialResearch(
        repo_root=REPO_ROOT,
        thread_dir=thread_dir,
        writer_lock=_writer_lock,
        direction_generator=_Generator(_draft("unused")),
        structural_assessor=_Assessor(),
        acquisition=_Acquisition(),
    )

    result = engine.advance_research(command_id="migrate")

    assert result["status"] == "hard_external_block"
    assert result["code"] == "operator_scope_conflict"
    assert engine.read_state() is None


def test_conflicting_legacy_success_scopes_create_operational_block(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    generator = _Generator(_draft("unused"))
    thread_dir = tmp_path / "runs" / "threads" / "thread_conflict"
    tree = thread_dir / "production" / "tree"
    tree.mkdir(parents=True)

    state = _empty_search_state()
    state["nodes"] = [
        _legacy_node(
            "n_a",
            "Same claim",
            status="pruned",
            target_scope="deployment",
        ),
        _legacy_node(
            "n_b",
            "Same claim",
            status="pruned",
            target_scope="feasibility",
        ),
    ]
    state["pruned_node_ids"] = ["n_a", "n_b"]
    (tree / "search_state.json").write_text(json.dumps(state), encoding="utf-8")
    engine = BlindSequentialResearch(
        repo_root=REPO_ROOT,
        thread_dir=thread_dir,
        writer_lock=_writer_lock,
        direction_generator=generator,
        structural_assessor=_Assessor(),
        acquisition=acquisition,
    )

    result = engine.advance_research(command_id="migrate")

    assert result["status"] == "hard_external_block"
    assert result["code"] == "operator_scope_conflict"
    assert engine.paths.migration_block.exists()
    assert engine.read_state() is None
    assert generator.calls == []
