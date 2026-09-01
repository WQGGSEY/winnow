from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping, TypeAlias

from research_harness.orchestrator.solution_contract import SolutionContract
from research_harness.schemas.validator import validate_named_schema


CheckpointReason = Literal[
    "time_budget",
    "cost_budget",
    "download_budget",
    "rate_limited",
    "transient_network",
    "generation_retry",
    "inconclusive_evidence",
]

_CHECKPOINT_REASONS = frozenset(
    {
        "time_budget",
        "cost_budget",
        "download_budget",
        "rate_limited",
        "transient_network",
        "generation_retry",
        "inconclusive_evidence",
    }
)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_CONTRACT_ID_RE = re.compile(r"^contract_[a-f0-9]{64}$")
_ATTEMPT_ID_RE = re.compile(r"^attempt_[A-Za-z0-9_-]+$")
_DIRECTION_ID_RE = re.compile(r"^direction_[a-f0-9]{64}$")
_FINGERPRINT_ID_RE = re.compile(r"^fingerprint_[a-f0-9]{64}$")
_RESERVATION_ID_RE = re.compile(r"^reservation_[a-f0-9]{64}$")
_CHECKPOINT_ID_RE = re.compile(r"^checkpoint_[a-f0-9]{64}$")


class ReorientationStateError(ValueError):
    pass


class HardExternalBlockCode(str, Enum):
    AUTH_REQUIRED = "auth_required"
    LAWFUL_ACCESS_UNAVAILABLE = "lawful_access_unavailable"
    LEGAL_ACCESS_DENIED = "legal_access_denied"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    OPERATOR_SCOPE_CONFLICT = "operator_scope_conflict"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReorientationStateError(f"{label} must be a non-empty string")
    return value.strip()


def _matching_text(value: object, pattern: re.Pattern[str], label: str) -> str:
    text = _text(value, label)
    if pattern.fullmatch(text) is None:
        raise ReorientationStateError(f"{label} is invalid")
    return text


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReorientationStateError(f"{label} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    label: str,
) -> None:
    missing = sorted(required - value.keys())
    unexpected = sorted(value.keys() - required)
    if missing:
        raise ReorientationStateError(f"{label} is missing keys {missing}")
    if unexpected:
        raise ReorientationStateError(f"{label} has unexpected keys {unexpected}")


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReorientationStateError(f"{label} must be a non-negative integer")
    return value


def _reservation_id(
    *,
    kind: Literal["generation", "acquisition"],
    expected_revision: int,
    request_digest: str,
) -> str:
    expected_revision = _nonnegative_integer(expected_revision, "expected revision")
    request_digest = _matching_text(request_digest, _SHA256_RE, "request digest")
    digest = _sha256(
        {
            "kind": kind,
            "expected_revision": expected_revision,
            "request_digest": request_digest,
        }
    )
    return f"reservation_{digest}"


@dataclass(frozen=True, slots=True)
class DirectionAttemptRef:
    attempt_id: str
    direction_id: str
    fingerprint_id: str
    ordinal: int

    def __post_init__(self) -> None:
        _matching_text(self.attempt_id, _ATTEMPT_ID_RE, "attempt id")
        _matching_text(self.direction_id, _DIRECTION_ID_RE, "direction id")
        _matching_text(self.fingerprint_id, _FINGERPRINT_ID_RE, "fingerprint id")
        _nonnegative_integer(self.ordinal, "attempt ordinal")


@dataclass(frozen=True, slots=True)
class ClosedDirectionAttempt:
    attempt: DirectionAttemptRef
    failure_receipt_sha256: str
    private_lesson_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, DirectionAttemptRef):
            raise ReorientationStateError("closed attempt reference is invalid")
        _matching_text(
            self.failure_receipt_sha256,
            _SHA256_RE,
            "failure receipt digest",
        )
        _matching_text(
            self.private_lesson_sha256,
            _SHA256_RE,
            "private lesson digest",
        )


@dataclass(frozen=True, slots=True)
class GenerationReservation:
    reservation_id: str
    expected_revision: int
    request_digest: str

    def __post_init__(self) -> None:
        _matching_text(self.reservation_id, _RESERVATION_ID_RE, "reservation id")
        _nonnegative_integer(self.expected_revision, "expected revision")
        _matching_text(self.request_digest, _SHA256_RE, "request digest")
        expected_id = _reservation_id(
            kind="generation",
            expected_revision=self.expected_revision,
            request_digest=self.request_digest,
        )
        if self.reservation_id != expected_id:
            raise ReorientationStateError(
                "generation reservation identity does not match its content"
            )


@dataclass(frozen=True, slots=True)
class AcquisitionReservation:
    reservation_id: str
    expected_revision: int
    request_digest: str

    def __post_init__(self) -> None:
        _matching_text(self.reservation_id, _RESERVATION_ID_RE, "reservation id")
        _nonnegative_integer(self.expected_revision, "expected revision")
        _matching_text(self.request_digest, _SHA256_RE, "request digest")
        expected_id = _reservation_id(
            kind="acquisition",
            expected_revision=self.expected_revision,
            request_digest=self.request_digest,
        )
        if self.reservation_id != expected_id:
            raise ReorientationStateError(
                "acquisition reservation identity does not match its content"
            )


def make_generation_reservation(
    *,
    expected_revision: int,
    request_digest: str,
) -> GenerationReservation:
    return GenerationReservation(
        reservation_id=_reservation_id(
            kind="generation",
            expected_revision=expected_revision,
            request_digest=request_digest,
        ),
        expected_revision=expected_revision,
        request_digest=request_digest,
    )


def make_acquisition_reservation(
    *,
    expected_revision: int,
    request_digest: str,
) -> AcquisitionReservation:
    return AcquisitionReservation(
        reservation_id=_reservation_id(
            kind="acquisition",
            expected_revision=expected_revision,
            request_digest=request_digest,
        ),
        expected_revision=expected_revision,
        request_digest=request_digest,
    )


@dataclass(frozen=True, slots=True)
class Seeking:
    kind: Literal["seeking"] = field(default="seeking", init=False)


@dataclass(frozen=True, slots=True)
class GenerationReserved:
    reservation: GenerationReservation
    kind: Literal["generation_reserved"] = field(
        default="generation_reserved", init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.reservation, GenerationReservation):
            raise ReorientationStateError("generation reservation is invalid")


@dataclass(frozen=True, slots=True)
class AcquisitionReserved:
    active_attempt: DirectionAttemptRef
    reservation: AcquisitionReservation
    kind: Literal["acquisition_reserved"] = field(
        default="acquisition_reserved", init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.active_attempt, DirectionAttemptRef):
            raise ReorientationStateError("active attempt is invalid")
        if not isinstance(self.reservation, AcquisitionReservation):
            raise ReorientationStateError("acquisition reservation is invalid")


@dataclass(frozen=True, slots=True)
class AwaitingEvidence:
    active_attempt: DirectionAttemptRef
    kind: Literal["awaiting_evidence"] = field(
        default="awaiting_evidence", init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.active_attempt, DirectionAttemptRef):
            raise ReorientationStateError("active attempt is invalid")


Continuation: TypeAlias = (
    Seeking | GenerationReserved | AcquisitionReserved | AwaitingEvidence
)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    reason: CheckpointReason
    continuation_digest: str

    def __post_init__(self) -> None:
        _matching_text(self.checkpoint_id, _CHECKPOINT_ID_RE, "checkpoint id")
        if self.reason not in _CHECKPOINT_REASONS:
            raise ReorientationStateError(f"unsupported checkpoint reason {self.reason!r}")
        _matching_text(
            self.continuation_digest,
            _SHA256_RE,
            "checkpoint continuation digest",
        )


@dataclass(frozen=True, slots=True)
class Checkpointed:
    checkpoint: Checkpoint
    continuation: Continuation
    kind: Literal["checkpointed"] = field(default="checkpointed", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoint, Checkpoint):
            raise ReorientationStateError("checkpoint is invalid")
        _require_continuation(self.continuation)
        expected = _continuation_digest(self.continuation)
        if self.checkpoint.continuation_digest != expected:
            raise ReorientationStateError(
                "checkpoint continuation digest does not match its payload"
            )
        checkpoint_digest = _sha256(
            {
                "reason": self.checkpoint.reason,
                "continuation_digest": expected,
            }
        )
        if self.checkpoint.checkpoint_id != f"checkpoint_{checkpoint_digest}":
            raise ReorientationStateError(
                "checkpoint identity does not match its reason and continuation"
            )


@dataclass(frozen=True, slots=True)
class GoalAchieved:
    strong_result_receipt_sha256: str
    kind: Literal["goal_achieved"] = field(default="goal_achieved", init=False)

    def __post_init__(self) -> None:
        _matching_text(
            self.strong_result_receipt_sha256,
            _SHA256_RE,
            "strong result receipt digest",
        )


@dataclass(frozen=True, slots=True)
class HardExternalBlock:
    code: HardExternalBlockCode
    required_external_action: str
    continuation: Continuation
    kind: Literal["hard_external_block"] = field(
        default="hard_external_block", init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.code, HardExternalBlockCode):
            raise ReorientationStateError("hard external block code is invalid")
        _text(self.required_external_action, "required external action")
        _require_continuation(self.continuation)


ReorientationPhase: TypeAlias = (
    Seeking
    | GenerationReserved
    | AcquisitionReserved
    | AwaitingEvidence
    | Checkpointed
    | GoalAchieved
    | HardExternalBlock
)


@dataclass(frozen=True, slots=True)
class ReorientationState:
    version: Literal[2]
    contract_id: str
    revision: int
    closed_attempts: tuple[ClosedDirectionAttempt, ...]
    phase: ReorientationPhase

    def __post_init__(self) -> None:
        if self.version != 2:
            raise ReorientationStateError("unsupported reorientation state version")
        _matching_text(self.contract_id, _CONTRACT_ID_RE, "contract id")
        _nonnegative_integer(self.revision, "state revision")
        if not isinstance(self.closed_attempts, tuple):
            raise ReorientationStateError("closed attempts must be an immutable tuple")
        if any(
            not isinstance(attempt, ClosedDirectionAttempt)
            for attempt in self.closed_attempts
        ):
            raise ReorientationStateError("closed attempts are invalid")
        if not isinstance(
            self.phase,
            (
                Seeking,
                GenerationReserved,
                AcquisitionReserved,
                AwaitingEvidence,
                Checkpointed,
                GoalAchieved,
                HardExternalBlock,
            ),
        ):
            raise ReorientationStateError("reorientation phase is invalid")
        closed_ids = [item.attempt.attempt_id for item in self.closed_attempts]
        closed_ordinals = [item.attempt.ordinal for item in self.closed_attempts]
        if len(closed_ids) != len(set(closed_ids)):
            raise ReorientationStateError("closed attempt ids must be unique")
        if len(closed_ordinals) != len(set(closed_ordinals)):
            raise ReorientationStateError("closed attempt ordinals must be unique")
        active = _active_attempt(self.phase)
        if active is not None and active.attempt_id in set(closed_ids):
            raise ReorientationStateError("active attempt is already closed")
        if active is not None and active.ordinal in set(closed_ordinals):
            raise ReorientationStateError("active attempt ordinal is already closed")
        if isinstance(self.phase, GenerationReserved):
            if self.phase.reservation.expected_revision + 1 != self.revision:
                raise ReorientationStateError(
                    "generation reservation revision is stale"
                )
        if isinstance(self.phase, AcquisitionReserved):
            if self.phase.reservation.expected_revision + 1 != self.revision:
                raise ReorientationStateError(
                    "acquisition reservation revision is stale"
                )


def _require_continuation(value: object) -> None:
    if not isinstance(
        value,
        (Seeking, GenerationReserved, AcquisitionReserved, AwaitingEvidence),
    ):
        raise ReorientationStateError("continuation phase is invalid")


def _active_attempt(value: ReorientationPhase | Continuation) -> DirectionAttemptRef | None:
    if isinstance(value, (AcquisitionReserved, AwaitingEvidence)):
        return value.active_attempt
    if isinstance(value, (Checkpointed, HardExternalBlock)):
        return _active_attempt(value.continuation)
    return None


def _attempt_to_dict(value: DirectionAttemptRef) -> dict[str, object]:
    return {
        "attempt_id": value.attempt_id,
        "direction_id": value.direction_id,
        "fingerprint_id": value.fingerprint_id,
        "ordinal": value.ordinal,
    }


def _reservation_to_dict(
    value: GenerationReservation | AcquisitionReservation,
    kind: Literal["generation", "acquisition"],
) -> dict[str, object]:
    return {
        "kind": kind,
        "reservation_id": value.reservation_id,
        "expected_revision": value.expected_revision,
        "request_digest": value.request_digest,
    }


def _continuation_to_dict(value: Continuation) -> dict[str, object]:
    if isinstance(value, Seeking):
        return {"kind": "seeking"}
    if isinstance(value, GenerationReserved):
        return {
            "kind": "generation_reserved",
            "reservation": _reservation_to_dict(value.reservation, "generation"),
        }
    if isinstance(value, AcquisitionReserved):
        return {
            "kind": "acquisition_reserved",
            "active_attempt": _attempt_to_dict(value.active_attempt),
            "reservation": _reservation_to_dict(value.reservation, "acquisition"),
        }
    if isinstance(value, AwaitingEvidence):
        return {
            "kind": "awaiting_evidence",
            "active_attempt": _attempt_to_dict(value.active_attempt),
        }
    raise ReorientationStateError("continuation phase is invalid")


def _continuation_digest(value: Continuation) -> str:
    return f"sha256:{_sha256(_continuation_to_dict(value))}"


def make_checkpoint(
    continuation: Continuation,
    *,
    reason: CheckpointReason,
) -> Checkpointed:
    _require_continuation(continuation)
    if reason not in _CHECKPOINT_REASONS:
        raise ReorientationStateError(f"unsupported checkpoint reason {reason!r}")
    continuation_digest = _continuation_digest(continuation)
    checkpoint_digest = _sha256(
        {"reason": reason, "continuation_digest": continuation_digest}
    )
    return Checkpointed(
        checkpoint=Checkpoint(
            checkpoint_id=f"checkpoint_{checkpoint_digest}",
            reason=reason,
            continuation_digest=continuation_digest,
        ),
        continuation=continuation,
    )


def initialize_reorientation_state(
    contract: SolutionContract,
) -> ReorientationState:
    if not isinstance(contract, SolutionContract):
        raise ReorientationStateError("state initialization requires a SolutionContract")
    return ReorientationState(
        version=2,
        contract_id=contract.contract_id,
        revision=0,
        closed_attempts=(),
        phase=Seeking(),
    )


def _phase_to_dict(value: ReorientationPhase) -> dict[str, object]:
    if isinstance(
        value,
        (Seeking, GenerationReserved, AcquisitionReserved, AwaitingEvidence),
    ):
        return _continuation_to_dict(value)
    if isinstance(value, Checkpointed):
        return {
            "kind": "checkpointed",
            "checkpoint": {
                "checkpoint_id": value.checkpoint.checkpoint_id,
                "reason": value.checkpoint.reason,
                "continuation_digest": value.checkpoint.continuation_digest,
            },
            "continuation": _continuation_to_dict(value.continuation),
        }
    if isinstance(value, GoalAchieved):
        return {
            "kind": "goal_achieved",
            "strong_result_receipt_sha256": value.strong_result_receipt_sha256,
        }
    if isinstance(value, HardExternalBlock):
        return {
            "kind": "hard_external_block",
            "code": value.code.value,
            "required_external_action": value.required_external_action,
            "continuation": _continuation_to_dict(value.continuation),
        }
    raise ReorientationStateError("reorientation phase is invalid")


def serialize_reorientation_state(state: ReorientationState) -> dict[str, object]:
    if not isinstance(state, ReorientationState):
        raise ReorientationStateError("value is not a ReorientationState")
    document = {
        "version": state.version,
        "contract_id": state.contract_id,
        "revision": state.revision,
        "closed_attempts": [
            {
                "attempt": _attempt_to_dict(item.attempt),
                "failure_receipt_sha256": item.failure_receipt_sha256,
                "private_lesson_sha256": item.private_lesson_sha256,
            }
            for item in state.closed_attempts
        ],
        "phase": _phase_to_dict(state.phase),
    }
    validate_named_schema("reorientation_state", document)
    return document


def _parse_attempt(value: object, label: str) -> DirectionAttemptRef:
    raw = _mapping(value, label)
    _exact_keys(
        raw,
        required=frozenset(
            {"attempt_id", "direction_id", "fingerprint_id", "ordinal"}
        ),
        label=label,
    )
    return DirectionAttemptRef(
        attempt_id=_matching_text(raw["attempt_id"], _ATTEMPT_ID_RE, "attempt id"),
        direction_id=_matching_text(
            raw["direction_id"], _DIRECTION_ID_RE, "direction id"
        ),
        fingerprint_id=_matching_text(
            raw["fingerprint_id"], _FINGERPRINT_ID_RE, "fingerprint id"
        ),
        ordinal=_nonnegative_integer(raw["ordinal"], "attempt ordinal"),
    )


def _parse_reservation(
    value: object,
    *,
    expected_kind: Literal["generation", "acquisition"],
) -> GenerationReservation | AcquisitionReservation:
    raw = _mapping(value, f"{expected_kind} reservation")
    _exact_keys(
        raw,
        required=frozenset(
            {"kind", "reservation_id", "expected_revision", "request_digest"}
        ),
        label=f"{expected_kind} reservation",
    )
    if raw["kind"] != expected_kind:
        raise ReorientationStateError(
            f"reservation kind must be {expected_kind!r}"
        )
    fields = {
        "reservation_id": _matching_text(
            raw["reservation_id"], _RESERVATION_ID_RE, "reservation id"
        ),
        "expected_revision": _nonnegative_integer(
            raw["expected_revision"], "expected revision"
        ),
        "request_digest": _matching_text(
            raw["request_digest"], _SHA256_RE, "request digest"
        ),
    }
    if expected_kind == "generation":
        return GenerationReservation(**fields)
    return AcquisitionReservation(**fields)


def _parse_continuation(value: object) -> Continuation:
    raw = _mapping(value, "continuation")
    kind = raw.get("kind")
    if kind == "seeking":
        _exact_keys(raw, required=frozenset({"kind"}), label="seeking continuation")
        return Seeking()
    if kind == "generation_reserved":
        _exact_keys(
            raw,
            required=frozenset({"kind", "reservation"}),
            label="generation continuation",
        )
        reservation = _parse_reservation(raw["reservation"], expected_kind="generation")
        if not isinstance(reservation, GenerationReservation):
            raise ReorientationStateError("generation reservation is invalid")
        return GenerationReserved(reservation=reservation)
    if kind == "acquisition_reserved":
        _exact_keys(
            raw,
            required=frozenset({"kind", "active_attempt", "reservation"}),
            label="acquisition continuation",
        )
        reservation = _parse_reservation(
            raw["reservation"], expected_kind="acquisition"
        )
        if not isinstance(reservation, AcquisitionReservation):
            raise ReorientationStateError("acquisition reservation is invalid")
        return AcquisitionReserved(
            active_attempt=_parse_attempt(raw["active_attempt"], "active attempt"),
            reservation=reservation,
        )
    if kind == "awaiting_evidence":
        _exact_keys(
            raw,
            required=frozenset({"kind", "active_attempt"}),
            label="evidence continuation",
        )
        return AwaitingEvidence(
            active_attempt=_parse_attempt(raw["active_attempt"], "active attempt")
        )
    raise ReorientationStateError(f"unsupported continuation phase {kind!r}")


def _parse_phase(value: object) -> ReorientationPhase:
    raw = _mapping(value, "reorientation phase")
    kind = raw.get("kind")
    if kind in {
        "seeking",
        "generation_reserved",
        "acquisition_reserved",
        "awaiting_evidence",
    }:
        return _parse_continuation(raw)
    if kind == "checkpointed":
        _exact_keys(
            raw,
            required=frozenset({"kind", "checkpoint", "continuation"}),
            label="checkpointed phase",
        )
        checkpoint_raw = _mapping(raw["checkpoint"], "checkpoint")
        _exact_keys(
            checkpoint_raw,
            required=frozenset(
                {"checkpoint_id", "reason", "continuation_digest"}
            ),
            label="checkpoint",
        )
        reason = checkpoint_raw["reason"]
        if reason not in _CHECKPOINT_REASONS:
            raise ReorientationStateError(
                f"unsupported checkpoint reason {reason!r}"
            )
        return Checkpointed(
            checkpoint=Checkpoint(
                checkpoint_id=_matching_text(
                    checkpoint_raw["checkpoint_id"],
                    _CHECKPOINT_ID_RE,
                    "checkpoint id",
                ),
                reason=reason,
                continuation_digest=_matching_text(
                    checkpoint_raw["continuation_digest"],
                    _SHA256_RE,
                    "checkpoint continuation digest",
                ),
            ),
            continuation=_parse_continuation(raw["continuation"]),
        )
    if kind == "goal_achieved":
        _exact_keys(
            raw,
            required=frozenset({"kind", "strong_result_receipt_sha256"}),
            label="goal achieved phase",
        )
        return GoalAchieved(
            strong_result_receipt_sha256=_matching_text(
                raw["strong_result_receipt_sha256"],
                _SHA256_RE,
                "strong result receipt digest",
            )
        )
    if kind == "hard_external_block":
        _exact_keys(
            raw,
            required=frozenset(
                {"kind", "code", "required_external_action", "continuation"}
            ),
            label="hard external block phase",
        )
        try:
            code = HardExternalBlockCode(raw["code"])
        except (TypeError, ValueError) as exc:
            raise ReorientationStateError("hard external block code is invalid") from exc
        return HardExternalBlock(
            code=code,
            required_external_action=_text(
                raw["required_external_action"], "required external action"
            ),
            continuation=_parse_continuation(raw["continuation"]),
        )
    raise ReorientationStateError(f"unsupported reorientation phase {kind!r}")


def parse_reorientation_state(value: object) -> ReorientationState:
    raw = _mapping(value, "reorientation state")
    validate_named_schema("reorientation_state", dict(raw))
    _exact_keys(
        raw,
        required=frozenset(
            {"version", "contract_id", "revision", "closed_attempts", "phase"}
        ),
        label="reorientation state",
    )
    closed_raw = raw["closed_attempts"]
    if not isinstance(closed_raw, list):
        raise ReorientationStateError("closed attempts must be an array")
    closed: list[ClosedDirectionAttempt] = []
    for index, value in enumerate(closed_raw):
        item = _mapping(value, f"closed_attempts[{index}]")
        _exact_keys(
            item,
            required=frozenset(
                {"attempt", "failure_receipt_sha256", "private_lesson_sha256"}
            ),
            label=f"closed_attempts[{index}]",
        )
        closed.append(
            ClosedDirectionAttempt(
                attempt=_parse_attempt(
                    item["attempt"], f"closed_attempts[{index}].attempt"
                ),
                failure_receipt_sha256=_matching_text(
                    item["failure_receipt_sha256"],
                    _SHA256_RE,
                    "failure receipt digest",
                ),
                private_lesson_sha256=_matching_text(
                    item["private_lesson_sha256"],
                    _SHA256_RE,
                    "private lesson digest",
                ),
            )
        )
    return ReorientationState(
        version=raw["version"],
        contract_id=_matching_text(raw["contract_id"], _CONTRACT_ID_RE, "contract id"),
        revision=_nonnegative_integer(raw["revision"], "state revision"),
        closed_attempts=tuple(closed),
        phase=_parse_phase(raw["phase"]),
    )
