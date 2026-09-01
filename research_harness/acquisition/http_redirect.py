from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from .cache import AcquisitionStorageError
from .http_policy import HttpResponse
from .http_receipt import (
    HttpResponseRecord,
    parse_http_response_record,
    serialize_http_response_record,
)
from .model import (
    AcquisitionConflictError,
    AcquisitionContractError,
    canonical_json_bytes,
    content_digest,
)


@dataclass(frozen=True, slots=True)
class RedirectReplay:
    replay_id: str
    command_id: str
    purpose: Literal["robots", "content"]
    source_uri: str
    request_uri: str
    response: HttpResponse
    record: HttpResponseRecord

    def __post_init__(self) -> None:
        if not isinstance(self.replay_id, str) or not self.replay_id.startswith(
            "redirect_"
        ):
            raise AcquisitionContractError("redirect replay id is invalid")
        if self.purpose not in {"robots", "content"}:
            raise AcquisitionContractError("redirect replay purpose is invalid")
        if self.response.status not in {301, 302, 303, 307, 308}:
            raise AcquisitionContractError("redirect replay response is not a redirect")
        if (
            self.record.purpose != self.purpose
            or self.record.uri != self.request_uri
            or self.record.status != self.response.status
            or self.record.peer_ip != self.response.peer_ip
            or self.record.byte_count != len(self.response.body)
            or self.record.content_sha256
            != f"sha256:{hashlib.sha256(self.response.body).hexdigest()}"
        ):
            raise AcquisitionContractError("redirect replay record does not match response")
        expected = f"redirect_{content_digest(_replay_payload(self))[7:]}"
        if self.replay_id != expected:
            raise AcquisitionContractError("redirect replay identity changed")


class RedirectReplayStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve() / "http_redirect_replays"
        self.root.mkdir(parents=True, exist_ok=True)

    def load(
        self,
        *,
        command_id: str,
        purpose: Literal["robots", "content"],
        source_uri: str,
        request_uri: str,
    ) -> RedirectReplay | None:
        path = self._path(command_id, purpose, source_uri, request_uri)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            replay = _parse_replay(json.loads(raw))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise AcquisitionConflictError(str(exc)) from exc
        if (
            replay.command_id != command_id
            or replay.purpose != purpose
            or replay.source_uri != source_uri
            or replay.request_uri != request_uri
            or raw != canonical_json_bytes(_serialize_replay(replay)) + b"\n"
        ):
            raise AcquisitionConflictError("redirect replay was modified")
        return replay

    def issue(self, replay: RedirectReplay) -> None:
        path = self._path(
            replay.command_id,
            replay.purpose,
            replay.source_uri,
            replay.request_uri,
        )
        payload = canonical_json_bytes(_serialize_replay(replay)) + b"\n"
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
                    raise AcquisitionConflictError("redirect replay conflict")
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

    def _path(
        self,
        command_id: str,
        purpose: Literal["robots", "content"],
        source_uri: str,
        request_uri: str,
    ) -> Path:
        key = f"{command_id}\0{purpose}\0{source_uri}\0{request_uri}".encode()
        return self.root / f"{hashlib.sha256(key).hexdigest()}.json"


def make_redirect_replay(
    *,
    command_id: str,
    purpose: Literal["robots", "content"],
    source_uri: str,
    request_uri: str,
    response: HttpResponse,
    record: HttpResponseRecord,
) -> RedirectReplay:
    fields = {
        "command_id": command_id,
        "purpose": purpose,
        "source_uri": source_uri,
        "request_uri": request_uri,
        "response": response,
        "record": record,
    }
    payload = _replay_fields_payload(**fields)
    return RedirectReplay(
        replay_id=f"redirect_{content_digest(payload)[7:]}",
        **fields,
    )


def _replay_payload(value: RedirectReplay) -> dict[str, object]:
    return _replay_fields_payload(
        command_id=value.command_id,
        purpose=value.purpose,
        source_uri=value.source_uri,
        request_uri=value.request_uri,
        response=value.response,
        record=value.record,
    )


def _replay_fields_payload(
    *,
    command_id: str,
    purpose: Literal["robots", "content"],
    source_uri: str,
    request_uri: str,
    response: HttpResponse,
    record: HttpResponseRecord,
) -> dict[str, object]:
    return {
        "command_id": command_id,
        "purpose": purpose,
        "source_uri": source_uri,
        "request_uri": request_uri,
        "response": {
            "status": response.status,
            "peer_ip": response.peer_ip,
            "headers": [list(item) for item in response.headers],
            "body_base64": base64.b64encode(response.body).decode("ascii"),
        },
        "record": serialize_http_response_record(record),
    }


def _serialize_replay(value: RedirectReplay) -> dict[str, object]:
    return {"replay_id": value.replay_id, **_replay_payload(value)}


def _parse_replay(value: object) -> RedirectReplay:
    if not isinstance(value, Mapping) or set(value) != {
        "replay_id", "command_id", "purpose", "source_uri", "request_uri",
        "response", "record",
    }:
        raise AcquisitionContractError("redirect replay has an unsupported shape")
    response = value["response"]
    if not isinstance(response, Mapping) or set(response) != {
        "status", "peer_ip", "headers", "body_base64",
    }:
        raise AcquisitionContractError("redirect replay response is malformed")
    headers = response["headers"]
    if not isinstance(headers, list):
        raise AcquisitionContractError("redirect replay headers are malformed")
    return RedirectReplay(
        replay_id=value["replay_id"],
        command_id=value["command_id"],
        purpose=value["purpose"],
        source_uri=value["source_uri"],
        request_uri=value["request_uri"],
        response=HttpResponse(
            status=response["status"],
            peer_ip=response["peer_ip"],
            headers=tuple(tuple(item) for item in headers),
            body=base64.b64decode(response["body_base64"], validate=True),
        ),
        record=parse_http_response_record(value["record"]),
    )
