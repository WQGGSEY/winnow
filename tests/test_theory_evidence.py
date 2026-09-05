from __future__ import annotations

from pathlib import Path

import pytest

from research_harness.publishing.theory_evidence import (
    TheoryEvidenceError,
    check_theory_evidence,
    verify_theory_evidence,
)

LEAN = Path.home() / ".cache/research-harness/lean4-v4.30.0/bin/lean"


def _contract(**updates):
    contract = {
        "version": 1,
        "theorem_id": "identity",
        "declaration_name": "IdentityProof",
        "statement": "x = x",
        "assumptions": [{"name": "x", "type": "Nat"}],
        "imports": ["Init"],
        "allowed_axioms": [],
        "lean_version": "4.30.0",
    }
    contract.update(updates)
    return contract


@pytest.fixture
def lean():
    if not LEAN.is_file():
        pytest.skip("pinned optional Lean checker is not installed")
    return LEAN


def test_checks_and_replays_kernel_proof(tmp_path, lean):
    proof = tmp_path / "identity.proof"
    proof.write_text("  rfl\n", encoding="utf-8")

    receipt = check_theory_evidence(
        _contract(), proof, tmp_path / "evidence", lean_path=lean
    )

    assert receipt["used_axioms"] == []
    assert (
        verify_theory_evidence(
            _contract(), proof, tmp_path / "evidence/theory_evidence_receipt.json"
        )
        == receipt
    )


def test_false_theorem_is_rejected_by_lean(tmp_path, lean):
    proof = tmp_path / "false.proof"
    proof.write_text("  rfl\n", encoding="utf-8")
    with pytest.raises(TheoryEvidenceError, match="Lean rejected"):
        check_theory_evidence(
            _contract(statement="x = x + 1"),
            proof,
            tmp_path / "evidence",
            lean_path=lean,
        )


@pytest.mark.parametrize("body", ["  sorry\n", "  admit\n", "  axiom bad : False\n"])
def test_admissions_and_inline_axioms_are_rejected(tmp_path, lean, body):
    proof = tmp_path / "bad.proof"
    proof.write_text(body, encoding="utf-8")
    with pytest.raises(TheoryEvidenceError, match="admitted or declared axiom"):
        check_theory_evidence(_contract(), proof, tmp_path / "evidence", lean_path=lean)


def test_custom_axiom_cannot_be_whitelisted(tmp_path, lean):
    proof = tmp_path / "bad.proof"
    proof.write_text("  rfl\n", encoding="utf-8")
    with pytest.raises(TheoryEvidenceError, match="untrusted axiom"):
        check_theory_evidence(
            _contract(allowed_axioms=["ResearchHarness.customAxiom"]),
            proof,
            tmp_path / "evidence",
            lean_path=lean,
        )


def test_changed_assumptions_invalidate_receipt(tmp_path, lean):
    proof = tmp_path / "identity.proof"
    proof.write_text("  rfl\n", encoding="utf-8")
    destination = tmp_path / "evidence"
    check_theory_evidence(_contract(), proof, destination, lean_path=lean)

    with pytest.raises(TheoryEvidenceError, match="contract_sha256"):
        verify_theory_evidence(
            _contract(assumptions=[{"name": "y", "type": "Nat"}]),
            proof,
            destination / "theory_evidence_receipt.json",
        )


def test_changed_log_invalidates_receipt_before_replay(tmp_path, lean):
    proof = tmp_path / "identity.proof"
    proof.write_text("  rfl\n", encoding="utf-8")
    destination = tmp_path / "evidence"
    check_theory_evidence(_contract(), proof, destination, lean_path=lean)
    (destination / "lean_check.log").write_text("forged\n", encoding="utf-8")

    with pytest.raises(TheoryEvidenceError, match="log_sha256"):
        verify_theory_evidence(
            _contract(), proof, destination / "theory_evidence_receipt.json"
        )
