#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research_harness.orchestrator.blind_reorientation import (
    AcquisitionReserved,
    DirectionAttemptRef,
    GenerationReservation,
    GenerationReserved,
    HardExternalBlock,
    HardExternalBlockCode,
    ReorientationState,
    ReorientationStateError,
    make_acquisition_reservation,
    make_checkpoint,
    make_generation_reservation,
    parse_reorientation_state,
    serialize_reorientation_state,
)
from research_harness.connector.field_sampler import load_categories
from research_harness.orchestrator.attempt_evidence import (
    ConclusiveFailure,
    StrongCandidate,
    derive_attempt_evidence,
)
from research_harness.orchestrator.direction_generation import (
    GenerationRequest,
    gate_direction_novelty,
    invoke_direction_generator,
    make_direction_draft,
    make_direction_fingerprint,
    make_structural_equivalence_assessment,
    sample_random_perspective,
    serialize_direction_draft,
)
from research_harness.orchestrator.goal_contract import (
    compile_goal_contract,
    goal_contract_input_from_artifacts,
    parse_goal_contract,
    serialize_goal_contract,
)


BASELINE_DOSSIER_ID = "bd_agent_harness_20260523"
BASELINE_PROVENANCE_PREFIX = f"baseline_dossier:{BASELINE_DOSSIER_ID}#"


class _GenerationSpy:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def generate(self, request):
        self.calls.append(deepcopy(request))
        return deepcopy(self.response)


class _ChangedAssessor:
    def __init__(self) -> None:
        self.calls = 0

    def assess(self, candidate, reference):
        self.calls += 1
        return make_structural_equivalence_assessment(
            candidate=candidate,
            reference=reference,
            mechanism="changed",
            intervention="changed",
            observables_and_data="changed",
            analysis_unit="changed",
            timescale="changed",
            system_boundary="changed",
            evidence_source_digest="sha256:" + "7" * 64,
        )


def _worker_report(*, supported: bool = False) -> dict[str, object]:
    metric_value = 0.57 if supported else 0.52
    comparison_status = "passed" if supported else "failed"
    return {
        "node_id": "node_verifier",
        "status": "completed",
        "claim_verdict_candidate": "supported" if supported else "contradicted",
        "metrics": {"utility": metric_value},
        "baselines": {"incumbent": 0.50},
        "baseline_evidence_status": {
            "overall": comparison_status,
            "results": [
                {
                    "role": "current_best_known",
                    "metric_key": "utility",
                    "baseline_key": "incumbent",
                    "operator": "greater_equal",
                    "margin": 0.05,
                    "required": True,
                    "metric_value": metric_value,
                    "baseline_value": 0.50,
                    "status": comparison_status,
                    "reason": "deterministic comparison result",
                }
            ],
        },
        "disproof_conditions_hit": [],
        "artifacts": ["artifacts/metrics.json"],
        "unexpected_observations": [],
        "failure_record_candidate": None,
    }


def _artifacts() -> tuple[dict, dict]:
    grilling = {
        "rounds": [],
        "extracted": {
            "claim_under_test": "The intervention beats the incumbent.",
            "mandatory_baselines": ["incumbent"],
            "success_criteria": ["utility improves by at least 5%"],
            "disproof_conditions": ["utility improvement is below 5%"],
            "taste_constraints": ["no access-control bypass"],
        },
    }
    envelope = {
        "operator_intent": {
            "target_deploy_grade_scope": "deployment",
            "acceptable_alternative_scopes": ["deployment"],
            "data_source_snapshot_id": "as_" + "1" * 64,
        },
        "data_sources_available": [
            {"id": "current", "snapshot_id": "as_" + "1" * 64}
        ],
        "external_falsifier": {
            "kind": "real_holdout",
            "holdout_source_id": "operator_holdout",
            "predicate": {"metric": "utility", "op": ">=", "threshold": 0.05},
            "registered_by": "operator",
        },
    }
    return grilling, envelope


def _compile(envelope: dict):
    grilling, _ = _artifacts()
    source = goal_contract_input_from_artifacts(
        repo_root=REPO_ROOT,
        baseline_dossier_id=BASELINE_DOSSIER_ID,
        operator_problem="Find a safe intervention that improves utility.",
        grilling_record=grilling,
        feasibility_envelope=envelope,
    )
    return compile_goal_contract(source)


def verify() -> dict[str, object]:
    _, envelope = _artifacts()
    first = _compile(envelope)
    changed = deepcopy(envelope)
    changed["operator_intent"]["data_source_snapshot_id"] = "as_" + "2" * 64
    changed["data_sources_available"] = [
        {"id": "replacement", "snapshot_id": "as_" + "2" * 64}
    ]
    second = _compile(changed)
    if second != first:
        raise RuntimeError("resource changes altered the goal contract")
    if parse_goal_contract(serialize_goal_contract(first)) != first:
        raise RuntimeError("goal contract round-trip failed")
    provenance = {
        item
        for baseline in first.baseline_evidence
        for item in baseline.provenance
    }
    expected_provenance = {
        BASELINE_PROVENANCE_PREFIX
        + "candidates/c1_sakana_ai_scientist_v2.md",
        BASELINE_PROVENANCE_PREFIX + "candidates/c2_direct_api_port.md",
        BASELINE_PROVENANCE_PREFIX + "candidates/c3_no_orchestrated_harness.md",
        "https://github.com/SakanaAI/AI-Scientist-v2",
    }
    if not expected_provenance <= provenance:
        raise RuntimeError("baseline evidence is missing resolvable provenance")

    fingerprint = make_direction_fingerprint(
        mechanism="bounded causal mechanism",
        intervention="bounded intervention",
        observables_and_data="measured aggregate outcomes",
        analysis_unit="deployment unit",
        timescale="thirty days",
        system_boundary="production service",
    )
    draft = make_direction_draft(
        claim="The intervention improves utility.",
        fingerprint=fingerprint,
        experiment_objective="Compare measured utility with the incumbent.",
        predicted_outcomes=(
            "The intervention clears the threshold.",
            "The intervention fails the threshold.",
        ),
    )
    spy = _GenerationSpy(serialize_direction_draft(draft))
    for draw_index in (0, 1):
        request = GenerationRequest(
            goal_contract=first,
            random_perspective=sample_random_perspective(
                seed=41,
                draw_index=draw_index,
            ),
        )
        if invoke_direction_generator(request, spy) != draft:
            raise RuntimeError("blind generator draft round-trip failed")
    if any(
        set(call) != {"goal_contract", "random_perspective"}
        for call in spy.calls
    ):
        raise RuntimeError("generator request leaked private state")
    beyond_namespace = len(load_categories()) * 3 + 2
    if sample_random_perspective(
        seed=41,
        draw_index=beyond_namespace,
    ) != sample_random_perspective(seed=41, draw_index=beyond_namespace):
        raise RuntimeError("unbounded perspective sampling is not deterministic")

    closed_fingerprints = tuple(
        make_direction_fingerprint(
            mechanism=f"mechanism {index}",
            intervention=f"intervention {index}",
            observables_and_data=f"observables {index}",
            analysis_unit=f"unit {index}",
            timescale=f"timescale {index}",
            system_boundary=f"boundary {index}",
        )
        for index in range(3)
    )
    assessor = _ChangedAssessor()
    novelty = gate_direction_novelty(
        fingerprint,
        closed_fingerprints,
        assessor,
    )
    if novelty.decision != "accepted" or assessor.calls != 3:
        raise RuntimeError("six-axis novelty escalation failed")

    failure = derive_attempt_evidence(
        _worker_report(),
        data_status="satisfied",
    )
    candidate = derive_attempt_evidence(
        _worker_report(supported=True),
        data_status="satisfied",
    )
    if not isinstance(failure, ConclusiveFailure):
        raise RuntimeError("deterministic baseline failure did not close")
    if not isinstance(candidate, StrongCandidate):
        raise RuntimeError("supported evidence did not remain a candidate")

    attempt = DirectionAttemptRef(
        attempt_id="attempt_0",
        direction_id="direction_" + "a" * 64,
        fingerprint=fingerprint,
        ordinal=0,
    )
    continuation = AcquisitionReserved(
        active_attempt=attempt,
        reservation=make_acquisition_reservation(
            expected_revision=0,
            request_digest="sha256:" + "d" * 64,
        ),
    )
    checkpointed = make_checkpoint(continuation, reason="download_budget")
    state = ReorientationState(
        version=2,
        contract_id=first.contract_id,
        revision=1,
        closed_attempts=(),
        phase=checkpointed,
    )
    document = serialize_reorientation_state(state)
    if parse_reorientation_state(document) != state:
        raise RuntimeError("reorientation state round-trip failed")

    generation = make_generation_reservation(
        expected_revision=0,
        request_digest="sha256:" + "e" * 64,
    )
    live_reserved = ReorientationState(
        version=2,
        contract_id=first.contract_id,
        revision=1,
        closed_attempts=(),
        phase=GenerationReserved(reservation=generation),
    )
    if parse_reorientation_state(
        serialize_reorientation_state(live_reserved)
    ) != live_reserved:
        raise RuntimeError("live reservation round-trip failed")
    try:
        GenerationReservation(
            reservation_id="reservation_" + "0" * 64,
            expected_revision=0,
            request_digest="sha256:" + "e" * 64,
        )
    except ReorientationStateError:
        pass
    else:
        raise RuntimeError("forged reservation identity was accepted")

    changed_checkpoint = deepcopy(document)
    changed_checkpoint["phase"]["continuation"]["active_attempt"][
        "attempt_id"
    ] = "attempt_changed"
    try:
        parse_reorientation_state(changed_checkpoint)
    except ValueError:
        pass
    else:
        raise RuntimeError("checkpoint accepted a changed continuation")

    blocked = ReorientationState(
        version=2,
        contract_id=first.contract_id,
        revision=2,
        closed_attempts=(),
        phase=HardExternalBlock(
            code=HardExternalBlockCode.AUTH_REQUIRED,
            required_external_action="Configure an existing credential profile.",
            continuation=continuation,
        ),
    )
    if parse_reorientation_state(serialize_reorientation_state(blocked)) != blocked:
        raise RuntimeError("hard external block round-trip failed")

    return {
        "status": "ok",
        "checks": [
            "deterministic_contract",
            "resource_independent_digest",
            "strict_contract_round_trip",
            "resolvable_baseline_provenance",
            "blind_generation_request",
            "unbounded_random_perspective",
            "six_axis_novelty_gate",
            "harness_derived_evidence",
            "checkpoint_correlation",
            "content_addressed_reservation",
            "closed_hard_block_code",
        ],
        "contract_id": first.contract_id,
    }


def main() -> int:
    print(json.dumps(verify(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
