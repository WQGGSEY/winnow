from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal, Mapping, Sequence, TypeAlias
from urllib.parse import urlsplit

from research_harness.orchestrator.blind_reorientation import (
    CheckpointReason,
    HardExternalBlockCode,
)
from research_harness.orchestrator.direction_generation import (
    DirectionDraft,
    DataNeed,
    parse_direction_draft,
    serialize_direction_draft,
)


SourceKind = Literal["registered_adapter", "public_api", "public_page", "crawl"]
ValidationStatus = Literal["passed", "not_evaluated"]
MissingnessStatus = Literal["passed", "not_evaluated"]
RobotsDecision = Literal["not_applicable", "allowed", "denied", "unavailable"]

_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ID_PATTERNS = {
    "acquisition command id": re.compile(r"^acqcmd_[a-f0-9]{64}$"),
    "cursor id": re.compile(r"^acqcursor_[a-f0-9]{64}$"),
    "receipt id": re.compile(r"^acqreceipt_[a-f0-9]{64}$"),
    "manifest id": re.compile(r"^acqmanifest_[a-f0-9]{64}$"),
    "reservation id": re.compile(r"^reservation_[a-f0-9]{64}$"),
    "attempt id": re.compile(r"^attempt_[A-Za-z0-9_-]+$"),
    "direction id": re.compile(r"^direction_[a-f0-9]{64}$"),
}


class AcquisitionContractError(ValueError):
    pass


class AcquisitionConflictError(AcquisitionContractError):
    pass


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def content_digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AcquisitionContractError(f"{label} must be canonical non-empty text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AcquisitionContractError(f"{label} must be a non-negative integer")
    return value


def _positive(value: object, label: str) -> int:
    value = _nonnegative(value, label)
    if value == 0:
        raise AcquisitionContractError(f"{label} must be positive")
    return value


def _matching(value: object, label: str) -> str:
    text = _text(value, label)
    pattern = _ID_PATTERNS[label]
    if pattern.fullmatch(text) is None:
        raise AcquisitionContractError(f"{label} is invalid")
    return text


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise AcquisitionContractError(f"{label} is invalid")
    return text


def _public_uri(value: object) -> str:
    uri = _text(value, "public source URI")
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError as exc:
        raise AcquisitionContractError("public source URI is invalid") from exc
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise AcquisitionContractError("public source URI must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise AcquisitionContractError("public source URI cannot contain credentials")
    if parsed.query or parsed.fragment:
        raise AcquisitionContractError(
            "public source URI query and fragment must be supplied by a safe adapter"
        )
    if port is not None and not 1 <= port <= 65535:
        raise AcquisitionContractError("public source URI port is invalid")
    return uri


@dataclass(frozen=True, slots=True)
class AcquisitionBudget:
    max_requests: int
    max_download_bytes: int
    max_wall_seconds: int
    max_cost_cents: Literal[0] = 0

    def __post_init__(self) -> None:
        _nonnegative(self.max_requests, "maximum requests")
        _nonnegative(self.max_download_bytes, "maximum download bytes")
        _nonnegative(self.max_wall_seconds, "maximum wall seconds")
        if self.max_cost_cents != 0:
            raise AcquisitionContractError("public acquisition cannot spend money")


@dataclass(frozen=True, slots=True)
class RegisteredSource:
    adapter_id: str
    snapshot_id: str
    path: str
    content_sha256: str
    size_bytes: int
    entry_count: int
    provenance: str
    retrieved_at: str
    license_evidence: str | None = None
    kind: Literal["registered_adapter"] = field(
        default="registered_adapter", init=False
    )

    def __post_init__(self) -> None:
        _text(self.adapter_id, "adapter id")
        _text(self.snapshot_id, "snapshot id")
        _text(self.path, "registered source path")
        _sha256(self.content_sha256, "content digest")
        _nonnegative(self.size_bytes, "source size")
        _positive(self.entry_count, "source entry count")
        _text(self.provenance, "source provenance")
        _text(self.retrieved_at, "retrieval time")
        _optional_text(self.license_evidence, "license evidence")


@dataclass(frozen=True, slots=True)
class PublicSource:
    kind: Literal["public_api", "public_page", "crawl"]
    uri: str
    credential_profile_name: str | None = None
    license_evidence: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"public_api", "public_page", "crawl"}:
            raise AcquisitionContractError(f"unsupported public source {self.kind!r}")
        _public_uri(self.uri)
        _optional_text(self.credential_profile_name, "credential profile name")
        _optional_text(self.license_evidence, "license evidence")


SourceCandidate: TypeAlias = RegisteredSource | PublicSource


@dataclass(frozen=True, slots=True)
class NeedPlan:
    need_index: int
    need: DataNeed
    candidates: tuple[SourceCandidate, ...]

    def __post_init__(self) -> None:
        _nonnegative(self.need_index, "need index")
        if not isinstance(self.need, DataNeed):
            raise AcquisitionContractError("need plan requires a DataNeed")
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(item, (RegisteredSource, PublicSource))
            for item in self.candidates
        ):
            raise AcquisitionContractError("source candidates must be an immutable tuple")


@dataclass(frozen=True, slots=True)
class AcquisitionCommand:
    command_id: str
    reservation_id: str
    node_id: str
    attempt_id: str
    direction: DirectionDraft
    needs: tuple[NeedPlan, ...]
    budget: AcquisitionBudget

    def __post_init__(self) -> None:
        _matching(self.command_id, "acquisition command id")
        _matching(self.reservation_id, "reservation id")
        _text(self.node_id, "node id")
        _matching(self.attempt_id, "attempt id")
        if not isinstance(self.direction, DirectionDraft):
            raise AcquisitionContractError("acquisition direction is invalid")
        if not isinstance(self.needs, tuple) or any(
            not isinstance(item, NeedPlan) for item in self.needs
        ):
            raise AcquisitionContractError("need plans must be an immutable tuple")
        if tuple(item.need_index for item in self.needs) != tuple(range(len(self.needs))):
            raise AcquisitionContractError("need plans must use contiguous ordered indexes")
        if tuple(item.need for item in self.needs) != self.direction.data_needs:
            raise AcquisitionContractError("need plans must match the direction data needs")
        if not isinstance(self.budget, AcquisitionBudget):
            raise AcquisitionContractError("acquisition budget is invalid")
        expected = f"acqcmd_{content_digest(_command_payload(self))[7:]}"
        if self.command_id != expected:
            raise AcquisitionContractError("command identity does not match its content")


def make_acquisition_command(
    *,
    reservation_id: str,
    node_id: str,
    attempt_id: str,
    direction: DirectionDraft,
    needs: Sequence[NeedPlan],
    budget: AcquisitionBudget,
) -> AcquisitionCommand:
    normalized_needs = tuple(needs)
    payload = _command_fields_payload(
        reservation_id=reservation_id,
        node_id=node_id,
        attempt_id=attempt_id,
        direction=direction,
        needs=normalized_needs,
        budget=budget,
    )
    return AcquisitionCommand(
        command_id=f"acqcmd_{content_digest(payload)[7:]}",
        reservation_id=reservation_id,
        node_id=node_id,
        attempt_id=attempt_id,
        direction=direction,
        needs=normalized_needs,
        budget=budget,
    )


@dataclass(frozen=True, slots=True)
class CacheObject:
    content_sha256: str
    size_bytes: int
    entry_count: int
    object_kind: Literal["file", "directory"]

    def __post_init__(self) -> None:
        _sha256(self.content_sha256, "content digest")
        _nonnegative(self.size_bytes, "cache object size")
        _positive(self.entry_count, "cache object entry count")
        if self.object_kind not in {"file", "directory"}:
            raise AcquisitionContractError("cache object kind is invalid")


@dataclass(frozen=True, slots=True)
class ValidationReport:
    status: ValidationStatus
    detail: str

    def __post_init__(self) -> None:
        if self.status not in {"passed", "not_evaluated"}:
            raise AcquisitionContractError("validation status is invalid")
        _text(self.detail, "validation detail")


@dataclass(frozen=True, slots=True)
class MissingnessReport:
    status: MissingnessStatus
    fraction: float | None
    detail: str

    def __post_init__(self) -> None:
        if self.status not in {"passed", "not_evaluated"}:
            raise AcquisitionContractError("missingness status is invalid")
        if self.fraction is not None and not 0 <= self.fraction <= 1:
            raise AcquisitionContractError("missingness fraction must be between zero and one")
        _text(self.detail, "missingness detail")


@dataclass(frozen=True, slots=True)
class ResponseReceipt:
    receipt_id: str
    source_kind: SourceKind
    source_uri: str
    retrieved_at: str
    robots_decision: RobotsDecision
    rate_limit_events: tuple[str, ...]
    credential_profile_name: str | None
    adapter_id: str | None
    snapshot_id: str | None
    provenance: str
    license_evidence: str | None
    cache_object: CacheObject
    validation: ValidationReport
    missingness: MissingnessReport

    def __post_init__(self) -> None:
        _matching(self.receipt_id, "receipt id")
        if self.source_kind not in {
            "registered_adapter", "public_api", "public_page", "crawl"
        }:
            raise AcquisitionContractError("response source kind is invalid")
        _text(self.source_uri, "response source URI")
        _text(self.retrieved_at, "retrieval time")
        if self.robots_decision not in {
            "not_applicable", "allowed", "denied", "unavailable"
        }:
            raise AcquisitionContractError("robots decision is invalid")
        if not isinstance(self.rate_limit_events, tuple) or any(
            not isinstance(item, str) or not item for item in self.rate_limit_events
        ):
            raise AcquisitionContractError("rate limit events must be immutable text")
        _optional_text(self.credential_profile_name, "credential profile name")
        _optional_text(self.adapter_id, "adapter id")
        _optional_text(self.snapshot_id, "snapshot id")
        if self.source_kind == "registered_adapter" and (
            self.adapter_id is None or self.snapshot_id is None
        ):
            raise AcquisitionContractError(
                "registered receipt requires adapter and snapshot identities"
            )
        _text(self.provenance, "source provenance")
        _optional_text(self.license_evidence, "license evidence")
        if not isinstance(self.cache_object, CacheObject):
            raise AcquisitionContractError("receipt cache object is invalid")
        if not isinstance(self.validation, ValidationReport):
            raise AcquisitionContractError("validation report is invalid")
        if not isinstance(self.missingness, MissingnessReport):
            raise AcquisitionContractError("missingness report is invalid")
        expected = f"acqreceipt_{content_digest(_receipt_payload(self))[7:]}"
        if self.receipt_id != expected:
            raise AcquisitionContractError("receipt identity does not match its content")


@dataclass(frozen=True, slots=True)
class AcquiredNeed:
    need_index: int
    source_candidate_index: int
    description: str
    receipt: ResponseReceipt

    def __post_init__(self) -> None:
        _nonnegative(self.need_index, "acquired need index")
        _nonnegative(self.source_candidate_index, "source candidate index")
        _text(self.description, "acquired need description")
        if not isinstance(self.receipt, ResponseReceipt):
            raise AcquisitionContractError("acquired need receipt is invalid")


@dataclass(frozen=True, slots=True)
class AcquisitionCursor:
    cursor_id: str
    command_id: str
    next_need_index: int
    next_candidate_index: int
    requests_used: int
    download_bytes_used: int
    completed: tuple[AcquiredNeed, ...]

    def __post_init__(self) -> None:
        _matching(self.cursor_id, "cursor id")
        _matching(self.command_id, "acquisition command id")
        _nonnegative(self.next_need_index, "next need index")
        _nonnegative(self.next_candidate_index, "next candidate index")
        _nonnegative(self.requests_used, "used requests")
        _nonnegative(self.download_bytes_used, "used download bytes")
        if not isinstance(self.completed, tuple) or any(
            not isinstance(item, AcquiredNeed) for item in self.completed
        ):
            raise AcquisitionContractError("completed needs must be immutable")
        if tuple(item.need_index for item in self.completed) != tuple(range(len(self.completed))):
            raise AcquisitionContractError("completed needs must be contiguous and ordered")
        expected = f"acqcursor_{content_digest(_cursor_payload(self))[7:]}"
        if self.cursor_id != expected:
            raise AcquisitionContractError("cursor identity does not match its content")


@dataclass(frozen=True, slots=True)
class PinnedNodeManifest:
    manifest_id: str
    command_id: str
    node_id: str
    attempt_id: str
    direction_id: str
    acquired_needs: tuple[AcquiredNeed, ...]

    def __post_init__(self) -> None:
        _matching(self.manifest_id, "manifest id")
        _matching(self.command_id, "acquisition command id")
        _text(self.node_id, "node id")
        _matching(self.attempt_id, "attempt id")
        _matching(self.direction_id, "direction id")
        if not isinstance(self.acquired_needs, tuple) or any(
            not isinstance(item, AcquiredNeed) for item in self.acquired_needs
        ):
            raise AcquisitionContractError("manifest needs must be immutable")
        if tuple(item.need_index for item in self.acquired_needs) != tuple(
            range(len(self.acquired_needs))
        ):
            raise AcquisitionContractError("manifest needs must be contiguous and ordered")
        expected = f"acqmanifest_{content_digest(_manifest_payload(self))[7:]}"
        if self.manifest_id != expected:
            raise AcquisitionContractError("manifest identity does not match its content")


@dataclass(frozen=True, slots=True)
class AcquisitionComplete:
    manifest: PinnedNodeManifest
    kind: Literal["complete"] = field(default="complete", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, PinnedNodeManifest):
            raise AcquisitionContractError("complete outcome manifest is invalid")


@dataclass(frozen=True, slots=True)
class AcquisitionCheckpoint:
    reason: CheckpointReason
    cursor: AcquisitionCursor
    kind: Literal["checkpointed"] = field(default="checkpointed", init=False)

    def __post_init__(self) -> None:
        if self.reason not in {
            "time_budget", "cost_budget", "download_budget", "request_budget",
            "rate_limited", "transient_network", "generation_retry",
            "inconclusive_evidence",
        }:
            raise AcquisitionContractError("checkpoint reason is invalid")
        if not isinstance(self.cursor, AcquisitionCursor):
            raise AcquisitionContractError("checkpoint cursor is invalid")


@dataclass(frozen=True, slots=True)
class AcquisitionBlocked:
    code: HardExternalBlockCode
    required_external_action: str
    cursor: AcquisitionCursor
    kind: Literal["hard_external_block"] = field(
        default="hard_external_block", init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.code, HardExternalBlockCode):
            raise AcquisitionContractError("hard block code is invalid")
        _text(self.required_external_action, "required external action")
        if not isinstance(self.cursor, AcquisitionCursor):
            raise AcquisitionContractError("hard block cursor is invalid")


AcquisitionOutcome: TypeAlias = (
    AcquisitionComplete | AcquisitionCheckpoint | AcquisitionBlocked
)


def _source_payload(source: SourceCandidate) -> dict[str, object]:
    if isinstance(source, RegisteredSource):
        return {
            "kind": source.kind,
            "adapter_id": source.adapter_id,
            "snapshot_id": source.snapshot_id,
            "path": source.path,
            "content_sha256": source.content_sha256,
            "size_bytes": source.size_bytes,
            "entry_count": source.entry_count,
            "provenance": source.provenance,
            "retrieved_at": source.retrieved_at,
            "license_evidence": source.license_evidence,
        }
    return {
        "kind": source.kind,
        "uri": source.uri,
        "credential_profile_name": source.credential_profile_name,
        "license_evidence": source.license_evidence,
    }


def _budget_payload(budget: AcquisitionBudget) -> dict[str, int]:
    return {
        "max_requests": budget.max_requests,
        "max_download_bytes": budget.max_download_bytes,
        "max_wall_seconds": budget.max_wall_seconds,
        "max_cost_cents": budget.max_cost_cents,
    }


def _command_payload(command: AcquisitionCommand) -> dict[str, object]:
    return _command_fields_payload(
        reservation_id=command.reservation_id,
        node_id=command.node_id,
        attempt_id=command.attempt_id,
        direction=command.direction,
        needs=command.needs,
        budget=command.budget,
    )


def serialize_command(command: AcquisitionCommand) -> dict[str, object]:
    if not isinstance(command, AcquisitionCommand):
        raise AcquisitionContractError("value is not an AcquisitionCommand")
    return {"command_id": command.command_id, **_command_payload(command)}


def parse_command(value: object) -> AcquisitionCommand:
    if not isinstance(value, Mapping):
        raise AcquisitionContractError("acquisition command must be an object")
    required = {
        "command_id", "reservation_id", "node_id", "attempt_id",
        "direction", "needs", "budget",
    }
    if set(value) != required:
        raise AcquisitionContractError("acquisition command has an unsupported shape")
    direction = parse_direction_draft(value["direction"])
    raw_needs = value["needs"]
    raw_budget = value["budget"]
    if not isinstance(raw_needs, list) or not isinstance(raw_budget, Mapping):
        raise AcquisitionContractError("acquisition command plans or budget are malformed")
    budget_keys = {
        "max_requests", "max_download_bytes", "max_wall_seconds", "max_cost_cents"
    }
    if set(raw_budget) != budget_keys:
        raise AcquisitionContractError("acquisition budget has an unsupported shape")
    plans = tuple(_parse_need_plan(item, direction) for item in raw_needs)
    return AcquisitionCommand(
        command_id=value["command_id"],
        reservation_id=value["reservation_id"],
        node_id=value["node_id"],
        attempt_id=value["attempt_id"],
        direction=direction,
        needs=plans,
        budget=AcquisitionBudget(**raw_budget),
    )


def _parse_need_plan(value: object, direction: DirectionDraft) -> NeedPlan:
    if not isinstance(value, Mapping) or set(value) != {
        "need_index", "need", "candidates"
    }:
        raise AcquisitionContractError("need plan has an unsupported shape")
    need_index = _nonnegative(value["need_index"], "need index")
    raw_need = value["need"]
    candidates = value["candidates"]
    if (
        need_index >= len(direction.data_needs)
        or not isinstance(raw_need, Mapping)
        or set(raw_need) != {"kind", "description"}
        or not isinstance(candidates, list)
    ):
        raise AcquisitionContractError("need plan is malformed")
    need = DataNeed(kind=raw_need["kind"], description=raw_need["description"])
    return NeedPlan(
        need_index=need_index,
        need=need,
        candidates=tuple(_parse_source(item) for item in candidates),
    )


def _parse_source(value: object) -> SourceCandidate:
    if not isinstance(value, Mapping):
        raise AcquisitionContractError("source candidate must be an object")
    kind = value.get("kind")
    if kind == "registered_adapter":
        required = {
            "kind", "adapter_id", "snapshot_id", "path", "content_sha256",
            "size_bytes", "entry_count", "provenance", "retrieved_at",
            "license_evidence",
        }
        if set(value) != required:
            raise AcquisitionContractError("registered source has an unsupported shape")
        return RegisteredSource(
            adapter_id=value["adapter_id"],
            snapshot_id=value["snapshot_id"],
            path=value["path"],
            content_sha256=value["content_sha256"],
            size_bytes=value["size_bytes"],
            entry_count=value["entry_count"],
            provenance=value["provenance"],
            retrieved_at=value["retrieved_at"],
            license_evidence=value["license_evidence"],
        )
    required = {"kind", "uri", "credential_profile_name", "license_evidence"}
    if set(value) != required:
        raise AcquisitionContractError("public source has an unsupported shape")
    return PublicSource(
        kind=kind,
        uri=value["uri"],
        credential_profile_name=value["credential_profile_name"],
        license_evidence=value["license_evidence"],
    )


def _command_fields_payload(
    *,
    reservation_id: str,
    node_id: str,
    attempt_id: str,
    direction: DirectionDraft,
    needs: tuple[NeedPlan, ...],
    budget: AcquisitionBudget,
) -> dict[str, object]:
    return {
        "reservation_id": reservation_id,
        "node_id": node_id,
        "attempt_id": attempt_id,
        "direction": serialize_direction_draft(direction),
        "needs": [
            {
                "need_index": item.need_index,
                "need": {
                    "kind": item.need.kind,
                    "description": item.need.description,
                },
                "candidates": [_source_payload(source) for source in item.candidates],
            }
            for item in needs
        ],
        "budget": _budget_payload(budget),
    }


def _cache_payload(value: CacheObject) -> dict[str, object]:
    return {
        "content_sha256": value.content_sha256,
        "size_bytes": value.size_bytes,
        "entry_count": value.entry_count,
        "object_kind": value.object_kind,
    }


def _validation_payload(value: ValidationReport) -> dict[str, object]:
    return {"status": value.status, "detail": value.detail}


def _missingness_payload(value: MissingnessReport) -> dict[str, object]:
    return {"status": value.status, "fraction": value.fraction, "detail": value.detail}


def _receipt_payload(value: ResponseReceipt) -> dict[str, object]:
    return _receipt_fields_payload(
        source_kind=value.source_kind,
        source_uri=value.source_uri,
        retrieved_at=value.retrieved_at,
        robots_decision=value.robots_decision,
        rate_limit_events=value.rate_limit_events,
        credential_profile_name=value.credential_profile_name,
        adapter_id=value.adapter_id,
        snapshot_id=value.snapshot_id,
        provenance=value.provenance,
        license_evidence=value.license_evidence,
        cache_object=value.cache_object,
        validation=value.validation,
        missingness=value.missingness,
    )


def _receipt_fields_payload(
    *,
    source_kind: SourceKind,
    source_uri: str,
    retrieved_at: str,
    robots_decision: RobotsDecision,
    rate_limit_events: tuple[str, ...],
    credential_profile_name: str | None,
    adapter_id: str | None,
    snapshot_id: str | None,
    provenance: str,
    license_evidence: str | None,
    cache_object: CacheObject,
    validation: ValidationReport,
    missingness: MissingnessReport,
) -> dict[str, object]:
    return {
        "source_kind": source_kind,
        "source_uri": source_uri,
        "retrieved_at": retrieved_at,
        "robots_decision": robots_decision,
        "rate_limit_events": list(rate_limit_events),
        "credential_profile_name": credential_profile_name,
        "adapter_id": adapter_id,
        "snapshot_id": snapshot_id,
        "provenance": provenance,
        "license_evidence": license_evidence,
        "cache_object": _cache_payload(cache_object),
        "validation": _validation_payload(validation),
        "missingness": _missingness_payload(missingness),
    }


def serialize_response_receipt(value: ResponseReceipt) -> dict[str, object]:
    return {"receipt_id": value.receipt_id, **_receipt_payload(value)}


def make_response_receipt(
    *,
    source_kind: SourceKind,
    source_uri: str,
    retrieved_at: str,
    robots_decision: RobotsDecision,
    rate_limit_events: Sequence[str],
    credential_profile_name: str | None,
    adapter_id: str | None,
    snapshot_id: str | None,
    provenance: str,
    license_evidence: str | None,
    cache_object: CacheObject,
    validation: ValidationReport,
    missingness: MissingnessReport,
) -> ResponseReceipt:
    normalized_events = tuple(rate_limit_events)
    payload = _receipt_fields_payload(
        source_kind=source_kind,
        source_uri=source_uri,
        retrieved_at=retrieved_at,
        robots_decision=robots_decision,
        rate_limit_events=normalized_events,
        credential_profile_name=credential_profile_name,
        adapter_id=adapter_id,
        snapshot_id=snapshot_id,
        provenance=provenance,
        license_evidence=license_evidence,
        cache_object=cache_object,
        validation=validation,
        missingness=missingness,
    )
    return ResponseReceipt(
        receipt_id=f"acqreceipt_{content_digest(payload)[7:]}",
        source_kind=source_kind,
        source_uri=source_uri,
        retrieved_at=retrieved_at,
        robots_decision=robots_decision,
        rate_limit_events=normalized_events,
        credential_profile_name=credential_profile_name,
        adapter_id=adapter_id,
        snapshot_id=snapshot_id,
        provenance=provenance,
        license_evidence=license_evidence,
        cache_object=cache_object,
        validation=validation,
        missingness=missingness,
    )


def _need_payload(value: AcquiredNeed) -> dict[str, object]:
    return {
        "need_index": value.need_index,
        "source_candidate_index": value.source_candidate_index,
        "description": value.description,
        "receipt": serialize_response_receipt(value.receipt),
    }


def _cursor_payload(value: AcquisitionCursor) -> dict[str, object]:
    return _cursor_fields_payload(
        command_id=value.command_id,
        next_need_index=value.next_need_index,
        next_candidate_index=value.next_candidate_index,
        requests_used=value.requests_used,
        download_bytes_used=value.download_bytes_used,
        completed=value.completed,
    )


def _cursor_fields_payload(
    *,
    command_id: str,
    next_need_index: int,
    next_candidate_index: int,
    requests_used: int,
    download_bytes_used: int,
    completed: tuple[AcquiredNeed, ...],
) -> dict[str, object]:
    return {
        "command_id": command_id,
        "next_need_index": next_need_index,
        "next_candidate_index": next_candidate_index,
        "requests_used": requests_used,
        "download_bytes_used": download_bytes_used,
        "completed": [_need_payload(item) for item in completed],
    }


def make_cursor(
    *,
    command_id: str,
    next_need_index: int,
    next_candidate_index: int,
    requests_used: int,
    download_bytes_used: int,
    completed: Sequence[AcquiredNeed],
) -> AcquisitionCursor:
    normalized_completed = tuple(completed)
    payload = _cursor_fields_payload(
        command_id=command_id,
        next_need_index=next_need_index,
        next_candidate_index=next_candidate_index,
        requests_used=requests_used,
        download_bytes_used=download_bytes_used,
        completed=normalized_completed,
    )
    return AcquisitionCursor(
        cursor_id=f"acqcursor_{content_digest(payload)[7:]}",
        command_id=command_id,
        next_need_index=next_need_index,
        next_candidate_index=next_candidate_index,
        requests_used=requests_used,
        download_bytes_used=download_bytes_used,
        completed=normalized_completed,
    )


def _manifest_payload(value: PinnedNodeManifest) -> dict[str, object]:
    return _manifest_fields_payload(
        command_id=value.command_id,
        node_id=value.node_id,
        attempt_id=value.attempt_id,
        direction_id=value.direction_id,
        acquired_needs=value.acquired_needs,
    )


def _manifest_fields_payload(
    *,
    command_id: str,
    node_id: str,
    attempt_id: str,
    direction_id: str,
    acquired_needs: tuple[AcquiredNeed, ...],
) -> dict[str, object]:
    return {
        "command_id": command_id,
        "node_id": node_id,
        "attempt_id": attempt_id,
        "direction_id": direction_id,
        "acquired_needs": [_need_payload(item) for item in acquired_needs],
    }


def make_manifest(
    *,
    command_id: str,
    node_id: str,
    attempt_id: str,
    direction_id: str,
    acquired_needs: Sequence[AcquiredNeed],
) -> PinnedNodeManifest:
    normalized_needs = tuple(acquired_needs)
    payload = _manifest_fields_payload(
        command_id=command_id,
        node_id=node_id,
        attempt_id=attempt_id,
        direction_id=direction_id,
        acquired_needs=normalized_needs,
    )
    return PinnedNodeManifest(
        manifest_id=f"acqmanifest_{content_digest(payload)[7:]}",
        command_id=command_id,
        node_id=node_id,
        attempt_id=attempt_id,
        direction_id=direction_id,
        acquired_needs=normalized_needs,
    )


def serialize_manifest(value: PinnedNodeManifest) -> dict[str, object]:
    return {"manifest_id": value.manifest_id, **_manifest_payload(value)}


def parse_manifest(value: object) -> PinnedNodeManifest:
    if not isinstance(value, Mapping):
        raise AcquisitionContractError("manifest must be an object")
    required = {
        "manifest_id", "command_id", "node_id", "attempt_id",
        "direction_id", "acquired_needs",
    }
    if set(value) != required or not isinstance(value["acquired_needs"], list):
        raise AcquisitionContractError("manifest has an unsupported shape")
    acquired = tuple(_parse_acquired_need(item) for item in value["acquired_needs"])
    return PinnedNodeManifest(
        manifest_id=value["manifest_id"],
        command_id=value["command_id"],
        node_id=value["node_id"],
        attempt_id=value["attempt_id"],
        direction_id=value["direction_id"],
        acquired_needs=acquired,
    )


def _parse_acquired_need(value: object) -> AcquiredNeed:
    if not isinstance(value, Mapping) or set(value) != {
        "need_index", "source_candidate_index", "description", "receipt"
    }:
        raise AcquisitionContractError("acquired need has an unsupported shape")
    return AcquiredNeed(
        need_index=value["need_index"],
        source_candidate_index=value["source_candidate_index"],
        description=value["description"],
        receipt=_parse_receipt(value["receipt"]),
    )


def _parse_receipt(value: object) -> ResponseReceipt:
    if not isinstance(value, Mapping):
        raise AcquisitionContractError("response receipt must be an object")
    required = {
        "receipt_id", "source_kind", "source_uri", "retrieved_at",
        "robots_decision", "rate_limit_events", "credential_profile_name",
        "adapter_id", "snapshot_id", "provenance", "license_evidence",
        "cache_object", "validation", "missingness",
    }
    if set(value) != required:
        raise AcquisitionContractError("response receipt has an unsupported shape")
    cache = value["cache_object"]
    validation = value["validation"]
    missingness = value["missingness"]
    if not all(isinstance(item, Mapping) for item in (cache, validation, missingness)):
        raise AcquisitionContractError("response receipt reports are malformed")
    events = value["rate_limit_events"]
    if not isinstance(events, list):
        raise AcquisitionContractError("rate limit events must be an array")
    return ResponseReceipt(
        receipt_id=value["receipt_id"],
        source_kind=value["source_kind"],
        source_uri=value["source_uri"],
        retrieved_at=value["retrieved_at"],
        robots_decision=value["robots_decision"],
        rate_limit_events=tuple(events),
        credential_profile_name=value["credential_profile_name"],
        adapter_id=value["adapter_id"],
        snapshot_id=value["snapshot_id"],
        provenance=value["provenance"],
        license_evidence=value["license_evidence"],
        cache_object=CacheObject(**cache),
        validation=ValidationReport(**validation),
        missingness=MissingnessReport(**missingness),
    )
