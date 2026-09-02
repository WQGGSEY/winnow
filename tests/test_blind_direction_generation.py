from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from research_harness.connector.field_sampler import load_categories
from research_harness.orchestrator.attempt_evidence import (
    AttemptEvidenceError,
    ConclusiveFailure,
    FailureEvidenceReceipt,
    NeedsData,
    NeedsMoreEvidence,
    NeedsMoreEvidenceReason,
    StrongCandidate,
    derive_attempt_evidence,
    parse_failure_evidence_receipt,
    serialize_failure_evidence_receipt,
)
from research_harness.orchestrator.blind_reorientation import (
    AwaitingEvidence,
    DirectionAttemptRef,
    GoalAchieved,
    ReorientationState,
    parse_reorientation_state,
    serialize_reorientation_state,
)
from research_harness.orchestrator.direction_generation import (
    DataNeed,
    DirectionDraft,
    DirectionFingerprint,
    DirectionGenerationError,
    GenerationRequest,
    NoveltyReason,
    StructuralEquivalenceAssessment,
    gate_direction_novelty,
    invoke_direction_generator,
    make_direction_draft,
    make_direction_fingerprint,
    make_structural_equivalence_assessment,
    parse_direction_draft,
    parse_generation_request,
    sample_random_perspective,
    serialize_direction_draft,
    serialize_generation_request,
)
from research_harness.orchestrator.goal_contract import (
    compile_goal_contract,
    goal_contract_input_from_artifacts,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DOSSIER_ID = "bd_agent_harness_20260523"


def _contract():
    grilling = {
        "rounds": [],
        "extracted": {
            "claim_under_test": "A deployable intervention beats the incumbent.",
            "mandatory_baselines": ["incumbent", "naive", "random/null"],
            "success_criteria": ["utility improves by at least 5%"],
            "disproof_conditions": ["utility improvement is below 5%"],
            "taste_constraints": ["no access-control bypass"],
        },
    }
    envelope = {
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
    }
    source = goal_contract_input_from_artifacts(
        repo_root=REPO_ROOT,
        baseline_dossier_id=BASELINE_DOSSIER_ID,
        operator_problem="Find a safe intervention that improves utility.",
        grilling_record=grilling,
        feasibility_envelope=envelope,
    )
    return compile_goal_contract(source)


def _fingerprint(label: str) -> DirectionFingerprint:
    return make_direction_fingerprint(
        mechanism=f"{label} mechanism",
        intervention=f"{label} intervention",
        observables_and_data=f"{label} observables",
        analysis_unit=f"{label} unit",
        timescale=f"{label} timescale",
        system_boundary=f"{label} boundary",
    )


def _draft(label: str = "candidate") -> DirectionDraft:
    return make_direction_draft(
        claim=f"The {label} intervention improves utility.",
        fingerprint=_fingerprint(label),
        experiment_objective="Measure utility against every mandatory baseline.",
        data_needs=(
            DataNeed(
                kind="public_api",
                description="Daily aggregate outcome observations",
            ),
        ),
        predicted_outcomes=(
            "Utility clears the success threshold.",
            "Utility fails one mandatory baseline.",
        ),
    )


class _SpyTransport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def generate(self, request):
        self.calls.append(deepcopy(request))
        return self.responses.pop(0)


def _worker_report(
    *,
    status: str = "completed",
    verdict: str = "contradicted",
    baseline_overall: str = "failed",
) -> dict[str, object]:
    comparison_status = "failed" if baseline_overall == "failed" else "passed"
    return {
        "node_id": "node_1",
        "status": status,
        "claim_verdict_candidate": verdict,
        "metrics": {"utility": 0.52},
        "baselines": {"incumbent": 0.50},
        "baseline_evidence_status": {
            "overall": baseline_overall,
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
                    "status": comparison_status,
                    "reason": "deterministic comparison result",
                }
            ],
        },
        "disproof_conditions_hit": [],
        "artifacts": ["nodes/node_1/workspace/artifacts/metrics.json"],
        "unexpected_observations": [],
        "failure_record_candidate": None,
    }


def test_generator_receives_only_contract_and_perspective_after_failure() -> None:
    contract = _contract()
    transport = _SpyTransport(
        [
            serialize_direction_draft(_draft("first")),
            serialize_direction_draft(_draft("second")),
        ]
    )
    first_request = GenerationRequest(
        goal_contract=contract,
        random_perspective=sample_random_perspective(seed=17, draw_index=0),
    )
    invoke_direction_generator(first_request, transport)
    failure = derive_attempt_evidence(
        _worker_report(),
        data_status="satisfied",
    )
    assert isinstance(failure, ConclusiveFailure)
    second_request = GenerationRequest(
        goal_contract=contract,
        random_perspective=sample_random_perspective(seed=17, draw_index=1),
    )
    invoke_direction_generator(second_request, transport)

    assert [set(call) for call in transport.calls] == [
        {"goal_contract", "random_perspective"},
        {"goal_contract", "random_perspective"},
    ]
    serialized = serialize_generation_request(second_request)
    assert parse_generation_request(serialized) == second_request
    assert not {"state", "history", "failure", "lesson", "resources"}.intersection(
        serialized
    )
    leaked = deepcopy(serialized)
    leaked["failure"] = serialize_failure_evidence_receipt(failure.receipt)
    with pytest.raises(ValueError, match="unexpected"):
        parse_generation_request(leaked)


def test_direction_ids_and_unbounded_perspectives_are_deterministic() -> None:
    draft = _draft()
    assert parse_direction_draft(serialize_direction_draft(draft)) == draft
    tampered = serialize_direction_draft(draft)
    tampered["fingerprint"]["mechanism"] = "renamed mechanism"
    with pytest.raises(ValueError, match="identity"):
        parse_direction_draft(tampered)

    namespace_size = len(load_categories())
    draw_index = namespace_size * 4 + 7
    first = sample_random_perspective(seed=88, draw_index=draw_index)
    second = sample_random_perspective(seed=88, draw_index=draw_index)
    assert first == second
    assert first.epoch == 4
    assert first.draw_index == draw_index


def _assessment(
    candidate: DirectionFingerprint,
    reference: DirectionFingerprint,
    *changed: str,
    digest_digit: str = "a",
) -> StructuralEquivalenceAssessment:
    relations = {
        axis: "changed" if axis in changed else "equivalent"
        for axis in (
            "mechanism",
            "intervention",
            "observables_and_data",
            "analysis_unit",
            "timescale",
            "system_boundary",
        )
    }
    return make_structural_equivalence_assessment(
        candidate=candidate,
        reference=reference,
        **relations,
        evidence_source_digest="sha256:" + digest_digit * 64,
    )


class _Assessor:
    def __init__(self, assessments: list[StructuralEquivalenceAssessment]) -> None:
        self.assessments = assessments
        self.calls: list[tuple[str, str]] = []

    def assess(self, candidate, reference):
        self.calls.append((candidate.fingerprint_id, reference.fingerprint_id))
        return self.assessments.pop(0)


def test_novelty_gate_uses_required_six_axis_assessments() -> None:
    candidate = _fingerprint("candidate")
    closed = [_fingerprint("one"), _fingerprint("two"), _fingerprint("three")]

    equivalent = _Assessor([_assessment(candidate, closed[0])])
    one_rejected = gate_direction_novelty(candidate, closed[:1], equivalent)
    assert one_rejected.reason is NoveltyReason.INSUFFICIENT_AXIS_CHANGE
    assert len(equivalent.calls) == 1

    one_changed = _Assessor(
        [_assessment(candidate, closed[0], "mechanism", "timescale")]
    )
    assert gate_direction_novelty(
        candidate,
        closed[:1],
        one_changed,
    ).decision == "accepted"

    core_assessor = _Assessor(
        [
            _assessment(
                candidate,
                closed[0],
                "mechanism",
                "intervention",
                digest_digit="b",
            ),
            _assessment(
                candidate,
                closed[1],
                "mechanism",
                digest_digit="c",
            ),
        ]
    )
    core_rejected = gate_direction_novelty(candidate, closed[:2], core_assessor)
    assert core_rejected.reason is NoveltyReason.CORE_AXES_EQUIVALENT
    assert len(core_assessor.calls) == 2

    boundary_assessor = _Assessor(
        [
            _assessment(
                candidate,
                closed[0],
                "mechanism",
                "intervention",
                "system_boundary",
            ),
            _assessment(
                candidate,
                closed[1],
                "mechanism",
                "intervention",
                "system_boundary",
            ),
            _assessment(
                candidate,
                closed[2],
                "mechanism",
                "intervention",
            ),
        ]
    )
    boundary_rejected = gate_direction_novelty(
        candidate,
        closed,
        boundary_assessor,
    )
    assert boundary_rejected.reason is NoveltyReason.SYSTEM_BOUNDARY_EQUIVALENT
    assert len(boundary_assessor.calls) == 3

    accepted_assessor = _Assessor(
        [
            _assessment(
                candidate,
                closed[index],
                "mechanism",
                "intervention",
                "system_boundary",
            )
            for index in range(3)
        ]
    )
    assert gate_direction_novelty(
        candidate,
        closed,
        accepted_assessor,
    ).decision == "accepted"

    duplicate_assessor = _Assessor([])
    duplicate = gate_direction_novelty(closed[0], closed, duplicate_assessor)
    assert duplicate.reason is NoveltyReason.EXACT_FINGERPRINT_DUPLICATE
    assert duplicate_assessor.calls == []

    stale = _Assessor(
        [
            _assessment(
                candidate,
                closed[1],
                "mechanism",
                "timescale",
            )
        ]
    )
    with pytest.raises(DirectionGenerationError, match="fingerprint pair"):
        gate_direction_novelty(candidate, closed[:1], stale)

    bound = _assessment(
        candidate,
        closed[0],
        "mechanism",
        "timescale",
    )
    with pytest.raises(DirectionGenerationError, match="receipt digest"):
        replace(bound, mechanism="equivalent")
    with pytest.raises(DirectionGenerationError, match="receipt digest"):
        replace(bound, assessment_receipt_digest="sha256:" + "0" * 64)


def test_attempt_ref_round_trips_the_full_fingerprint() -> None:
    fingerprint = _fingerprint("embedded")
    attempt = DirectionAttemptRef(
        attempt_id="attempt_embedded",
        direction_id="direction_" + "a" * 64,
        fingerprint=fingerprint,
        ordinal=0,
    )
    state = ReorientationState(
        version=2,
        contract_id=_contract().contract_id,
        revision=0,
        closed_attempts=(),
        phase=AwaitingEvidence(active_attempt=attempt),
    )
    document = serialize_reorientation_state(state)

    assert "fingerprint_id" not in document["phase"]["active_attempt"]
    assert document["phase"]["active_attempt"]["fingerprint"][
        "fingerprint_id"
    ] == fingerprint.fingerprint_id
    assert parse_reorientation_state(document) == state


def test_only_deterministic_baseline_failure_closes_scientifically() -> None:
    failure_report = _worker_report()
    for data_status in ("missing", "invalid"):
        assert isinstance(
            derive_attempt_evidence(failure_report, data_status=data_status),
            NeedsData,
        )

    runner_failure = deepcopy(failure_report)
    runner_failure["status"] = "failed"
    runner_failure["claim_verdict_candidate"] = "not_evaluable"
    assert isinstance(
        derive_attempt_evidence(runner_failure, data_status="satisfied"),
        NeedsMoreEvidence,
    )

    label_only = _worker_report(baseline_overall="passed")
    assert isinstance(
        derive_attempt_evidence(label_only, data_status="satisfied"),
        NeedsMoreEvidence,
    )

    forged_failure = deepcopy(failure_report)
    forged_failure["baseline_evidence_status"]["results"][0][
        "metric_value"
    ] = 0.20
    forged_outcome = derive_attempt_evidence(
        forged_failure,
        data_status="satisfied",
    )
    assert isinstance(forged_outcome, NeedsMoreEvidence)
    assert forged_outcome.reason is NeedsMoreEvidenceReason.MALFORMED_EVIDENCE

    empty_node = deepcopy(failure_report)
    empty_node["node_id"] = ""
    empty_outcome = derive_attempt_evidence(
        empty_node,
        data_status="satisfied",
    )
    assert isinstance(empty_outcome, NeedsMoreEvidence)
    assert empty_outcome.reason is NeedsMoreEvidenceReason.MALFORMED_EVIDENCE

    padded_node = deepcopy(failure_report)
    padded_node["node_id"] = " node_1 "
    padded_outcome = derive_attempt_evidence(
        padded_node,
        data_status="satisfied",
    )
    assert isinstance(padded_outcome, NeedsMoreEvidence)
    assert padded_outcome.reason is NeedsMoreEvidenceReason.MALFORMED_EVIDENCE

    for huge_report in (
        deepcopy(failure_report),
        _worker_report(verdict="supported", baseline_overall="passed"),
    ):
        huge_report["metrics"]["utility"] = 10**4000
        huge_outcome = derive_attempt_evidence(
            huge_report,
            data_status="satisfied",
        )
        assert isinstance(huge_outcome, NeedsMoreEvidence)
        assert huge_outcome.reason is NeedsMoreEvidenceReason.MALFORMED_EVIDENCE

    conclusive = derive_attempt_evidence(
        failure_report,
        data_status="satisfied",
    )
    assert isinstance(conclusive, ConclusiveFailure)
    assert (
        parse_failure_evidence_receipt(
            serialize_failure_evidence_receipt(conclusive.receipt)
        )
        == conclusive.receipt
    )

    changed_prose = deepcopy(failure_report)
    changed_prose["baseline_evidence_status"]["results"][0]["reason"] = (
        "Different explanatory prose."
    )
    changed_prose["failure_record_candidate"] = {
        "category": "negative_result",
        "reason": "Professor-like explanation that is not measured evidence.",
    }
    same_measurements = derive_attempt_evidence(
        changed_prose,
        data_status="satisfied",
    )
    assert isinstance(same_measurements, ConclusiveFailure)
    assert same_measurements == conclusive

    professor_input = deepcopy(failure_report)
    professor_input["final_verdict"] = "contradicted"
    assert isinstance(
        derive_attempt_evidence(professor_input, data_status="satisfied"),
        NeedsMoreEvidence,
    )


def test_failure_receipt_rejects_content_addressed_padded_node_id() -> None:
    node_id = " node_1 "
    evidence_sha256 = "sha256:" + "1" * 64
    payload = {
        "version": 1,
        "node_id": node_id,
        "evidence_sha256": evidence_sha256,
        "baseline_failure_count": 1,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(AttemptEvidenceError, match="must be canonical"):
        FailureEvidenceReceipt(
            version=1,
            receipt_id=f"failure_receipt_{digest}",
            receipt_sha256=f"sha256:{digest}",
            node_id=node_id,
            evidence_sha256=evidence_sha256,
            baseline_failure_count=1,
        )


def test_supported_evidence_is_only_a_strong_candidate() -> None:
    supported = _worker_report(
        verdict="supported",
        baseline_overall="passed",
    )
    outcome = derive_attempt_evidence(supported, data_status="satisfied")

    assert isinstance(outcome, StrongCandidate)
    assert not isinstance(outcome, GoalAchieved)
    assert not hasattr(outcome, "strong_result_receipt_sha256")
