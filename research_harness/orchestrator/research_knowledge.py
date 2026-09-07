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


def failed_measurement(previous: dict[str, Any]) -> bool:
    outcome = previous.get('outcome', {})
    observation = outcome.get('observation') or {}
    return (observation.get('execution_status') not in (None, 'completed') or
            observation.get('measurement_status') not in (None, 'completed') or
            outcome.get('execution_result') in {'execution_failed', 'interrupted', 'rejected'})


def planning_response_schema(previous: dict[str, Any], *, available_evidence: set[str],
                             available_hypotheses: set[str]) -> dict[str, Any]:
    """Constrain receipt facts before generation; scientific judgments remain open."""
    from research_harness.schemas.validator import load_schema
    schema = load_schema('research_work')
    del schema['properties']['required_observations']
    schema['required'].remove('required_observations')
    solution = schema['properties']['solution_path']
    del solution['properties']['parent_work_id']
    solution['required'].remove('parent_work_id')
    def bound_prose(node: Any) -> None:
        if isinstance(node, dict):
            if node.get('type') == 'string' and 'enum' not in node:
                node['maxLength'] = 1600
            for child in node.values():
                bound_prose(child)
        elif isinstance(node, list):
            for child in node:
                bound_prose(child)
    bound_prose(schema)
    schema['properties']['test']['maxLength'] = 3200
    evidence_ids = schema['properties']['evidence_ids']
    if available_evidence:
        evidence_ids['items']['enum'] = sorted(available_evidence)
    else:
        evidence_ids['maxItems'] = 0
    hypothesis_ids = schema['properties']['hypothesis_ids']
    if available_hypotheses:
        hypothesis_ids['items']['enum'] = sorted(available_hypotheses)
    else:
        hypothesis_ids['maxItems'] = 0
    field = schema['properties']['previous_result']
    if previous.get('status') != 'completed':
        schema['properties']['previous_result'] = {'type': 'null'}
        return schema
    assessment = field['anyOf'][1]
    schema['properties']['previous_result'] = assessment
    properties = assessment['properties']
    properties['evidence_ids']['items']['enum'] = sorted(available_evidence)
    properties['work_id']['enum'] = [previous['work_id']]
    count = len(previous['decision'].get('alternatives', []))
    updates = properties['prediction_updates']
    updates.update(minItems=count, maxItems=count)
    if count:
        updates['items']['properties']['alternative_index']['maximum'] = count - 1
    if failed_measurement(previous):
        properties['result_kind']['enum'] = ['execution_failure']
        updates['items']['properties']['effect']['enum'] = ['unresolved']
    elif previous.get('outcome', {}).get('execution_result') == 'analysis_inconclusive':
        properties['result_kind']['enum'] = ['inconclusive']
        updates['items']['properties']['effect']['enum'] = ['unresolved']
    else:
        from research_harness.orchestrator.research_observations import prediction_support
        eligible = prediction_support(previous)
        if previous.get('outcome', {}).get('measurement_support', {}).get('evaluable') is False:
            properties['result_kind']['enum'] = ['inconclusive', 'partial'] if any(
                row['eligible_for_interpretation'] for row in eligible) else ['inconclusive']
            if properties['result_kind']['enum'] == ['inconclusive']:
                updates['items']['properties']['effect']['enum'] = ['unresolved']
        else:
            properties['result_kind']['enum'] = ['inconclusive', 'informative']
    if count:
        import copy
        from research_harness.orchestrator.research_observations import prediction_support
        branches = []
        for row in prediction_support(previous):
            branch = copy.deepcopy(updates['items'])
            branch['properties']['alternative_index']['enum'] = [row['alternative_index']]
            if not row['eligible_for_interpretation']:
                branch['properties']['effect']['enum'] = ['unresolved']
            names = row['required_observations']
            if names:
                branch['properties']['observation_ids']['items']['enum'] = names
            else:
                branch['properties']['observation_ids']['maxItems'] = 0
            branches.append(branch)
        updates['items'] = {'anyOf': branches}
    return schema


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
    execution_failed = failed_measurement(previous)
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
        if assessment['result_kind'] not in {'inconclusive', 'partial'}:
            raise ValueError('Missing or empty eligible observations cannot establish an informative result.')
    from research_harness.orchestrator.research_observations import prediction_support
    eligibility = {row['alternative_index']: row for row in prediction_support(previous)}
    for update in updates:
        row = eligibility[update['alternative_index']]
        cited = set(update.get('observation_ids', []))
        if len(cited) != len(update.get('observation_ids', [])):
            raise ValueError('Prediction observation citations must be unique.')
        if cited - set(row['required_observations']):
            raise ValueError('Prediction update cites observations outside its declared dependencies.')
        if update['effect'] != 'unresolved' and support is not None:
            if not row['eligible_for_interpretation'] or cited != set(row['required_observations']):
                raise ValueError('A prediction update needs all its declared nonempty observations; another family cannot supply them.')
    if assessment['result_kind'] == 'partial':
        if not any(item['effect'] != 'unresolved' for item in updates) or not assessment['missing_evidence']:
            raise ValueError('Partial interpretation needs a supported update and an explicit unresolved limitation.')
        if support is None or support['evaluable']:
            raise ValueError('Partial interpretation requires a recorded partial measurement-support boundary.')
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
    # The selector receives findings, not a second assignment to inspect all code.
    research = compact.get('research', {})
    research.pop('research_brief', None)  # Already supplied at the top level.
    compact['analysis_findings'] = {
        key: {field: value[field] for field in ('question', 'status', 'conclusion_excerpt',
              'truncated', 'limitations', 'next_steps', 'evidence_ids', 'evidence_scope', 'receipt_path') if field in value}
        for key, value in compact.get('analysis_findings', {}).items()
    }
    paths = [('protocol_note_history',), ('implementation_context',),
             ('executed_diagnostic_bindings',), ('active_sampling_registration',),
             ('research', 'sources'), ('research', 'connector_candidates'),
             ('hypotheses', 'candidates'), ('prepared_implementations',),
             ('research', 'baseline_method_notes'), ('execution_inventory',), ('future_confirmation_sampling',)]
    for keys in paths:
        parent = compact
        for key in keys[:-1]:
            parent = parent.get(key, {})
        value = parent.get(keys[-1])
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
        if len(raw) <= 12000 and keys not in {('execution_inventory',), ('implementation_context',), ('future_confirmation_sampling',)}:
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
            'usage': 'Full material is retained for downstream source analysis and implementation review. This selector has not inspected it. Select a provisional work; do not claim source or protocol approval.',
        }
        if keys == ('hypotheses', 'candidates'):
            parent[keys[-1]]['preview'] = [
                {**{field: item[field] for field in ('id', 'claim_under_test', 'status', 'selected_for_diagnostic', 'scientific_support') if field in item},
                 'method_definition': item.get('candidate', {}).get('proposal', {}).get('fingerprint'),
                 'proposed_test': item.get('candidate', {}).get('discriminating_test')}
                for item in value]
        elif keys == ('research', 'sources'):
            parent[keys[-1]]['preview'] = {
                key: {field: item[field] for field in ('id', 'title', 'evidence_kind') if field in item}
                for key, item in value.items() if isinstance(item, dict)}
    previous = compact.get('previous_work', {})
    previous.pop('source_observations', None)  # Already supplied as development_evidence.
    compact['authoritative_previous_result'] = {
        'work_id': previous.get('work_id'), 'status': previous.get('status'),
        'outcome': previous.get('outcome'),
        'usage': 'This is the latest result. Previous decision text describes intent and older failures, not the cause of this result. Use measurement_error/report_path here before proposing a repair.',
    }
    if previous.get('status') == 'completed' and failed_measurement(previous):
        # No scientific observation was obtained: choose how to recover this test,
        # not a fresh literature/hypothesis search across the whole project.
        compact['selection_scope'] = 'The failed measurement leaves scientific predictions unresolved. Recover it or replace the procedure using concrete cost, feasibility or discriminating-value evidence. Existing-source analysis and small exploratory controls remain available. Do not treat an operational failure as scientific refutation.'
        compact['full_context_digest'] = hashlib.sha256(
            json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        compact['research'] = {key: compact.get('research', {}).get(key)
                              for key in ('research_question', 'problem_definition')}
        compact['previous_work'] = {key: previous[key] for key in
            ('work_id', 'status', 'decision', 'outcome', 'binding') if key in previous}
        compact['analysis_findings'] = dict(list(compact.get('analysis_findings', {}).items())[-2:])
        compact['development_evidence'] = dict(list(compact.get('development_evidence', {}).items())[-2:])
        for key in ('hypotheses', 'retrieved_sources', 'prepared_implementations',
                    'execution_inventory', 'future_confirmation_sampling',
                    'sealed_evaluation_bank', 'active_sampling_registration'):
            compact.pop(key, None)
        available = set(compact.get('available_evidence_ids', []))
        visible = (set(compact['analysis_findings']) | set(compact['development_evidence']) |
                   set(previous.get('decision', {}).get('evidence_ids', [])) |
                   {'work_' + previous['work_id']})
        compact['available_evidence_ids'] = sorted(available & visible)
        compact['repair_hypothesis_ids'] = previous.get('decision', {}).get('hypothesis_ids', [])
    from research_harness.orchestrator.research_observations import prediction_support
    completed = previous.get('status') == 'completed'
    if completed:
        relevant = set(previous.get('decision', {}).get('evidence_ids', []))
        findings = compact.get('analysis_findings', {})
        selected = [key for key in findings if key in relevant]
        selected = set(selected[-2:] + list(findings)[-2:])
        older = {key: value for key, value in findings.items() if key not in selected}
        if older:
            raw = json.dumps(older, sort_keys=True, ensure_ascii=False).encode()
            path = thread / 'production/research_control/context' / (hashlib.sha256(raw).hexdigest() + '.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            compact['older_analysis_reference'] = {'path': str(path.resolve()), 'keys': list(older),
                                                   'scope': 'Retained evidence; source analysis may retrieve a relevant entry.'}
            compact['analysis_findings'] = {key: value for key, value in findings.items() if key in selected}
        compact['development_evidence'] = dict(list(compact.get('development_evidence', {}).items())[-2:])
        compact['previous_work'] = {key: previous[key] for key in
            ('work_id', 'status', 'decision', 'outcome', 'binding') if key in previous}
    compact['decision_focus'] = {
        'previous_work_id': previous.get('work_id'),
        'prediction_support': prediction_support(previous),
        'existing_measurement_artifacts': list(compact.get('measurement_context', {})),
        'next_choice': 'Interpret the available part, identify the remaining limitation, then choose the smallest work that changes the next intervention decision toward the original goal.',
        'recovery_options': ['Use existing artifact values through source analysis when only reporting or interpretation is missing.',
                             'Replace an uninformative diagnostic instead of endlessly repairing it.',
                             'Try a bounded intuitive intervention with a matched control before a complete causal explanation.'],
        'scientific_limit': 'Support eligibility is not evidence that a prediction is true. Keep validity, scope, confounding and uncertainty explicit.'}
    compact['context_reading'] = 'This selector has no tools. Use visible findings to choose one provisional work. Exact references are for downstream analysis/implementation, which must inspect relevant source and full protocol history before relying on them. Missing detail warrants analysis only if it changes which scientific test to select.'
    return compact
