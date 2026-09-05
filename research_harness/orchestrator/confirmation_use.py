"""Single-use confirmation-holdout policy for adaptive research.

This module owns the durable use ledger. Callers must initialize it from the
frozen goal contract before direction generation and consume it with a digest
of the still-opaque observation before computing or returning the result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping

from research_harness.orchestrator.goal_contract import GoalContract


_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_BINDING_KEYS = (
    "contract_id",
    "attempt_id",
    "direction_id",
    "node_id",
    "manifest_id",
)

WriterLock = Callable[[], ContextManager[None]]


class ConfirmationUseError(ValueError):
    """Raised when confirmation policy initialization or use is invalid."""


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def derive_confirmation_protocol(contract: GoalContract) -> dict[str, Any]:
    """Derive the immutable single-use policy from a frozen goal contract."""
    if not isinstance(contract, GoalContract):
        raise ConfirmationUseError("confirmation protocol requires a GoalContract")
    holdout = contract.holdout_requirement
    identity = {
        "contract_id": contract.contract_id,
        "contract_digest": contract.digest,
        "holdout_source_id": holdout.holdout_source_id,
        "predicate": {
            "metric": holdout.predicate.metric,
            "op": holdout.predicate.operator,
            "threshold": holdout.predicate.threshold,
        },
    }
    digest = _canonical_digest(identity)
    return {
        "version": 1,
        "protocol_id": f"confirmation_{digest.removeprefix('sha256:')}",
        "digest": digest,
        "max_uses": 1,
        **identity,
    }


def digest_confirmation_evidence(
    contract: GoalContract, evidence: Mapping[str, Any]
) -> str:
    """Bind opaque evidence content to the frozen holdout source identity."""
    if not isinstance(contract, GoalContract):
        raise ConfirmationUseError("confirmation evidence requires a GoalContract")
    if not isinstance(evidence, Mapping) or set(evidence) != {"observed"}:
        raise ConfirmationUseError(
            "real-holdout confirmation evidence must contain exactly observed"
        )
    observed = evidence["observed"]
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        raise ConfirmationUseError("confirmation observation must be numeric")
    return _canonical_digest(
        {
            "holdout_source_id": contract.holdout_requirement.holdout_source_id,
            "evidence": {"observed": observed},
        }
    )


def initialize_confirmation_ledger(
    path: Path,
    contract: GoalContract,
    *,
    writer_lock: WriterLock,
) -> dict[str, Any]:
    """Persist the frozen policy before generation; identical retries converge."""
    with writer_lock():
        return initialize_confirmation_ledger_locked(path, contract)


def initialize_confirmation_ledger_locked(
    path: Path, contract: GoalContract
) -> dict[str, Any]:
    """Initialize while the caller already holds the adaptive writer lock."""
    protocol = derive_confirmation_protocol(contract)
    if path.exists():
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("protocol") != protocol:
            raise ConfirmationUseError(
                "confirmation ledger does not match the frozen holdout contract"
            )
        return ledger
    ledger = {"version": 1, "protocol": protocol, "consumption": None}
    _atomic_write(path, ledger)
    return ledger


def consume_confirmation(
    path: Path,
    contract: GoalContract,
    *,
    binding: Mapping[str, str],
    evidence_digest: str,
    writer_lock: WriterLock,
) -> dict[str, Any]:
    """Consume the holdout before disclosure and return an idempotent receipt."""
    protocol = derive_confirmation_protocol(contract)
    normalized_binding = _binding(binding, contract_id=contract.contract_id)
    if _SHA256_RE.fullmatch(evidence_digest) is None:
        raise ConfirmationUseError("confirmation evidence digest is invalid")
    receipt_payload = {
        "version": 1,
        "protocol_id": protocol["protocol_id"],
        "protocol_digest": protocol["digest"],
        "holdout_source_id": protocol["holdout_source_id"],
        "binding": normalized_binding,
        "evidence_digest": evidence_digest,
    }
    receipt_digest = _canonical_digest(receipt_payload)
    receipt = {
        **receipt_payload,
        "receipt_id": f"confirmation_use_{receipt_digest.removeprefix('sha256:')}",
        "receipt_digest": receipt_digest,
    }

    with writer_lock():
        if not path.exists():
            raise ConfirmationUseError(
                "confirmation ledger must be initialized before direction generation"
            )
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("protocol") != protocol:
            raise ConfirmationUseError(
                "confirmation ledger does not match the frozen holdout contract"
            )
        existing = ledger.get("consumption")
        if existing is not None:
            if existing == receipt:
                return existing
            raise ConfirmationUseError(
                "confirmation holdout was already consumed by different evidence or binding"
            )
        ledger["consumption"] = receipt
        _atomic_write(path, ledger)
        return receipt


def verify_terminal_confirmation(
    path: Path,
    contract: GoalContract,
    *,
    binding: Mapping[str, str],
    evidence_digest: str,
) -> None:
    """Reject terminal completion unless it matches the one durable use receipt."""
    if not path.exists():
        raise ConfirmationUseError("terminal confirmation receipt is missing")
    ledger = json.loads(path.read_text(encoding="utf-8"))
    receipt = ledger.get("consumption")
    if not isinstance(receipt, Mapping):
        raise ConfirmationUseError("terminal confirmation receipt is missing")
    protocol = derive_confirmation_protocol(contract)
    normalized_binding = _binding(binding, contract_id=contract.contract_id)
    payload = {
        "version": 1,
        "protocol_id": protocol["protocol_id"],
        "protocol_digest": protocol["digest"],
        "holdout_source_id": protocol["holdout_source_id"],
        "binding": normalized_binding,
        "evidence_digest": evidence_digest,
    }
    expected_digest = _canonical_digest(payload)
    expected = {
        **payload,
        "receipt_id": f"confirmation_use_{expected_digest.removeprefix('sha256:')}",
        "receipt_digest": expected_digest,
    }
    if ledger.get("protocol") != protocol or dict(receipt) != expected:
        raise ConfirmationUseError(
            "terminal result does not match the confirmation use receipt"
        )


def _binding(
    value: Mapping[str, str], *, contract_id: str
) -> dict[str, str]:
    if set(value) != set(_BINDING_KEYS):
        raise ConfirmationUseError("confirmation binding must contain exactly five IDs")
    normalized = {key: value[key] for key in _BINDING_KEYS}
    if any(not isinstance(item, str) or not item.strip() for item in normalized.values()):
        raise ConfirmationUseError("confirmation binding IDs must be non-empty strings")
    if normalized["contract_id"] != contract_id:
        raise ConfirmationUseError("confirmation binding contract is stale")
    return normalized
