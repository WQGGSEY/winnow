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
    return {
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
    if assessment['result_kind'] == 'informative' and not any(item['effect'] != 'unresolved' for item in updates):
        raise ValueError('An informative result must change at least one competing prediction.')
    if assessment['result_kind'] == 'inconclusive' and not assessment['missing_evidence']:
        raise ValueError('An inconclusive result must identify the missing discriminating evidence.')
