"""Bind researcher baseline decisions to the exact proposal and replayed evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from research_harness.memory.baseline_dossier import dossier_path, load_baseline_dossier, validate_baseline_selection
from research_harness.orchestrator.operator_prompts import enqueue_prompt, list_pending, submit_response


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def _packet(repo: Path, thread_dir: Path, qualification: dict[str, Any]) -> dict[str, Any]:
    from research_harness.settings_scoped import resolve_for_thread

    brief = json.loads((thread_dir / 'market/market_research_brief.json').read_text())
    if qualification.get('dossier_id') != brief.get('baseline_dossier_id'):
        raise ValueError("qualification must use this thread's researched dossier")
    dossier = load_baseline_dossier(repo, qualification['dossier_id'])
    verified = validate_baseline_selection(
        repo,
        dossier,
        qualification,
        artifact_root=thread_dir / 'production/tree',
        runner_settings=resolve_for_thread(repo, thread_dir.name),
    )
    base = dossier_path(repo, qualification['dossier_id']).parent
    details = {candidate['id']: (base / candidate['detail_file']).read_text() for candidate in dossier['candidates_index']}
    return {'qualification': qualification, 'dossier': dossier, 'candidate_details': details, 'execution_verification': verified}


def _digest(packet: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def propose_baselines(repo: Path, thread_dir: Path, qualification: dict[str, Any]) -> dict[str, Any]:
    packet = _packet(repo, thread_dir, qualification)
    digest = _digest(packet)
    root = thread_dir / 'market/baseline_reviews'
    review_path = root / f'{digest}.review.json'
    if review_path.exists():
        review = json.loads(review_path.read_text())
        return {'status': 'ok' if review['decision'] == 'approve' else 'rejected', 'review': review}
    proposal_path = root / f'{digest}.proposal.json'
    _write(proposal_path, packet)
    event = enqueue_prompt(
        thread_dir, kind='decision_request', source_rail='baseline_scientific_review',
        event_id=f'opr_baseline_{digest[:16]}',
        prompt=f'Assess baseline method fidelity, source sufficiency, comparator suitability and metric validity. Execution alone is not approval. Review packet: {proposal_path}',
    )
    return {'status': 'awaiting_operator', 'proposal_digest': digest, 'proposal_path': str(proposal_path), 'event_id': event['event_id']}


def review_baselines(repo: Path, thread_dir: Path, *, proposal_digest: str, decision: str, reason: str) -> dict[str, Any]:
    if len(proposal_digest) != 64 or any(c not in '0123456789abcdef' for c in proposal_digest):
        raise ValueError('invalid proposal digest')
    if decision not in {'approve', 'reject'} or not isinstance(reason, str) or not reason.strip():
        raise ValueError('approve/reject decision and a concrete reason are required')
    if (thread_dir / 'production/reorientation/goal_contract.json').exists():
        raise ValueError('goal contract is frozen; start a new research revision')
    root = thread_dir / 'market/baseline_reviews'
    packet = json.loads((root / f'{proposal_digest}.proposal.json').read_text())
    current = _packet(repo, thread_dir, packet['qualification'])
    if _digest(current) != proposal_digest:
        raise ValueError('baseline proposal or execution evidence changed since review request')
    record = {'proposal_digest': proposal_digest, 'decision': decision, 'reason': reason.strip(), 'reviewer': 'operator'}
    review_path = root / f'{proposal_digest}.review.json'
    if review_path.exists() and json.loads(review_path.read_text()) != record:
        raise ValueError('review is immutable; submit a revised proposal')
    _write(review_path, record)
    if decision == 'approve':
        _write(thread_dir / 'market/baseline_qualification.json', packet['qualification'])
    event_id = f'opr_baseline_{proposal_digest[:16]}'
    if any(item['event_id'] == event_id for item in list_pending(thread_dir)):
        submit_response(thread_dir, event_id=event_id, response=f'{decision}: {reason.strip()}')
    return record


def require_baseline_approval(repo: Path, thread_dir: Path, qualification: dict[str, Any]) -> None:
    digest = _digest(_packet(repo, thread_dir, qualification))
    path = thread_dir / 'market/baseline_reviews' / f'{digest}.review.json'
    if not path.exists() or json.loads(path.read_text()).get('decision') != 'approve':
        raise ValueError('baseline scientific review is missing or rejected for these exact artifacts')
