from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import shutil

import pytest

from research_harness.memory.baseline_dossier import BaselineDossierError
from research_harness.orchestrator.blind_reorientation import (
    AcquisitionReserved,
    AwaitingEvidence,
    ClosedDirectionAttempt,
    DirectionAttemptRef,
    GenerationReservation,
    GenerationReserved,
    GoalAchieved,
    HardExternalBlock,
    HardExternalBlockCode,
    ReorientationState,
    ReorientationStateError,
    Seeking,
    initialize_reorientation_state,
    make_acquisition_reservation,
    make_checkpoint,
    make_generation_reservation,
    parse_reorientation_state,
    serialize_reorientation_state,
)
from research_harness.orchestrator.direction_generation import (
    make_direction_fingerprint,
    serialize_direction_fingerprint,
)
from research_harness.schemas.validator import (
    SchemaValidationError,
    validate_named_schema,
    validate_schema,
)
from research_harness.orchestrator.solution_contract import (
    FalsifierPredicate,
    SolutionContractError,
    compile_solution_contract,
    parse_solution_contract,
    project_research_goal,
    serialize_solution_contract,
    solution_contract_input_from_artifacts,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DOSSIER_ID = "bd_agent_harness_20260523"
BASELINE_PROVENANCE_PREFIX = f"baseline_dossier:{BASELINE_DOSSIER_ID}#"


def _artifacts() -> tuple[dict, dict]:
    grilling = {
        "rounds": [{"user_response": "Keep the false-positive rate below 1%."}],
        "extracted": {
            "claim_under_test": "A deployable intervention beats the incumbent.",
            "mandatory_baselines": ["incumbent", "no-action baseline"],
            "success_criteria": ["utility improves by at least 5%"],
            "disproof_conditions": ["utility improvement is below 5%"],
            "taste_constraints": ["no access-control bypass"],
            "alternative_claim_formulations": [
                {"claim_under_test": "A candidate direction that must not leak."}
            ],
        },
    }
    envelope = {
        "operator_intent": {
            "target_deploy_grade_scope": "deployment",
            "acceptable_alternative_scopes": ["deployment", "feasibility"],
            "data_source_anchor": "current_adapter",
            "data_source_snapshot_id": "as_" + "1" * 64,
        },
        "data_sources_available": [
            {
                "kind": "real_adapter",
                "id": "current_adapter",
                "snapshot_id": "as_" + "1" * 64,
            }
        ],
        "runtime_capabilities": ["data:current_adapter"],
        "external_falsifier": {
            "kind": "real_holdout",
            "holdout_source_id": "operator_holdout",
            "predicate": {"metric": "utility", "op": ">=", "threshold": 0.05},
            "registered_by": "operator",
        },
    }
    return grilling, envelope


def _compiler_input(
    grilling: dict,
    envelope: dict,
    *,
    repo_root: Path = REPO_ROOT,
):
    return solution_contract_input_from_artifacts(
        repo_root=repo_root,
        baseline_dossier_id=BASELINE_DOSSIER_ID,
        operator_problem="Find a safe intervention that improves utility.",
        grilling_record=grilling,
        feasibility_envelope=envelope,
    )


def _contract():
    grilling, envelope = _artifacts()
    return compile_solution_contract(_compiler_input(grilling, envelope))


def test_contract_is_deterministic_and_resource_snapshots_do_not_author_it() -> None:
    grilling, envelope = _artifacts()
    first = compile_solution_contract(_compiler_input(grilling, envelope))
    changed = deepcopy(envelope)
    changed["operator_intent"]["data_source_anchor"] = "replacement_adapter"
    changed["operator_intent"]["data_source_snapshot_id"] = "as_" + "2" * 64
    changed["data_sources_available"] = [
        {
            "kind": "real_adapter",
            "id": "replacement_adapter",
            "snapshot_id": "as_" + "2" * 64,
        }
    ]
    changed["runtime_capabilities"] = ["data:replacement_adapter"]
    changed_grilling = deepcopy(grilling)
    changed_grilling["extracted"]["alternative_claim_formulations"] = [
        {"claim_under_test": "An unrelated candidate direction."}
    ]
    changed_grilling["failure"] = "A prior direction failed."
    changed_grilling["lesson"] = "Do not anchor the next direction here."
    second = compile_solution_contract(_compiler_input(changed_grilling, changed))

    assert second == first
    assert parse_solution_contract(serialize_solution_contract(first)) == first
    assert {
        provenance
        for baseline in first.baseline_evidence
        for provenance in baseline.provenance
    } >= {
        BASELINE_PROVENANCE_PREFIX
        + "candidates/c1_sakana_ai_scientist_v2.md",
        BASELINE_PROVENANCE_PREFIX + "candidates/c2_direct_api_port.md",
        BASELINE_PROVENANCE_PREFIX + "candidates/c3_no_orchestrated_harness.md",
        "https://github.com/SakanaAI/AI-Scientist-v2",
    }


def test_contract_canonicalization_is_closed_under_round_trip() -> None:
    grilling, envelope = _artifacts()
    clean_source = _compiler_input(grilling, envelope)
    expected = compile_solution_contract(clean_source)
    dirty_grilling = deepcopy(grilling)
    dirty_grilling["rounds"] = [
        {"user_response": "  Keep the false-positive rate below 1%.  "},
        {"user_response": "Keep the false-positive rate below 1%."},
    ]
    dirty_grilling["extracted"]["claim_under_test"] = (
        "  A deployable intervention   beats the incumbent. "
    )
    for key in ("mandatory_baselines", "success_criteria", "disproof_conditions"):
        values = dirty_grilling["extracted"][key]
        dirty_grilling["extracted"][key] = [f"  {values[0]}  ", *values]
    dirty_grilling["extracted"]["taste_constraints"] = [
        " no access-control bypass ",
        "no access-control bypass",
    ]
    dirty_envelope = deepcopy(envelope)
    dirty_envelope["operator_intent"]["acceptable_alternative_scopes"] = [
        "deployment",
        "feasibility",
        "deployment",
    ]
    dirty_envelope["external_falsifier"]["holdout_source_id"] = (
        "  operator_holdout  "
    )
    dirty_envelope["external_falsifier"]["predicate"]["metric"] = " utility "
    dirty_source = _compiler_input(dirty_grilling, dirty_envelope)
    baseline = dirty_source.baseline_evidence[0]
    dirty_baseline = replace(
        baseline,
        candidate_id=f"  {baseline.candidate_id}  ",
        method=f"  {baseline.method.replace(' ', '   ')}  ",
        provenance=(
            f"  {baseline.provenance[0]}  ",
            *baseline.provenance,
            baseline.provenance[0],
        ),
    )
    dirty_holdout = replace(
        dirty_source.holdout_requirement,
        holdout_source_id="  operator_holdout  ",
        predicate=FalsifierPredicate(
            metric="  utility  ",
            operator=dirty_source.holdout_requirement.predicate.operator,
            threshold=dirty_source.holdout_requirement.predicate.threshold,
        ),
    )
    dirty_source = replace(
        dirty_source,
        baseline_evidence=(
            dirty_baseline,
            *dirty_source.baseline_evidence[1:],
            dirty_baseline,
        ),
        holdout_requirement=dirty_holdout,
    )
    actual = compile_solution_contract(
        replace(
            dirty_source,
            question="  Find a safe intervention   that improves utility. ",
        )
    )

    assert actual == expected
    serialized = serialize_solution_contract(actual)
    assert (
        serialize_solution_contract(parse_solution_contract(serialized))
        == serialized
    )


def test_contract_rejects_no_falsifier_and_leaking_serialized_fields() -> None:
    grilling, envelope = _artifacts()
    envelope["external_falsifier"] = {"kind": "none"}
    with pytest.raises(SolutionContractError, match="real_holdout"):
        _compiler_input(grilling, envelope)

    envelope["external_falsifier"] = {
        "kind": "cross_generator_transfer",
        "holdout_source_id": "generator_b",
        "predicate": {"metric": "rho", "op": ">=", "threshold": 0.8},
        "registered_by": "operator",
    }
    with pytest.raises(SolutionContractError, match="real_holdout"):
        _compiler_input(grilling, envelope)

    envelope["external_falsifier"] = {
        "kind": "real_holdout",
        "holdout_source_id": "operator_holdout",
        "predicate": {"metric": "utility", "op": ">=", "threshold": 0.05},
        "registered_by": "adversary_pass",
    }
    with pytest.raises(SolutionContractError, match="registrant"):
        _compiler_input(grilling, envelope)

    legacy_screen = serialize_solution_contract(_contract())
    legacy_screen["holdout_requirement"]["kind"] = "cross_generator_transfer"
    with pytest.raises(ValueError, match="enum|real_holdout"):
        parse_solution_contract(legacy_screen)

    leaked = serialize_solution_contract(_contract())
    leaked["failure"] = "prior direction failed"
    leaked["data_source_snapshot_id"] = "as_" + "3" * 64
    with pytest.raises(ValueError, match="unexpected"):
        parse_solution_contract(leaked)


@pytest.mark.parametrize(
    ("corruption", "message"),
    (("path_traversal", "relative"), ("missing_artifact", "missing")),
)
def test_contract_adapter_rejects_unsafe_candidate_artifacts(
    tmp_path: Path,
    corruption: str,
    message: str,
) -> None:
    dossier_dir = tmp_path / "memory" / "baseline_dossiers"
    shutil.copytree(
        REPO_ROOT / "research_harness" / "memory" / "baseline_dossiers",
        dossier_dir,
    )
    detail_file = dossier_dir / "candidates" / "c1_sakana_ai_scientist_v2.md"
    if corruption == "path_traversal":
        dossier_path = dossier_dir / f"{BASELINE_DOSSIER_ID}.yaml"
        dossier_path.write_text(
            dossier_path.read_text(encoding="utf-8").replace(
                "detail_file: candidates/c1_sakana_ai_scientist_v2.md",
                "detail_file: ../c1_sakana_ai_scientist_v2.md",
                1,
            ),
            encoding="utf-8",
        )
    else:
        detail_file.unlink()

    grilling, envelope = _artifacts()
    with pytest.raises(BaselineDossierError, match=message):
        _compiler_input(grilling, envelope, repo_root=tmp_path)


def test_research_goal_is_a_contract_digest_projection() -> None:
    contract = _contract()
    goal = project_research_goal(contract)

    assert goal["bar_digest"] == contract.digest
    assert goal["bar"]["external_falsifier"]["kind"] == "real_holdout"
    assert "data_source_snapshot_id" not in goal["bar"]
    assert "data_sources_available" not in goal["bar"]


def _attempt(ordinal: int) -> DirectionAttemptRef:
    digit = str(ordinal + 1)
    return DirectionAttemptRef(
        attempt_id=f"attempt_{ordinal}",
        direction_id="direction_" + digit * 64,
        fingerprint=_fingerprint(ordinal),
        ordinal=ordinal,
    )


def _fingerprint(ordinal: int):
    suffix = str(ordinal + 1)
    return make_direction_fingerprint(
        mechanism=f"mechanism {suffix}",
        intervention=f"intervention {suffix}",
        observables_and_data=f"observables {suffix}",
        analysis_unit=f"analysis unit {suffix}",
        timescale=f"timescale {suffix}",
        system_boundary=f"system boundary {suffix}",
    )


def _state(phase) -> ReorientationState:
    closed = ClosedDirectionAttempt(
        attempt=_attempt(0),
        failure_receipt_sha256="sha256:" + "a" * 64,
        private_lesson_sha256="sha256:" + "b" * 64,
    )
    return ReorientationState(
        version=2,
        contract_id=_contract().contract_id,
        revision=4,
        closed_attempts=(closed,),
        phase=phase,
    )


def _generation_reservation(expected_revision: int = 3):
    return make_generation_reservation(
        expected_revision=expected_revision,
        request_digest="sha256:" + "d" * 64,
    )


def _acquisition_reservation(expected_revision: int = 3):
    return make_acquisition_reservation(
        expected_revision=expected_revision,
        request_digest="sha256:" + "f" * 64,
    )


def test_state_round_trips_every_allowed_phase() -> None:
    active = _attempt(1)
    acquisition = AcquisitionReserved(
        active_attempt=active,
        reservation=_acquisition_reservation(),
    )
    suspended_acquisition = AcquisitionReserved(
        active_attempt=active,
        reservation=_acquisition_reservation(expected_revision=0),
    )
    phases = [
        Seeking(),
        GenerationReserved(reservation=_generation_reservation()),
        acquisition,
        AwaitingEvidence(active_attempt=active),
        make_checkpoint(suspended_acquisition, reason="download_budget"),
        GoalAchieved(strong_result_receipt_sha256="sha256:" + "9" * 64),
        HardExternalBlock(
            code=HardExternalBlockCode.AUTH_REQUIRED,
            required_external_action="Configure an existing credential profile.",
            continuation=suspended_acquisition,
        ),
    ]

    for phase in phases:
        state = _state(phase)
        assert parse_reorientation_state(serialize_reorientation_state(state)) == state

    initialized = initialize_reorientation_state(_contract())
    assert initialized.phase == Seeking()
    assert initialized.closed_attempts == ()


def test_checkpoint_rejects_a_changed_or_second_active_attempt() -> None:
    active = _attempt(1)
    checkpointed = make_checkpoint(
        AcquisitionReserved(
            active_attempt=active,
            reservation=_acquisition_reservation(),
        ),
        reason="download_budget",
    )
    changed = serialize_reorientation_state(_state(checkpointed))
    changed["phase"]["continuation"]["active_attempt"] = {
        **changed["phase"]["continuation"]["active_attempt"],
        "attempt_id": "attempt_replacement",
    }
    with pytest.raises(ValueError, match="continuation digest"):
        parse_reorientation_state(changed)

    second = serialize_reorientation_state(_state(checkpointed))
    second["phase"]["active_attempt"] = {
        "attempt_id": "attempt_2",
        "direction_id": "direction_" + "3" * 64,
        "fingerprint": serialize_direction_fingerprint(_fingerprint(2)),
        "ordinal": 2,
    }
    with pytest.raises(ValueError, match="oneOf"):
        parse_reorientation_state(second)


def test_hard_external_block_code_is_closed() -> None:
    state = serialize_reorientation_state(
        _state(
            HardExternalBlock(
                code=HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                required_external_action="Provide a lawful public substitute.",
                continuation=Seeking(),
            )
        )
    )
    state["phase"]["code"] = "robots_denied"

    with pytest.raises(ValueError, match="oneOf|code"):
        parse_reorientation_state(state)


def test_reservation_identity_and_live_revision_are_enforced() -> None:
    assert _generation_reservation() == _generation_reservation()
    assert (
        _generation_reservation().reservation_id
        != _acquisition_reservation().reservation_id
    )
    with pytest.raises(ReorientationStateError, match="identity"):
        GenerationReservation(
            reservation_id="reservation_" + "0" * 64,
            expected_revision=3,
            request_digest="sha256:" + "d" * 64,
        )

    stale_generation = GenerationReserved(
        reservation=_generation_reservation(expected_revision=1)
    )
    stale_acquisition = AcquisitionReserved(
        active_attempt=_attempt(1),
        reservation=_acquisition_reservation(expected_revision=1),
    )
    for stale in (stale_generation, stale_acquisition):
        with pytest.raises(ReorientationStateError, match="stale"):
            _state(stale)

    suspended = make_checkpoint(stale_generation, reason="generation_retry")
    assert parse_reorientation_state(
        serialize_reorientation_state(_state(suspended))
    ).phase == suspended


def test_schema_validator_enforces_const_and_tagged_one_of() -> None:
    validate_schema({"const": True}, True)
    with pytest.raises(SchemaValidationError, match="constant"):
        validate_schema({"const": True}, 1)
    with pytest.raises(SchemaValidationError, match="matched 2"):
        validate_schema({"oneOf": [{"type": "object"}, {"type": "object"}]}, {})

    document = serialize_reorientation_state(_state(Seeking()))
    document["phase"]["active_attempt"] = {
        "attempt_id": "attempt_1",
        "direction_id": "direction_" + "2" * 64,
        "fingerprint": serialize_direction_fingerprint(_fingerprint(1)),
        "ordinal": 1,
    }
    with pytest.raises(SchemaValidationError, match="oneOf"):
        validate_named_schema("reorientation_state", document)

    missing = serialize_reorientation_state(_state(Seeking()))
    missing["phase"] = {"kind": "acquisition_reserved"}
    with pytest.raises(SchemaValidationError, match="oneOf"):
        validate_named_schema("reorientation_state", missing)

    checkpointed = serialize_reorientation_state(
        _state(make_checkpoint(Seeking(), reason="time_budget"))
    )
    checkpointed["phase"]["continuation"]["active_attempt"] = {
        "attempt_id": "attempt_1",
        "direction_id": "direction_" + "2" * 64,
        "fingerprint": serialize_direction_fingerprint(_fingerprint(1)),
        "ordinal": 1,
    }
    with pytest.raises(SchemaValidationError, match="oneOf"):
        validate_named_schema("reorientation_state", checkpointed)
