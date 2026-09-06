"""Acquire primary sources for ongoing work through the existing HTTP boundary."""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from research_harness.acquisition.cache import ContentAddressedCache
from research_harness.acquisition.http_receipt import PolicyReceiptStore
from research_harness.acquisition.http_runtime import HttpAcquirer, HttpStepComplete, HttpStepCheckpoint, SystemAcquisitionClock
from research_harness.acquisition.live_http import SocketDnsResolver, StdlibOneHopTransport
from research_harness.acquisition.model import NeedPlan, PublicSource, serialize_response_receipt, parse_response_receipt
from research_harness.orchestrator.direction_generation import DataNeed
from research_harness.orchestrator.research_control import _read, _write, _digest, current_work


@dataclass(frozen=True)
class WorkSourceRequest:
    command_id: str
    needs: tuple[NeedPlan, ...]


def retrieved_sources(thread: Path) -> dict:
    records = {}
    for path in sorted((thread / 'market/research_sources').glob('*/receipt.json')):
        receipt = _read(path)
        if receipt['source_id'] != 'source_' + _digest({'owner': receipt['owner'], 'url': receipt['url']}):
            raise ValueError('Source receipt identity differs from its work and URL.')
        if receipt.get('acquisition_receipt'):
            acquired = parse_response_receipt(receipt['acquisition_receipt'])
            policy = PolicyReceiptStore(thread / 'production/reorientation/acquisition_cache').load(acquired.policy_receipt_id)
            if (policy.requested_uri != receipt['url'] or policy.cache_object != acquired.cache_object
                    or policy.final_uri != acquired.source_uri
                    or receipt['raw']['sha256'] != acquired.cache_object.content_sha256[7:]):
                raise ValueError('Source receipt differs from its HTTP acquisition provenance.')
        for field in ('raw', 'text'):
            artifact = receipt.get(field)
            if artifact:
                target = Path(artifact['path']).resolve()
                target.relative_to(thread.resolve())
                if hashlib.sha256(target.read_bytes()).hexdigest() != artifact['sha256']:
                    raise ValueError('Retrieved source changed: ' + str(target))
        records[receipt['source_id']] = {**receipt, 'receipt_path': str(path.resolve())}
    return records


def acquire_source(thread: Path, work_id: str, url: str, *, transport=None, resolver=None, clock=None) -> dict:
    from research_harness.orchestrator.research_control import PLANNING_POLICY_VERSION

    work = current_work(thread)
    if (work.get('work_id') != work_id or work.get('status') != 'planned'
            or work['decision']['kind'] != 'analysis' or work['decision'].get('source_mode') != 'acquire'):
        raise ValueError('Plan an analysis with source_mode=acquire before retrieving its source.')
    if work.get('planning_policy_version') != PLANNING_POLICY_VERSION:
        raise ValueError('Research capabilities changed; call plan_research_work.')
    receipt = _retrieve_source(thread, {'kind': 'research_work', 'id': work_id}, url,
                               work['decision']['uncertainty'], transport=transport, resolver=resolver, clock=clock)
    work['next_tool_to_call'] = 'resolve_research_work'
    _write(thread / 'production/research_control/current.json', work)
    _write(thread / 'production/research_control/work' / work_id / 'work.json', work)
    return {**receipt, 'next_tool_to_call': 'resolve_research_work'}


def acquire_reference_source(thread: Path, reference_id: str, url: str, *, transport=None, resolver=None, clock=None) -> dict:
    papers = _read(thread / 'market/market_research_brief.json').get('papers', [])
    papers += _read(thread / 'market/paper_references.json').get('papers', [])
    if reference_id not in {paper['id'] for paper in papers}:
        raise ValueError('Retrieve bibliographic metadata with search_paper_references before binding a reference source.')
    return _retrieve_source(thread, {'kind': 'reference', 'id': reference_id}, url,
                            'Inspect the primary material for reference ' + reference_id,
                            transport=transport, resolver=resolver, clock=clock)


def _retrieve_source(thread: Path, owner: dict, url: str, description: str, *, transport=None, resolver=None, clock=None) -> dict:
    source = PublicSource(kind='public_page', uri=url)
    identifier = 'source_' + _digest({'owner': owner, 'url': url})
    records = retrieved_sources(thread)
    if identifier in records:
        return records[identifier]
    if sum(record['owner'] == owner for record in records.values()) >= 3:
        raise ValueError('Three source attempts are recorded. Analyze them before planning another acquisition work.')
    destination = thread / 'market/research_sources' / identifier
    destination.mkdir(parents=True, exist_ok=True)
    cache_root = thread / 'production/reorientation/acquisition_cache'
    clock = clock or SystemAcquisitionClock()
    cache = ContentAddressedCache(cache_root, monotonic=clock.monotonic)
    http = HttpAcquirer(transport=transport or StdlibOneHopTransport(),
                        resolver=resolver or SocketDnsResolver(), cache=cache, cache_root=cache_root,
                        policy_receipts=PolicyReceiptStore(cache_root), clock=clock, user_agent='ResearchHarness')
    request = WorkSourceRequest('acqcmd_' + _digest({'owner': owner, 'url': url}),
        (NeedPlan(need_index=0, need=DataNeed(kind='public_page', description=description),
                  candidates=(source,)),))
    result = http.acquire(command=request, need_index=0, source_candidate_index=0, source=source,
                          deadline=clock.monotonic() + 30, request_budget=8, download_budget=20 * 1024 * 1024)
    receipt = {'source_id': identifier, 'owner': owner, 'url': url, 'retrieved_at': clock.iso_now(),
               'scope': 'Source acquisition, not scientific validation or confirmation evidence'}
    if isinstance(result, HttpStepComplete):
        acquired = result.acquired.receipt
        raw_path = cache.object_path(acquired.cache_object)
        receipt.update(status='retrieved', acquisition_receipt=serialize_response_receipt(acquired),
                       raw={'path': str(raw_path), 'sha256': acquired.cache_object.content_sha256[7:]})
        text_path = destination / 'source.txt'
        try:
            raw = raw_path.read_bytes()
            if raw.startswith(b'%PDF-'):
                subprocess.run(['pdftotext', '-layout', str(raw_path), str(text_path)],
                               check=True, capture_output=True, timeout=30)
            else:
                text_path.write_text(raw.decode('utf-8'))
            if not text_path.read_text().strip():
                raise ValueError('No extractable text; inspect the retained raw source.')
            receipt['text'] = {'path': str(text_path.resolve()), 'sha256': hashlib.sha256(text_path.read_bytes()).hexdigest()}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            receipt.update(status='raw_only', extraction_error=str(exc))
    elif isinstance(result, HttpStepCheckpoint):
        receipt.update(status='deferred', reason=result.reason)
    else:
        receipt.update(status='unavailable', reason=result.required_external_action)
    _write(destination / 'receipt.json', receipt)
    return receipt
