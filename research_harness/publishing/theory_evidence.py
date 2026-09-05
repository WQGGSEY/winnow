from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from research_harness.schemas.validator import validate_named_schema


class TheoryEvidenceError(ValueError):
    """Raised when formal-theory evidence cannot be trusted."""


_FORBIDDEN = re.compile(r"\b(?:sorry|admit|axiom|constant)\b", re.IGNORECASE)
_AXIOMS = re.compile(r"depends on axioms:\s*\[(.*?)\]")
_NO_AXIOMS = re.compile(r"does not depend on any axioms")
_TRUSTED_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_file(path: Path) -> str:
    return _digest_bytes(path.read_bytes())


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _lean_binary(explicit: str | Path | None) -> Path:
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        Path(os.environ["RESEARCH_HARNESS_LEAN"]).expanduser()
        if os.environ.get("RESEARCH_HARNESS_LEAN")
        else None,
        Path.home() / ".cache/research-harness/lean4-v4.30.0/bin/lean",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise TheoryEvidenceError(
        "Lean is unavailable; install a pinned Lean 4 release or set RESEARCH_HARNESS_LEAN"
    )


def _validate_contract(contract: dict[str, Any]) -> None:
    validate_named_schema("theory_evidence_contract", contract)
    if not set(contract["allowed_axioms"]).issubset(_TRUSTED_AXIOMS):
        raise TheoryEvidenceError("allowed_axioms contains an untrusted axiom")
    fragments = [contract["statement"]]
    fragments.extend(item["type"] for item in contract["assumptions"])
    for fragment in fragments:
        if "\n" in fragment or "\r" in fragment or ";" in fragment or ":=" in fragment:
            raise TheoryEvidenceError(
                "theorem interface contains Lean command injection"
            )
        if re.search(
            r"(?:^|\s)(?:axiom|constant|theorem|def|opaque|unsafe|sorry|admit|#)",
            fragment,
        ):
            raise TheoryEvidenceError(
                "theorem interface contains a forbidden declaration"
            )


def _source(contract: dict[str, Any], proof_body: str) -> str:
    imports = "\n".join(f"import {name}" for name in contract["imports"])
    assumptions = " ".join(
        f"({item['name']} : {item['type']})" for item in contract["assumptions"]
    )
    return (
        f"{imports}\n\n"
        f"theorem {contract['declaration_name']} {assumptions} : {contract['statement']} := by\n"
        f"{proof_body.rstrip()}\n\n"
        f"#print axioms {contract['declaration_name']}\n"
    )


def _run(lean: Path, source: Path) -> tuple[str, list[str]]:
    result = subprocess.run(
        [str(lean), str(source)],
        cwd=source.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    log = result.stdout + result.stderr
    if result.returncode != 0:
        raise TheoryEvidenceError(f"Lean rejected the proof:\n{log}")
    match = _AXIOMS.search(log)
    if not match and _NO_AXIOMS.search(log):
        return log, []
    if not match:
        raise TheoryEvidenceError("Lean did not emit an axiom audit")
    axioms = [value.strip() for value in match.group(1).split(",") if value.strip()]
    return log, axioms


def _version(lean: Path) -> str:
    result = subprocess.run(
        [str(lean), "--version"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def check_theory_evidence(
    contract: dict[str, Any],
    proof_path: str | Path,
    output_dir: str | Path,
    *,
    lean_path: str | Path | None = None,
) -> dict[str, Any]:
    """Check a separate proof body and persist replayable kernel evidence."""
    _validate_contract(contract)
    proof = Path(proof_path).resolve()
    body = proof.read_text(encoding="utf-8")
    if _FORBIDDEN.search(body):
        raise TheoryEvidenceError("proof body uses an admitted or declared axiom")
    lean = _lean_binary(lean_path)
    version = _version(lean)
    if contract["lean_version"] not in version:
        raise TheoryEvidenceError("Lean version does not match the frozen contract")
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    source = destination / "checked_theorem.lean"
    log_path = destination / "lean_check.log"
    receipt_path = destination / "theory_evidence_receipt.json"
    source.write_text(_source(contract, body), encoding="utf-8")
    log, axioms = _run(lean, source)
    if not set(axioms).issubset(set(contract["allowed_axioms"])):
        raise TheoryEvidenceError(f"proof depends on undeclared axioms: {axioms}")
    log_path.write_text(log, encoding="utf-8")
    receipt = {
        "version": 1,
        "theorem_id": contract["theorem_id"],
        "declaration_name": contract["declaration_name"],
        "contract_sha256": _digest_bytes(_canonical(contract)),
        "proof_path": str(proof),
        "proof_sha256": _digest_file(proof),
        "source_path": str(source),
        "source_sha256": _digest_file(source),
        "log_path": str(log_path),
        "log_sha256": _digest_file(log_path),
        "lean_path": str(lean),
        "lean_sha256": _digest_file(lean),
        "lean_version": version,
        "used_axioms": axioms,
    }
    validate_named_schema("theory_evidence_receipt", receipt)
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def verify_theory_evidence(
    contract: dict[str, Any], proof_path: str | Path, receipt_path: str | Path
) -> dict[str, Any]:
    """Re-open every bound artifact and replay the exact theorem with the pinned tool."""
    _validate_contract(contract)
    receipt_file = Path(receipt_path).resolve()
    if receipt_file.name != "theory_evidence_receipt.json":
        raise TheoryEvidenceError("theory receipt has a non-canonical name")
    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    validate_named_schema("theory_evidence_receipt", receipt)
    proof = Path(proof_path).resolve()
    source = Path(receipt["source_path"])
    log_path = Path(receipt["log_path"])
    lean = Path(receipt["lean_path"])
    if source != receipt_file.parent / "checked_theorem.lean":
        raise TheoryEvidenceError("checked source is outside the evidence directory")
    if log_path != receipt_file.parent / "lean_check.log":
        raise TheoryEvidenceError("Lean log is outside the evidence directory")
    expected = {
        "contract_sha256": _digest_bytes(_canonical(contract)),
        "proof_sha256": _digest_file(proof),
        "source_sha256": _digest_file(source),
        "log_sha256": _digest_file(log_path),
        "lean_sha256": _digest_file(lean),
    }
    for key, value in expected.items():
        if receipt[key] != value:
            raise TheoryEvidenceError(f"stale or changed theory evidence: {key}")
    if receipt["proof_path"] != str(proof):
        raise TheoryEvidenceError("receipt is bound to a different proof path")
    body = proof.read_text(encoding="utf-8")
    if _FORBIDDEN.search(body) or source.read_text(encoding="utf-8") != _source(
        contract, body
    ):
        raise TheoryEvidenceError("checked source no longer matches contract and proof")
    if _version(lean) != receipt["lean_version"]:
        raise TheoryEvidenceError("Lean tool version changed")
    replay_log, axioms = _run(lean, source)
    if _digest_bytes(replay_log.encode()) != receipt["log_sha256"]:
        raise TheoryEvidenceError("Lean replay log changed")
    if axioms != receipt["used_axioms"] or not set(axioms).issubset(
        set(contract["allowed_axioms"])
    ):
        raise TheoryEvidenceError("Lean replay axiom audit changed")
    return receipt
