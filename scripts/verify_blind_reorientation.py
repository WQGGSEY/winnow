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
from research_harness.orchestrator.solution_contract import (
    compile_solution_contract,
    parse_solution_contract,
    serialize_solution_contract,
    solution_contract_input_from_artifacts,
)


BASELINE_DOSSIER_ID = "bd_agent_harness_20260523"
BASELINE_PROVENANCE_PREFIX = f"baseline_dossier:{BASELINE_DOSSIER_ID}#"


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
    source = solution_contract_input_from_artifacts(
        repo_root=REPO_ROOT,
        baseline_dossier_id=BASELINE_DOSSIER_ID,
        operator_problem="Find a safe intervention that improves utility.",
        grilling_record=grilling,
        feasibility_envelope=envelope,
    )
    return compile_solution_contract(source)


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
        raise RuntimeError("resource changes altered the solution contract")
    if parse_solution_contract(serialize_solution_contract(first)) != first:
        raise RuntimeError("solution contract round-trip failed")
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

    attempt = DirectionAttemptRef(
        attempt_id="attempt_0",
        direction_id="direction_" + "a" * 64,
        fingerprint_id="fingerprint_" + "b" * 64,
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
