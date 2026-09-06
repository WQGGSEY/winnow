"""Prospective development amendments that preserve the registered success bar."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import hashlib
from typing import Any
from research_harness.evaluation_vault import sealed_bank_metadata
from research_harness.confirmation_sampling import read_sampling_spec, active_sampling_registration

from research_harness.orchestrator.research_control import (
    PLANNING_POLICY_VERSION, StaleResearchWork, _digest, _read, _write,
    analysis_findings, current_work, development_evidence, execution_inventory, executed_diagnostic_bindings,
)
from research_harness.orchestrator.research_review import review_research_packet


def revise_evaluation_protocol(repo: Path, thread: Path, *, work_id: str, notes: str, rationale: str, replace_holdout: bool = False, defer_holdout_generation: bool = False) -> dict[str, Any]:
    if defer_holdout_generation and not replace_holdout:
        raise ValueError('Deferred sampling requires retirement of the old holdout.')
    work = current_work(thread)
    if work.get('work_id') == work_id and work.get('status') == 'completed' and work.get('outcome', {}).get('protocol_revision'):
        prior = _read(Path(work['outcome']['protocol_revision']))
        if (prior['protocol']['notes'] != notes or prior['rationale'] != rationale
                or bool(prior.get('replacement_holdout_bank') or prior.get('replacement_sampling_spec')) != replace_holdout
                or bool(prior.get('replacement_sampling_spec')) != defer_holdout_generation):
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
    if (production / 'confirmation_execution.json').exists():
        raise ValueError('Confirmation sampling has begun; this protocol cannot be amended.')
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
    bank = sealed_bank_metadata(thread) if replace_holdout and not defer_holdout_generation else None
    sampling_spec = read_sampling_spec(thread) if defer_holdout_generation else None
    if replace_holdout and bank is None and sampling_spec is None:
        raise ValueError('A server-sealed bank or registered future sampling procedure is required.')
    prepared = work.get('prepared_implementation')
    if work['decision']['protocol_change'] == 'component_binding' and not prepared:
        raise ValueError('This work requires actual component binding. Use design_experiment_template with this same work_id to prepare source bytes first; another promise to prepare cannot complete it.')
    if prepared:
        for source in prepared['source_files']:
            if hashlib.sha256(Path(source['path']).read_bytes()).hexdigest() != source['sha256']:
                raise ValueError('Prepared source changed; prepare a new revision before binding.')
    identity = {'work_id': work_id, 'notes': notes, 'rationale': rationale}
    if prepared:
        identity['prepared_source'] = prepared
    if bank:
        identity['replacement_holdout_bank'] = bank
    if sampling_spec:
        identity['replacement_sampling_spec'] = sampling_spec
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
            'prepared_source': prepared,
            'original_goal': _read(production / 'reorientation/goal_contract.json'),
            'development_evidence': evidence, 'thread_dir': str(thread.resolve()),
            'analysis_findings': analysis_findings(thread),
            'execution_inventory': execution_inventory(thread),
            'executed_diagnostic_bindings': executed_diagnostic_bindings(thread),
            'replacement_holdout_bank': bank,
            'protocol_history': approved_protocol_revisions(thread),
            'replacement_sampling_spec': sampling_spec,
            'active_sampling_registration': active_sampling_registration(thread),
            'execution_plans': [str(path.resolve()) for path in sorted((production / 'tree').rglob('experiment_plan.json'))],
        }
        _write(request_path, packet)
    if sampling_spec:
        partition_rules = (
            'This proposal retires ALL previous confirmation banks and registers a fixed future sampling PROCEDURE. '
            'Its source, inputs, arguments, sample counts and seed law are committed before data collection. '
            'The harness will freeze the evaluated implementation, checkpoints and measurement program before executing this sampler exactly once. '
            'A realized bank ID is intentionally unavailable before future random draws. Do not require completed data collection or an access audit '
            'of nonexistent future data to approve a prospective sampling procedure. The realized bank digest must bind the eventual execution receipt. '
            'Review the committed sampler and its relevance to the original question. Preserve endpoint meanings, thresholds, statistical units, '
            'sample counts, uncertainty calculation and fair comparison. Generation failures abort; no outcome-selected retries. '
            'Disclose the new population, retirement and access limitations of old banks, and the new registration timing. '
            'The supplied specification is the authoritative resource identity and is persisted with approval; notes need not retype every hash. '
            'This approves the future design only, never a realization, method, result or claim. '
        )
    elif not replace_holdout and active_sampling_registration(thread):
        partition_rules = (
            'Preserve the active, approved future sampling specification in protocol history. No confirmation execution ledger exists. '
            'The confirmation population is defined by the committed sampler and future random draws; there is no existing confirmation dataset to audit. '
            'Retired banks must remain excluded. Do not demand creation of future data merely to amend a development method binding. '
        )
    else:
        partition_rules = (
        'This proposal retires the ENTIRE previous confirmation partition and registers the server-sealed replacement bank. '
        'The transaction in this tool is the registration capability; do not rely on historical claims that no such tool exists. '
        'Its concealed content digest has been verified by the harness; inspect public generator/source/provenance only, never private content. '
        'Require the exact bank ID in the notes, explicit retirement of all old cells, disclosure of known contamination and uncertain access history, '
        'and an explicit new prospective registration. Prior observations are development evidence only. '
        'Do not demand an untouched old partition or exhaustive historical reconstruction to retire it. '
        'A new sampling distribution is permissible only if it still answers the original user question, has no performance-based selection, '
        'preserves task endpoints, success thresholds, statistical units, sample counts and fair comparison rules. '
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
        'The proposal must complete the selected work_decision, not substitute a promise to do that work later. '
        'For repair of an executed diagnostic, compare against executed_diagnostic_bindings and inspect its verified approval trace and actual source, '
        'not only an older component-binding amendment. The diagnostic pre-execution transaction can already have approved instrumentation and reporting repairs. '
        'Do not demand their reversion merely because those exact diagnostic bytes were bound through that transaction instead of a separate protocol amendment. '
        'Require disclosure of further changes and preserve task semantics, endpoint definitions and pass criteria. Process exit success is not an audit pass when structured metrics report a contradiction. '
        'For protocol_change=component_binding, inspect prepared_source files and verify the proposal actually binds those concrete paths and hashes '
        'with the requested method semantics. Reject a preparation-only authorization in place of component binding. '
        'Verify that the same task endpoints, units, success thresholds, dependence-aware uncertainty and fair comparison rules '
        'are preserved in meaning, not merely in their names. Reject any semantic lowering, outcome redefinition, post-hoc exclusion or retroactive approval. '
        'Removing an untested candidate-specific design lock can be legitimate before final evaluation; development results may guide method selection '
        'but not tune the held-out success bar. Require eventual comparator qualification and a fixed candidate/checkpoint before final evaluation. '
        'A failed study configuration is not the failure of the original research question. A prospective NEW-study amendment may redesign qualification procedures '
        'when an earlier procedure prevents investigating the stated phenomenon, but must preserve the old failed verdict and its evidence. '
        'Require an explicit scientific justification, faithful source implementation, credible tuning budgets, task-feasibility controls and an appropriate strong comparator. '
        'A failed execution or checkpoint cannot be relabeled as qualified. This is not a permanent ban on its algorithm family or source code. '
        'A prospective new study may reuse a method after a scientifically justified change to an invalid task setup, implementation or development procedure. '
        'Require the amendment to name the demonstrated cause, changed variable, fresh execution, budget and stopping rule; preserve and disclose all failed attempts. '
        'Review explicit replacement of earlier method-reuse prohibitions on those grounds. Do not require a different algorithm name when it leaves the failure cause unchanged. '
        'Do not approve unchanged outcome-selected retries, reuse failed receipts as successful qualification, or merely reduce a threshold to approve the old result. '
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
    if prepared and any(hashlib.sha256(Path(source['path']).read_bytes()).hexdigest() != source['sha256']
                        for source in prepared['source_files']):
        raise ValueError('Prepared source changed during component review; prepare a new revision.')
    record = {'work_id': work_id, 'previous_protocol': packet['previous_protocol'],
              'protocol': packet['proposal'], 'rationale': rationale, 'review': review,
              'recorded_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'prospective development amendment; not original preregistration or scientific approval'}
    if prepared:
        record['prepared_source'] = prepared
    if bank:
        if sealed_bank_metadata(thread) != bank:
            raise StaleResearchWork('Sealed bank changed during protocol review.')
        record['replacement_holdout_bank'] = bank
    if sampling_spec:
        if read_sampling_spec(thread) != sampling_spec:
            raise StaleResearchWork('Future sampling specification changed during review.')
        record['replacement_sampling_spec'] = sampling_spec
    _write(envelope_path, packet['proposal'])
    if not (directory / 'approved.json').exists():
        _write(directory / 'approved.json', record)
    work.pop('reconsideration_available', None)
    work.pop('protocol_review_error', None)
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


def protocol_note_history(thread: Path) -> dict[str, Any]:
    """Follow installed amendments backwards, excluding unrelated or rejected proposals."""
    current = _read(thread / 'production/feasibility_envelope.json')
    records = [(path, _read(path)) for path in (thread / 'production/protocol_revisions').glob('*/approved.json')]
    records.sort(key=lambda item: item[1]['recorded_at'], reverse=True)
    chain = []
    for path, record in records:
        if record['protocol'] == current:
            chain.append({'notes': current.get('notes', ''), 'receipt_path': str(path.resolve()),
                          'recorded_at': record['recorded_at']})
            current = record['previous_protocol']
    entries = [{'notes': current.get('notes', ''), 'scope': 'Protocol before the first linked amendment'}, *reversed(chain)]
    return {'interpretation': 'Read in chronological order. A later amendment supersedes only the terms it explicitly changes. '
                             'References to unchanged prior rules inherit their definitions from earlier entries. '
                             'Retired banks, replaced methods and superseded restrictions are historical, not active requirements.',
            'entries': entries}
