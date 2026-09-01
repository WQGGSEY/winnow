from __future__ import annotations

from pathlib import Path

from research_harness.orchestrator.blind_reorientation import HardExternalBlockCode

from .cache import (
    AcquisitionDeadlineExceeded,
    AcquisitionStorageError,
    ContentAddressedCache,
    ManifestStore,
)
from .http_policy import DnsResolver, OneHopTransport, canonical_http_url
from .http_receipt import PolicyReceiptStore
from .http_runtime import (
    AcquisitionClock,
    HttpAcquirer,
    HttpStepCheckpoint,
    HttpStepComplete,
    HttpStepRejected,
    SystemAcquisitionClock,
)
from .live_http import CredentialProvider, SocketDnsResolver, StdlibOneHopTransport
from .model import (
    AcquiredNeed,
    AcquisitionBlocked,
    AcquisitionCheckpoint,
    AcquisitionCommand,
    AcquisitionComplete,
    AcquisitionConflictError,
    AcquisitionContractError,
    AcquisitionCursor,
    AcquisitionOutcome,
    MissingnessReport,
    NeedPlan,
    PinnedNodeManifest,
    PublicSource,
    RegisteredSource,
    ValidationReport,
    make_cursor,
    make_manifest,
    make_response_receipt,
)


def _initial_cursor(command: AcquisitionCommand) -> AcquisitionCursor:
    return make_cursor(
        command_id=command.command_id,
        next_need_index=0,
        next_candidate_index=0,
        requests_used=0,
        download_bytes_used=0,
        completed=(),
    )


class PublicAcquisition:
    @classmethod
    def live(
        cls,
        cache_root: Path,
        *,
        credential_provider: CredentialProvider | None = None,
        clock: AcquisitionClock | None = None,
        user_agent: str = "ResearchHarness",
    ) -> PublicAcquisition:
        return cls(
            cache_root,
            transport=StdlibOneHopTransport(
                credential_provider=credential_provider
            ),
            resolver=SocketDnsResolver(),
            clock=clock,
            user_agent=user_agent,
        )

    def __init__(
        self,
        cache_root: Path,
        *,
        transport: OneHopTransport | None = None,
        resolver: DnsResolver | None = None,
        clock: AcquisitionClock | None = None,
        user_agent: str = "ResearchHarness",
    ) -> None:
        if (transport is None) != (resolver is None):
            raise AcquisitionContractError(
                "public acquisition requires both transport and DNS resolver"
            )
        self._clock = clock or SystemAcquisitionClock()
        self._cache = ContentAddressedCache(
            cache_root,
            monotonic=self._clock.monotonic,
        )
        self._manifests = ManifestStore(cache_root, self._cache)
        self._policy_receipts = PolicyReceiptStore(cache_root)
        self._http = (
            HttpAcquirer(
                transport=transport,
                resolver=resolver,
                cache=self._cache,
                cache_root=cache_root,
                policy_receipts=self._policy_receipts,
                clock=self._clock,
                user_agent=user_agent,
            )
            if transport is not None and resolver is not None
            else None
        )

    def acquire(
        self,
        command: AcquisitionCommand,
        *,
        cursor: AcquisitionCursor | None = None,
    ) -> AcquisitionOutcome:
        if not isinstance(command, AcquisitionCommand):
            raise AcquisitionContractError("acquire requires an AcquisitionCommand")
        started_at = self._clock.monotonic()
        deadline = started_at + command.budget.max_wall_seconds
        current = cursor or _initial_cursor(command)
        self._validate_cursor(command, current)
        try:
            self._manifests.record_command(command)
            if self._clock.monotonic() >= deadline:
                return AcquisitionCheckpoint(reason="time_budget", cursor=current)
            existing = self._manifests.load_for_node(
                command.node_id,
                deadline=deadline,
            )
        except AcquisitionDeadlineExceeded:
            return AcquisitionCheckpoint(reason="time_budget", cursor=current)
        except AcquisitionStorageError:
            return self._storage_block(current)
        if existing is not None:
            if existing.command_id != command.command_id:
                raise AcquisitionConflictError(
                    "node is already pinned to a different acquisition command"
                )
            try:
                verified = self._verify_manifest(
                    existing.manifest_id,
                    node_id=command.node_id,
                    deadline=deadline,
                )
            except AcquisitionDeadlineExceeded:
                return AcquisitionCheckpoint(reason="time_budget", cursor=current)
            return AcquisitionComplete(verified)

        completed = list(current.completed)
        need_index = current.next_need_index
        candidate_index = current.next_candidate_index
        requests_used = current.requests_used
        download_bytes_used = current.download_bytes_used
        cycle_requests_used = 0
        cycle_download_bytes_used = 0
        last_rejection: HttpStepRejected | None = None

        while need_index < len(command.needs):
            plan = command.needs[need_index]
            if self._clock.monotonic() - started_at >= command.budget.max_wall_seconds:
                return AcquisitionCheckpoint(
                    reason="time_budget",
                    cursor=self._cursor(
                        command,
                        need_index,
                        candidate_index,
                        requests_used,
                        download_bytes_used,
                        completed,
                    ),
                )
            if candidate_index >= len(plan.candidates):
                if last_rejection is not None:
                    return AcquisitionBlocked(
                        code=last_rejection.code,
                        required_external_action=(
                            last_rejection.required_external_action
                        ),
                        cursor=self._cursor(
                            command,
                            need_index,
                            candidate_index,
                            requests_used,
                            download_bytes_used,
                            completed,
                        ),
                    )
                return AcquisitionBlocked(
                    code=HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE,
                    required_external_action=(
                        f"Provide one lawful source for data need {need_index}: "
                        f"{plan.need.description}"
                    ),
                    cursor=self._cursor(
                        command,
                        need_index,
                        candidate_index,
                        requests_used,
                        download_bytes_used,
                        completed,
                    ),
                )
            source = plan.candidates[candidate_index]
            if isinstance(source, PublicSource):
                if self._http is None:
                    reason = (
                        "request_budget"
                        if command.budget.max_requests == 0
                        else "generation_retry"
                    )
                    return AcquisitionCheckpoint(
                        reason=reason,
                        cursor=self._cursor(
                            command,
                            need_index,
                            candidate_index,
                            requests_used,
                            download_bytes_used,
                            completed,
                        ),
                    )
                result = self._http.acquire(
                    command=command,
                    need_index=need_index,
                    source_candidate_index=candidate_index,
                    source=source,
                    deadline=deadline,
                    request_budget=max(
                        0,
                        command.budget.max_requests - cycle_requests_used,
                    ),
                    download_budget=max(
                        0,
                        command.budget.max_download_bytes
                        - cycle_download_bytes_used,
                    ),
                )
                requests_used += result.requests_used
                download_bytes_used += result.download_bytes_used
                cycle_requests_used += result.requests_used
                cycle_download_bytes_used += result.download_bytes_used
                if isinstance(result, HttpStepCheckpoint):
                    return AcquisitionCheckpoint(
                        reason=result.reason,
                        cursor=self._cursor(
                            command,
                            need_index,
                            candidate_index,
                            requests_used,
                            download_bytes_used,
                            completed,
                        ),
                    )
                if isinstance(result, HttpStepRejected):
                    last_rejection = result
                    candidate_index += 1
                    continue
                if not isinstance(result, HttpStepComplete):
                    raise AssertionError("unsupported HTTP acquisition result")
                completed.append(result.acquired)
                need_index += 1
                candidate_index = 0
                last_rejection = None
                continue
            try:
                acquired = self._acquire_registered(
                    plan.need.description,
                    need_index,
                    candidate_index,
                    source,
                    deadline,
                )
            except AcquisitionDeadlineExceeded:
                return AcquisitionCheckpoint(
                    reason="time_budget",
                    cursor=self._cursor(
                        command,
                        need_index,
                        candidate_index,
                        requests_used,
                        download_bytes_used,
                        completed,
                    ),
                )
            except AcquisitionConflictError:
                candidate_index += 1
                continue
            except AcquisitionStorageError:
                candidate_index += 1
                if candidate_index < len(plan.candidates):
                    continue
                return AcquisitionBlocked(
                    code=HardExternalBlockCode.STORAGE_UNAVAILABLE,
                    required_external_action=(
                        f"Restore readable durable storage for data need {need_index}"
                    ),
                    cursor=self._cursor(
                        command,
                        need_index,
                        candidate_index,
                        requests_used,
                        download_bytes_used,
                        completed,
                    ),
                )
            completed.append(acquired)
            need_index += 1
            candidate_index = 0
            last_rejection = None

        manifest = make_manifest(
            command_id=command.command_id,
            node_id=command.node_id,
            attempt_id=command.attempt_id,
            direction_id=command.direction.direction_id,
            acquired_needs=completed,
        )
        try:
            self._manifests.pin(manifest)
        except AcquisitionStorageError:
            return self._storage_block(
                self._cursor(
                    command,
                    need_index,
                    candidate_index,
                    requests_used,
                    download_bytes_used,
                    completed,
                )
            )
        if self._clock.monotonic() >= deadline:
            return AcquisitionCheckpoint(
                reason="time_budget",
                cursor=self._cursor(
                    command,
                    need_index,
                    candidate_index,
                    requests_used,
                    download_bytes_used,
                    completed,
                ),
            )
        try:
            verified = self._verify_manifest(
                manifest.manifest_id,
                node_id=command.node_id,
                deadline=deadline,
            )
        except AcquisitionDeadlineExceeded:
            return AcquisitionCheckpoint(
                reason="time_budget",
                cursor=self._cursor(
                    command,
                    need_index,
                    candidate_index,
                    requests_used,
                    download_bytes_used,
                    completed,
                ),
            )
        except AcquisitionStorageError:
            return self._storage_block(
                self._cursor(
                    command,
                    need_index,
                    candidate_index,
                    requests_used,
                    download_bytes_used,
                    completed,
                )
            )
        return AcquisitionComplete(verified)

    def verify_manifest(
        self,
        manifest_id: str,
        *,
        node_id: str,
    ) -> PinnedNodeManifest:
        return self._verify_manifest(manifest_id, node_id=node_id, deadline=None)

    def _verify_manifest(
        self,
        manifest_id: str,
        *,
        node_id: str,
        deadline: float | None,
    ) -> PinnedNodeManifest:
        manifest = self._manifests.load(
            manifest_id,
            node_id=node_id,
            deadline=deadline,
        )
        command = self._manifests.load_command(manifest.command_id)
        if (
            manifest.node_id != command.node_id
            or manifest.attempt_id != command.attempt_id
            or manifest.direction_id != command.direction.direction_id
            or len(manifest.acquired_needs) != len(command.needs)
        ):
            raise AcquisitionConflictError("manifest does not match its acquisition command")
        for acquired, plan in zip(
            manifest.acquired_needs,
            command.needs,
            strict=True,
        ):
            if acquired.description != plan.need.description:
                raise AcquisitionConflictError("manifest completed a different data need")
            self._validate_acquired_source(command, plan, acquired)
        return manifest

    def _acquire_registered(
        self,
        description: str,
        need_index: int,
        source_candidate_index: int,
        source: RegisteredSource,
        deadline: float,
    ) -> AcquiredNeed:
        cache_object = self._cache.put_registered(
            Path(source.path),
            expected_digest=source.content_sha256,
            expected_size=source.size_bytes,
            expected_entry_count=source.entry_count,
            deadline=deadline,
        )
        validation = ValidationReport(
            status="not_evaluated",
            detail="registered snapshot has no declared record schema",
        )
        missingness = MissingnessReport(
            status="not_evaluated",
            fraction=None,
            detail="registered snapshot has no declared missingness rule",
        )
        receipt = make_response_receipt(
            source_kind="registered_adapter",
            source_uri=f"file://{Path(source.path).expanduser().resolve()}",
            retrieved_at=source.retrieved_at,
            robots_decision="not_applicable",
            rate_limit_events=(),
            credential_profile_name=None,
            adapter_id=source.adapter_id,
            snapshot_id=source.snapshot_id,
            provenance=source.provenance,
            policy_receipt_id=None,
            license_evidence=source.license_evidence,
            cache_object=cache_object,
            validation=validation,
            missingness=missingness,
        )
        return AcquiredNeed(
            need_index=need_index,
            source_candidate_index=source_candidate_index,
            description=description,
            receipt=receipt,
        )

    def _validate_cursor(
        self,
        command: AcquisitionCommand,
        cursor: AcquisitionCursor,
    ) -> None:
        if not isinstance(cursor, AcquisitionCursor):
            raise AcquisitionContractError("cursor is invalid")
        if cursor.command_id != command.command_id:
            raise AcquisitionConflictError("cursor belongs to a different command")
        if cursor.next_need_index > len(command.needs):
            raise AcquisitionContractError("cursor need index is outside the command")
        if cursor.next_need_index == len(command.needs):
            if cursor.next_candidate_index != 0:
                raise AcquisitionContractError("completed cursor cannot select a candidate")
        elif cursor.next_candidate_index > len(
            command.needs[cursor.next_need_index].candidates
        ):
            raise AcquisitionContractError("cursor candidate index is outside the need plan")
        if len(cursor.completed) != cursor.next_need_index:
            raise AcquisitionContractError("cursor completion does not match its need index")
        for acquired, plan in zip(
            cursor.completed,
            command.needs[: len(cursor.completed)],
            strict=True,
        ):
            if acquired.description != plan.need.description:
                raise AcquisitionConflictError("cursor completed a different data need")
            self._validate_acquired_source(command, plan, acquired)

    def _validate_acquired_source(
        self,
        command: AcquisitionCommand,
        plan: NeedPlan,
        acquired: AcquiredNeed,
    ) -> None:
        if acquired.source_candidate_index >= len(plan.candidates):
            raise AcquisitionConflictError("receipt source candidate is outside the plan")
        source = plan.candidates[acquired.source_candidate_index]
        receipt = acquired.receipt
        if isinstance(source, RegisteredSource):
            expected_uri = f"file://{Path(source.path).expanduser().resolve()}"
            if (
                receipt.source_kind != source.kind
                or receipt.source_uri != expected_uri
                or receipt.adapter_id != source.adapter_id
                or receipt.snapshot_id != source.snapshot_id
                or receipt.provenance != source.provenance
                or receipt.license_evidence != source.license_evidence
                or receipt.retrieved_at != source.retrieved_at
                or receipt.cache_object.content_sha256 != source.content_sha256
                or receipt.cache_object.size_bytes != source.size_bytes
                or receipt.cache_object.entry_count != source.entry_count
            ):
                raise AcquisitionConflictError(
                    "registered receipt does not match its planned source"
                )
            return
        if receipt.policy_receipt_id is None:
            raise AcquisitionConflictError(
                "public receipts require a durable acquisition policy receipt"
            )
        policy = self._policy_receipts.load(receipt.policy_receipt_id)
        if (
            receipt.source_kind != source.kind
            or receipt.adapter_id is not None
            or receipt.snapshot_id is not None
            or receipt.credential_profile_name != source.credential_profile_name
            or policy.command_id != command.command_id
            or policy.need_index != acquired.need_index
            or policy.source_candidate_index != acquired.source_candidate_index
            or policy.source_kind != source.kind
            or policy.requested_uri != source.uri
            or policy.final_uri != receipt.source_uri
            or policy.credential_profile_name != source.credential_profile_name
            or policy.license_evidence != receipt.license_evidence
            or policy.cache_object != receipt.cache_object
            or policy.validation != receipt.validation
            or policy.missingness != receipt.missingness
        ):
            raise AcquisitionConflictError(
                "public receipt does not match its issued policy receipt"
            )
        if canonical_http_url(policy.final_uri) != policy.final_uri:
            raise AcquisitionConflictError("public receipt final URI is not canonical")

    @staticmethod
    def _cursor(
        command: AcquisitionCommand,
        need_index: int,
        candidate_index: int,
        requests_used: int,
        download_bytes_used: int,
        completed: list[AcquiredNeed],
    ) -> AcquisitionCursor:
        return make_cursor(
            command_id=command.command_id,
            next_need_index=need_index,
            next_candidate_index=candidate_index,
            requests_used=requests_used,
            download_bytes_used=download_bytes_used,
            completed=completed,
        )

    @staticmethod
    def _storage_block(cursor: AcquisitionCursor) -> AcquisitionBlocked:
        return AcquisitionBlocked(
            code=HardExternalBlockCode.STORAGE_UNAVAILABLE,
            required_external_action="Restore writable durable acquisition storage",
            cursor=cursor,
        )


__all__ = ["PublicAcquisition"]
