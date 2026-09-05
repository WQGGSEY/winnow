"""Prospective development amendments that preserve the registered success bar."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from research_harness.evaluation_vault import sealed_bank_metadata

from research_harness.orchestrator.research_control import (
    PLANNING_POLICY_VERSION, StaleResearchWork, _digest, _read, _write,
    analysis_findings, current_work, development_evidence, execution_inventory,
)
from research_harness.orchestrator.research_review import review_research_packet


def revise_evaluation_protocol(repo: Path, thread: Path, *, work_id: str, notes: str, rationale: str, replace_holdout: bool = False) -> dict[str, Any]:
    work = current_work(thread)
    if work.get('work_id') == work_id and work.get('status') == 'completed' and work.get('outcome', {}).get('protocol_revision'):
        prior = _read(Path(work['outcome']['protocol_revision']))
        if (prior['protocol']['notes'] != notes or prior['rationale'] != rationale
                or bool(prior.get('replacement_holdout_bank')) != replace_holdout):
            raise ValueError('This work already committed another amendment; plan a new work.')
        return {**work, 'research_work_checkpoint': work_id}
    if (work.get('work_id') != work_id or work.get('status') != 'planned'
            or work['decision']['kind'] != 'protocol_revision'):
        raise ValueError('Plan a protocol_revision work before proposing an amendment.')
    if work.get('planning_policy_version') != PLANNING_POLICY_VERSION:
        raise StaleResearchWork('Research planning policy changed; call plan_research_work.')
    if not notes.strip() or not rationale.strip():
        raise ValueError('An amendment needs replacement protocol notes and a development rationale.')
    production = thread / 'production'
    if (thread / 'market/baseline_qualification.json').exists():
        raise ValueError('Baseline assignments are qualified; this development amendment window is closed.')
    state = _read(production / 'tree/search_state.json')
    if state.get('promoted_node_ids') or any(
        path.name in {'falsifier_result.json', 'user_goal_attestation.json', 'strong_result_receipt.json'}
        for path in production.rglob('*.json')
    ):
        raise ValueError('Final evaluation or scientific promotion is recorded; a new study is required.')
    evidence = development_evidence(thread)
    if work['evidence_digest'] != _digest(evidence):
        raise StaleResearchWork('Development evidence changed; call plan_research_work.')
    envelope_path = production / 'feasibility_envelope.json'
    existing = _read(envelope_path)
    bank = sealed_bank_metadata(thread) if replace_holdout else None
    if replace_holdout and bank is None:
        raise ValueError('A server-sealed evaluation bank is required before proposing partition replacement.')
    identity = {'work_id': work_id, 'notes': notes, 'rationale': rationale}
    if bank:
        identity['replacement_holdout_bank'] = bank
    directory = production / 'protocol_revisions' / _digest(identity)
    request_path = directory / 'request.json'
    packet = _read(request_path)
    if packet:
        if existing not in (packet['previous_protocol'], packet['proposal']):
            raise StaleResearchWork('Another protocol revision arrived; call plan_research_work.')
    else:
        if work.get('protocol_digest') != _digest(existing):
            raise StaleResearchWork('Registered protocol changed; call plan_research_work.')
        if notes == existing.get('notes'):
            raise ValueError('The amendment must change the protocol notes.')
        packet = {
            'previous_protocol': existing, 'proposal': {**existing, 'notes': notes},
            'rationale': rationale, 'work_decision': work['decision'],
            'original_goal': _read(production / 'reorientation/goal_contract.json'),
            'development_evidence': evidence, 'thread_dir': str(thread.resolve()),
            'analysis_findings': analysis_findings(thread),
            'execution_inventory': execution_inventory(thread),
            'replacement_holdout_bank': bank,
            'protocol_history': approved_protocol_revisions(thread),
            'execution_plans': [str(path.resolve()) for path in sorted((production / 'tree').rglob('experiment_plan.json'))],
        }
        _write(request_path, packet)
    partition_rules = (
        'This proposal retires the ENTIRE previous confirmation partition and registers the server-sealed replacement bank. '
        'The transaction in this tool is the registration capability; do not rely on historical claims that no such tool exists. '
        'Its concealed content digest has been verified by the harness; inspect public generator/source/provenance only, never private content. '
        'Require the exact bank ID in the notes, explicit retirement of all old cells, disclosure of known contamination and uncertain access history, '
        'and an explicit new prospective registration. Prior observations are development evidence only. '
        'Do not demand an untouched old partition or exhaustive historical reconstruction to retire it. '
        'A new sampling distribution is permissible only if it still answers the original user question, has no performance-based selection, '
        'preserves task endpoints, success thresholds, statistical units, sample counts, comparator competence and fair comparison rules. '
        'Inspect the simulator to identify actual independent factors; different integer seeds are not automatically different conditions. '
        'Disclose any changed sampling distribution or balanced starting conditions and restrict eventual conclusions to that population. '
        'A pinned generator and a specified seed law define a sampling frame without enumerating every layout. '
        'Inspect supplied sampling provenance and distinguish reconstructed records from contemporaneous receipts. '
        'Compare endpoint and qualification definitions with protocol_history when the current notes only contain a termination notice. '
        'Do not approve retrospective confirmation or changes intended to turn an observed failure into a pass. '
    ) if bank else (
        'Preserve the held-out partition. Independently inspect executed source and available execution records to establish that the reserved holdout has not been evaluated or inspected. '
        'Absence of a falsifier receipt and author assertions do not establish this. Reject if access history is incomplete or uncertain; do not read holdout outcomes yourself. '
    )
    review = review_research_packet(repo, directory / 'review', packet, purpose=(
        'Review a prospective development protocol amendment. Only the notes may change; structured goal, resources and predicate remain identical. '
        'Verify that the same task endpoints, units, success thresholds, dependence-aware uncertainty and fair comparison rules '
        'are preserved in meaning, not merely in their names. Reject any semantic lowering, outcome redefinition, post-hoc exclusion or retroactive approval. '
        'Removing an untested candidate-specific design lock can be legitimate before final evaluation; development results may guide method selection '
        'but not tune the held-out success bar. Require eventual comparator qualification and a fixed candidate/checkpoint before final evaluation. '
        + partition_rules +
        'Check the amendment against the original user problem, not an accidental earlier method choice. '
        'The previous protocol and its failure history will remain disclosed. Do not call this the original preregistration or approve any scientific claim.'
    ))
    if review['assessment']['decision'] != 'approve':
        work.update(protocol_review={**review['assessment'], 'request_path': str(request_path.resolve())},
                    reconsideration_available=True)
        _write(production / 'research_control/current.json', work)
        _write(production / 'research_control/work' / work_id / 'work.json', work)
        return {'status': 'rejected', 'review': review, 'work_id': work_id,
                'reconsideration_available': True,
                'next_tool_to_call': 'revise_evaluation_protocol'}
    if _read(envelope_path) != existing or _digest(development_evidence(thread)) != work['evidence_digest']:
        raise StaleResearchWork('Protocol or evidence changed during review; call plan_research_work.')
    record = {'work_id': work_id, 'previous_protocol': packet['previous_protocol'],
              'protocol': packet['proposal'], 'rationale': rationale, 'review': review,
              'recorded_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'prospective development amendment; not original preregistration or scientific approval'}
    if bank:
        if sealed_bank_metadata(thread) != bank:
            raise StaleResearchWork('Sealed bank changed during protocol review.')
        record['replacement_holdout_bank'] = bank
    _write(envelope_path, packet['proposal'])
    if not (directory / 'approved.json').exists():
        _write(directory / 'approved.json', record)
    work.pop('reconsideration_available', None)
    work['protocol_review'] = {**review['assessment'], 'request_path': str(request_path.resolve())}
    work.update(status='completed', outcome={
        'execution_result': 'protocol_revised', 'protocol_revision': str((directory / 'approved.json').resolve()),
        'new_observation': False, 'scientific_verdict': 'unverified',
    }, next_tool_to_call='plan_research_work')
    _write(production / 'research_control/current.json', work)
    _write(production / 'research_control/work' / work_id / 'work.json', work)
    return {**work, 'research_work_checkpoint': work_id}


def approved_protocol_revisions(thread: Path) -> list[dict[str, Any]]:
    return [_read(path) for path in sorted((thread / 'production/protocol_revisions').glob('*/approved.json'))]
