from __future__ import annotations

import time
from pathlib import Path

import pytest

import research_harness.acquisition.cache as acquisition_cache
from research_harness.acquisition import (
    AcquiredNeed,
    AcquisitionBudget,
    AcquisitionCheckpoint,
    AcquisitionComplete,
    AcquisitionConflictError,
    AcquisitionContractError,
    NeedPlan,
    PublicAcquisition,
    PublicSource,
    RegisteredSource,
    make_acquisition_command,
)
from research_harness.data_adapters import fingerprint_path
from research_harness.acquisition.cache import ContentAddressedCache
from research_harness.acquisition.model import make_cursor, make_response_receipt
from research_harness.orchestrator.direction_generation import (
    DataNeed,
    make_direction_draft,
    make_direction_fingerprint,
)


def _direction(*needs: DataNeed):
    return make_direction_draft(
        claim="Intervention B improves target A.",
        fingerprint=make_direction_fingerprint(
            mechanism="feedback control",
            intervention="adaptive scheduler",
            observables_and_data="public event records",
            analysis_unit="daily cohort",
            timescale="four weeks",
            system_boundary="regional service",
        ),
        experiment_objective="Estimate the positive intervention effect.",
        data_needs=needs,
        predicted_outcomes=("effect above threshold", "holdout replication"),
    )


def _registered(path: Path, *, retrieved_at: str = "2026-09-01T00:00:00Z"):
    digest, size, count = fingerprint_path(path)
    return RegisteredSource(
        adapter_id="events",
        snapshot_id=f"snapshot_{digest}",
        path=str(path.resolve()),
        content_sha256=f"sha256:{digest}",
        size_bytes=size,
        entry_count=count,
        provenance="operator-registered fixture",
        retrieved_at=retrieved_at,
        license_evidence="CC0-1.0",
    )


def _command(
    direction,
    plans,
    *,
    node_id: str = "node-1",
    reservation_digit: str = "1",
    budget: AcquisitionBudget | None = None,
):
    return make_acquisition_command(
        reservation_id=f"reservation_{reservation_digit * 64}",
        node_id=node_id,
        attempt_id="attempt_1",
        direction=direction,
        needs=plans,
        budget=budget
        or AcquisitionBudget(
            max_requests=4,
            max_download_bytes=10_000,
            max_wall_seconds=30,
        ),
    )


def test_registered_source_converges_on_durable_cache_and_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("day,value\n1,7\n", encoding="utf-8")
    need = DataNeed(kind="registered_adapter", description="Daily event values")
    direction = _direction(need)
    command = _command(
        direction,
        (NeedPlan(need_index=0, need=need, candidates=(_registered(source),)),),
    )
    acquisition = PublicAcquisition(tmp_path / "cache")

    first = acquisition.acquire(command)
    second = acquisition.acquire(command)

    assert isinstance(first, AcquisitionComplete)
    assert second == first
    receipt = first.manifest.acquired_needs[0].receipt
    assert receipt.robots_decision == "not_applicable"
    assert receipt.rate_limit_events == ()
    assert receipt.credential_profile_name is None
    assert receipt.adapter_id == "events"
    assert receipt.snapshot_id.startswith("snapshot_")
    assert receipt.provenance == "operator-registered fixture"
    assert receipt.cache_object.size_bytes == source.stat().st_size
    assert receipt.validation.status == "not_evaluated"
    assert receipt.missingness.status == "not_evaluated"
    assert command.budget.max_cost_cents == 0
    cached = ContentAddressedCache(tmp_path / "cache").object_path(
        receipt.cache_object
    )
    assert cached.read_bytes() == source.read_bytes()
    assert len(list((tmp_path / "cache" / "commands").rglob("*.json"))) == 1
    assert not list((tmp_path / "cache").rglob("*.tmp"))


def test_registered_directory_is_copied_without_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "dataset"
    source.mkdir()
    (source / "a.jsonl").write_text('{"x": 1}\n', encoding="utf-8")
    branch = source / "a"
    branch.mkdir()
    (branch / "child").write_text("nested", encoding="utf-8")
    (source / "a.txt").write_text("sibling", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / "b.jsonl").write_text('{"x": 2}\n', encoding="utf-8")
    need = DataNeed(kind="registered_adapter", description="Event directory")
    command = _command(
        _direction(need),
        (NeedPlan(0, need, (_registered(source),)),),
    )

    outcome = PublicAcquisition(tmp_path / "cache").acquire(command)

    assert isinstance(outcome, AcquisitionComplete)
    assert outcome.manifest.acquired_needs[0].receipt.cache_object.object_kind == "directory"


def test_cache_publish_syncs_content_and_uses_atomic_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("durable", encoding="utf-8")
    need = DataNeed(kind="registered_adapter", description="Durable values")
    command = _command(
        _direction(need),
        (NeedPlan(0, need, (_registered(source),)),),
    )
    actual_fsync = acquisition_cache.os.fsync
    actual_rename = acquisition_cache.os.rename
    syncs: list[int] = []
    renames: list[tuple[Path, Path]] = []

    def tracking_fsync(descriptor: int) -> None:
        syncs.append(descriptor)
        actual_fsync(descriptor)

    def tracking_rename(source_path, target_path) -> None:
        renames.append((Path(source_path), Path(target_path)))
        actual_rename(source_path, target_path)

    monkeypatch.setattr(acquisition_cache.os, "fsync", tracking_fsync)
    monkeypatch.setattr(acquisition_cache.os, "rename", tracking_rename)

    outcome = PublicAcquisition(tmp_path / "cache").acquire(command)

    assert isinstance(outcome, AcquisitionComplete)
    assert syncs
    assert len(renames) == 1
    assert renames[0][0].suffix == ".tmp"
    assert renames[0][1].suffix == ".file"


def test_manifest_verification_rejects_cache_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("trusted", encoding="utf-8")
    need = DataNeed(kind="registered_adapter", description="Trusted values")
    command = _command(
        _direction(need),
        (NeedPlan(0, need, (_registered(source),)),),
    )
    acquisition = PublicAcquisition(tmp_path / "cache")
    outcome = acquisition.acquire(command)
    assert isinstance(outcome, AcquisitionComplete)
    cache_object = outcome.manifest.acquired_needs[0].receipt.cache_object
    ContentAddressedCache(tmp_path / "cache").object_path(cache_object).write_text(
        "tampered",
        encoding="utf-8",
    )

    with pytest.raises(AcquisitionConflictError, match="modified"):
        acquisition.verify_manifest(outcome.manifest.manifest_id, node_id="node-1")


def test_manifest_lookup_rejects_an_invalid_id_before_path_resolution(
    tmp_path: Path,
) -> None:
    acquisition = PublicAcquisition(tmp_path / "cache")

    with pytest.raises(AcquisitionContractError, match="manifest id"):
        acquisition.verify_manifest("../../outside", node_id="node-1")


def test_node_pin_rejects_a_different_command(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("trusted", encoding="utf-8")
    need = DataNeed(kind="registered_adapter", description="Trusted values")
    direction = _direction(need)
    plan = (NeedPlan(0, need, (_registered(source),)),)
    acquisition = PublicAcquisition(tmp_path / "cache")
    acquisition.acquire(_command(direction, plan, reservation_digit="1"))

    with pytest.raises(AcquisitionConflictError, match="different acquisition command"):
        acquisition.acquire(_command(direction, plan, reservation_digit="2"))


def test_public_only_plan_checkpoints_without_a_negative_terminal(tmp_path: Path) -> None:
    need = DataNeed(kind="public_api", description="Current public observations")
    command = _command(
        _direction(need),
        (
            NeedPlan(
                0,
                need,
                (PublicSource(kind="public_api", uri="https://example.test/data"),),
            ),
        ),
    )

    outcome = PublicAcquisition(tmp_path / "cache").acquire(command)

    assert isinstance(outcome, AcquisitionCheckpoint)
    assert outcome.reason == "generation_retry"
    assert outcome.cursor.command_id == command.command_id


def test_cursor_cannot_resume_a_different_command(tmp_path: Path) -> None:
    need = DataNeed(kind="public_page", description="Public documentation")
    direction = _direction(need)
    plan = (NeedPlan(0, need, (PublicSource("public_page", "https://example.test"),)),)
    acquisition = PublicAcquisition(tmp_path / "cache")
    first_command = _command(direction, plan, reservation_digit="1")
    checkpoint = acquisition.acquire(first_command)
    assert isinstance(checkpoint, AcquisitionCheckpoint)
    second_command = _command(direction, plan, reservation_digit="2")

    with pytest.raises(AcquisitionConflictError, match="different command"):
        acquisition.acquire(second_command, cursor=checkpoint.cursor)


def test_cursor_receipts_must_come_from_the_bound_source_plan(tmp_path: Path) -> None:
    first_source = tmp_path / "first.txt"
    second_source = tmp_path / "second.txt"
    first_source.write_text("first", encoding="utf-8")
    second_source.write_text("second", encoding="utf-8")
    first_need = DataNeed(kind="registered_adapter", description="First values")
    second_need = DataNeed(kind="public_api", description="Second values")
    direction = _direction(first_need, second_need)
    first_plan = NeedPlan(0, first_need, (_registered(first_source),))
    public_plan = NeedPlan(
        1,
        second_need,
        (PublicSource("public_api", "https://example.test/data"),),
    )
    acquisition = PublicAcquisition(tmp_path / "cache")
    checkpoint = acquisition.acquire(_command(direction, (first_plan, public_plan)))
    assert isinstance(checkpoint, AcquisitionCheckpoint)
    substituted_plan = NeedPlan(0, first_need, (_registered(second_source),))
    changed = _command(
        direction,
        (substituted_plan, public_plan),
        reservation_digit="2",
        node_id="node-2",
    )
    forged = make_cursor(
        command_id=changed.command_id,
        next_need_index=1,
        next_candidate_index=0,
        requests_used=0,
        download_bytes_used=checkpoint.cursor.download_bytes_used,
        completed=checkpoint.cursor.completed,
    )

    with pytest.raises(AcquisitionConflictError, match="planned source"):
        acquisition.acquire(changed, cursor=forged)


def test_public_cursor_cannot_promote_unissued_bytes_to_a_manifest(
    tmp_path: Path,
) -> None:
    seed_source = tmp_path / "seed.txt"
    seed_source.write_text("untrusted for public plan", encoding="utf-8")
    seed_need = DataNeed(kind="registered_adapter", description="Seed values")
    acquisition = PublicAcquisition(tmp_path / "cache")
    seed = acquisition.acquire(
        _command(
            _direction(seed_need),
            (NeedPlan(0, seed_need, (_registered(seed_source),)),),
            node_id="seed-node",
        )
    )
    assert isinstance(seed, AcquisitionComplete)
    seed_receipt = seed.manifest.acquired_needs[0].receipt
    public_need = DataNeed(kind="public_api", description="Official public values")
    public_command = _command(
        _direction(public_need),
        (
            NeedPlan(
                0,
                public_need,
                (PublicSource("public_api", "https://official.example/data"),),
            ),
        ),
        node_id="public-node",
        reservation_digit="2",
    )
    forged_receipt = make_response_receipt(
        source_kind="public_api",
        source_uri="https://attacker.example/fake",
        retrieved_at="2026-09-01T00:00:00Z",
        robots_decision="allowed",
        rate_limit_events=(),
        credential_profile_name=None,
        adapter_id=None,
        snapshot_id=None,
        provenance="caller asserted",
        policy_receipt_id="httppolicy_" + "0" * 64,
        license_evidence=None,
        cache_object=seed_receipt.cache_object,
        validation=seed_receipt.validation,
        missingness=seed_receipt.missingness,
    )
    forged_cursor = make_cursor(
        command_id=public_command.command_id,
        next_need_index=1,
        next_candidate_index=0,
        requests_used=1,
        download_bytes_used=seed_receipt.cache_object.size_bytes,
        completed=(
            AcquiredNeed(
                need_index=0,
                source_candidate_index=0,
                description=public_need.description,
                receipt=forged_receipt,
            ),
        ),
    )

    with pytest.raises(AcquisitionConflictError, match="policy receipt"):
        acquisition.acquire(public_command, cursor=forged_cursor)


def test_registered_snapshot_does_not_consume_network_download_budget(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("already local", encoding="utf-8")
    need = DataNeed(kind="registered_adapter", description="Local values")
    command = _command(
        _direction(need),
        (NeedPlan(0, need, (_registered(source),)),),
        budget=AcquisitionBudget(0, 0, 30),
    )

    outcome = PublicAcquisition(tmp_path / "cache").acquire(command)

    assert isinstance(outcome, AcquisitionComplete)


def test_wall_budget_interrupts_registered_cache_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("slow local storage", encoding="utf-8")
    need = DataNeed(kind="registered_adapter", description="Slow local values")
    command = _command(
        _direction(need),
        (NeedPlan(0, need, (_registered(source),)),),
        budget=AcquisitionBudget(0, 0, 1),
    )
    original = ContentAddressedCache.put_registered

    def delayed_put(cache, *args, **kwargs):
        time.sleep(1.05)
        return original(cache, *args, **kwargs)

    monkeypatch.setattr(ContentAddressedCache, "put_registered", delayed_put)
    started = time.monotonic()

    outcome = PublicAcquisition(tmp_path / "cache").acquire(command)

    assert time.monotonic() - started < 1.5
    assert isinstance(outcome, AcquisitionCheckpoint)
    assert outcome.reason == "time_budget"
    assert not list((tmp_path / "cache" / "manifests").rglob("*.json"))


@pytest.mark.parametrize(
    ("budget", "source", "reason"),
    [
        (AcquisitionBudget(0, 100, 30), "public", "request_budget"),
        (AcquisitionBudget(4, 100, 0), "registered", "time_budget"),
    ],
)
def test_physical_budgets_checkpoint_before_external_work(
    tmp_path: Path,
    budget: AcquisitionBudget,
    source: str,
    reason: str,
) -> None:
    local = tmp_path / "source.txt"
    local.write_text("four", encoding="utf-8")
    need = DataNeed(kind="public_api", description="Bounded values")
    candidate = (
        _registered(local)
        if source == "registered"
        else PublicSource("public_api", "https://example.test/data")
    )
    command = _command(
        _direction(need),
        (NeedPlan(0, need, (candidate,)),),
        budget=budget,
    )

    outcome = PublicAcquisition(tmp_path / "cache").acquire(command)

    assert isinstance(outcome, AcquisitionCheckpoint)
    assert outcome.reason == reason
    assert not list((tmp_path / "cache" / "objects").rglob("*.file"))


def test_acquisition_budget_forbids_spending() -> None:
    with pytest.raises(AcquisitionContractError, match="cannot spend money"):
        AcquisitionBudget(1, 1, 1, 1)


@pytest.mark.parametrize(
    "uri",
    [
        "https://alice:supersecret@example.test/data",
        "https://example.test/data?token=supersecret",
        "file:///tmp/private-data",
    ],
)
def test_public_source_rejects_secret_bearing_or_non_http_uris(uri: str) -> None:
    with pytest.raises(AcquisitionContractError, match="public source URI"):
        PublicSource("public_api", uri)


def test_credentialed_public_source_requires_https() -> None:
    with pytest.raises(AcquisitionContractError, match="require HTTPS"):
        PublicSource(
            "public_api",
            "http://example.test/data",
            credential_profile_name="official-api",
        )


def test_public_source_rejects_oversized_license_evidence() -> None:
    with pytest.raises(AcquisitionContractError, match="safe limit"):
        PublicSource(
            "crawl",
            "https://example.test/data",
            license_evidence="x" * 8193,
        )
