"""Bind independent baseline decisions to the exact proposal and replayed evidence."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from research_harness.memory.baseline_dossier import dossier_path, load_baseline_dossier, validate_baseline_selection, validate_baseline_dossier
from research_harness.orchestrator.research_review import review_research_packet


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
    tree = (thread_dir / 'production/tree').resolve()
    artifacts = {}
    for assignment in qualification['assignments']:
        receipt = assignment['reproducibility_receipt']
        for key in ('experiment_plan_path', 'worker_report_path'):
            path = (tree / receipt[key]).resolve()
            if not path.is_relative_to(tree):
                raise ValueError('baseline review artifact escapes the thread tree')
            artifacts[str(path)] = json.loads(path.read_text())
    goal_path = thread_dir / 'production/reorientation/goal_contract.json'
    goal = json.loads(goal_path.read_text()) if goal_path.exists() else {}
    return {'qualification': qualification, 'dossier': dossier, 'candidate_details': details,
            'execution_verification': verified, 'artifacts': artifacts,
            'pending_assignment_goal': goal if goal.get('baseline_evidence') == [] else None}


def _digest(packet: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def baseline_roles_frozen(thread_dir: Path) -> bool:
    """Freeze approved implementations, not the question that precedes them."""
    path = thread_dir / 'production/reorientation/goal_contract.json'
    if not path.exists():
        return False
    contract = json.loads(path.read_text())
    return contract.get('baseline_evidence') != [] or (thread_dir / 'market/baseline_qualification.json').exists()


def require_goal_baseline_approval(repo: Path, thread_dir: Path, contract: dict[str, Any]) -> None:
    """A goal frozen without baseline assignments still requires their later review."""
    if contract.get('baseline_evidence') != []:
        return
    path = thread_dir / 'market/baseline_qualification.json'
    if not path.exists():
        raise ValueError('baseline qualification is pending; submit_baseline_qualification before promoting a comparative claim')
    require_baseline_approval(repo, thread_dir, json.loads(path.read_text()))


def update_baseline_sources(repo: Path, thread_dir: Path, dossier: dict[str, Any], details: dict[str, str]) -> dict[str, Any]:
    """Store unqualified literature candidates without granting arbitrary file writes."""
    if baseline_roles_frozen(thread_dir):
        raise ValueError('baseline sources are frozen for the approved baseline roles')
    brief = json.loads((thread_dir / 'market/market_research_brief.json').read_text())
    dossier = json.loads(json.dumps(dossier))
    identifier = brief['baseline_dossier_id']
    if not re.fullmatch(r'[A-Za-z0-9_-]+', identifier) or dossier['id'] != identifier:
        raise ValueError('only this thread baseline dossier may be updated')
    if dossier.get('selected') is not None:
        raise ValueError('source updates cannot select or approve baselines')
    candidates = dossier['candidates_index']
    ids = [candidate['id'] for candidate in candidates]
    if len(set(ids)) != len(ids) or set(details) != set(ids):
        raise ValueError('provide exactly one detail text per unique candidate')
    revision = _digest({'dossier': dossier, 'details': details})
    for index, candidate in enumerate(candidates):
        if candidate['decision'] != 'unqualified' or not isinstance(details[candidate['id']], str) or not details[candidate['id']].strip():
            raise ValueError('candidates need unqualified status and nonempty source details')
        candidate['detail_file'] = f'{identifier}/{revision}/{index}.md'
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary)
        for candidate in candidates:
            path = staging / candidate['detail_file']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(details[candidate['id']])
        validate_baseline_dossier(repo, dossier, base_dir=staging)
        destination = repo / 'memory/baseline_dossiers'
        for candidate in candidates:
            path = destination / candidate['detail_file']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((staging / candidate['detail_file']).read_text())
        _write(destination / f'{identifier}.yaml', dossier)
    return {'status': 'recorded', 'dossier_id': identifier, 'revision': revision, 'scientific_approval': False}


def _record_qualification(thread_dir: Path, qualification: dict[str, Any]) -> None:
    _write(thread_dir / 'market/baseline_qualification.json', qualification)
    state_path = thread_dir / 'production/tree/search_state.json'
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text())
    references = [{
        'baseline_dossier_id': qualification['dossier_id'],
        'candidate_ids': [row['candidate_id'] for row in qualification['assignments']],
        'roles': [row['role'] for row in qualification['assignments']],
    }]
    for node in state['nodes']:
        if (not node.get('baseline_refs') and node.get('status') not in {'pruned', 'archived'}
                and (node.get('strategy') or {}).get('derived_from_direction_id')):
            node['baseline_refs'] = references
            node_path = state_path.parent / 'nodes' / node['id'] / 'node.json'
            if node_path.exists():
                stored = json.loads(node_path.read_text())
                stored['baseline_refs'] = references
                _write(node_path, stored)
    _write(state_path, state)


def propose_baselines(repo: Path, thread_dir: Path, qualification: dict[str, Any]) -> dict[str, Any]:
    packet = _packet(repo, thread_dir, qualification)
    digest = _digest(packet)
    root = thread_dir / 'market/baseline_reviews'
    review_path = root / f'{digest}.review.json'
    if review_path.exists():
        review = json.loads(review_path.read_text())
        if review.get('reviewer') == 'independent-research-review':
            if review['decision'] == 'approve':
                _record_qualification(thread_dir, qualification)
            return {'status': 'ok' if review['decision'] == 'approve' else 'rejected', 'review': review}
    proposal_path = root / f'{digest}.proposal.json'
    _write(proposal_path, packet)
    previous = [json.loads(path.read_text()) for path in sorted(root.glob('*.review.json'))]
    review = review_research_packet(
        repo, root / 'independent', {'proposal': packet, 'prior_reviews': previous},
        purpose='Qualify these baseline roles for the proposed research task.',
    )
    # Reopen the evidence after review; a changed artifact needs a fresh review.
    if _digest(_packet(repo, thread_dir, qualification)) != digest:
        raise ValueError('baseline evidence changed during independent review')
    assessment = review['assessment']
    record = {
        'proposal_digest': digest, 'decision': assessment['decision'],
        'reason': assessment['reason'], 'evidence': assessment['evidence'],
        'required_work': assessment['required_work'], 'reviewer': review['reviewer'],
        'review_request_sha256': review['request_sha256'],
    }
    _write(review_path, record)
    if record['decision'] == 'approve':
        _record_qualification(thread_dir, qualification)
    return {'status': 'ok' if record['decision'] == 'approve' else 'rejected',
            'proposal_digest': digest, 'review': record,
            'next_step': 'Continue research.' if record['decision'] == 'approve' else 'Resolve the review findings and submit revised execution evidence.'}


def require_baseline_approval(repo: Path, thread_dir: Path, qualification: dict[str, Any]) -> None:
    digest = _digest(_packet(repo, thread_dir, qualification))
    path = thread_dir / 'market/baseline_reviews' / f'{digest}.review.json'
    record = json.loads(path.read_text()) if path.exists() else {}
    if record.get('decision') != 'approve' or record.get('reviewer') != 'independent-research-review':
        raise ValueError('baseline scientific review is missing or rejected for these exact artifacts')
