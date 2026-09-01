from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence

from .cache import AcquisitionStorageError
from .model import (
    AcquisitionConflictError,
    AcquisitionContractError,
    CacheObject,
    MissingnessReport,
    ValidationReport,
    canonical_json_bytes,
    content_digest,
)


ExchangePurpose = Literal["robots", "content"]
_POLICY_ID_RE = re.compile(r"^httppolicy_[a-f0-9]{64}$")
_ATTEMPT_ID_RE = re.compile(r"^httpattempt_[a-f0-9]{64}$")
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_SAFE_HEADERS = frozenset(
    {
        "content-length",
        "content-type",
        "etag",
        "last-modified",
        "license",
        "link",
        "location",
        "retry-after",
    }
)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AcquisitionContractError(f"{label} must be canonical non-empty text")
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AcquisitionContractError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class HttpResponseRecord:
    purpose: ExchangePurpose
    uri: str
    retrieved_at: str
    status: int
    peer_ip: str
    headers: tuple[tuple[str, str], ...]
    byte_count: int
    content_sha256: str
    robots_decision: Literal["not_applicable", "allowed", "unavailable"]
    rate_limit_events: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.purpose not in {"robots", "content"}:
            raise AcquisitionContractError("HTTP response purpose is invalid")
        _text(self.uri, "HTTP response URI")
        _text(self.retrieved_at, "HTTP retrieval time")
        if (
            isinstance(self.status, bool)
            or not isinstance(self.status, int)
            or not 100 <= self.status <= 599
        ):
            raise AcquisitionContractError("HTTP response status is invalid")
        _text(self.peer_ip, "HTTP peer IP")
        if not isinstance(self.headers, tuple):
            raise AcquisitionContractError("HTTP response headers must be immutable")
        normalized = tuple(sorted(self.headers))
        if self.headers != normalized or any(
            not isinstance(name, str)
            or name not in _SAFE_HEADERS
            or not isinstance(value, str)
            or "\n" in value
            or "\r" in value
            for name, value in self.headers
        ):
            raise AcquisitionContractError("HTTP response headers are not safe canonical metadata")
        _nonnegative(self.byte_count, "HTTP response byte count")
        if not isinstance(self.content_sha256, str) or _SHA256_RE.fullmatch(
            self.content_sha256
        ) is None:
            raise AcquisitionContractError("HTTP response checksum is invalid")
        if self.robots_decision not in {"not_applicable", "allowed", "unavailable"}:
            raise AcquisitionContractError("HTTP robots decision is invalid")
        if not isinstance(self.rate_limit_events, tuple) or any(
            not isinstance(item, str) or not item for item in self.rate_limit_events
        ):
            raise AcquisitionContractError("HTTP rate events must be immutable text")


@dataclass(frozen=True, slots=True)
class PublicPolicyReceipt:
    policy_receipt_id: str
    command_id: str
    need_index: int
    source_candidate_index: int
    source_kind: Literal["public_api", "public_page", "crawl"]
    requested_uri: str
    final_uri: str
    credential_profile_name: str | None
    license_evidence: str | None
    cache_object: CacheObject
    validation: ValidationReport
    missingness: MissingnessReport
    exchanges: tuple[HttpResponseRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policy_receipt_id, str) or _POLICY_ID_RE.fullmatch(
            self.policy_receipt_id
        ) is None:
            raise AcquisitionContractError("HTTP policy receipt id is invalid")
        _text(self.command_id, "acquisition command id")
        _nonnegative(self.need_index, "policy need index")
        _nonnegative(self.source_candidate_index, "policy source candidate index")
        if self.source_kind not in {"public_api", "public_page", "crawl"}:
            raise AcquisitionContractError("HTTP policy source kind is invalid")
        _text(self.requested_uri, "requested source URI")
        _text(self.final_uri, "final source URI")
        if self.credential_profile_name is not None:
            _text(self.credential_profile_name, "credential profile name")
        if self.license_evidence is not None:
            _text(self.license_evidence, "license evidence")
        if not isinstance(self.cache_object, CacheObject):
            raise AcquisitionContractError("HTTP policy cache object is invalid")
        if not isinstance(self.validation, ValidationReport):
            raise AcquisitionContractError("HTTP policy validation is invalid")
        if not isinstance(self.missingness, MissingnessReport):
            raise AcquisitionContractError("HTTP policy missingness is invalid")
        if not isinstance(self.exchanges, tuple) or not self.exchanges:
            raise AcquisitionContractError("HTTP policy requires response records")
        contents = tuple(item for item in self.exchanges if item.purpose == "content")
        if not contents:
            raise AcquisitionContractError("HTTP policy final response is not recorded")
        expected_final = contents[0].uri if self.source_kind == "crawl" else contents[-1].uri
        if expected_final != self.final_uri:
            raise AcquisitionContractError("HTTP policy final response is not recorded")
        expected = f"httppolicy_{content_digest(_policy_payload(self))[7:]}"
        if self.policy_receipt_id != expected:
            raise AcquisitionContractError(
                "HTTP policy receipt identity does not match its content"
            )


@dataclass(frozen=True, slots=True)
class HttpAttemptReceipt:
    attempt_receipt_id: str
    command_id: str
    source_kind: Literal["public_api", "public_page", "crawl"]
    requested_source_uri: str
    credential_profile_name: str | None
    license_evidence: str | None
    validation: ValidationReport
    missingness: MissingnessReport
    exchange: HttpResponseRecord

    def __post_init__(self) -> None:
        if _ATTEMPT_ID_RE.fullmatch(self.attempt_receipt_id) is None:
            raise AcquisitionContractError("HTTP attempt receipt id is invalid")
        _text(self.command_id, "HTTP attempt command id")
        if self.source_kind not in {"public_api", "public_page", "crawl"}:
            raise AcquisitionContractError("HTTP attempt source kind is invalid")
        _text(self.requested_source_uri, "HTTP attempt source URI")
        if self.credential_profile_name is not None:
            _text(self.credential_profile_name, "credential profile name")
        if self.license_evidence is not None:
            _text(self.license_evidence, "license evidence")
        if not isinstance(self.validation, ValidationReport):
            raise AcquisitionContractError("HTTP attempt validation is invalid")
        if not isinstance(self.missingness, MissingnessReport):
            raise AcquisitionContractError("HTTP attempt missingness is invalid")
        if not isinstance(self.exchange, HttpResponseRecord):
            raise AcquisitionContractError("HTTP attempt exchange is invalid")
        expected = f"httpattempt_{content_digest(_attempt_payload(self))[7:]}"
        if self.attempt_receipt_id != expected:
            raise AcquisitionContractError(
                "HTTP attempt receipt identity does not match its content"
            )


def _cache_payload(value: CacheObject) -> dict[str, object]:
    return {
        "content_sha256": value.content_sha256,
        "size_bytes": value.size_bytes,
        "entry_count": value.entry_count,
        "object_kind": value.object_kind,
    }


def _exchange_payload(value: HttpResponseRecord) -> dict[str, object]:
    return {
        "purpose": value.purpose,
        "uri": value.uri,
        "retrieved_at": value.retrieved_at,
        "status": value.status,
        "peer_ip": value.peer_ip,
        "headers": [list(item) for item in value.headers],
        "byte_count": value.byte_count,
        "content_sha256": value.content_sha256,
        "robots_decision": value.robots_decision,
        "rate_limit_events": list(value.rate_limit_events),
    }


def serialize_http_response_record(value: HttpResponseRecord) -> dict[str, object]:
    return _exchange_payload(value)


def _policy_payload(value: PublicPolicyReceipt) -> dict[str, object]:
    return _policy_fields_payload(
        command_id=value.command_id,
        need_index=value.need_index,
        source_candidate_index=value.source_candidate_index,
        source_kind=value.source_kind,
        requested_uri=value.requested_uri,
        final_uri=value.final_uri,
        credential_profile_name=value.credential_profile_name,
        license_evidence=value.license_evidence,
        cache_object=value.cache_object,
        validation=value.validation,
        missingness=value.missingness,
        exchanges=value.exchanges,
    )


def _policy_fields_payload(
    *,
    command_id: str,
    need_index: int,
    source_candidate_index: int,
    source_kind: Literal["public_api", "public_page", "crawl"],
    requested_uri: str,
    final_uri: str,
    credential_profile_name: str | None,
    license_evidence: str | None,
    cache_object: CacheObject,
    validation: ValidationReport,
    missingness: MissingnessReport,
    exchanges: tuple[HttpResponseRecord, ...],
) -> dict[str, object]:
    return {
        "command_id": command_id,
        "need_index": need_index,
        "source_candidate_index": source_candidate_index,
        "source_kind": source_kind,
        "requested_uri": requested_uri,
        "final_uri": final_uri,
        "credential_profile_name": credential_profile_name,
        "license_evidence": license_evidence,
        "cache_object": _cache_payload(cache_object),
        "validation": {
            "status": validation.status,
            "detail": validation.detail,
        },
        "missingness": {
            "status": missingness.status,
            "fraction": missingness.fraction,
            "detail": missingness.detail,
        },
        "exchanges": [_exchange_payload(item) for item in exchanges],
    }


def _attempt_payload(value: HttpAttemptReceipt) -> dict[str, object]:
    return {
        "command_id": value.command_id,
        "source_kind": value.source_kind,
        "requested_source_uri": value.requested_source_uri,
        "credential_profile_name": value.credential_profile_name,
        "license_evidence": value.license_evidence,
        "validation": {
            "status": value.validation.status,
            "detail": value.validation.detail,
        },
        "missingness": {
            "status": value.missingness.status,
            "fraction": value.missingness.fraction,
            "detail": value.missingness.detail,
        },
        "exchange": _exchange_payload(value.exchange),
    }


def make_attempt_receipt(
    *,
    command_id: str,
    source_kind: Literal["public_api", "public_page", "crawl"],
    requested_source_uri: str,
    credential_profile_name: str | None,
    license_evidence: str | None,
    validation: ValidationReport,
    missingness: MissingnessReport,
    exchange: HttpResponseRecord,
) -> HttpAttemptReceipt:
    fields = {
        "command_id": command_id,
        "source_kind": source_kind,
        "requested_source_uri": requested_source_uri,
        "credential_profile_name": credential_profile_name,
        "license_evidence": license_evidence,
        "validation": validation,
        "missingness": missingness,
        "exchange": exchange,
    }
    payload = {
        "command_id": command_id,
        "source_kind": source_kind,
        "requested_source_uri": requested_source_uri,
        "credential_profile_name": credential_profile_name,
        "license_evidence": license_evidence,
        "validation": {
            "status": validation.status,
            "detail": validation.detail,
        },
        "missingness": {
            "status": missingness.status,
            "fraction": missingness.fraction,
            "detail": missingness.detail,
        },
        "exchange": _exchange_payload(exchange),
    }
    return HttpAttemptReceipt(
        attempt_receipt_id=f"httpattempt_{content_digest(payload)[7:]}",
        **fields,
    )


def serialize_attempt_receipt(value: HttpAttemptReceipt) -> dict[str, object]:
    return {"attempt_receipt_id": value.attempt_receipt_id, **_attempt_payload(value)}


def make_policy_receipt(
    *,
    command_id: str,
    need_index: int,
    source_candidate_index: int,
    source_kind: Literal["public_api", "public_page", "crawl"],
    requested_uri: str,
    final_uri: str,
    credential_profile_name: str | None,
    license_evidence: str | None,
    cache_object: CacheObject,
    validation: ValidationReport,
    missingness: MissingnessReport,
    exchanges: Sequence[HttpResponseRecord],
) -> PublicPolicyReceipt:
    normalized = tuple(exchanges)
    payload = _policy_fields_payload(
        command_id=command_id,
        need_index=need_index,
        source_candidate_index=source_candidate_index,
        source_kind=source_kind,
        requested_uri=requested_uri,
        final_uri=final_uri,
        credential_profile_name=credential_profile_name,
        license_evidence=license_evidence,
        cache_object=cache_object,
        validation=validation,
        missingness=missingness,
        exchanges=normalized,
    )
    return PublicPolicyReceipt(
        policy_receipt_id=f"httppolicy_{content_digest(payload)[7:]}",
        command_id=command_id,
        need_index=need_index,
        source_candidate_index=source_candidate_index,
        source_kind=source_kind,
        requested_uri=requested_uri,
        final_uri=final_uri,
        credential_profile_name=credential_profile_name,
        license_evidence=license_evidence,
        cache_object=cache_object,
        validation=validation,
        missingness=missingness,
        exchanges=normalized,
    )


def serialize_policy_receipt(value: PublicPolicyReceipt) -> dict[str, object]:
    return {"policy_receipt_id": value.policy_receipt_id, **_policy_payload(value)}


class PolicyReceiptStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve() / "http_policy_receipts" / "sha256"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, policy_receipt_id: str) -> Path:
        if _POLICY_ID_RE.fullmatch(policy_receipt_id) is None:
            raise AcquisitionContractError("HTTP policy receipt id is invalid")
        return self.root / f"{policy_receipt_id.removeprefix('httppolicy_')}.json"

    def issue(self, receipt: PublicPolicyReceipt) -> None:
        payload = canonical_json_bytes(serialize_policy_receipt(receipt)) + b"\n"
        path = self._path(receipt.policy_receipt_id)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise AcquisitionConflictError("HTTP policy receipt conflict")
            temporary.unlink()
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except AcquisitionConflictError:
            if temporary.exists():
                temporary.unlink()
            raise
        except OSError as exc:
            if temporary.exists():
                temporary.unlink()
            raise AcquisitionStorageError(str(exc)) from exc

    def load(self, policy_receipt_id: str) -> PublicPolicyReceipt:
        path = self._path(policy_receipt_id)
        if not path.exists():
            raise AcquisitionConflictError("HTTP policy receipt was not issued")
        try:
            raw = path.read_bytes()
            document = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        try:
            receipt = _parse_policy_receipt(document)
        except (ValueError, TypeError) as exc:
            raise AcquisitionConflictError(str(exc)) from exc
        canonical = canonical_json_bytes(serialize_policy_receipt(receipt)) + b"\n"
        if receipt.policy_receipt_id != policy_receipt_id or raw != canonical:
            raise AcquisitionConflictError("HTTP policy receipt was modified")
        return receipt


class HttpAttemptReceiptStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve() / "http_attempt_receipts" / "sha256"
        self.root.mkdir(parents=True, exist_ok=True)

    def issue(self, receipt: HttpAttemptReceipt) -> None:
        payload = canonical_json_bytes(serialize_attempt_receipt(receipt)) + b"\n"
        path = self._path(receipt.attempt_receipt_id)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise AcquisitionConflictError("HTTP attempt receipt conflict")
            temporary.unlink()
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except AcquisitionConflictError:
            if temporary.exists():
                temporary.unlink()
            raise
        except OSError as exc:
            if temporary.exists():
                temporary.unlink()
            raise AcquisitionStorageError(str(exc)) from exc

    def load(self, attempt_receipt_id: str) -> HttpAttemptReceipt:
        path = self._path(attempt_receipt_id)
        if not path.exists():
            raise AcquisitionConflictError("HTTP attempt receipt was not issued")
        try:
            raw = path.read_bytes()
            receipt = _parse_attempt_receipt(json.loads(raw))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise AcquisitionConflictError(str(exc)) from exc
        if (
            receipt.attempt_receipt_id != attempt_receipt_id
            or raw != canonical_json_bytes(serialize_attempt_receipt(receipt)) + b"\n"
        ):
            raise AcquisitionConflictError("HTTP attempt receipt was modified")
        return receipt

    def _path(self, attempt_receipt_id: str) -> Path:
        if _ATTEMPT_ID_RE.fullmatch(attempt_receipt_id) is None:
            raise AcquisitionContractError("HTTP attempt receipt id is invalid")
        return self.root / f"{attempt_receipt_id.removeprefix('httpattempt_')}.json"


def _parse_policy_receipt(value: object) -> PublicPolicyReceipt:
    if not isinstance(value, Mapping):
        raise AcquisitionContractError("HTTP policy receipt must be an object")
    required = {
        "policy_receipt_id", "command_id", "need_index", "source_candidate_index",
        "source_kind", "requested_uri", "final_uri", "credential_profile_name",
        "license_evidence", "cache_object", "validation", "missingness", "exchanges",
    }
    if set(value) != required:
        raise AcquisitionContractError("HTTP policy receipt has an unsupported shape")
    cache = value["cache_object"]
    validation = value["validation"]
    missingness = value["missingness"]
    exchanges = value["exchanges"]
    if (
        not isinstance(cache, Mapping)
        or not isinstance(validation, Mapping)
        or not isinstance(missingness, Mapping)
        or not isinstance(exchanges, list)
    ):
        raise AcquisitionContractError("HTTP policy receipt reports are malformed")
    return PublicPolicyReceipt(
        policy_receipt_id=value["policy_receipt_id"],
        command_id=value["command_id"],
        need_index=value["need_index"],
        source_candidate_index=value["source_candidate_index"],
        source_kind=value["source_kind"],
        requested_uri=value["requested_uri"],
        final_uri=value["final_uri"],
        credential_profile_name=value["credential_profile_name"],
        license_evidence=value["license_evidence"],
        cache_object=CacheObject(**cache),
        validation=ValidationReport(**validation),
        missingness=MissingnessReport(**missingness),
        exchanges=tuple(_parse_exchange(item) for item in exchanges),
    )


def _parse_attempt_receipt(value: object) -> HttpAttemptReceipt:
    if not isinstance(value, Mapping) or set(value) != {
        "attempt_receipt_id", "command_id", "source_kind",
        "requested_source_uri", "credential_profile_name", "license_evidence",
        "validation", "missingness", "exchange",
    }:
        raise AcquisitionContractError("HTTP attempt receipt has an unsupported shape")
    validation = value["validation"]
    missingness = value["missingness"]
    if not isinstance(validation, Mapping) or not isinstance(missingness, Mapping):
        raise AcquisitionContractError("HTTP attempt reports are malformed")
    return HttpAttemptReceipt(
        attempt_receipt_id=value["attempt_receipt_id"],
        command_id=value["command_id"],
        source_kind=value["source_kind"],
        requested_source_uri=value["requested_source_uri"],
        credential_profile_name=value["credential_profile_name"],
        license_evidence=value["license_evidence"],
        validation=ValidationReport(**validation),
        missingness=MissingnessReport(**missingness),
        exchange=_parse_exchange(value["exchange"]),
    )


def _parse_exchange(value: object) -> HttpResponseRecord:
    if not isinstance(value, Mapping):
        raise AcquisitionContractError("HTTP response record must be an object")
    required = {
        "purpose", "uri", "retrieved_at", "status", "peer_ip", "headers",
        "byte_count", "content_sha256", "robots_decision", "rate_limit_events",
    }
    if set(value) != required:
        raise AcquisitionContractError("HTTP response record has an unsupported shape")
    headers = value["headers"]
    events = value["rate_limit_events"]
    if not isinstance(headers, list) or not isinstance(events, list):
        raise AcquisitionContractError("HTTP response metadata is malformed")
    return HttpResponseRecord(
        purpose=value["purpose"],
        uri=value["uri"],
        retrieved_at=value["retrieved_at"],
        status=value["status"],
        peer_ip=value["peer_ip"],
        headers=tuple(tuple(item) for item in headers),
        byte_count=value["byte_count"],
        content_sha256=value["content_sha256"],
        robots_decision=value["robots_decision"],
        rate_limit_events=tuple(events),
    )


def parse_http_response_record(value: object) -> HttpResponseRecord:
    return _parse_exchange(value)
