import json
from copy import deepcopy
from pathlib import Path

import pytest

from research_harness.agent_runtime import AgentUsage, CompletionResult
from research_harness.orchestrator.hypothesis_development import develop_hypotheses


REPO = Path(__file__).resolve().parents[1]


def candidate(index):
    return {
        'explanation_role': 'null_or_confound' if index == 0 else 'mechanism',
        'proposal': {'claim': f'Intervention {index} improves utility.',
                     'fingerprint': {axis: f'{axis} {index}' for axis in ('mechanism', 'intervention', 'observables_and_data', 'analysis_unit', 'timescale', 'system_boundary')},
                     'experiment_objective': 'Distinguish the mechanism from measurement error.', 'data_needs': [],
                     'predicted_outcomes': ['Effect persists with independent measurement.', 'Effect disappears under calibration.']},
        'causal_rationale': 'A resource mismatch changes the observed outcome.',
        'competing_explanation': 'The measurement is biased.',
        'discriminating_test': 'Compare independent measurements under a controlled intervention.',
        'supporting_outcome': 'Independent measurements agree on the intervention effect.',
        'refuting_outcome': 'Calibration removes the apparent effect.',
        'critical_assumptions': ['Independent measurements are available.'], 'source_ids': [],
        'source_limitations': 'No empirical method source retrieved yet.',
        'novelty_vs_closest_work': 'Novelty is unresolved.', 'test_budget_seconds': 30,
        'test_prerequisites': ['Calibrate measurements.'],
    }


def critique(ready):
    return {'assessments': [{'candidate_index': i, 'ready_for_test': ready, 'reason': 'Review of the proposed test.',
                             'required_changes': [] if ready else ['Specify independent calibration.']} for i in range(3)],
            'preferred_index': 1, 'selection_reason': 'Best separates the competing explanations.'}


def thread(tmp_path):
    (tmp_path / 'thread.json').write_text(json.dumps({'user_goal': 'Explain an observed performance plateau.'}))
    (tmp_path / 'connector').mkdir()
    (tmp_path / 'connector/connector_session.json').write_text(json.dumps({'claims': [
        {'claim_contract': {'claim_under_test': 'A prospective connector idea.'}}
    ]}))
    return tmp_path


def test_hypotheses_are_visible_before_qualification_and_revised_from_critique(tmp_path):
    tdir = thread(tmp_path)
    draft = {'candidates': [candidate(i) for i in range(3)]}
    revision = deepcopy(draft)
    revision['candidates'][1]['discriminating_test'] += ' Use an independently calibrated reference.'
    responses = [draft, critique(False), revision, critique(True)]
    requests = []

    class Transport:
        def complete(self, request):
            if not requests:
                visible = json.loads((tdir / 'production/hypotheses/current.json').read_text())
                assert visible['candidates'][0]['claim_under_test'] == 'A prospective connector idea.'
            requests.append(request)
            return CompletionResult(text=json.dumps(responses.pop(0)), usage=AgentUsage(), thread_id='test')

    for _ in range(4):
        result = develop_hypotheses(REPO, tdir, transport=Transport())
    assert result['status'] == 'ready_for_diagnostic'
    assert result['candidates'][1]['selected_for_diagnostic']
    assert all(row['scientific_support'] == 'unverified' for row in result['candidates'])
    assert not (tdir / 'market/baseline_qualification.json').exists()
    assert not (tdir / 'production/tree/search_state.json').exists()
    assert 'critique' in json.loads(requests[2].prompt.input)
    assert develop_hypotheses(REPO, tdir, transport=Transport()) == result
    assert len(requests) == 4


def test_unknown_source_does_not_become_a_reviewed_hypothesis(tmp_path):
    tdir = thread(tmp_path)
    draft = {'candidates': [candidate(i) for i in range(3)]}
    draft['candidates'][0]['source_ids'] = ['invented-paper']

    requests = []

    class Transport:
        def complete(self, request):
            requests.append(request)
            return CompletionResult(text=json.dumps(draft), usage=AgentUsage(), thread_id='test')

    with pytest.raises(ValueError, match='sources absent'):
        develop_hypotheses(REPO, tdir, transport=Transport())
    visible = json.loads((tdir / 'production/hypotheses/current.json').read_text())
    assert visible['status'] == 'developing'
    assert not any(row['selected_for_diagnostic'] for row in visible['candidates'])
    draft['candidates'][0]['source_ids'] = []
    result = develop_hypotheses(REPO, tdir, transport=Transport())
    assert result['status'] == 'awaiting_critique'
    assert 'previous_invalid_response' in json.loads(requests[-1].prompt.input)


def test_contradictory_critique_is_rejected_and_returned_for_correction(tmp_path):
    tdir = thread(tmp_path)
    invalid = critique(True)
    invalid['assessments'][0]['required_changes'] = ['Remove the measurement confound.']
    responses = [{'candidates': [candidate(i) for i in range(3)]}, invalid, critique(True)]
    requests = []

    class Transport:
        def complete(self, request):
            requests.append(request)
            return CompletionResult(text=json.dumps(responses.pop(0)), usage=AgentUsage(), thread_id='test')

    develop_hypotheses(REPO, tdir, transport=Transport())
    with pytest.raises(ValueError, match='unresolved required changes'):
        develop_hypotheses(REPO, tdir, transport=Transport())
    visible = json.loads((tdir / 'production/hypotheses/current.json').read_text())
    assert not any(row['selected_for_diagnostic'] for row in visible['candidates'])
    result = develop_hypotheses(REPO, tdir, transport=Transport())
    assert result['status'] == 'ready_for_diagnostic'
    feedback = json.loads(requests[-1].prompt.input)['previous_invalid_response']
    assert feedback['response'] == invalid
    assert 'not ready' in feedback['validation_error']
