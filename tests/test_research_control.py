import copy
import json
from pathlib import Path

import pytest

from research_harness.agent_runtime import AgentUsage, CompletionResult
from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.experiment_plan import build_demo_experiment_plan
from research_harness.orchestrator.research_control import (
    bind_work, current_work, development_evidence, finish_work, plan_research_work,
)
from research_harness.runner.baseline_preflight import execute_baseline_preflight

REPO = Path(__file__).resolve().parents[1]


class Planner:
    def __init__(self, kind='diagnostic', budget=10):
        self.calls = []
        self.kind = kind
        self.budget = budget

    def complete(self, request):
        packet = json.loads(request.prompt.input)
        self.calls.append(packet)
        decision = {
            'kind': self.kind, 'uncertainty': 'Is the measurement implementation valid?',
            'evidence_ids': list(packet['development_evidence']),
            'interpretation': 'Execution validity must be established before testing the mechanism.',
            'alternatives': [
                {'explanation': 'The implementation is broken.', 'prediction': 'Independent replay disagrees.',
                 'decision_if_observed': 'Repair the transition construction.'},
                {'explanation': 'The implementation is valid.', 'prediction': 'Independent replay agrees.',
                 'decision_if_observed': 'Measure learning competence.'},
            ],
            'test': 'Compare an independent replay with stored measurements.',
            'next_if_inconclusive': 'Inspect the first divergence in the raw trace.',
            'max_runtime_seconds': self.budget,
        }
        return CompletionResult(text=json.dumps(decision), usage=AgentUsage(), thread_id='test')


def fixture(tmp_path):
    thread = tmp_path / 'thread'
    tree = thread / 'production/tree'
    tree.mkdir(parents=True)
    (thread / 'thread.json').write_text(json.dumps({'user_goal': 'Explain a plateau.'}))
    (thread / 'production/feasibility_envelope.json').write_text(json.dumps({
        'compute_budget': {'max_runner_seconds_per_node': 30, 'max_total_node_hours': 1},
    }))
    node = _demo_node()
    plan = build_demo_experiment_plan(node, tree)
    requirement = plan['baseline_evidence_requirements'][2]
    plan['baseline_evidence_requirements'] = [requirement]
    plan['resources']['timeout_sec'] = 10
    plan['source_files'] = [{'path': 'experiment.py', 'purpose': 'Reproduce invalid measurement.',
                             'content': "raise ValueError('measurement construction is invalid')\n"}]
    return thread, tree, node, plan, requirement['role']


def test_actual_execution_failure_changes_next_work_without_refuting_claim(tmp_path):
    thread, tree, node, plan, role = fixture(tmp_path)
    planner = Planner()
    work = plan_research_work(REPO, thread, transport=planner)
    assert plan_research_work(REPO, thread, transport=planner) == work
    assert len(planner.calls) == 1
    with pytest.raises(ValueError, match='work_id'):
        bind_work(thread, 'wrong', node['id'], plan)
    expensive = copy.deepcopy(plan)
    expensive['resources']['timeout_sec'] = 30
    with pytest.raises(ValueError, match='budget'):
        bind_work(thread, work['work_id'], node['id'], expensive)
    bind_work(thread, work['work_id'], node['id'], plan)
    result = execute_baseline_preflight(REPO, thread, node=node, plan=plan, role=role, settings={})
    checkpoint = finish_work(thread, result)
    assert checkpoint['research_work_checkpoint'] == work['work_id']
    assert checkpoint['next_tool_to_call'] == 'plan_research_work'
    assert current_work(thread)['outcome']['scientific_verdict'] == 'unverified'
    assert not (tree / 'search_state.json').exists()
    assert list(development_evidence(thread).values())[0]['error'] == ['ValueError: measurement construction is invalid']
    with pytest.raises(ValueError, match='diagnostic'):
        plan_research_work(REPO, thread, transport=Planner('comparison'))
    next_work = plan_research_work(REPO, thread, transport=planner)
    assert next_work['work_id'] != work['work_id']
    packet = planner.calls[-1]
    assert packet['diagnostic_required']
    assert packet['previous_work']['outcome']['observation']['execution_status'] == 'failed'
    assert next_work['decision']['evidence_ids'] == [node['id']]


def test_restart_reconciles_reserved_work_and_never_reads_final_holdout(tmp_path):
    thread, _, node, plan, role = fixture(tmp_path)
    work = plan_research_work(REPO, thread, transport=Planner())
    bind_work(thread, work['work_id'], node['id'], plan)
    execute_baseline_preflight(REPO, thread, node=node, plan=plan, role=role, settings={})
    (thread / 'production/falsifier_result.json').write_text('{"secret_holdout": "DO_NOT_USE"}')
    planner = Planner()
    following = plan_research_work(REPO, thread, transport=planner)
    assert following['status'] == 'planned'
    assert 'DO_NOT_USE' not in json.dumps(planner.calls)
    assert planner.calls[0]['previous_work']['status'] == 'completed'


def test_dispatch_rejection_preserves_question_and_allows_corrected_input(tmp_path):
    thread, _, node, plan, _ = fixture(tmp_path)
    planner = Planner()
    work = plan_research_work(REPO, thread, transport=planner)
    bind_work(thread, work['work_id'], node['id'], plan)
    result = finish_work(thread, {'status': 'rejected', 'reason': 'preflight must use the operator-selected input snapshot'})
    resumed = plan_research_work(REPO, thread, transport=planner)
    assert resumed['work_id'] == work['work_id']
    assert resumed['status'] == 'planned'
    assert resumed['outcome']['reason'] == result['reason']
    assert 'research_work_checkpoint' not in result
    assert len(planner.calls) == 1
    corrected = copy.deepcopy(plan)
    corrected['inputs'] = []
    bind_work(thread, work['work_id'], node['id'], corrected)
