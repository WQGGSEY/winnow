from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from research_harness.orchestrator.confirmation_use import (
    ConfirmationUseError,
    consume_confirmation,
    derive_confirmation_protocol,
    digest_confirmation_evidence,
    initialize_confirmation_ledger,
    initialize_confirmation_ledger_locked,
    verify_terminal_confirmation,
)
from research_harness.orchestrator.goal_contract import (
    BaselineEvidence,
    FalsifierPredicate,
    GoalContractCompilerInput,
    HoldoutRequirement,
    compile_goal_contract,
)


@contextmanager
def _writer_lock():
    yield


def _contract(threshold: float = 0.5):
    return compile_goal_contract(
        GoalContractCompilerInput(
            question="Does the method improve utility?",
            mandatory_baselines=("incumbent",),
            success_criteria=("utility >= 0.5",),
            disproof_conditions=("utility < 0.5",),
            operator_requirements=("public data",),
            target_scope="feasibility",
            acceptable_scopes=("feasibility",),
            baseline_evidence=(
                BaselineEvidence("candidate", "incumbent", "current_best_known", ("source",)),
                BaselineEvidence("naive", "mean", "naive", ("source",)),
                BaselineEvidence("null", "shuffle", "random_or_null", ("source",)),
            ),
            safety_limits=("no private data",),
            holdout_requirement=HoldoutRequirement(
                kind="real_holdout",
                holdout_source_id="sealed_holdout_v1",
                predicate=FalsifierPredicate("utility", ">=", threshold),
                registered_by="operator",
            ),
        )
    )


def _binding(contract_id: str, *, attempt_id: str = "attempt_1") -> dict[str, str]:
    return {
        "contract_id": contract_id,
        "attempt_id": attempt_id,
        "direction_id": "direction_1",
        "node_id": "node_1",
        "manifest_id": "manifest_1",
    }


def _digest(contract, observation: float) -> str:
    return digest_confirmation_evidence(contract, {"observed": observation})


def test_protocol_is_derived_from_frozen_holdout_and_initialized_before_use(tmp_path: Path):
    contract = _contract()
    path = tmp_path / "confirmation_use.json"
    ledger = initialize_confirmation_ledger(path, contract, writer_lock=_writer_lock)

    assert ledger["consumption"] is None
    assert ledger["protocol"] == derive_confirmation_protocol(contract)
    assert ledger["protocol"]["contract_digest"] == contract.digest
    assert ledger["protocol"]["holdout_source_id"] == "sealed_holdout_v1"
    assert ledger["protocol"]["predicate"] == {
        "metric": "utility", "op": ">=", "threshold": 0.5
    }

    assert initialize_confirmation_ledger_locked(path, contract) == ledger


def test_identical_binding_and_evidence_retry_is_idempotent(tmp_path: Path):
    contract = _contract()
    path = tmp_path / "confirmation_use.json"
    initialize_confirmation_ledger(path, contract, writer_lock=_writer_lock)
    binding = _binding(contract.contract_id)

    first = consume_confirmation(
        path, contract, binding=binding, evidence_digest=_digest(contract, 0.4), writer_lock=_writer_lock
    )
    repeated = consume_confirmation(
        path, contract, binding=binding, evidence_digest=_digest(contract, 0.4), writer_lock=_writer_lock
    )

    assert repeated == first
    assert json.loads(path.read_text(encoding="utf-8"))["consumption"] == first


@pytest.mark.parametrize(
    ("binding", "observation"),
    [("different_attempt", 0.4), ("same_attempt", 0.6)],
)
def test_reuse_with_different_attempt_or_observation_is_rejected(
    tmp_path: Path, binding: str, observation: float
):
    contract = _contract()
    path = tmp_path / "confirmation_use.json"
    initialize_confirmation_ledger(path, contract, writer_lock=_writer_lock)
    consume_confirmation(
        path,
        contract,
        binding=_binding(contract.contract_id),
        evidence_digest=_digest(contract, 0.4),
        writer_lock=_writer_lock,
    )
    next_binding = _binding(
        contract.contract_id,
        attempt_id="attempt_2" if binding == "different_attempt" else "attempt_1",
    )

    with pytest.raises(ConfirmationUseError, match="already consumed"):
        consume_confirmation(
            path,
            contract,
            binding=next_binding,
            evidence_digest=_digest(contract, observation),
            writer_lock=_writer_lock,
        )


def test_changed_frozen_holdout_cannot_reuse_ledger(tmp_path: Path):
    path = tmp_path / "confirmation_use.json"
    initialize_confirmation_ledger(path, _contract(), writer_lock=_writer_lock)

    with pytest.raises(ConfirmationUseError, match="frozen holdout contract"):
        initialize_confirmation_ledger(path, _contract(0.7), writer_lock=_writer_lock)


def test_terminal_check_requires_exact_consumption_receipt(tmp_path: Path):
    contract = _contract()
    binding = _binding(contract.contract_id)
    evidence_digest = _digest(contract, 0.8)
    path = tmp_path / "confirmation_use.json"
    initialize_confirmation_ledger(path, contract, writer_lock=_writer_lock)
    receipt = consume_confirmation(
        path,
        contract,
        binding=binding,
        evidence_digest=evidence_digest,
        writer_lock=_writer_lock,
    )

    verify_terminal_confirmation(
        path, contract, binding=binding, evidence_digest=evidence_digest
    )
    with pytest.raises(ConfirmationUseError, match="does not match"):
        verify_terminal_confirmation(
            path, contract, binding=binding, evidence_digest=_digest(contract, 0.9)
        )
