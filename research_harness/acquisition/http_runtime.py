from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import BinaryIO, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from research_harness.orchestrator.blind_reorientation import (
    CheckpointReason,
    HardExternalBlockCode,
)

from .cache import (
    AcquisitionDeadlineExceeded,
    AcquisitionDownloadExceeded,
    AcquisitionStorageError,
    ContentAddressedCache,
)
from .crawl_state import (
    CrawlProgress,
    CrawlProgressStore,
    CrawledPage,
    PendingCrawlPage,
    extract_same_origin_links,
)
from .http_policy import (
    MAX_REDIRECTS,
    ROBOTS_PARSE_LIMIT_BYTES,
    DnsResolver,
    HttpPolicyError,
    HttpRequest,
    HttpResponse,
    OneHopTransport,
    ResponseLimitError,
    RobotsDecision,
    canonical_http_url,
    require_bounded_response,
    resolve_public_endpoint,
    resolve_redirect,
    robots_response_decision,
    validate_public_peer,
)
from .http_receipt import (
    HttpAttemptReceiptStore,
    HttpResponseRecord,
    PolicyReceiptStore,
    make_attempt_receipt,
    make_policy_receipt,
)
from .http_redirect import RedirectReplayStore, make_redirect_replay
from .live_http import CredentialProfileUnavailable
from .model import (
    AcquiredNeed,
    AcquisitionCommand,
    AcquisitionConflictError,
    CacheObject,
    MissingnessReport,
    PublicSource,
    ValidationReport,
    make_response_receipt,
)


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CRAWL_ARCHIVE_PREFIX = b'{"format":"research-harness-crawl-v1","pages":['
_CRAWL_PAGE_PREFIX = b'{"body_base64":"'
_RECEIPT_HEADERS = frozenset(
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


class AcquisitionClock(Protocol):
    def monotonic(self) -> float: ...

    def epoch(self) -> float: ...

    def iso_now(self) -> str: ...


class SystemAcquisitionClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def epoch(self) -> float:
        return time.time()

    def iso_now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class HttpStepComplete:
    acquired: AcquiredNeed
    requests_used: int
    download_bytes_used: int


@dataclass(frozen=True, slots=True)
class HttpStepCheckpoint:
    reason: CheckpointReason
    requests_used: int
    download_bytes_used: int


@dataclass(frozen=True, slots=True)
class HttpStepRejected:
    code: HardExternalBlockCode
    required_external_action: str
    requests_used: int
    download_bytes_used: int


HttpStepResult = HttpStepComplete | HttpStepCheckpoint | HttpStepRejected


@dataclass(slots=True)
class _Usage:
    request_limit: int
    download_limit: int
    requests: int = 0
    download_bytes: int = 0


class _CheckpointSignal(Exception):
    def __init__(self, reason: CheckpointReason) -> None:
        self.reason = reason


class _RejectedSignal(Exception):
    def __init__(
        self,
        code: HardExternalBlockCode,
        required_external_action: str,
    ) -> None:
        self.code = code
        self.required_external_action = required_external_action


@dataclass(frozen=True, slots=True)
class _RobotsCacheEntry:
    origin: str
    fetched_at_epoch: float
    response: HttpResponse
    records: tuple[HttpResponseRecord, ...]


class _RobotsPolicyStore:
    def __init__(self, root: Path, clock: AcquisitionClock) -> None:
        self.root = root.resolve() / "robots_cache"
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock

    def _path(self, origin: str) -> Path:
        digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def load(self, origin: str) -> _RobotsCacheEntry | None:
        path = self._path(origin)
        if not path.exists():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            entry = _parse_robots_cache(document)
        except OSError as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise AcquisitionConflictError("robots cache was modified") from exc
        age = self.clock.epoch() - entry.fetched_at_epoch
        if entry.origin != origin or age < 0 or age > 24 * 60 * 60:
            return None
        return entry

    def store(self, entry: _RobotsCacheEntry) -> None:
        path = self._path(entry.origin)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(
            _serialize_robots_cache(entry),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
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


class _RateLimitStore:
    def __init__(self, root: Path, clock: AcquisitionClock) -> None:
        self.root = root.resolve() / "http_rate_limits"
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock

    def limited(self, origin: str) -> bool:
        path = self._path(origin)
        if not path.exists():
            return False
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"origin", "not_before_epoch"}
            or value["origin"] != origin
            or isinstance(value["not_before_epoch"], bool)
            or not isinstance(value["not_before_epoch"], (int, float))
            or not math.isfinite(value["not_before_epoch"])
        ):
            raise AcquisitionStorageError("HTTP rate-limit state is malformed")
        return self.clock.epoch() < value["not_before_epoch"]

    def defer(self, origin: str, not_before_epoch: float) -> None:
        if not math.isfinite(not_before_epoch):
            return
        path = self._path(origin)
        existing = self._not_before(path, origin)
        value = {
            "origin": origin,
            "not_before_epoch": max(existing or 0.0, not_before_epoch),
        }
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
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

    def _not_before(self, path: Path, origin: str) -> float | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        if (
            not isinstance(value, dict)
            or value.get("origin") != origin
            or isinstance(value.get("not_before_epoch"), bool)
            or not isinstance(value.get("not_before_epoch"), (int, float))
        ):
            raise AcquisitionStorageError("HTTP rate-limit state is malformed")
        return float(value["not_before_epoch"])

    def _path(self, origin: str) -> Path:
        digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"


def _serialize_robots_cache(entry: _RobotsCacheEntry) -> dict[str, object]:
    payload = {
        "origin": entry.origin,
        "fetched_at_epoch": entry.fetched_at_epoch,
        "response": {
            "status": entry.response.status,
            "peer_ip": entry.response.peer_ip,
            "headers": [list(item) for item in entry.response.headers],
            "body_base64": base64.b64encode(entry.response.body).decode("ascii"),
        },
        "records": [
            {
                "purpose": item.purpose,
                "uri": item.uri,
                "retrieved_at": item.retrieved_at,
                "status": item.status,
                "peer_ip": item.peer_ip,
                "headers": [list(header) for header in item.headers],
                "byte_count": item.byte_count,
                "content_sha256": item.content_sha256,
                "robots_decision": item.robots_decision,
                "rate_limit_events": list(item.rate_limit_events),
            }
            for item in entry.records
        ],
    }
    return {
        "cache_sha256": f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}",
        **payload,
    }


def _parse_robots_cache(value: object) -> _RobotsCacheEntry:
    if not isinstance(value, dict) or set(value) != {
        "cache_sha256", "origin", "fetched_at_epoch", "response", "records"
    }:
        raise ValueError("robots cache has an unsupported shape")
    payload = {key: item for key, item in value.items() if key != "cache_sha256"}
    expected_digest = f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"
    if value["cache_sha256"] != expected_digest:
        raise ValueError("robots cache checksum changed")
    if (
        not isinstance(value["fetched_at_epoch"], (int, float))
        or isinstance(value["fetched_at_epoch"], bool)
        or not math.isfinite(value["fetched_at_epoch"])
    ):
        raise ValueError("robots cache time is invalid")
    response = value["response"]
    records = value["records"]
    if not isinstance(response, dict) or not isinstance(records, list):
        raise ValueError("robots cache is malformed")
    if set(response) != {"status", "peer_ip", "headers", "body_base64"}:
        raise ValueError("robots response cache is malformed")
    headers = response["headers"]
    if not isinstance(headers, list):
        raise ValueError("robots response headers are malformed")
    return _RobotsCacheEntry(
        origin=value["origin"],
        fetched_at_epoch=value["fetched_at_epoch"],
        response=HttpResponse(
            status=response["status"],
            peer_ip=response["peer_ip"],
            headers=tuple(tuple(item) for item in headers),
            body=base64.b64decode(response["body_base64"], validate=True),
        ),
        records=tuple(_parse_cached_record(item) for item in records),
    )


def _parse_cached_record(value: object) -> HttpResponseRecord:
    if not isinstance(value, dict):
        raise ValueError("robots response record is malformed")
    return HttpResponseRecord(
        purpose=value["purpose"],
        uri=value["uri"],
        retrieved_at=value["retrieved_at"],
        status=value["status"],
        peer_ip=value["peer_ip"],
        headers=tuple(tuple(item) for item in value["headers"]),
        byte_count=value["byte_count"],
        content_sha256=value["content_sha256"],
        robots_decision=value["robots_decision"],
        rate_limit_events=tuple(value["rate_limit_events"]),
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class HttpAcquirer:
    def __init__(
        self,
        *,
        transport: OneHopTransport,
        resolver: DnsResolver,
        cache: ContentAddressedCache,
        cache_root: Path,
        policy_receipts: PolicyReceiptStore,
        clock: AcquisitionClock,
        user_agent: str,
    ) -> None:
        self.transport = transport
        self.resolver = resolver
        self.cache = cache
        self.policy_receipts = policy_receipts
        self.attempt_receipts = HttpAttemptReceiptStore(cache_root)
        self.clock = clock
        self.user_agent = user_agent
        self.robots_store = _RobotsPolicyStore(cache_root, clock)
        self.rate_limits = _RateLimitStore(cache_root, clock)
        self.redirect_replays = RedirectReplayStore(cache_root)
        self.crawl_store = CrawlProgressStore(cache_root)

    def acquire(
        self,
        *,
        command: AcquisitionCommand,
        need_index: int,
        source_candidate_index: int,
        source: PublicSource,
        deadline: float,
        request_budget: int,
        download_budget: int,
    ) -> HttpStepResult:
        usage = _Usage(request_budget, download_budget)
        exchanges: list[HttpResponseRecord] = []
        robots: dict[str, tuple[HttpResponse, tuple[HttpResponseRecord, ...]]] = {}
        try:
            if source.kind == "crawl":
                return self._acquire_crawl(
                    command=command,
                    need_index=need_index,
                    source_candidate_index=source_candidate_index,
                    source=source,
                    deadline=deadline,
                    usage=usage,
                )
            requested_uri = canonical_http_url(source.uri)
            final_uri, response, final_robots = self._fetch_content(
                requested_uri,
                source=source,
                command=command,
                deadline=deadline,
                usage=usage,
                exchanges=exchanges,
                robots=robots,
            )
            cache_object = self.cache.put_stream(
                io.BytesIO(response.body),
                max_bytes=len(response.body),
                deadline=deadline,
            )
            validation, missingness = _validate_response(
                source,
                response,
            )
            license_evidence = _license_evidence(source, response)
            return self._complete(
                command=command,
                need_index=need_index,
                source_candidate_index=source_candidate_index,
                source=source,
                final_uri=final_uri,
                retrieved_at=exchanges[-1].retrieved_at,
                robots_decision=final_robots,
                cache_object=cache_object,
                validation=validation,
                missingness=missingness,
                license_evidence=license_evidence,
                exchanges=exchanges,
                usage=usage,
            )
        except _CheckpointSignal as signal:
            return HttpStepCheckpoint(
                signal.reason,
                usage.requests,
                usage.download_bytes,
            )
        except _RejectedSignal as signal:
            return HttpStepRejected(
                signal.code,
                signal.required_external_action,
                usage.requests,
                usage.download_bytes,
            )
        except AcquisitionDeadlineExceeded:
            return HttpStepCheckpoint(
                "time_budget",
                usage.requests,
                usage.download_bytes,
            )
        except AcquisitionDownloadExceeded:
            return HttpStepCheckpoint(
                "download_budget",
                usage.requests,
                usage.download_bytes,
            )
        except AcquisitionStorageError:
            return HttpStepRejected(
                HardExternalBlockCode.STORAGE_UNAVAILABLE,
                "Restore writable durable acquisition storage",
                usage.requests,
                usage.download_bytes,
            )
        except HttpPolicyError as exc:
            return HttpStepRejected(
                HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                str(exc),
                usage.requests,
                usage.download_bytes,
            )

    def _complete(
        self,
        *,
        command: AcquisitionCommand,
        need_index: int,
        source_candidate_index: int,
        source: PublicSource,
        final_uri: str,
        retrieved_at: str,
        robots_decision: RobotsDecision,
        cache_object: CacheObject,
        validation: ValidationReport,
        missingness: MissingnessReport,
        license_evidence: str | None,
        exchanges: list[HttpResponseRecord],
        usage: _Usage,
    ) -> HttpStepComplete:
        policy = make_policy_receipt(
            command_id=command.command_id,
            need_index=need_index,
            source_candidate_index=source_candidate_index,
            source_kind=source.kind,
            requested_uri=source.uri,
            final_uri=final_uri,
            credential_profile_name=source.credential_profile_name,
            license_evidence=license_evidence,
            cache_object=cache_object,
            validation=validation,
            missingness=missingness,
            exchanges=exchanges,
        )
        self.policy_receipts.issue(policy)
        rate_events = tuple(
            event for exchange in exchanges for event in exchange.rate_limit_events
        )
        receipt = make_response_receipt(
            source_kind=source.kind,
            source_uri=final_uri,
            retrieved_at=retrieved_at,
            robots_decision=robots_decision.status,
            rate_limit_events=rate_events,
            credential_profile_name=source.credential_profile_name,
            adapter_id=None,
            snapshot_id=None,
            provenance=f"public_http:{source.uri}",
            policy_receipt_id=policy.policy_receipt_id,
            license_evidence=license_evidence,
            cache_object=cache_object,
            validation=validation,
            missingness=missingness,
        )
        return HttpStepComplete(
            acquired=AcquiredNeed(
                need_index=need_index,
                source_candidate_index=source_candidate_index,
                description=command.needs[need_index].need.description,
                receipt=receipt,
            ),
            requests_used=usage.requests,
            download_bytes_used=usage.download_bytes,
        )

    def _acquire_crawl(
        self,
        *,
        command: AcquisitionCommand,
        need_index: int,
        source_candidate_index: int,
        source: PublicSource,
        deadline: float,
        usage: _Usage,
    ) -> HttpStepComplete:
        requested_uri = canonical_http_url(source.uri)
        progress = self.crawl_store.load_or_create(
            command_id=command.command_id,
            need_index=need_index,
            source_candidate_index=source_candidate_index,
            source_uri=requested_uri,
        )
        if sum(page.cache_object.size_bytes for page in progress.pages) > (
            source.crawl_max_total_bytes
        ):
            raise AcquisitionConflictError("crawl progress exceeds its total byte bound")
        exchanges = list(progress.exchanges)
        robots: dict[str, tuple[HttpResponse, tuple[HttpResponseRecord, ...]]] = {}
        while progress.pending and len(progress.pages) < source.crawl_max_pages:
            archived_bytes = sum(
                page.cache_object.size_bytes for page in progress.pages
            )
            remaining_archive_bytes = source.crawl_max_total_bytes - archived_bytes
            if remaining_archive_bytes <= 0:
                break
            pending = progress.pending[0]
            try:
                final_uri, response, decision = self._fetch_content(
                    pending.uri,
                    source=source,
                    command=command,
                    deadline=deadline,
                    usage=usage,
                    exchanges=exchanges,
                    robots=robots,
                    response_cap=remaining_archive_bytes,
                )
                _validate_response(source, response)
            except _CheckpointSignal:
                self.crawl_store.save(
                    replace(progress, exchanges=tuple(exchanges))
                )
                raise
            except _RejectedSignal:
                if not progress.pages:
                    self.crawl_store.save(
                        replace(progress, exchanges=tuple(exchanges))
                    )
                    raise
                progress = replace(
                    progress,
                    pending=progress.pending[1:],
                    visited=_append_unique(progress.visited, pending.uri),
                    exchanges=tuple(exchanges),
                )
                self.crawl_store.save(progress)
                continue
            page_cache = self.cache.put_stream(
                io.BytesIO(response.body),
                max_bytes=len(response.body),
                deadline=deadline,
            )
            discovered = (
                extract_same_origin_links(final_uri, response.body)
                if pending.depth < source.crawl_max_depth
                else ()
            )
            visited = _append_unique(progress.visited, pending.uri, final_uri)
            pending_uris = {item.uri for item in progress.pending[1:]}
            additions = tuple(
                PendingCrawlPage(uri, pending.depth + 1)
                for uri in discovered
                if uri not in visited and uri not in pending_uris
            )
            content_record = next(
                item
                for item in reversed(exchanges)
                if item.purpose == "content" and item.uri == final_uri
            )
            page = CrawledPage(
                requested_uri=pending.uri,
                final_uri=final_uri,
                depth=pending.depth,
                retrieved_at=content_record.retrieved_at,
                content_type=response.header("content-type") or "text/html",
                robots_decision=decision.status,
                license_evidence=_license_evidence(source, response),
                cache_object=page_cache,
            )
            progress = replace(
                progress,
                pending=progress.pending[1:] + additions,
                visited=visited,
                pages=progress.pages + (page,),
                exchanges=tuple(exchanges),
            )
            self.crawl_store.save(progress)
        if not progress.pages:
            raise _RejectedSignal(
                HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                "crawl produced no lawful HTML pages",
            )
        archive_limit = _crawl_archive_size(progress)
        archive = _crawl_archive(
            progress,
            self.cache,
            deadline,
            self.clock,
            archive_limit,
        )
        try:
            cache_object = self.cache.put_stream(
                archive,
                max_bytes=archive_limit,
                deadline=deadline,
            )
        finally:
            archive.close()
        validation = ValidationReport(
            "passed",
            f"crawl archived {len(progress.pages)} robots-compliant HTML pages",
        )
        missingness = MissingnessReport(
            "not_evaluated",
            None,
            "HTML crawl has no declared missingness rule",
        )
        root = progress.pages[0]
        license_evidence = source.license_evidence or root.license_evidence
        return self._complete(
            command=command,
            need_index=need_index,
            source_candidate_index=source_candidate_index,
            source=source,
            final_uri=root.final_uri,
            retrieved_at=progress.pages[-1].retrieved_at,
            robots_decision=RobotsDecision(root.robots_decision),
            cache_object=cache_object,
            validation=validation,
            missingness=missingness,
            license_evidence=license_evidence,
            exchanges=list(progress.exchanges),
            usage=usage,
        )

    def _fetch_content(
        self,
        uri: str,
        *,
        source: PublicSource,
        command: AcquisitionCommand,
        deadline: float,
        usage: _Usage,
        exchanges: list[HttpResponseRecord],
        robots: dict[str, tuple[HttpResponse, tuple[HttpResponseRecord, ...]]],
        response_cap: int | None = None,
    ) -> tuple[str, HttpResponse, RobotsDecision]:
        current = uri
        for redirect_count in range(MAX_REDIRECTS + 1):
            decision, robot_records = self._robots_for(
                current,
                source=source,
                command=command,
                deadline=deadline,
                usage=usage,
                robots=robots,
            )
            for record in robot_records:
                if record not in exchanges:
                    exchanges.append(record)
            if not decision.allowed:
                raise _RejectedSignal(
                    HardExternalBlockCode.LEGAL_ACCESS_DENIED,
                    f"robots policy denies {current}",
                )
            response, record = self._send_with_redirect_replay(
                current,
                purpose="content",
                robots_decision=decision,
                source=source,
                command=command,
                deadline=deadline,
                usage=usage,
                response_cap=response_cap,
            )
            exchanges.append(record)
            if response.status in _REDIRECT_STATUSES:
                if redirect_count == MAX_REDIRECTS:
                    raise _RejectedSignal(
                        HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                        "HTTP redirect limit exceeded",
                    )
                location = response.header("location")
                if location is None:
                    raise _RejectedSignal(
                        HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                        "HTTP redirect has no Location header",
                    )
                redirected = resolve_redirect(current, location)
                if source.kind == "crawl" and _origin(redirected) != _origin(current):
                    raise _RejectedSignal(
                        HardExternalBlockCode.LEGAL_ACCESS_DENIED,
                        "Crawling cannot cross an origin boundary",
                    )
                if (
                    source.credential_profile_name is not None
                    and _origin(redirected) != _origin(current)
                ):
                    raise _RejectedSignal(
                        HardExternalBlockCode.AUTH_REQUIRED,
                        "Credentialed HTTP sources cannot redirect across origins",
                    )
                current = redirected
                continue
            if 200 <= response.status <= 299:
                return current, response, decision
            if response.status in {401, 403}:
                raise _RejectedSignal(
                    HardExternalBlockCode.AUTH_REQUIRED,
                    "Configure an existing credential profile for the public source",
                )
            if response.status == 429:
                raise _CheckpointSignal("rate_limited")
            if 500 <= response.status <= 599:
                raise _CheckpointSignal("transient_network")
            raise _RejectedSignal(
                HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                f"public source returned HTTP {response.status}",
            )
        raise AssertionError("redirect loop did not terminate")

    def _robots_for(
        self,
        target_uri: str,
        *,
        source: PublicSource,
        command: AcquisitionCommand,
        deadline: float,
        usage: _Usage,
        robots: dict[str, tuple[HttpResponse, tuple[HttpResponseRecord, ...]]],
    ) -> tuple[RobotsDecision, tuple[HttpResponseRecord, ...]]:
        parts = urlsplit(target_uri)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        cached = robots.get(origin)
        if cached is None:
            durable = self.robots_store.load(origin)
            if durable is not None:
                cached = (durable.response, durable.records)
                robots[origin] = cached
        if cached is not None:
            return (
                robots_response_decision(
                    cached[0],
                    user_agent=self.user_agent,
                    target_url=target_uri,
                ),
                cached[1],
            )
        robots_uri = f"{origin}/robots.txt"
        response, records = self._fetch_robots_response(
            robots_uri,
            source=source,
            command=command,
            deadline=deadline,
            usage=usage,
        )
        decision = robots_response_decision(
            response,
            user_agent=self.user_agent,
            target_url=target_uri,
        )
        if response.status == 429:
            raise _CheckpointSignal("rate_limited")
        if 500 <= response.status <= 599:
            raise _CheckpointSignal("transient_network")
        robots[origin] = (response, records)
        if 200 <= response.status <= 299 or 400 <= response.status <= 499:
            self.robots_store.store(
                _RobotsCacheEntry(
                    origin=origin,
                    fetched_at_epoch=self.clock.epoch(),
                    response=response,
                    records=records,
                )
            )
        return decision, records

    def _fetch_robots_response(
        self,
        uri: str,
        *,
        source: PublicSource,
        command: AcquisitionCommand,
        deadline: float,
        usage: _Usage,
    ) -> tuple[HttpResponse, tuple[HttpResponseRecord, ...]]:
        current = uri
        records: list[HttpResponseRecord] = []
        for redirect_count in range(MAX_REDIRECTS + 1):
            response, record = self._send_with_redirect_replay(
                current,
                purpose="robots",
                robots_decision=RobotsDecision("allowed"),
                source=source,
                command=command,
                deadline=deadline,
                usage=usage,
                response_cap=ROBOTS_PARSE_LIMIT_BYTES,
            )
            records.append(record)
            if response.status not in _REDIRECT_STATUSES:
                return response, tuple(records)
            if redirect_count == MAX_REDIRECTS:
                raise _RejectedSignal(
                    HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                    "robots redirect limit exceeded",
                )
            location = response.header("location")
            if location is None:
                raise _RejectedSignal(
                    HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                    "robots redirect has no Location header",
                )
            current = resolve_redirect(current, location)
        raise AssertionError("robots redirect loop did not terminate")

    def _send_with_redirect_replay(
        self,
        uri: str,
        *,
        purpose: Literal["robots", "content"],
        robots_decision: RobotsDecision,
        source: PublicSource,
        command: AcquisitionCommand,
        deadline: float,
        usage: _Usage,
        response_cap: int | None = None,
    ) -> tuple[HttpResponse, HttpResponseRecord]:
        canonical = canonical_http_url(uri)
        replay = self.redirect_replays.load(
            command_id=command.command_id,
            purpose=purpose,
            source_uri=source.uri,
            request_uri=canonical,
        )
        if replay is not None:
            if self.rate_limits.limited(_origin(canonical)):
                raise _CheckpointSignal("rate_limited")
            return replay.response, replay.record
        response, record = self._send(
            canonical,
            purpose=purpose,
            robots_decision=robots_decision,
            source=source,
            command=command,
            deadline=deadline,
            usage=usage,
            response_cap=response_cap,
        )
        if response.status in _REDIRECT_STATUSES:
            self.redirect_replays.issue(
                make_redirect_replay(
                    command_id=command.command_id,
                    purpose=purpose,
                    source_uri=source.uri,
                    request_uri=canonical,
                    response=response,
                    record=record,
                )
            )
        return response, record

    def _send(
        self,
        uri: str,
        *,
        purpose: Literal["robots", "content"],
        robots_decision: RobotsDecision,
        source: PublicSource,
        command: AcquisitionCommand,
        deadline: float,
        usage: _Usage,
        response_cap: int | None = None,
    ) -> tuple[HttpResponse, HttpResponseRecord]:
        if usage.requests >= usage.request_limit:
            raise _CheckpointSignal("request_budget")
        remaining_bytes = usage.download_limit - usage.download_bytes
        if remaining_bytes <= 0:
            raise _CheckpointSignal("download_budget")
        remaining_time = deadline - self.clock.monotonic()
        if not math.isfinite(remaining_time) or remaining_time <= 0:
            raise _CheckpointSignal("time_budget")
        canonical = canonical_http_url(uri)
        origin = _origin(canonical)
        if self.rate_limits.limited(origin):
            raise _CheckpointSignal("rate_limited")
        try:
            endpoint = resolve_public_endpoint(
                canonical,
                self.resolver,
                timeout_seconds=remaining_time,
            )
        except OSError as exc:
            raise _CheckpointSignal("transient_network") from exc
        request = HttpRequest(
            url=canonical,
            headers=(("user-agent", self.user_agent),),
            credential_profile_name=(
                source.credential_profile_name if purpose == "content" else None
            ),
            approved_peer_ips=endpoint.addresses,
            max_response_bytes=min(remaining_bytes, response_cap or remaining_bytes),
            timeout_seconds=remaining_time,
        )
        usage.requests += 1
        try:
            response = require_bounded_response(request, self.transport.send(request))
        except CredentialProfileUnavailable as exc:
            raise _RejectedSignal(
                HardExternalBlockCode.AUTH_REQUIRED,
                str(exc),
            ) from exc
        except ResponseLimitError as exc:
            usage.download_bytes += exc.consumed_bytes
            raise _RejectedSignal(
                HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                "public response exceeds a bounded non-resumable download",
            ) from exc
        except TimeoutError as exc:
            raise _CheckpointSignal("transient_network") from exc
        except OSError as exc:
            raise _CheckpointSignal("transient_network") from exc
        usage.download_bytes += len(response.body)
        headers = tuple(
            sorted(
                (name.lower(), value)
                for name, value in response.headers
                if name.lower() in _RECEIPT_HEADERS
            )
        )
        events = (
            (f"retry-after:{response.header('retry-after')}",)
            if response.header("retry-after") is not None
            else ()
        )
        record = HttpResponseRecord(
            purpose=purpose,
            uri=canonical,
            retrieved_at=self.clock.iso_now(),
            status=response.status,
            peer_ip=response.peer_ip,
            headers=headers,
            byte_count=len(response.body),
            content_sha256=f"sha256:{hashlib.sha256(response.body).hexdigest()}",
            robots_decision=(
                "not_applicable" if purpose == "robots" else robots_decision.status
            ),
            rate_limit_events=events,
        )
        validate_public_peer(response.peer_ip, endpoint)
        self.attempt_receipts.issue(
            make_attempt_receipt(
                command_id=command.command_id,
                source_kind=source.kind,
                requested_source_uri=source.uri,
                credential_profile_name=request.credential_profile_name,
                license_evidence=_license_evidence(source, response),
                validation=ValidationReport(
                    "not_evaluated",
                    "response validation is recorded by the completed policy receipt",
                ),
                missingness=MissingnessReport(
                    "not_evaluated",
                    None,
                    "response missingness is recorded by the completed policy receipt",
                ),
                exchange=record,
            )
        )
        retry_at = _retry_not_before(response.header("retry-after"), self.clock.epoch())
        if retry_at is not None:
            self.rate_limits.defer(origin, retry_at)
        if self.clock.monotonic() >= deadline:
            raise _CheckpointSignal("time_budget")
        return response, record


def _validate_response(
    source: PublicSource,
    response: HttpResponse,
) -> tuple[ValidationReport, MissingnessReport]:
    content_type = (response.header("content-type") or "").lower()
    if source.kind == "public_api":
        if "json" not in content_type:
            raise _RejectedSignal(
                HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                "public API response is not JSON",
            )
        try:
            value = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _RejectedSignal(
                HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                "public API returned malformed JSON",
            ) from exc
        if not isinstance(value, (dict, list)) or not value:
            raise _RejectedSignal(
                HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                "public API returned no structured records",
            )
        missing, total = _missing_values(value)
        fraction = missing / total if total else 0.0
        return (
            ValidationReport("passed", "public API JSON parsed successfully"),
            MissingnessReport("passed", fraction, "JSON null fraction measured"),
        )
    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        raise _RejectedSignal(
            HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
            "public page response is not HTML",
        )
    return (
        ValidationReport("passed", "public HTML media type verified"),
        MissingnessReport(
            "not_evaluated",
            None,
            "HTML page has no declared missingness rule",
        ),
    )


def _missing_values(value: object) -> tuple[int, int]:
    if isinstance(value, dict):
        children = [_missing_values(item) for item in value.values()]
    elif isinstance(value, list):
        children = [_missing_values(item) for item in value]
    else:
        return (1, 1) if value is None else (0, 1)
    return sum(item[0] for item in children), sum(item[1] for item in children)


def _license_evidence(source: PublicSource, response: HttpResponse) -> str | None:
    if source.license_evidence is not None:
        return source.license_evidence
    license_header = response.header("license")
    if license_header:
        return license_header
    link = response.header("link")
    if link and 'rel="license"' in link.lower():
        return link
    return None


def _origin(uri: str) -> str:
    parts = urlsplit(uri)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _retry_not_before(value: str | None, now_epoch: float) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return now_epoch + int(stripped)
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _append_unique(existing: tuple[str, ...], *values: str) -> tuple[str, ...]:
    output = list(existing)
    for value in values:
        if value not in output:
            output.append(value)
    return tuple(output)


def _crawl_archive(
    progress: CrawlProgress,
    cache: ContentAddressedCache,
    deadline: float,
    clock: AcquisitionClock,
    max_bytes: int,
) -> BinaryIO:
    archive = tempfile.TemporaryFile(mode="w+b")
    try:
        _write_archive_bytes(
            archive,
            _CRAWL_ARCHIVE_PREFIX,
            clock=clock,
            deadline=deadline,
            max_bytes=max_bytes,
        )
        for index, page in enumerate(progress.pages):
            if index:
                _write_archive_bytes(
                    archive,
                    b",",
                    clock=clock,
                    deadline=deadline,
                    max_bytes=max_bytes,
                )
            _write_archive_bytes(
                archive,
                _CRAWL_PAGE_PREFIX,
                clock=clock,
                deadline=deadline,
                max_bytes=max_bytes,
            )
            path = cache.verify(page.cache_object, deadline=deadline)
            with path.open("rb") as body:
                _write_base64_body(
                    archive,
                    body,
                    clock=clock,
                    deadline=deadline,
                    max_bytes=max_bytes,
                )
            metadata = _crawl_page_metadata(page)
            _write_archive_bytes(
                archive,
                b'",' + metadata[1:],
                clock=clock,
                deadline=deadline,
                max_bytes=max_bytes,
            )
        suffix = _crawl_archive_suffix(progress)
        _write_archive_bytes(
            archive,
            suffix,
            clock=clock,
            deadline=deadline,
            max_bytes=max_bytes,
        )
        archive.flush()
        os.fsync(archive.fileno())
        archive.seek(0)
        return archive
    except (AcquisitionDeadlineExceeded, AcquisitionDownloadExceeded):
        archive.close()
        raise
    except OSError as exc:
        archive.close()
        raise AcquisitionStorageError(str(exc)) from exc


def _crawl_archive_size(progress: CrawlProgress) -> int:
    size = len(_CRAWL_ARCHIVE_PREFIX) + len(_crawl_archive_suffix(progress))
    for index, page in enumerate(progress.pages):
        metadata = _crawl_page_metadata(page)
        size += int(index > 0)
        size += len(_CRAWL_PAGE_PREFIX)
        size += ((page.cache_object.size_bytes + 2) // 3) * 4
        size += len(b'",') + len(metadata) - 1
    return size


def _crawl_page_metadata(page: CrawledPage) -> bytes:
    return json.dumps(
        {
            "content_sha256": page.cache_object.content_sha256,
            "content_type": page.content_type,
            "depth": page.depth,
            "license_evidence": page.license_evidence,
            "requested_uri": page.requested_uri,
            "retrieved_at": page.retrieved_at,
            "uri": page.final_uri,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _crawl_archive_suffix(progress: CrawlProgress) -> bytes:
    source_uri = json.dumps(progress.source_uri, ensure_ascii=False).encode("utf-8")
    return b'],"source_uri":' + source_uri + b"}"


def _write_base64_body(
    archive: BinaryIO,
    body: BinaryIO,
    *,
    clock: AcquisitionClock,
    deadline: float,
    max_bytes: int,
) -> None:
    remainder = b""
    while True:
        chunk = body.read(1024 * 1024)
        if not chunk:
            break
        combined = remainder + chunk
        boundary = (len(combined) // 3) * 3
        _write_archive_bytes(
            archive,
            base64.b64encode(combined[:boundary]),
            clock=clock,
            deadline=deadline,
            max_bytes=max_bytes,
        )
        remainder = combined[boundary:]
    if remainder:
        _write_archive_bytes(
            archive,
            base64.b64encode(remainder),
            clock=clock,
            deadline=deadline,
            max_bytes=max_bytes,
        )


def _write_archive_bytes(
    archive: BinaryIO,
    value: bytes,
    *,
    clock: AcquisitionClock,
    deadline: float,
    max_bytes: int,
) -> None:
    if clock.monotonic() >= deadline:
        raise AcquisitionDeadlineExceeded("crawl archive time budget exhausted")
    if archive.tell() + len(value) > max_bytes:
        raise AcquisitionDownloadExceeded("crawl archive exceeds its bound")
    archive.write(value)
