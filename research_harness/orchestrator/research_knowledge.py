"""Development interpretations linked to work, predictions and source receipts.

This projection does not promote claims or turn an agent's interpretation into
validated scientific evidence. It is also available before publication gates.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def work_history(thread: Path) -> list[tuple[Path, dict[str, Any]]]:
    records = [(path, json.loads(path.read_text())) for path in
               (thread / 'production/research_control/work').glob('*/work.json')]
    return sorted(records, key=lambda item: (item[1].get('created_at_ns', item[0].stat().st_mtime_ns),
                                            item[1]['work_id']))


def research_brief(thread: Path) -> dict[str, Any]:
    history = work_history(thread)
    decisions = []
    for path, work in history:
        interpretation = work.get('decision', {}).get('previous_result')
        if interpretation:
            decisions.append({**interpretation, 'next_work_id': work['work_id'],
                              'claim_ids': work.get('claim_ids', []),
                              'receipt_path': str(path.resolve())})
    solution_history = [{'work_id': work['work_id'], 'status': work['status'],
                         'path': work['decision']['solution_path'], 'receipt_path': str(path.resolve())}
                        for path, work in history if work.get('decision', {}).get('solution_path')]
    return {
        'solution_history': solution_history,
        'current_solution': solution_history[-1] if solution_history else None,
        'scope': 'Working research brief. Interpretations are fallible development judgments, not validated claims or a submission-ready manuscript.',
        'result_interpretations': decisions,
        'uninterpreted_completed_work_ids': [work['work_id'] for _, work in history
            if work.get('status') == 'completed' and work['work_id'] not in
            {item['work_id'] for item in decisions}],
        'work_index': [{
            'work_id': work['work_id'], 'claim_ids': work.get('claim_ids', []),
            'hypothesis_ids': work.get('decision', {}).get('hypothesis_ids', []),
            'status': work['status'], 'kind': work.get('decision', {}).get('kind'),
            'question': work.get('decision', {}).get('uncertainty'),
            'evidence_ids': work.get('decision', {}).get('evidence_ids', []),
            'superseded_by': work.get('superseded_by'), 'receipt_path': str(path.resolve()),
        } for path, work in history],
        'publication_questions': [
            'What contribution is supported, and which observations support each part?',
            'What is the closest primary source, and what difference has actually been established?',
            'Which alternative explanations remain and what evidence would distinguish them?',
            'Which claims still need generalization, replication or prospective confirmation?',
        ],
    }


def brief_context(thread: Path, brief: dict[str, Any]) -> dict[str, Any]:
    """Bound default prompt growth; retain the full dependency index on disk."""
    return {
        'scope': brief['scope'],
        'current_solution': brief.get('current_solution'),
        'recent_solution_revisions': brief.get('solution_history', [])[-3:],
        'result_interpretations': brief['result_interpretations'][-8:],
        'older_interpretation_count': max(0, len(brief['result_interpretations']) - 8),
        'work_count': len(brief['work_index']),
        'full_history_path': str((thread / 'production/research_control/research_brief.json').resolve()),
        'publication_questions': brief['publication_questions'],
        'history_usage': 'Older decisions are not refuted or forgotten by this view. Read the full work/evidence dependency index when the current question depends on them.',
    }


def validate_previous_result(decision: dict[str, Any], previous: dict[str, Any],
                             available_evidence: set[str]) -> None:
    """Check interpretation provenance and operational scope, not scientific truth."""
    assessment = decision['previous_result']
    if previous.get('status') != 'completed':
        if assessment is not None:
            raise ValueError('There is no completed previous work to interpret.')
        return
    if not assessment or assessment['work_id'] != previous['work_id']:
        raise ValueError('Interpret the completed previous work before choosing new work.')
    if set(assessment['evidence_ids']) - available_evidence or not assessment['evidence_ids']:
        raise ValueError('Previous-result interpretation requires available evidence citations.')
    if 'work_' + previous['work_id'] not in assessment['evidence_ids']:
        raise ValueError('Cite the actual previous work receipt when interpreting its result.')
    outcome = previous.get('outcome', {})
    observation = outcome.get('observation') or {}
    execution_failed = (observation.get('execution_status') not in (None, 'completed') or
                        observation.get('measurement_status') not in (None, 'completed') or
                        outcome.get('execution_result') in {'execution_failed', 'interrupted', 'rejected'})
    updates = assessment['prediction_updates']
    predictions = previous['decision'].get('alternatives', [])
    if sorted(item['alternative_index'] for item in updates) != list(range(len(predictions))):
        raise ValueError('Interpret every previous competing prediction exactly once.')
    if execution_failed:
        if assessment['result_kind'] != 'execution_failure' or any(item['effect'] != 'unresolved' for item in updates):
            raise ValueError('An execution or measurement failure cannot support or weaken a scientific prediction.')
    elif assessment['result_kind'] == 'execution_failure':
        raise ValueError('Execution failure requires a failed execution or measurement receipt.')
    support = outcome.get('measurement_support')
    if not execution_failed and support is not None and not support['evaluable']:
        if assessment['result_kind'] != 'inconclusive':
            raise ValueError('Missing or empty eligible observations cannot establish an informative result.')
    if assessment['result_kind'] == 'inconclusive' and any(item['effect'] != 'unresolved' for item in updates):
        raise ValueError('Inconclusive evidence leaves competing predictions unresolved.')
    if (outcome.get('execution_result') == 'analysis_inconclusive'
            and assessment['result_kind'] != 'inconclusive'):
        raise ValueError('An unresolved source analysis cannot become an informative result.')
    if (assessment['result_kind'] == 'informative'
            and previous['decision'].get('inquiry_mode') != 'exploration'
            and not any(item['effect'] != 'unresolved' for item in updates)):
        raise ValueError('An informative result must change at least one competing prediction.')
    if assessment['result_kind'] == 'inconclusive' and not assessment['missing_evidence']:
        raise ValueError('An inconclusive result must identify the missing discriminating evidence.')


def validate_solution_path(decision: dict[str, Any], brief: dict[str, Any]) -> None:
    current = brief.get('current_solution')
    expected_parent = current['work_id'] if current else None
    if decision['solution_path']['parent_work_id'] != expected_parent:
        raise ValueError('Link solution_path to the current explanation work ID; do not discard its history.')
    if decision['inquiry_mode'] != 'exploration' and len(decision['alternatives']) < 2:
        raise ValueError('Discrimination and intervention need at least two competing predictions.')


def compact_planning_context(thread: Path, packet: dict[str, Any]) -> dict[str, Any]:
    """Move bulky secondary material to exact, addressable files for targeted reads."""
    import copy
    import hashlib

    compact = copy.deepcopy(packet)
    paths = [('protocol_note_history',), ('implementation_context',),
             ('executed_diagnostic_bindings',), ('active_sampling_registration',),
             ('research', 'sources'), ('research', 'connector_candidates'),
             ('hypotheses', 'candidates')]
    for keys in paths:
        parent = compact
        for key in keys[:-1]:
            parent = parent.get(key, {})
        value = parent.get(keys[-1])
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
        if len(raw) <= 12000:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        path = thread / 'production/research_control/context' / (digest + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        parent[keys[-1]] = {
            'context_reference': str(path.resolve()), 'sha256': digest,
            'bytes': len(raw), 'field': '.'.join(keys),
            'keys': list(value) if isinstance(value, dict) else [],
            'item_ids': [item.get('id') for item in value if isinstance(item, dict)] if isinstance(value, list) else [],
            'usage': 'Full material is retained, not absent. Read the relevant entries before relying on their requirements, implementation or scientific content. Later protocol amendments do not erase unchanged earlier clauses.',
        }
    previous = compact.get('previous_work', {})
    previous.pop('source_observations', None)  # Already supplied as development_evidence.
    compact['authoritative_previous_result'] = {
        'work_id': previous.get('work_id'), 'status': previous.get('status'),
        'outcome': previous.get('outcome'),
        'usage': 'This is the latest result. Previous decision text describes intent and older failures, not the cause of this result. Use measurement_error/report_path here before proposing a repair.',
    }
    return compact
