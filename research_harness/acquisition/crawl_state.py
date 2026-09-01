from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Mapping
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from .cache import AcquisitionStorageError
from .http_policy import canonical_http_url
from .http_receipt import (
    HttpResponseRecord,
    parse_http_response_record,
    serialize_http_response_record,
)
from .model import (
    AcquisitionConflictError,
    AcquisitionContractError,
    CacheObject,
    canonical_json_bytes,
)


@dataclass(frozen=True, slots=True)
class PendingCrawlPage:
    uri: str
    depth: int

    def __post_init__(self) -> None:
        if canonical_http_url(self.uri) != self.uri:
            raise AcquisitionContractError("pending crawl URI is not canonical")
        if (
            isinstance(self.depth, bool)
            or not isinstance(self.depth, int)
            or self.depth < 0
        ):
            raise AcquisitionContractError("pending crawl depth is invalid")


@dataclass(frozen=True, slots=True)
class CrawledPage:
    requested_uri: str
    final_uri: str
    depth: int
    retrieved_at: str
    content_type: str
    robots_decision: str
    license_evidence: str | None
    cache_object: CacheObject

    def __post_init__(self) -> None:
        if canonical_http_url(self.requested_uri) != self.requested_uri:
            raise AcquisitionContractError("crawl request URI is not canonical")
        if canonical_http_url(self.final_uri) != self.final_uri:
            raise AcquisitionContractError("crawl final URI is not canonical")
        if (
            isinstance(self.depth, bool)
            or not isinstance(self.depth, int)
            or self.depth < 0
        ):
            raise AcquisitionContractError("crawled page depth is invalid")
        if not isinstance(self.retrieved_at, str) or not self.retrieved_at:
            raise AcquisitionContractError("crawled page retrieval time is invalid")
        if not isinstance(self.content_type, str) or not self.content_type:
            raise AcquisitionContractError("crawled page media type is invalid")
        if self.robots_decision not in {"allowed", "unavailable"}:
            raise AcquisitionContractError("crawled page robots decision is invalid")
        if self.license_evidence is not None and (
            not isinstance(self.license_evidence, str) or not self.license_evidence
        ):
            raise AcquisitionContractError("crawled page license evidence is invalid")
        if not isinstance(self.cache_object, CacheObject):
            raise AcquisitionContractError("crawled page cache object is invalid")


@dataclass(frozen=True, slots=True)
class CrawlProgress:
    command_id: str
    need_index: int
    source_candidate_index: int
    source_uri: str
    pending: tuple[PendingCrawlPage, ...]
    visited: tuple[str, ...]
    pages: tuple[CrawledPage, ...]
    exchanges: tuple[HttpResponseRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not self.command_id:
            raise AcquisitionContractError("crawl command identity is invalid")
        for value, label in (
            (self.need_index, "crawl need index"),
            (self.source_candidate_index, "crawl source candidate index"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AcquisitionContractError(f"{label} is invalid")
        if canonical_http_url(self.source_uri) != self.source_uri:
            raise AcquisitionContractError("crawl source URI is not canonical")
        if not isinstance(self.pending, tuple) or not isinstance(self.pages, tuple):
            raise AcquisitionContractError("crawl progress collections must be immutable")
        if not isinstance(self.visited, tuple) or any(
            canonical_http_url(uri) != uri for uri in self.visited
        ):
            raise AcquisitionContractError("crawl visited URIs are invalid")
        if len(set(self.visited)) != len(self.visited):
            raise AcquisitionContractError("crawl visited URIs are duplicated")
        pending_uris = tuple(item.uri for item in self.pending)
        if len(set(pending_uris)) != len(pending_uris):
            raise AcquisitionContractError("crawl pending URIs are duplicated")
        if set(pending_uris) & set(self.visited):
            raise AcquisitionContractError("crawl URI cannot be pending and visited")
        if any(page.requested_uri not in self.visited for page in self.pages):
            raise AcquisitionContractError("crawled pages must be marked visited")
        origin = _origin(self.source_uri)
        all_uris = (
            tuple(item.uri for item in self.pending)
            + self.visited
            + tuple(page.final_uri for page in self.pages)
        )
        if any(_origin(uri) != origin for uri in all_uris):
            raise AcquisitionContractError("crawl progress crossed its source origin")
        if not isinstance(self.exchanges, tuple) or any(
            not isinstance(item, HttpResponseRecord) for item in self.exchanges
        ):
            raise AcquisitionContractError("crawl exchanges are invalid")


class CrawlProgressStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve() / "crawl_progress"
        self.root.mkdir(parents=True, exist_ok=True)

    def load_or_create(
        self,
        *,
        command_id: str,
        need_index: int,
        source_candidate_index: int,
        source_uri: str,
    ) -> CrawlProgress:
        path = self._path(command_id, need_index, source_candidate_index)
        if not path.exists():
            return CrawlProgress(
                command_id=command_id,
                need_index=need_index,
                source_candidate_index=source_candidate_index,
                source_uri=source_uri,
                pending=(PendingCrawlPage(source_uri, 0),),
                visited=(),
                pages=(),
                exchanges=(),
            )
        try:
            raw = path.read_bytes()
            progress = _parse_progress(json.loads(raw))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise AcquisitionConflictError(str(exc)) from exc
        if raw != canonical_json_bytes(_serialize_progress(progress)) + b"\n":
            raise AcquisitionConflictError("crawl progress encoding was modified")
        if (
            progress.command_id != command_id
            or progress.need_index != need_index
            or progress.source_candidate_index != source_candidate_index
            or progress.source_uri != source_uri
        ):
            raise AcquisitionConflictError("crawl progress belongs to another source")
        return progress

    def save(self, progress: CrawlProgress) -> None:
        path = self._path(
            progress.command_id,
            progress.need_index,
            progress.source_candidate_index,
        )
        payload = canonical_json_bytes(_serialize_progress(progress)) + b"\n"
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            if temporary.exists():
                temporary.unlink()
            raise AcquisitionStorageError(str(exc)) from exc

    def _path(
        self,
        command_id: str,
        need_index: int,
        source_candidate_index: int,
    ) -> Path:
        key = f"{command_id}\0{need_index}\0{source_candidate_index}".encode()
        return self.root / f"{hashlib.sha256(key).hexdigest()}.json"


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() not in {"a", "area"}:
            return
        values = {name.lower(): value for name, value in attrs}
        rel = (values.get("rel") or "").lower().split()
        href = values.get("href")
        if href and "nofollow" not in rel:
            self.links.append(href)


def extract_same_origin_links(base_uri: str, body: bytes) -> tuple[str, ...]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")
    parser = _LinkParser()
    parser.feed(text)
    origin = _origin(base_uri)
    links: set[str] = set()
    for href in parser.links:
        without_fragment, _ = urldefrag(urljoin(base_uri, href))
        try:
            candidate = canonical_http_url(without_fragment)
        except ValueError:
            continue
        parts = urlsplit(candidate)
        if _origin(candidate) == origin and not parts.query:
            links.add(candidate)
    return tuple(sorted(links))


def _origin(uri: str) -> str:
    parts = urlsplit(uri)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _cache_payload(value: CacheObject) -> dict[str, object]:
    return {
        "content_sha256": value.content_sha256,
        "size_bytes": value.size_bytes,
        "entry_count": value.entry_count,
        "object_kind": value.object_kind,
    }


def _serialize_progress(value: CrawlProgress) -> dict[str, object]:
    return {
        "command_id": value.command_id,
        "need_index": value.need_index,
        "source_candidate_index": value.source_candidate_index,
        "source_uri": value.source_uri,
        "pending": [
            {"uri": item.uri, "depth": item.depth} for item in value.pending
        ],
        "visited": list(value.visited),
        "pages": [
            {
                "requested_uri": item.requested_uri,
                "final_uri": item.final_uri,
                "depth": item.depth,
                "retrieved_at": item.retrieved_at,
                "content_type": item.content_type,
                "robots_decision": item.robots_decision,
                "license_evidence": item.license_evidence,
                "cache_object": _cache_payload(item.cache_object),
            }
            for item in value.pages
        ],
        "exchanges": [
            serialize_http_response_record(item) for item in value.exchanges
        ],
    }


def _parse_progress(value: object) -> CrawlProgress:
    if not isinstance(value, Mapping) or set(value) != {
        "command_id", "need_index", "source_candidate_index", "source_uri",
        "pending", "visited", "pages", "exchanges",
    }:
        raise AcquisitionContractError("crawl progress has an unsupported shape")
    pending = value["pending"]
    visited = value["visited"]
    pages = value["pages"]
    exchanges = value["exchanges"]
    if not all(isinstance(item, list) for item in (pending, visited, pages, exchanges)):
        raise AcquisitionContractError("crawl progress collections are malformed")
    return CrawlProgress(
        command_id=value["command_id"],
        need_index=value["need_index"],
        source_candidate_index=value["source_candidate_index"],
        source_uri=value["source_uri"],
        pending=tuple(PendingCrawlPage(**item) for item in pending),
        visited=tuple(visited),
        pages=tuple(_parse_page(item) for item in pages),
        exchanges=tuple(parse_http_response_record(item) for item in exchanges),
    )


def _parse_page(value: object) -> CrawledPage:
    if not isinstance(value, Mapping) or set(value) != {
        "requested_uri", "final_uri", "depth", "retrieved_at", "content_type",
        "robots_decision", "license_evidence", "cache_object",
    }:
        raise AcquisitionContractError("crawled page has an unsupported shape")
    cache = value["cache_object"]
    if not isinstance(cache, Mapping):
        raise AcquisitionContractError("crawled page cache object is malformed")
    return CrawledPage(
        requested_uri=value["requested_uri"],
        final_uri=value["final_uri"],
        depth=value["depth"],
        retrieved_at=value["retrieved_at"],
        content_type=value["content_type"],
        robots_decision=value["robots_decision"],
        license_evidence=value["license_evidence"],
        cache_object=CacheObject(**cache),
    )
