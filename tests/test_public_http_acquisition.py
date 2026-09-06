from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from research_harness.acquisition import (
    AcquisitionBlocked,
    AcquisitionBudget,
    AcquisitionCheckpoint,
    AcquisitionComplete,
    NeedPlan,
    PublicAcquisition,
    PublicSource,
    make_acquisition_command,
)
from research_harness.acquisition.http_policy import HttpRequest, HttpResponse
from research_harness.acquisition.cache import ContentAddressedCache
from research_harness.acquisition.http_receipt import HttpAttemptReceiptStore
from research_harness.acquisition.model import AcquisitionConflictError
from research_harness.orchestrator.blind_reorientation import HardExternalBlockCode
from research_harness.orchestrator.direction_generation import (
    DataNeed,
    make_direction_draft,
    make_direction_fingerprint,
)


PUBLIC_IP = "93.184.216.34"


def test_reference_acquisition_does_not_reopen_confirmation(tmp_path):
    from research_harness.orchestrator.research_control import _write
    from research_harness.orchestrator.research_sources import acquire_reference_source

    _write(tmp_path / 'market/paper_references.json', {'papers': [{'id': 'prior_method'}]})
    confirmation = tmp_path / 'production/confirmation_execution.json'
    _write(confirmation, {'status': 'completed', 'work_id': 'frozen'})
    before = confirmation.read_bytes()
    transport = ScriptedTransport(_response(404), _response(200, body=b'<html>Primary method</html>',
        headers=(("content-type", "text/html"),)))
    source = acquire_reference_source(tmp_path, 'prior_method', 'https://data.example/paper',
        transport=transport, resolver=_resolver('data.example'), clock=FixedClock())
    assert source['owner'] == {'kind': 'reference', 'id': 'prior_method'}
    assert source['status'] == 'retrieved'
    assert confirmation.read_bytes() == before
    assert not (tmp_path / 'production/research_control/current.json').exists()


@pytest.mark.parametrize('media_type,body,expected_status', [
    ('text/html', b'<html>Primary method</html>', 'retrieved'),
    ('application/pdf', b'%PDF-1.4\nnot a complete document', 'raw_only'),
])
def test_ongoing_work_acquires_source_without_direction_transition(tmp_path, media_type, body, expected_status):
    from research_harness.orchestrator.research_control import _write, _digest, PLANNING_POLICY_VERSION, resolve_research_work
    from research_harness.orchestrator.research_sources import acquire_source, retrieved_sources

    work = {'work_id': 'a' * 64, 'status': 'planned', 'planning_policy_version': PLANNING_POLICY_VERSION,
            'decision': {'kind': 'analysis', 'source_mode': 'acquire', 'uncertainty': 'Read the original method.'},
            'protocol_digest': _digest({}), 'evidence_digest': _digest({})}
    _write(tmp_path / 'production/research_control/current.json', work)
    # No direction, learner, or formal claim needs to be synthesized to get a paper.
    with pytest.raises(ValueError, match='Retrieve a source'):
        resolve_research_work(Path(__file__).resolve().parents[1], tmp_path, work['work_id'])
    transport = ScriptedTransport(_response(404), _response(200, body=body,
        headers=(("content-type", media_type),)))
    result = acquire_source(tmp_path, work['work_id'], 'https://data.example/paper',
        transport=transport, resolver=_resolver('data.example'), clock=FixedClock())
    assert result['status'] == expected_status
    assert Path(result['raw']['path']).read_bytes() == body
    if expected_status == 'retrieved':
        assert Path(result['text']['path']).read_text() == body.decode()
    else:
        assert 'extraction_error' in result
    assert result['acquisition_receipt']['policy_receipt_id'].startswith('httppolicy_')
    assert not (tmp_path / 'production/reorientation/state.json').exists()
    assert len(transport.requests) == 2
    assert acquire_source(tmp_path, work['work_id'], 'https://data.example/paper')['status'] == expected_status
    Path(result['raw']['path']).write_text('changed')
    with pytest.raises(ValueError, match='source changed'):
        retrieved_sources(tmp_path)


class ScriptedTransport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class ScriptedResolver:
    def __init__(self, answers: dict[tuple[str, int], tuple[str, ...]]) -> None:
        self.answers = answers
        self.queries: list[tuple[str, int]] = []

    def resolve(
        self,
        hostname: str,
        port: int,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        self.queries.append((hostname, port))
        return self.answers[(hostname, port)]


class FixedClock:
    def monotonic(self) -> float:
        return 10.0

    def epoch(self) -> float:
        return 1_788_192_000.0

    def iso_now(self) -> str:
        return "2026-09-01T00:00:00Z"


class MutableClock(FixedClock):
    def __init__(self) -> None:
        self.now = 1_788_192_000.0

    def epoch(self) -> float:
        return self.now


def _command(
    source: PublicSource,
    *,
    max_requests: int = 8,
    max_download_bytes: int = 10_000,
):
    need = DataNeed(kind=source.kind, description="Current public observations")
    direction = make_direction_draft(
        claim="Intervention B improves target A.",
        fingerprint=make_direction_fingerprint(
            mechanism="feedback control",
            intervention="adaptive scheduler",
            observables_and_data="current public observations",
            analysis_unit="daily cohort",
            timescale="four weeks",
            system_boundary="regional service",
        ),
        experiment_objective="Estimate the positive intervention effect.",
        data_needs=(need,),
        predicted_outcomes=("effect above threshold", "holdout replication"),
    )
    return make_acquisition_command(
        reservation_id="reservation_" + "1" * 64,
        node_id="public-node",
        attempt_id="attempt_1",
        direction=direction,
        needs=(NeedPlan(0, need, (source,)),),
        budget=AcquisitionBudget(
            max_requests=max_requests,
            max_download_bytes=max_download_bytes,
            max_wall_seconds=30,
        ),
    )


def _resolver(*hosts: str) -> ScriptedResolver:
    return ScriptedResolver({(host, 443): (PUBLIC_IP,) for host in hosts})


def _response(
    status: int,
    *,
    body: bytes = b"",
    headers: tuple[tuple[str, str], ...] = (),
    peer_ip: str = PUBLIC_IP,
) -> HttpResponse:
    return HttpResponse(status, peer_ip, headers, body)


def test_public_api_fetch_pins_policy_receipt_and_verified_bytes(
    tmp_path: Path,
) -> None:
    source = PublicSource(
        "public_api",
        "https://data.example/observations",
        license_evidence="CC-BY-4.0",
    )
    transport = ScriptedTransport(
        _response(404),
        _response(
            200,
            body=b'{"rows":[{"value":7},{"value":null}]}',
            headers=(("content-type", "application/json"),),
        ),
    )
    acquisition = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    )

    outcome = acquisition.acquire(_command(source))

    assert isinstance(outcome, AcquisitionComplete)
    receipt = outcome.manifest.acquired_needs[0].receipt
    assert receipt.policy_receipt_id.startswith("httppolicy_")
    assert receipt.robots_decision == "unavailable"
    assert receipt.validation.status == "passed"
    assert receipt.missingness.fraction == 1 / 2
    assert receipt.license_evidence == "CC-BY-4.0"
    assert [request.url for request in transport.requests] == [
        "https://data.example/robots.txt",
        "https://data.example/observations",
    ]
    assert acquisition.verify_manifest(
        outcome.manifest.manifest_id,
        node_id="public-node",
    ) == outcome.manifest


def test_request_budget_resumes_from_durable_robots_cache_after_restart(
    tmp_path: Path,
) -> None:
    source = PublicSource("public_api", "https://data.example/observations")
    command = _command(source, max_requests=1)
    transport = ScriptedTransport(
        _response(404),
        _response(
            200,
            body=b'{"rows":[{"value":7}]}',
            headers=(("content-type", "application/json"),),
        ),
    )
    first = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    ).acquire(command)
    assert isinstance(first, AcquisitionCheckpoint)
    assert first.reason == "request_budget"
    assert first.cursor.requests_used == 1

    second = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    ).acquire(command, cursor=first.cursor)

    assert isinstance(second, AcquisitionComplete)
    assert [request.url for request in transport.requests] == [
        "https://data.example/robots.txt",
        "https://data.example/observations",
    ]


def test_modified_robots_cache_fails_closed(tmp_path: Path) -> None:
    source = PublicSource("public_api", "https://data.example/observations")
    command = _command(source, max_requests=1)
    root = tmp_path / "cache"
    first = PublicAcquisition(
        root,
        transport=ScriptedTransport(_response(404)),
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    ).acquire(command)
    assert isinstance(first, AcquisitionCheckpoint)
    robots_path = next((root / "robots_cache").glob("*.json"))
    document = json.loads(robots_path.read_bytes())
    document["response"]["status"] = 200
    robots_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(AcquisitionConflictError, match="robots cache"):
        PublicAcquisition(
            root,
            transport=ScriptedTransport(),
            resolver=_resolver("data.example"),
            clock=FixedClock(),
        ).acquire(command, cursor=first.cursor)


def test_robots_redirect_resumes_without_repeating_completed_hops(
    tmp_path: Path,
) -> None:
    source = PublicSource("public_api", "https://data.example/observations")
    command = _command(source, max_requests=1)
    transport = ScriptedTransport(
        _response(301, headers=(("location", "/robots-v2.txt"),)),
        _response(404),
        _response(
            200,
            body=b'{"value":7}',
            headers=(("content-type", "application/json"),),
        ),
    )
    root = tmp_path / "cache"
    cursor = None
    for _ in range(2):
        outcome = PublicAcquisition(
            root,
            transport=transport,
            resolver=_resolver("data.example"),
            clock=FixedClock(),
        ).acquire(command, cursor=cursor)
        assert isinstance(outcome, AcquisitionCheckpoint)
        cursor = outcome.cursor
    final = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    ).acquire(command, cursor=cursor)

    assert isinstance(final, AcquisitionComplete)
    assert [request.url for request in transport.requests] == [
        "https://data.example/robots.txt",
        "https://data.example/robots-v2.txt",
        "https://data.example/observations",
    ]


def test_redirect_destination_gets_new_dns_peer_and_robots_checks(
    tmp_path: Path,
) -> None:
    source = PublicSource("public_page", "https://first.example/start")
    transport = ScriptedTransport(
        _response(404),
        _response(
            302,
            headers=(("location", "https://second.example/final"),),
        ),
        _response(200, body=b"User-agent: ResearchHarness\nDisallow: /final\n"),
    )
    resolver = _resolver("first.example", "second.example")

    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=resolver,
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionBlocked)
    assert outcome.code is HardExternalBlockCode.LEGAL_ACCESS_DENIED
    assert [request.url for request in transport.requests] == [
        "https://first.example/robots.txt",
        "https://first.example/start",
        "https://second.example/robots.txt",
    ]
    assert ("second.example", 443) in resolver.queries


def test_peer_outside_the_approved_dns_result_never_reaches_a_manifest(
    tmp_path: Path,
) -> None:
    source = PublicSource("public_page", "https://data.example/page")
    transport = ScriptedTransport(_response(404, peer_ip="8.8.8.8"))

    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionBlocked)
    assert outcome.code is HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE
    assert not list((tmp_path / "cache" / "manifests").rglob("*.json"))


def test_retry_after_response_checkpoints_without_closing_the_source(
    tmp_path: Path,
) -> None:
    source = PublicSource("public_api", "https://data.example/observations")
    transport = ScriptedTransport(
        _response(404),
        _response(429, headers=(("retry-after", "120"),)),
    )

    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionCheckpoint)
    assert outcome.reason == "rate_limited"
    assert outcome.cursor.requests_used == 2
    assert not list((tmp_path / "cache" / "manifests").rglob("*.json"))
    attempts = [
        json.loads(path.read_bytes())
        for path in (tmp_path / "cache" / "http_attempt_receipts").rglob("*.json")
    ]
    assert sorted(item["exchange"]["status"] for item in attempts) == [404, 429]
    retry = next(item for item in attempts if item["exchange"]["status"] == 429)
    assert retry["exchange"]["rate_limit_events"] == ["retry-after:120"]
    assert retry["validation"]["status"] == "not_evaluated"


def test_attempt_receipt_tampering_is_detected(tmp_path: Path) -> None:
    source = PublicSource("public_api", "https://data.example/observations")
    root = tmp_path / "cache"
    transport = ScriptedTransport(_response(429, headers=(("retry-after", "5"),)))

    PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    ).acquire(_command(source))
    path = next((root / "http_attempt_receipts").rglob("*.json"))
    document = json.loads(path.read_bytes())
    receipt_id = document["attempt_receipt_id"]
    document["exchange"]["status"] = 200
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(AcquisitionConflictError):
        HttpAttemptReceiptStore(root).load(receipt_id)


def test_retry_after_persists_across_restart_without_an_early_request(
    tmp_path: Path,
) -> None:
    source = PublicSource("public_api", "https://data.example/observations")
    command = _command(source)
    clock = MutableClock()
    transport = ScriptedTransport(
        _response(429, headers=(("retry-after", "5"),)),
        _response(404),
        _response(
            200,
            body=b'{"value":7}',
            headers=(("content-type", "application/json"),),
        ),
    )
    root = tmp_path / "cache"
    first = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("data.example"),
        clock=clock,
    ).acquire(command)
    assert isinstance(first, AcquisitionCheckpoint)

    early = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("data.example"),
        clock=clock,
    ).acquire(command, cursor=first.cursor)
    assert isinstance(early, AcquisitionCheckpoint)
    assert early.reason == "rate_limited"
    assert len(transport.requests) == 1

    clock.now += 5
    resumed = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("data.example"),
        clock=clock,
    ).acquire(command, cursor=early.cursor)

    assert isinstance(resumed, AcquisitionComplete)
    assert len(transport.requests) == 3


def test_robots_server_error_is_a_transient_checkpoint(tmp_path: Path) -> None:
    source = PublicSource("public_page", "https://data.example/page")
    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=ScriptedTransport(_response(503)),
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionCheckpoint)
    assert outcome.reason == "transient_network"


def test_dns_failure_is_a_transient_checkpoint(tmp_path: Path) -> None:
    class FailingResolver:
        def resolve(
            self,
            hostname: str,
            port: int,
            timeout_seconds: float,
        ) -> tuple[str, ...]:
            raise OSError("DNS unavailable")

    source = PublicSource("public_page", "https://data.example/page")
    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=ScriptedTransport(),
        resolver=FailingResolver(),
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionCheckpoint)
    assert outcome.reason == "transient_network"


def test_non_resumable_response_over_download_limit_rejects_the_source(
    tmp_path: Path,
) -> None:
    source = PublicSource("public_api", "https://data.example/observations")
    transport = ScriptedTransport(
        _response(404),
        _response(
            200,
            body=b'{"too":"large"}',
            headers=(("content-type", "application/json"),),
        ),
    )

    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    ).acquire(_command(source, max_download_bytes=4))

    assert isinstance(outcome, AcquisitionBlocked)
    assert outcome.code is HardExternalBlockCode.LAWFUL_ACCESS_UNAVAILABLE


def test_non_resumable_oversized_source_advances_to_a_lawful_substitute(
    tmp_path: Path,
) -> None:
    first = PublicSource("public_api", "https://large.example/observations")
    substitute = PublicSource("public_api", "https://small.example/observations")
    need = DataNeed(kind="public_api", description="Current public observations")
    direction = make_direction_draft(
        claim="Intervention B improves target A.",
        fingerprint=make_direction_fingerprint(
            mechanism="feedback control",
            intervention="adaptive scheduler",
            observables_and_data="current public observations",
            analysis_unit="daily cohort",
            timescale="four weeks",
            system_boundary="regional service",
        ),
        experiment_objective="Estimate the positive intervention effect.",
        data_needs=(need,),
        predicted_outcomes=("effect above threshold", "holdout replication"),
    )
    command = make_acquisition_command(
        reservation_id="reservation_" + "4" * 64,
        node_id="substitute-node",
        attempt_id="attempt_substitute",
        direction=direction,
        needs=(NeedPlan(0, need, (first, substitute)),),
        budget=AcquisitionBudget(4, 4, 30),
    )
    transport = ScriptedTransport(
        _response(404),
        _response(
            200,
            body=b'{"too":"large"}',
            headers=(("content-type", "application/json"),),
        ),
        _response(404),
        _response(
            200,
            body=b"[1]",
            headers=(("content-type", "application/json"),),
        ),
    )

    first_cycle = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("large.example", "small.example"),
        clock=FixedClock(),
    ).acquire(command)

    assert isinstance(first_cycle, AcquisitionCheckpoint)
    assert first_cycle.reason == "download_budget"
    assert first_cycle.cursor.next_candidate_index == 1
    assert first_cycle.cursor.download_bytes_used == len(b'{"too":"large"}')
    assert len(transport.requests) == 2

    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("large.example", "small.example"),
        clock=FixedClock(),
    ).acquire(command, cursor=first_cycle.cursor)

    assert isinstance(outcome, AcquisitionComplete)
    assert outcome.manifest.acquired_needs[0].source_candidate_index == 1
    assert len(transport.requests) == 4


def test_robots_requests_never_receive_a_credential_profile(tmp_path: Path) -> None:
    source = PublicSource(
        "public_api",
        "https://data.example/observations",
        credential_profile_name="official-api",
    )
    transport = ScriptedTransport(
        _response(404),
        _response(
            200,
            body=b'{"value":7}',
            headers=(("content-type", "application/json"),),
        ),
    )

    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("data.example"),
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionComplete)
    assert [request.credential_profile_name for request in transport.requests] == [
        None,
        "official-api",
    ]


def test_credentialed_redirect_cannot_cross_origins(tmp_path: Path) -> None:
    source = PublicSource(
        "public_api",
        "https://first.example/observations",
        credential_profile_name="official-api",
    )
    transport = ScriptedTransport(
        _response(404),
        _response(
            302,
            headers=(("location", "https://second.example/final"),),
        ),
    )

    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("first.example", "second.example"),
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionBlocked)
    assert outcome.code is HardExternalBlockCode.AUTH_REQUIRED
    assert all(
        request.url.startswith("https://first.example/")
        for request in transport.requests
    )


def test_crawl_archives_same_origin_pages_in_deterministic_order(
    tmp_path: Path,
) -> None:
    source = PublicSource(
        "crawl",
        "https://docs.example/start",
        crawl_max_pages=3,
        crawl_max_depth=1,
    )
    transport = ScriptedTransport(
        _response(404),
        _response(
            200,
            body=(
                b'<a href="/b">B</a><a href="/a">A</a>'
                b'<a href="https://elsewhere.example/x">X</a>'
                b'<a href="/ignored" rel="nofollow">N</a>'
            ),
            headers=(("content-type", "text/html; charset=utf-8"),),
        ),
        _response(200, body=b"<p>A</p>", headers=(("content-type", "text/html"),)),
        _response(200, body=b"<p>B</p>", headers=(("content-type", "text/html"),)),
    )
    root = tmp_path / "cache"

    outcome = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("docs.example"),
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionComplete)
    receipt = outcome.manifest.acquired_needs[0].receipt
    archive_path = ContentAddressedCache(root).object_path(receipt.cache_object)
    archive = json.loads(archive_path.read_bytes())
    assert [page["uri"] for page in archive["pages"]] == [
        "https://docs.example/start",
        "https://docs.example/a",
        "https://docs.example/b",
    ]
    assert [base64.b64decode(page["body_base64"]) for page in archive["pages"]] == [
        (
            b'<a href="/b">B</a><a href="/a">A</a>'
            b'<a href="https://elsewhere.example/x">X</a>'
            b'<a href="/ignored" rel="nofollow">N</a>'
        ),
        b"<p>A</p>",
        b"<p>B</p>",
    ]
    assert receipt.source_uri == "https://docs.example/start"
    assert receipt.validation.detail == "crawl archived 3 robots-compliant HTML pages"
    assert [request.url for request in transport.requests] == [
        "https://docs.example/robots.txt",
        "https://docs.example/start",
        "https://docs.example/a",
        "https://docs.example/b",
    ]
    assert PublicAcquisition(root).verify_manifest(
        outcome.manifest.manifest_id,
        node_id="public-node",
    ) == outcome.manifest


def test_crawl_request_checkpoint_resumes_its_durable_frontier(
    tmp_path: Path,
) -> None:
    source = PublicSource(
        "crawl",
        "https://docs.example/start",
        crawl_max_pages=3,
        crawl_max_depth=1,
    )
    transport = ScriptedTransport(
        _response(404),
        _response(
            200,
            body=b'<a href="/a">A</a><a href="/b">B</a>',
            headers=(("content-type", "text/html"),),
        ),
        _response(200, body=b"<p>A</p>", headers=(("content-type", "text/html"),)),
        _response(200, body=b"<p>B</p>", headers=(("content-type", "text/html"),)),
    )
    root = tmp_path / "cache"
    command = _command(source, max_requests=2)

    first = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("docs.example"),
        clock=FixedClock(),
    ).acquire(command)
    assert isinstance(first, AcquisitionCheckpoint)
    assert first.reason == "request_budget"

    second = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("docs.example"),
        clock=FixedClock(),
    ).acquire(command, cursor=first.cursor)

    assert isinstance(second, AcquisitionComplete)
    assert [request.url for request in transport.requests].count(
        "https://docs.example/start"
    ) == 1


def test_crawl_skips_a_robots_denied_child_after_a_lawful_root(
    tmp_path: Path,
) -> None:
    source = PublicSource(
        "crawl",
        "https://docs.example/start",
        crawl_max_pages=5,
        crawl_max_depth=1,
    )
    transport = ScriptedTransport(
        _response(
            200,
            body=(
                b"User-agent: ResearchHarness\n"
                b"Allow: /start\n"
                b"Disallow: /private\n"
            ),
        ),
        _response(
            200,
            body=b'<a href="/private">private</a>',
            headers=(("content-type", "text/html"),),
        ),
    )

    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("docs.example"),
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionComplete)
    assert outcome.manifest.acquired_needs[0].receipt.validation.detail == (
        "crawl archived 1 robots-compliant HTML pages"
    )
    assert len(transport.requests) == 2


def test_crawl_total_byte_bound_skips_an_oversized_child(tmp_path: Path) -> None:
    root_body = b'<a href="/large">large</a>'
    source = PublicSource(
        "crawl",
        "https://docs.example/start",
        crawl_max_pages=5,
        crawl_max_depth=1,
        crawl_max_total_bytes=len(root_body) + 2,
    )
    transport = ScriptedTransport(
        _response(404),
        _response(200, body=root_body, headers=(("content-type", "text/html"),)),
        _response(
            200,
            body=b"<p>too large</p>",
            headers=(("content-type", "text/html"),),
        ),
    )

    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("docs.example"),
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionComplete)
    assert outcome.manifest.acquired_needs[0].receipt.validation.detail == (
        "crawl archived 1 robots-compliant HTML pages"
    )


def test_crawl_archive_uses_its_exact_metadata_bound(tmp_path: Path) -> None:
    source = PublicSource(
        "crawl",
        "https://docs.example/start",
        license_evidence="x" * 8192,
    )
    transport = ScriptedTransport(
        _response(404),
        _response(
            200,
            body=b"<p>ok</p>",
            headers=(("content-type", "text/html"),),
        ),
    )

    outcome = PublicAcquisition(
        tmp_path / "cache",
        transport=transport,
        resolver=_resolver("docs.example"),
        clock=FixedClock(),
    ).acquire(_command(source))

    assert isinstance(outcome, AcquisitionComplete)
    assert outcome.manifest.acquired_needs[0].receipt.license_evidence == "x" * 8192


def test_cycle_request_budget_is_shared_across_needs(tmp_path: Path) -> None:
    first_need = DataNeed(kind="public_api", description="First public observations")
    second_need = DataNeed(kind="public_api", description="Second public observations")
    direction = make_direction_draft(
        claim="Two sources jointly identify the effect.",
        fingerprint=make_direction_fingerprint(
            mechanism="joint evidence",
            intervention="dual-source estimate",
            observables_and_data="two public APIs",
            analysis_unit="paired cohort",
            timescale="one month",
            system_boundary="two services",
        ),
        experiment_objective="Acquire both evidence sources.",
        data_needs=(first_need, second_need),
        predicted_outcomes=("first acquired", "second acquired"),
    )
    sources = (
        PublicSource("public_api", "https://first.example/data"),
        PublicSource("public_api", "https://second.example/data"),
    )
    command = make_acquisition_command(
        reservation_id="reservation_" + "3" * 64,
        node_id="two-need-node",
        attempt_id="attempt_two_needs",
        direction=direction,
        needs=(
            NeedPlan(0, first_need, (sources[0],)),
            NeedPlan(1, second_need, (sources[1],)),
        ),
        budget=AcquisitionBudget(2, 10_000, 30),
    )
    transport = ScriptedTransport(
        _response(404),
        _response(
            200,
            body=b'{"a":1}',
            headers=(("content-type", "application/json"),),
        ),
        _response(404),
        _response(
            200,
            body=b'{"b":2}',
            headers=(("content-type", "application/json"),),
        ),
    )
    root = tmp_path / "cache"
    first = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("first.example", "second.example"),
        clock=FixedClock(),
    ).acquire(command)

    assert isinstance(first, AcquisitionCheckpoint)
    assert first.reason == "request_budget"
    assert len(first.cursor.completed) == 1
    assert len(transport.requests) == 2
    resumed = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("first.example", "second.example"),
        clock=FixedClock(),
    ).acquire(command, cursor=first.cursor)
    assert isinstance(resumed, AcquisitionComplete)
    assert len(transport.requests) == 4


def test_cycle_download_budget_is_shared_across_needs(tmp_path: Path) -> None:
    first_need = DataNeed(kind="public_api", description="First public observations")
    second_need = DataNeed(kind="public_api", description="Second public observations")
    direction = make_direction_draft(
        claim="Two sources jointly identify the effect.",
        fingerprint=make_direction_fingerprint(
            mechanism="joint evidence",
            intervention="dual-source estimate",
            observables_and_data="two public APIs",
            analysis_unit="paired cohort",
            timescale="one month",
            system_boundary="two services",
        ),
        experiment_objective="Acquire both evidence sources.",
        data_needs=(first_need, second_need),
        predicted_outcomes=("first acquired", "second acquired"),
    )
    sources = (
        PublicSource("public_api", "https://first.example/data"),
        PublicSource("public_api", "https://second.example/data"),
    )
    command = make_acquisition_command(
        reservation_id="reservation_" + "5" * 64,
        node_id="two-need-download-node",
        attempt_id="attempt_two_need_downloads",
        direction=direction,
        needs=(
            NeedPlan(0, first_need, (sources[0],)),
            NeedPlan(1, second_need, (sources[1],)),
        ),
        budget=AcquisitionBudget(8, 3, 30),
    )
    transport = ScriptedTransport(
        _response(404),
        _response(
            200,
            body=b"[1]",
            headers=(("content-type", "application/json"),),
        ),
        _response(404),
        _response(
            200,
            body=b"[2]",
            headers=(("content-type", "application/json"),),
        ),
    )
    root = tmp_path / "cache"
    first = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("first.example", "second.example"),
        clock=FixedClock(),
    ).acquire(command)

    assert isinstance(first, AcquisitionCheckpoint)
    assert first.reason == "download_budget"
    assert len(first.cursor.completed) == 1
    assert len(transport.requests) == 2
    resumed = PublicAcquisition(
        root,
        transport=transport,
        resolver=_resolver("first.example", "second.example"),
        clock=FixedClock(),
    ).acquire(command, cursor=first.cursor)
    assert isinstance(resumed, AcquisitionComplete)
    assert len(transport.requests) == 4
