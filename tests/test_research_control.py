import copy
import json
from pathlib import Path

import pytest

from research_harness.agent_runtime import AgentUsage, CompletionResult
from research_harness.orchestrator.demo import _demo_node
from research_harness.orchestrator.experiment_plan import build_demo_experiment_plan
from research_harness.orchestrator.research_control import (
    bind_work, current_work, development_evidence, finish_work, plan_research_work,
    resolve_research_work,
    execution_inventory,
)
from research_harness.runner.baseline_preflight import execute_baseline_preflight

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('count', [None, 0])
def test_empty_measurement_cannot_be_interpreted_as_no_effect(tmp_path, monkeypatch, count):
    from research_harness.orchestrator import research_control
    from research_harness.orchestrator.research_knowledge import validate_previous_result, research_brief

    thread, _, node, plan, _ = fixture(tmp_path)
    work = plan_research_work(REPO, thread, transport=Planner())
    bind_work(thread, work['work_id'], node['id'], plan)
    research_control._write(thread / 'production/tree/baseline_preflight/measurement/experiment_plan.json', plan)
    measurement = {'execution_status': 'completed', 'measurement_status': 'completed',
                   'report_path': 'production/tree/baseline_preflight/measurement/worker_report.json',
                   'metrics': {'effect': 0, **({'eligible_count': count} if count is not None else {})},
                   'observation_digest': 'measurement'}
    monkeypatch.setattr(research_control, 'development_evidence', lambda thread, limit=8:
                        development_evidence(thread, limit=None) if limit is None else {node['id']: measurement})
    finish_work(thread, {'status': 'executed'})
    previous = current_work(thread)
    assert not previous['outcome']['measurement_support']['evaluable']
    class OverconfidentPlanner(Planner):
        def complete(self, request):
            response = super().complete(request)
            decision = json.loads(response.text)
            decision['previous_result']['result_kind'] = 'informative'
            decision['previous_result']['prediction_updates'][0]['effect'] = 'weakened'
            return CompletionResult(text=json.dumps(decision), usage=AgentUsage(), thread_id='test')

    with pytest.raises(ValueError, match='empty eligible'):
        plan_research_work(REPO, thread, transport=OverconfidentPlanner('analysis'))
    assert current_work(thread)['work_id'] == previous['work_id']
    corrected = Planner('analysis')
    next_work = plan_research_work(REPO, thread, transport=corrected)
    assert 'empty eligible' in corrected.calls[0]['previous_response_rejection']['error']
    forged = copy.deepcopy(next_work['decision'])
    forged['previous_result']['result_kind'] = 'informative'
    forged['previous_result']['prediction_updates'][0]['effect'] = 'weakened'
    with pytest.raises(ValueError, match='empty eligible'):
        validate_previous_result(forged, previous, {'work_' + previous['work_id']})
    brief = research_brief(thread)
    assert brief['result_interpretations'][-1]['result_kind'] == 'inconclusive'
    assert brief['result_interpretations'][-1]['work_id'] == previous['work_id']
    assert previous['work_id'] not in brief['uninterpreted_completed_work_ids']


def test_crash_cannot_weaken_scientific_prediction(tmp_path):
    from research_harness.orchestrator.research_knowledge import validate_previous_result

    thread, _, _, _, _ = fixture(tmp_path)
    previous = plan_research_work(REPO, thread, transport=Planner())
    previous.update(status='completed', outcome={'execution_result': 'execution_failed'})
    decision = {'previous_result': {'work_id': previous['work_id'], 'result_kind': 'informative',
        'evidence_ids': ['work_' + previous['work_id']],
        'prediction_updates': [{'alternative_index': i, 'effect': 'weakened'} for i in range(2)]}}
    with pytest.raises(ValueError, match='cannot support or weaken'):
        validate_previous_result(decision, previous, {'work_' + previous['work_id']})


def test_planning_consumes_critiqued_hypotheses_and_retains_work_link(tmp_path):
    from research_harness.orchestrator.research_control import _write, selected_hypotheses
    from research_harness.orchestrator.research_knowledge import research_brief, compact_planning_context

    thread, _, _, _, _ = fixture(tmp_path)
    _write(thread / 'production/hypotheses/current.json', {'status': 'completed', 'candidates': [
        {'id': 'hypothesis_1', 'claim_under_test': 'The measurement aliases distinct inputs.',
         'candidate': {'proposal': {'fingerprint': {'intervention': 'Use a rank-preserving feature map.',
                                                  'system_boundary': 'Keep the frozen input representation.'}},
                       'discriminating_test': 'Compare paired inputs with identical sampling.'},
         'selected_for_diagnostic': True, 'scientific_support': 'unverified'}]})

    class HypothesisPlanner(Planner):
        def complete(self, request):
            response = super().complete(request)
            packet = json.loads(request.prompt.input)
            decision = json.loads(response.text)
            decision['hypothesis_ids'] = [packet['hypotheses']['candidates'][0]['id']]
            return CompletionResult(text=json.dumps(decision), usage=AgentUsage(), thread_id='test')

    work = plan_research_work(REPO, thread, transport=HypothesisPlanner())
    assert work['decision']['hypothesis_ids'] == ['hypothesis_1']
    assert research_brief(thread)['work_index'][-1]['hypothesis_ids'] == ['hypothesis_1']
    hypotheses = selected_hypotheses(thread, work['decision'])
    assert list(hypotheses) == ['hypothesis_1']
    assert selected_hypotheses(thread, {'hypothesis_ids': []}) == {}
    candidate = hypotheses['hypothesis_1']
    candidate['long_history'] = 'historical critique ' * 1500
    compact = compact_planning_context(thread, {'hypotheses': {'candidates': [candidate]}})
    preview = compact['hypotheses']['candidates']['preview'][0]
    assert preview['method_definition'] == candidate['candidate']['proposal']['fingerprint']
    assert preview['proposed_test'] == candidate['candidate']['discriminating_test']


def test_execution_inventory_includes_preparation_without_reading_measurements(tmp_path):
    for scope, node_id in [('baseline_preflight', 'preflight'), ('nodes', 'formal')]:
        directory = tmp_path / 'production/tree' / scope / node_id
        (directory / 'workspace').mkdir(parents=True)
        (directory / 'job_manifest.json').write_text('{}')
        (directory / 'worker_report.json').write_text('unreadable scientific measurements')
        if scope == 'baseline_preflight':
            (directory / 'workspace/runner_result.json').write_text('{"status":"timeout"}')
    groups = execution_inventory(tmp_path)['groups']
    assert groups['baseline_preflight']['preflight'] == {'runner_status': 'timeout', 'runner_receipt_exists': True}
    assert groups['nodes']['formal'] == {'runner_status': None, 'runner_receipt_exists': False}


def test_historical_measurements_remain_discoverable_after_recent_window(tmp_path):
    from research_harness.orchestrator.research_control import development_result_index, focused_development_evidence
    for index in range(10):
        directory = tmp_path / 'production/tree/baseline_preflight' / f'measurement_{index}'
        workspace = directory / 'workspace'
        workspace.mkdir(parents=True)
        (directory / 'experiment_plan.json').write_text(json.dumps({'workspace': str(workspace),
                                                                   'objective': f'Question {index}'}))
        (directory / 'worker_report.json').write_text(json.dumps({'status': 'completed', 'metrics': {'value': index}}))
        (workspace / 'runner_result.json').write_text(json.dumps({'status': 'completed'}))
    (tmp_path / 'production/falsifier_result.json').write_text('must not read')
    assert 'measurement_0' not in development_evidence(tmp_path)
    index = development_result_index(tmp_path)
    assert len(index) == 10
    assert index['measurement_0']['declared_objective'] == 'Question 0'
    assert index['measurement_0']['metrics'] == {'value': 0}
    assert Path(index['measurement_0']['report_path']).is_file()
    focused = focused_development_evidence(tmp_path, development_evidence(tmp_path),
        {'decision': {'evidence_ids': ['measurement_0']}})
    assert set(focused) == {'measurement_0', 'measurement_8', 'measurement_9'}
    assert focused['measurement_0']['metrics'] == {'value': 0}


class Planner:
    def __init__(self, kind='diagnostic_experiment', budget=10, protocol_change=None):
        self.calls = []
        self.kind = kind
        self.budget = budget
        self.protocol_change = protocol_change or ('study_design' if kind == 'protocol_revision' else 'not_applicable')

    def complete(self, request):
        packet = json.loads(request.prompt.input)
        self.calls.append(packet)
        decision = {
            'inquiry_mode': 'discrimination',
            'solution_path': {'parent_work_id': (packet['research_brief'].get('current_solution') or {}).get('work_id'),
                'explanation': 'Measurements may be invalid.', 'unexplained_observations': 'Failure cause unknown.',
                'changed_assumption': 'None yet.', 'intervention': 'Repair only a measured defect.',
                'expected_goal_effect': 'Enable a valid learned solution.',
                'next_solution_decision': 'Choose repair or a learning intervention.',
                'original_scope_check': 'No task or endpoint changes.'},
            'previous_result': None,
            'hypothesis_ids': [],
            'source_mode': 'existing',
            'required_observations': [] if self.kind in {'analysis', 'protocol_revision'} else ['eligible_count'],
            'kind': self.kind, 'uncertainty': 'Is the measurement implementation valid?',
            'protocol_change': self.protocol_change,
            'evidence_ids': packet['available_evidence_ids'],
            'interpretation': 'Execution validity must be established before testing the mechanism.',
            'alternatives': [
                {'explanation': 'The implementation is broken.', 'prediction': 'Independent replay disagrees.',
                 'decision_if_observed': 'Repair the transition construction.'},
                {'explanation': 'The implementation is valid.', 'prediction': 'Independent replay agrees.',
                 'decision_if_observed': 'Measure learning competence.'},
            ],
            'test': 'Compare an independent replay with stored measurements.',
            'deferred_questions': ['Does a valid learner improve the team outcome?'],
            'next_if_inconclusive': 'Inspect the first divergence in the raw trace.',
            'max_runtime_seconds': self.budget,
        }
        for alternative in decision['alternatives']:
            alternative['required_observations'] = decision['required_observations']
        previous = packet['previous_work']
        if previous.get('status') == 'completed':
            outcome = previous.get('outcome', {})
            failed = outcome.get('execution_result') in {'execution_failed', 'interrupted', 'rejected'}
            decision['previous_result'] = {
                'work_id': previous['work_id'],
                'result_kind': 'execution_failure' if failed else 'inconclusive',
                'evidence_ids': ['work_' + previous['work_id']],
                'prediction_updates': [{'alternative_index': i, 'effect': 'unresolved', 'observation_ids': [],
                                        'reason': 'This record does not distinguish the predictions.'}
                                       for i, _ in enumerate(previous['decision']['alternatives'])],
                'missing_evidence': ['A valid distinguishing measurement.'],
                'next_decision': 'Inspect the first divergence in the raw trace.',
            }
        return CompletionResult(text=json.dumps(decision), usage=AgentUsage(), thread_id='test')


def fixture(tmp_path):
    thread = tmp_path / 'runs/threads/thread'
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
    plan['observation_bindings'] = {'eligible_count': {'artifact_path': plan['expected_outputs']['metrics_files'][0],
        'json_pointer': '/metrics/eligible_count', 'producer': 'experiment.py measurement output'}}
    plan['source_files'] = [{'path': 'experiment.py', 'purpose': 'Reproduce invalid measurement.',
                             'content': "raise ValueError('measurement construction is invalid')\n"}]
    return thread, tree, node, plan, requirement['role']


@pytest.mark.parametrize('explicit_node', [True, False])
def test_actual_execution_failure_changes_next_work_without_refuting_claim(tmp_path, monkeypatch, explicit_node):
    thread, tree, node, plan, role = fixture(tmp_path)
    if not explicit_node:
        plan.pop('workspace')
        plan['failure_index_hints'] = {}
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
    from research_harness import mcp_server
    from research_harness import settings_scoped
    from research_harness.orchestrator import research_review
    monkeypatch.setattr(mcp_server, '_repo_root', lambda: tmp_path)
    monkeypatch.setattr(mcp_server, '_thread_dir', lambda tid: thread)
    monkeypatch.setattr(settings_scoped, 'resolve_for_thread', lambda repo, tid: {})
    reviewed = []
    invalid_review = []
    budget_error = []
    def review(repo, directory, packet, *, purpose):
        if budget_error:
            from research_harness.adapters.call_budget import CallBudgetExhausted
            raise CallBudgetExhausted('bounded review budget exhausted')
        if invalid_review:
            raise research_review.ReviewContractError('Final confirmation requirements cannot block development.')
        assert current_work(thread)['execution_phase'] == 'implementation_review'
        reviewed.append(packet)
        return {'request_sha256': 'review', 'assessment': {
            'decision': 'reject' if len(reviewed) == 1 else 'approve',
            'reason': 'Check the actual measurement path.', 'evidence': ['experiment.py'],
            'required_work': ['Use the actual implementation.'] if len(reviewed) == 1 else [],
        }}
    monkeypatch.setattr(research_review, 'review_research_packet', review)
    request_path = thread / 'dispatch.json'
    request_plan = copy.deepcopy(plan)
    if not explicit_node:
        import hashlib
        source = thread / 'existing_measurement.py'
        source.write_text(plan['source_files'][0]['content'])
        request_plan['source_files'] = [{'path': 'experiment.py', 'purpose': plan['source_files'][0]['purpose'],
                                        'from_path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]
    if explicit_node:
        request_plan['role'] = role
    request_path.write_text(json.dumps({**({'node': node} if explicit_node else {}), 'experiment_plan': request_plan, 'work_id': work['work_id']}))
    outside = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(tmp_path / 'outside.json')})
    assert outside['status'] == 'rejected'
    conflicting_role = mcp_server.handle_execute_baseline_preflight({
        'thread_id': 'thread', 'experiment_plan': {**request_plan, 'role': 'diagnostic'}, 'work_id': work['work_id']})
    assert conflicting_role['status'] == 'rejected'
    assert 'role conflicts' in conflicting_role['reason']
    assert not reviewed
    budget_error.append(True)
    stopped = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})
    assert stopped['status'] == 'checkpoint'
    assert stopped['next_tool_to_call'] is None
    assert current_work(thread)['status'] == 'planned'
    assert not (tree / 'baseline_preflight' / node['id'] / 'job_manifest.json').exists()
    request_path = Path(stopped['dispatch_request_path'])
    preserved_request = request_path.read_bytes()
    from research_harness.orchestrator.research_control import execution_handoff
    source_handoff = execution_handoff(thread, current_work(thread))['current_source_files']
    assert len(source_handoff) == 1
    assert Path(source_handoff[0]['path']).read_text() == plan['source_files'][0]['content']
    if not explicit_node:
        assert json.loads(preserved_request)['experiment_plan']['source_files'] == plan['source_files']
        source.write_text('materialized successor replaced the referenced draft')
    refusal = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path),
        'updates': [{'path': ['experiment_plan', 'node_id'], 'value': 'unnecessary_rewrite'}]})
    assert refusal['status'] == 'resume_required'
    assert refusal['arguments']['request_path'] == str(request_path)
    assert request_path.read_bytes() == preserved_request
    assert mcp_server.handle_design_experiment_template({'thread_id': 'thread', 'work_id': work['work_id'],
        'plan_metadata': {'source_files': []}})['status'] == 'resume_required'
    budget_error.clear()
    invalid_review.append(True)
    invalid = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})
    assert invalid['status'] == 'review_invalid'
    assert invalid['next_tool_to_call'] == 'execute_baseline_preflight'
    assert current_work(thread)['status'] == 'planned'
    assert current_work(thread)['work_id'] == work['work_id']
    assert not (tree / 'baseline_preflight' / node['id'] / 'job_manifest.json').exists()
    invalid_review.clear()
    revision = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})
    assert revision['status'] == 'rejected'
    assert current_work(thread)['status'] == 'planned'
    assert not (thread / 'production/tree/baseline_preflight' / node['id'] / 'job_manifest.json').exists()
    assert mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})['status'] == 'rejected'
    assert len(reviewed) == 1
    malformed = copy.deepcopy(request_plan)
    malformed.pop('plan_id')
    malformed.pop('failure_index_hints')
    malformed['task_class'] = 'diagnostic_experiment'
    malformed['guardrails'].pop('allowed_write_roots')
    malformed['reproducibility'].pop('seed')
    malformed['reproducibility'].pop('code_snapshot')
    malformed['reproducibility']['sampling_seeds'] = [1, 2]
    rejected_input = mcp_server.handle_execute_baseline_preflight({
        'thread_id': 'thread', 'work_id': work['work_id'], 'experiment_plan': malformed})
    assert rejected_input['status'] == 'rejected'
    for field in ['plan_id', 'failure_index_hints', 'task_class', 'allowed_write_roots',
                  'seed', 'code_snapshot', 'sampling_seeds']:
        assert field in rejected_input['reason']
    assert len(reviewed) == 1
    assert current_work(thread)['status'] == 'planned'
    assert json.loads(Path(rejected_input['dispatch_request_path']).read_text())['experiment_plan'] == malformed
    assert revision['reconsideration_available']
    plan['source_files'][0]['content'] += '\n# revised measurement path\n'
    request_path.write_text(json.dumps({'node': node, 'experiment_plan': plan, 'role': role, 'work_id': work['work_id']}))
    budget_error.append(True)
    interrupted_revision = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})
    assert interrupted_revision['status'] == 'checkpoint'
    assert 'implementation_review' not in current_work(thread)
    assert current_work(thread)['prior_implementation_review']['decision'] == 'reject'
    budget_error.clear()
    import time
    invocation_budget = tmp_path / 'call_budget.json'
    invocation_budget.write_text(json.dumps({'deadline_epoch': time.time() + 6, 'calls': []}))
    monkeypatch.setenv('RESEARCH_HARNESS_CALL_BUDGET', str(invocation_budget))
    before_launch = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})
    assert before_launch['status'] == 'checkpoint'
    assert current_work(thread)['implementation_review']['decision'] == 'approve'
    assert current_work(thread)['status'] == 'planned'
    assert not (tree / 'baseline_preflight' / node['id'] / 'job_manifest.json').exists()
    assert json.loads(invocation_budget.read_text())['calls'] == []
    request_path = Path(before_launch['dispatch_request_path'])
    reviewed_count = len(reviewed)
    monkeypatch.delenv('RESEARCH_HARNESS_CALL_BUDGET')
    result = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})
    assert len(reviewed) == reviewed_count
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
    assert {node['id'], 'work_' + work['work_id']} <= set(next_work['decision']['evidence_ids'])
    plan['source_files'][0]['content'] += '\n# corrected request\n'
    request_path.write_text(json.dumps({**({'node': node} if explicit_node else {}), 'experiment_plan': plan, 'role': role, 'work_id': next_work['work_id']}))
    rejected = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})
    assert rejected['status'] == 'rejected'
    assert 'new node ID' in rejected['reason']
    assert current_work(thread)['status'] == 'planned'
    assert current_work(thread)['outcome']['reason'] == rejected['reason']
    assert Path(rejected['dispatch_request_path']).is_absolute()
    saved_path = Path(rejected['dispatch_request_path'])
    saved_bytes = saved_path.read_bytes()
    for bad_path in ['experiment_plan.node_id', ['experiment_plan', 'missing', 'node_id']]:
        bad_update = mcp_server.handle_execute_baseline_preflight({
            'thread_id': 'thread', 'work_id': next_work['work_id'],
            'request_path': str(saved_path),
            'updates': [{'path': ['experiment_plan', 'node_id'], 'value': 'not_committed'},
                        {'path': bad_path, 'value': 'n_corrected'}],
        })
        assert bad_update['status'] == 'rejected'
        assert bad_update['dispatch_request_path'] == str(saved_path)
        assert saved_path.read_bytes() == saved_bytes
        assert (saved_path.parent / 'rejected_dispatch_request.json').is_file()
        assert current_work(thread)['status'] == 'planned'
    corrected = mcp_server.handle_execute_baseline_preflight({
        'request_path': rejected['dispatch_request_path'],
        'updates': ([{'path': ['node', 'id'], 'value': 'n_corrected'}] if explicit_node else []) +
                   [{'path': ['experiment_plan', 'node_id'], 'value': 'n_corrected'},
                    {'path': ['experiment_plan', 'plan_id'], 'value': 'plan_corrected'},
                    {'path': ['experiment_plan', 'source_files', 0, 'purpose'], 'value': 'Corrected measurement diagnostic.'}],
    })
    assert corrected['status'] == 'execution_failed'
    assert current_work(thread)['binding']['node_id'] == 'n_corrected'


def test_restart_reconciles_reserved_work_and_never_reads_final_holdout(tmp_path):
    thread, _, node, plan, role = fixture(tmp_path)
    work = plan_research_work(REPO, thread, transport=Planner())
    bind_work(thread, work['work_id'], node['id'], plan)
    execute_baseline_preflight(REPO, thread, node=node, plan=plan, role=role, settings={})
    (thread / 'production/falsifier_result.json').write_text('{"secret_holdout": "DO_NOT_USE"}')
    class MissingLineagePlanner(Planner):
        def complete(self, request):
            from dataclasses import replace
            schema = json.loads(request.output_schema.read_text())
            assert 'parent_work_id' not in schema['properties']['solution_path']['properties']
            response = super().complete(request)
            decision = json.loads(response.text)
            del decision['solution_path']['parent_work_id']
            return replace(response, text=json.dumps(decision))
    planner = MissingLineagePlanner()
    following = plan_research_work(REPO, thread, transport=planner)
    assert following['status'] == 'planned'
    assert following['decision']['solution_path']['parent_work_id'] == work['work_id']
    assert 'DO_NOT_USE' not in json.dumps(planner.calls)
    assert planner.calls[0]['previous_work']['status'] == 'completed'


@pytest.mark.parametrize('work_kind', ['diagnostic_experiment', 'competence', 'comparison', 'replication'])
def test_diagnostic_executes_without_fabricating_a_baseline_or_supporting_a_claim(tmp_path, monkeypatch, work_kind):
    from research_harness.orchestrator import research_review
    from research_harness.orchestrator.experiment_plan import validate_experiment_plan
    from research_harness.runner.local_runner import LocalRunner
    from research_harness.schemas.validator import validate_named_schema
    from research_harness.orchestrator.research_control import executed_diagnostic_bindings, _digest, _write

    thread, tree, node, plan, _ = fixture(tmp_path)
    plan['baseline_evidence_requirements'] = []
    plan['mandatory_baselines'] = []
    node['claim_contract']['mandatory_baselines'] = []
    node['type'] = 'mechanism'
    with pytest.raises(ValueError):
        validate_named_schema('node', node)
    node['type'] = 'operational'
    with pytest.raises(ValueError, match='missing required baseline'):
        validate_experiment_plan(node, plan, tree)
    plan['source_files'][0]['content'] = (
        "from pathlib import Path\nPath('artifacts').mkdir(exist_ok=True)\n"
        "Path('artifacts/metrics.json').write_text('{\"metrics\": {\"measured_count\": 6}, \"claim_verdict_candidate\": \"supported\"}')\n"
    )
    plan['expected_outputs']['metrics_files'] = ['artifacts/metrics.json']
    with pytest.raises(ValueError, match='selected diagnostic'):
        execute_baseline_preflight(REPO, thread, node=node, plan=plan, role='diagnostic', settings={})
    work = plan_research_work(REPO, thread, transport=Planner(work_kind))
    bind_work(thread, work['work_id'], node['id'], plan)
    def fixture_review(repo, directory, packet, *, purpose):
        manifest = packet['execution_source_manifest']
        assert [source['relative_path'] for source in manifest] == ['experiment.py']
        assert Path(manifest[0]['path']).read_text() == plan['source_files'][0]['content']
        assert packet['analysis_findings'] == {}
        assert packet['other_analysis_index']['analysis_earlier'] == {
            'question': 'Earlier failed implementation?', 'status': 'answered', 'receipt_path': '/earlier/analysis.json'}
        request = {'packet': packet, 'purpose': purpose}
        digest = _digest(request)
        record = {'request_sha256': digest, 'assessment': {'decision': 'approve', 'reason': 'Fixture source review',
                                                         'evidence': [], 'required_work': []}}
        _write(directory / digest / 'request.json', request)
        _write(directory / digest / 'review.json', record)
        return record
    monkeypatch.setattr(research_review, 'review_research_packet', fixture_review)
    monkeypatch.setattr('research_harness.orchestrator.research_control.analysis_findings', lambda _: {
        'analysis_earlier': {'question': 'Earlier failed implementation?', 'status': 'answered',
                            'receipt_path': '/earlier/analysis.json', 'conclusion_excerpt': 'An earlier implementation failed.'}})
    original_execute = LocalRunner.execute
    def execute_with_binding(self, manifest):
        if work_kind == 'diagnostic_experiment':
            binding = current_work(thread)['diagnostic_source_binding']
            assert Path(binding['receipt_path']).is_file()
            assert set(binding['source_sha256']) == {'experiment.py'}
            assert binding['scientific_approval'] is False
        assert current_work(thread)['implementation_review']['decision'] == 'approve'
        return original_execute(self, manifest)
    monkeypatch.setattr(LocalRunner, 'execute', execute_with_binding)
    result = execute_baseline_preflight(REPO, thread, node=node, plan=plan, role='diagnostic',
                                       settings={}, research_work_id=work['work_id'])
    report = result['worker_report']
    assert result['status'] == 'executed'
    assert report['metrics'] == {'measured_count': 6}
    assert report['baselines'] == {}
    assert report['baseline_evidence_status'] == {'overall': 'not_required', 'results': []}
    assert report['claim_verdict_candidate'] == 'inconclusive'
    assert not result['scientific_approval']
    finish_work(thread, result)
    if work_kind != 'diagnostic_experiment':
        assert current_work(thread)['outcome']['scientific_verdict'] == 'unverified'
        return
    planner = Planner('protocol_revision')
    plan_research_work(REPO, thread, transport=planner)
    binding = planner.calls[0]['executed_diagnostic_bindings'][node['id']]
    assert binding['review_trace_verified']
    source = Path(binding['source_files'][0]['path'])
    assert source.read_text() == plan['source_files'][0]['content']
    source.write_text('changed after execution')
    with pytest.raises(ValueError, match='differs from its binding'):
        executed_diagnostic_bindings(thread)


@pytest.mark.parametrize('status', ['rejected', 'interrupted'])
def test_dispatch_rejection_preserves_question_and_allows_corrected_input(tmp_path, status):
    thread, _, node, plan, _ = fixture(tmp_path)
    planner = Planner()
    work = plan_research_work(REPO, thread, transport=planner)
    bind_work(thread, work['work_id'], node['id'], plan)
    result = finish_work(thread, {'status': status, 'reason': 'preflight must use the operator-selected input snapshot'})
    resumed = plan_research_work(REPO, thread, transport=planner)
    assert resumed['work_id'] == work['work_id']
    assert resumed['status'] == 'planned'
    assert resumed['outcome']['reason'] == result['reason']
    assert 'research_work_checkpoint' not in result
    assert len(planner.calls) == 1
    corrected = copy.deepcopy(plan)
    corrected['inputs'] = []
    bind_work(thread, work['work_id'], node['id'], corrected)
    assert 'outcome' not in current_work(thread)


def test_formal_experiment_review_rejects_before_dispatch_and_preserves_revision(tmp_path, monkeypatch):
    from research_harness import mcp_server
    from research_harness.orchestrator import experiment_plan, research_review
    from research_harness.orchestrator.search_state import initialize_search_state
    from research_harness.runner.local_runner import LocalRunner

    thread, tree, node, plan, _ = fixture(tmp_path)
    plan['baseline_evidence_requirements'] = build_demo_experiment_plan(node, tree)['baseline_evidence_requirements']
    state = initialize_search_state(search_id='s_review', root_node=node, policy={
        'max_depth': 5, 'max_debug_depth': 2, 'sunk_cost_policy': 'progress_gated',
        'scaleup_policy': 'disallow_by_default',
    })
    state_path = tree / 'search_state.json'
    state_path.write_text(json.dumps(state))
    work = plan_research_work(REPO, thread, transport=Planner())
    monkeypatch.setattr(mcp_server, '_thread_dir', lambda tid: thread)
    monkeypatch.setattr(mcp_server, '_require_authoritative_node', lambda *args: None)
    monkeypatch.setattr(experiment_plan, 'build_experiment_plan_for_node', lambda *args, **kwargs: (plan, True))
    calls = []
    def review(repo, directory, packet, *, purpose):
        calls.append(packet)
        return {'request_sha256': 'review', 'assessment': {'decision': 'reject',
                'reason': 'The surrogate does not call the collector under test.',
                'evidence': ['experiment.py'], 'required_work': ['Call the actual collector.']}}
    monkeypatch.setattr(research_review, 'review_research_packet', review)
    def no_dispatch(*args, **kwargs):
        raise AssertionError('Rejected implementation reached the runner')
    monkeypatch.setattr(LocalRunner, 'execute', no_dispatch)
    result = mcp_server.handle_execute_node_experiment({'thread_id': 'thread', 'node_id': node['id'], 'work_id': work['work_id']})
    assert result['status'] == 'rejected'
    assert 'surrogate' in result['reason']
    assert result['next_tool_to_call'] == 'design_experiment_template'
    assert current_work(thread)['status'] == 'planned'
    assert 'research_work_checkpoint' not in result
    assert len(calls) == 1
    assert json.loads(state_path.read_text()) == state
    assert not list(tree.rglob('job_manifest.json'))


def test_invalid_measurements_do_not_turn_runtime_into_research_evidence(tmp_path):
    thread, _, node, plan, role = fixture(tmp_path)
    payload = {'metrics': {'agreement': 0.0}, 'unexpected_observations': ['invalid observation shape']}
    plan['source_files'][0]['content'] = "from pathlib import Path\nPath('artifacts').mkdir(exist_ok=True)\nPath('artifacts/metrics.json').write_text(" + repr(json.dumps(payload)) + ")\n"
    result = execute_baseline_preflight(REPO, thread, node=node, plan=plan, role=role, settings={})
    assert result['status'] == 'execution_failed'
    first = development_evidence(thread)[node['id']]
    assert first['execution_status'] == 'completed'
    assert first['measurement_status'] == 'failed'
    assert first['measurement_error']
    assert first['metrics'] == {}
    report_path = thread / first['report_path']
    report = json.loads(report_path.read_text())
    report['metrics']['runner_elapsed_sec'] = 99
    report_path.write_text(json.dumps(report))
    assert development_evidence(thread)[node['id']]['observation_digest'] == first['observation_digest']


def test_analysis_resolves_existing_evidence_without_fabricating_execution(tmp_path, monkeypatch):
    from research_harness.orchestrator import research_review
    thread, tree, node, plan, _ = fixture(tmp_path)
    work = plan_research_work(REPO, thread, transport=Planner('analysis'))
    assert work['next_tool_to_call'] == 'resolve_research_work'
    with pytest.raises(ValueError, match='resolve_research_work'):
        bind_work(thread, work['work_id'], node['id'], plan)
    calls = []
    class Analyst:
        def complete(self, request):
            calls.append(json.loads(request.prompt.input))
            return CompletionResult(text=json.dumps({'status': 'answered',
                'answer': 'The source declares one epoch per update; competence is unmeasured.',
                'evidence': ['source method definition'], 'limitations': ['No learning competence measurement.'],
                'next_steps': ['Measure learning competence.']}), usage=AgentUsage(), thread_id='analysis')
    monkeypatch.setattr(research_review, 'CodexCliAdapter', Analyst)
    result = resolve_research_work(REPO, thread, work['work_id'])
    assert result == resolve_research_work(REPO, thread, work['work_id'])
    assert len(calls) == 1
    assert result['outcome']['execution_result'] == 'analysis_completed'
    assert not result['outcome']['new_observation']
    assert result['outcome']['scientific_verdict'] == 'unverified'
    assert not list(tree.rglob('worker_report.json'))

    planner = Planner('analysis')
    second = plan_research_work(REPO, thread, transport=planner)
    resolve_research_work(REPO, thread, second['work_id'])
    plan_research_work(REPO, thread, transport=planner)
    findings = planner.calls[-1]['analysis_findings']
    assert findings['analysis_' + work['work_id']]['limitations'] == ['No learning competence measurement.']
    assert set(findings) == {'analysis_' + work['work_id'], 'analysis_' + second['work_id']}
    assert all(row['conclusion_excerpt'].startswith('The source declares') for row in findings.values())


@pytest.mark.parametrize('replace_holdout,deferred,protocol_change', [
    (False, False, 'study_design'), (True, False, 'study_design'),
    (True, True, 'study_design'), (False, False, 'component_binding'),
])
def test_protocol_amendment_preserves_the_bar_and_requires_independent_review(tmp_path, monkeypatch, replace_holdout, deferred, protocol_change):
    from research_harness.orchestrator import protocol_revision
    from research_harness.publishing.manuscript import ManuscriptError, validate_sections

    thread, _, node, plan, _ = fixture(tmp_path)
    path = thread / 'production/feasibility_envelope.json'
    original = json.loads(path.read_text())
    original.update(notes='Only method A is allowed.', external_falsifier={'predicate': {'threshold': 1.0}})
    path.write_text(json.dumps(original))
    planner = Planner('protocol_revision', protocol_change=protocol_change)
    work = plan_research_work(REPO, thread, transport=planner)
    assert planner.calls[0]['registered_protocol'] == original
    assert work['next_tool_to_call'] == 'revise_evaluation_protocol'
    with pytest.raises(ValueError, match='revise_evaluation_protocol'):
        bind_work(thread, work['work_id'], node['id'], plan)
    calls = []
    bank = {'bank_id': 'bank_fresh', 'integrity_verified': True} if replace_holdout and not deferred else None
    monkeypatch.setattr(protocol_revision, 'sealed_bank_metadata', lambda thread: bank)
    sampling = {'spec_id': 'future_spec', 'source': 'pinned'} if deferred else None
    monkeypatch.setattr(protocol_revision, 'read_sampling_spec', lambda thread: sampling)
    def review(repo, directory, packet, *, purpose):
        calls.append(packet)
        return {'request_sha256': 'review', 'assessment': {
            'decision': 'reject' if len(calls) == 1 else 'approve',
            'reason': 'Prospective amendment review.', 'evidence': ['development-only source audit'],
            'required_work': ['Preserve the endpoint meaning.'] if len(calls) == 1 else [],
        }}
    monkeypatch.setattr(protocol_revision, 'review_research_packet', review)
    kwargs = {'work_id': work['work_id'], 'notes': 'Any qualified method; identical endpoints and untouched partition.',
              'rationale': 'The original method lock conflicts with development method selection.',
              'replace_holdout': replace_holdout, 'defer_holdout_generation': deferred}
    if protocol_change == 'component_binding':
        from research_harness import mcp_server
        monkeypatch.setattr(mcp_server, '_thread_dir', lambda tid: thread)
        with pytest.raises(ValueError, match='actual component binding'):
            protocol_revision.revise_evaluation_protocol(REPO, thread, **kwargs)
        assert not calls
        assert current_work(thread)['status'] == 'planned'
        source_args = {'thread_id': 'thread', 'work_id': work['work_id'],
                       'plan_metadata': {'source_files': [{'path': 'component.py', 'content': 'VALUE = 1\n'}]}}
        mcp_server.handle_design_experiment_template(source_args)
    from research_harness import mcp_server
    from research_harness.adapters.call_budget import CallBudgetExhausted
    from research_harness.orchestrator.research_control import execution_handoff
    monkeypatch.setattr(mcp_server, '_thread_dir', lambda tid: thread)
    def exhausted_review(*args, **kwargs):
        raise CallBudgetExhausted('Prompt limit reached before review.')
    monkeypatch.setattr(protocol_revision, 'review_research_packet', exhausted_review)
    paused = mcp_server.handle_revise_evaluation_protocol({'thread_id': thread.name, **kwargs})
    assert paused['status'] == 'checkpoint'
    assert paused['next_tool_to_call'] is None
    assert current_work(thread)['protocol_review_checkpoint']
    handoff = execution_handoff(thread, current_work(thread))
    assert handoff['tool'] == 'revise_evaluation_protocol'
    assert json.loads(Path(handoff['request_path']).read_text()) == {'thread_id': thread.name, **kwargs}
    redirected = mcp_server.handle_revise_evaluation_protocol({'thread_id': thread.name, **kwargs, 'replace_holdout': not replace_holdout})
    assert redirected['status'] == 'resume_required'
    assert redirected['arguments'] == handoff['arguments']
    assert json.loads(path.read_text()) == original
    monkeypatch.setattr(protocol_revision, 'review_research_packet', review)
    assert mcp_server.handle_revise_evaluation_protocol(handoff['arguments'])['status'] == 'rejected'
    assert [entry['notes'] for entry in calls[0]['protocol_note_history']['entries']] == [original['notes']]
    rejected = current_work(thread)
    assert rejected['status'] == 'planned'
    assert rejected['protocol_review']['required_work'] == ['Preserve the endpoint meaning.']
    assert rejected['reconsideration_available']
    assert json.loads(path.read_text()) == original
    if protocol_change == 'component_binding':
        source_args['plan_metadata']['source_files'][0]['content'] = 'VALUE = 2\n'
        mcp_server.handle_design_experiment_template(source_args)
    else:
        kwargs['notes'] += ' Clarify the source review requirement.'
    result = protocol_revision.revise_evaluation_protocol(REPO, thread, **kwargs)
    assert json.loads(path.read_text()) == {**original, 'notes': kwargs['notes']}
    assert result == protocol_revision.revise_evaluation_protocol(REPO, thread, **kwargs)
    assert len(calls) == 2
    prior = calls[1]['prior_review_context']
    assert prior['assessment']['required_work'] == ['Preserve the endpoint meaning.']
    assert prior['previous_notes'] == calls[0]['proposal']['notes']
    assert 'protocol_note_history' in prior['unchanged_packet_fields']
    if protocol_change == 'component_binding':
        assert calls[0]['prepared_source']['template_digest'] != calls[1]['prepared_source']['template_digest']
        assert Path(calls[1]['prepared_source']['source_files'][0]['path']).read_text() == 'VALUE = 2\n'
    assert result['outcome']['new_observation'] is False
    assert result['protocol_review']['decision'] == 'approve'
    assert 'reconsideration_available' not in result
    records = protocol_revision.approved_protocol_revisions(thread)
    assert records[0]['previous_protocol'] == original
    if protocol_change == 'component_binding':
        assert records[0]['prepared_source'] == calls[1]['prepared_source']
    assert records[0].get('replacement_holdout_bank') == bank
    assert calls[0]['replacement_holdout_bank'] == bank
    assert records[0].get('replacement_sampling_spec') == sampling
    with pytest.raises(ManuscriptError, match='disclose protocol amendment'):
        validate_sections({'method': {'prose_html': 'An amended study.', 'evidence_anchors': ['worker_report.status']}},
                          {'protocol_revisions': records, 'worker_report': {'status': 'completed'}}, require_citations=False)
    unrelated = thread / 'production/protocol_revisions/unrelated/approved.json'
    unrelated.parent.mkdir()
    unrelated.write_text(json.dumps({**records[0], 'recorded_at': '9999',
                                    'protocol': {**original, 'notes': 'Unrelated branch must not enter the active protocol.'}}))
    following_planner = Planner('protocol_revision')
    work = plan_research_work(REPO, thread, transport=following_planner)
    history = following_planner.calls[0]['protocol_note_history']['entries']
    assert [entry['notes'] for entry in history] == [original['notes'], kwargs['notes']]
    (thread / 'production/falsifier_result.json').write_text('{}')
    with pytest.raises(ValueError, match='Final evaluation'):
        protocol_revision.revise_evaluation_protocol(REPO, thread, **{**kwargs, 'work_id': work['work_id'], 'notes': 'Another change'})


@pytest.mark.parametrize('review_key', ['implementation_review', 'protocol_review', 'protocol_scope_analysis'])
def test_reconsideration_keeps_rejected_plan_and_does_not_create_observations(tmp_path, review_key):
    thread, tree, node, plan, _ = fixture(tmp_path)
    work = plan_research_work(REPO, thread, transport=Planner())
    with pytest.raises(ValueError, match='rejected implementation feedback'):
        plan_research_work(REPO, thread, reconsider_reason='Missing telemetry.', transport=Planner('analysis'))
    work[review_key] = {'decision': 'reject', 'reason': 'Missing reset logs cannot establish zero access.'}
    if review_key == 'protocol_scope_analysis':
        work[review_key] = {'assessment': {'status': 'unresolved', 'answer': 'Scope requires clarification.'}}
        work['requires_replanning'] = True
    (thread / 'production/research_control/current.json').write_text(json.dumps(work))
    bind_work(thread, work['work_id'], node['id'], plan)
    unresolved = finish_work(thread, {'status': 'rejected', 'reason': 'Pre-execution review rejected the plan.'})
    if review_key == 'protocol_scope_analysis':
        assert unresolved['next_tool_to_call'] == 'plan_research_work'
        assert unresolved['reconsideration_available']
    assert not current_work(thread)['outcome']['new_observation']
    planner = Planner('analysis')
    revised = plan_research_work(REPO, thread, reconsider_reason='Inspect available source provenance instead of inventing telemetry.', transport=planner)
    assert revised['next_tool_to_call'] == 'resolve_research_work'
    assert revised['evidence_digest'] == work['evidence_digest']
    assert planner.calls[0]['previous_work'][review_key] == work[review_key]
    assert not planner.calls[0]['diagnostic_required']
    assert planner.calls[0]['reconsider_reason']
    old = json.loads((thread / 'production/research_control/work' / work['work_id'] / 'work.json').read_text())
    assert old['status'] == 'superseded'
    assert old['superseded_by'] == revised['work_id']
    assert not list(tree.rglob('worker_report.json'))


def test_source_preparation_preserves_scientific_work_and_revision_bytes(tmp_path, monkeypatch):
    from research_harness import mcp_server
    from research_harness.confirmation_sampling import _hash
    from research_harness.orchestrator.research_control import prepared_implementations
    thread, tree, node, plan, _ = fixture(tmp_path)
    planner = Planner('diagnostic_experiment')
    work = plan_research_work(REPO, thread, transport=planner)
    assert work['next_tool_to_call'] == 'design_experiment_template'
    monkeypatch.setattr(mcp_server, '_thread_dir', lambda tid: thread)
    def no_claim_lookup(*args):
        raise AssertionError('Source preparation must not depend on a claim node')
    monkeypatch.setattr(mcp_server, '_require_authoritative_node', no_claim_lookup)
    args = {'thread_id': 'thread', 'work_id': work['work_id'],
            'plan_metadata': {'source_files': [{'path': 'prepared.py', 'content': "raise RuntimeError('must not execute during preparation')\n"}]}}
    prepared = mcp_server.handle_design_experiment_template(args)
    assert prepared['next_tool_to_call'] == 'design_experiment_template'
    revision = prepared['prepared_implementation']
    assert revision['execution_contract_errors']
    assert 'execution_request_path' not in revision
    assert prepared['preparation_checkpoint'] == revision['template_digest']
    assert 'research_work_checkpoint' not in prepared
    source = revision['source_files'][0]
    assert _hash(Path(source['path'])) == source['sha256']
    assert revision['new_observation'] is False
    assert prepared['status'] == 'planned'
    assert prepared['decision'] == work['decision']
    assert 'outcome' not in prepared
    assert not list(tree.rglob('runner_result.json'))
    assert mcp_server.handle_design_experiment_template(args) == prepared
    resumed = plan_research_work(REPO, thread, transport=planner)
    assert resumed['work_id'] == work['work_id']
    assert len(planner.calls) == 1
    args['plan_metadata']['source_files'][0]['content'] += '# repaired source\n'
    repaired = mcp_server.handle_design_experiment_template(args)
    latest = repaired['prepared_implementation']
    assert repaired['work_id'] == work['work_id']
    assert repaired['status'] == 'planned'
    assert latest['source_files'][0]['path'] != source['path']
    assert _hash(Path(source['path'])) == source['sha256']
    assert prepared_implementations(thread) == {revision['evidence_id']: revision, latest['evidence_id']: latest}
    from research_harness.orchestrator.research_control import execution_handoff
    from research_harness.orchestrator.experiment_plan import resolve_source_files
    complete_metadata = {**plan, 'source_files': args['plan_metadata']['source_files']}
    complete_metadata.pop('plan_id')
    complete_metadata.pop('node_id')
    complete = mcp_server.handle_design_experiment_template({**args, 'plan_metadata': complete_metadata})
    assert complete['next_tool_to_call'] == 'execute_baseline_preflight'
    complete_revision = complete['prepared_implementation']
    assert complete_revision['execution_contract_errors'] == []
    request = json.loads(Path(complete_revision['execution_request_path']).read_text())
    assert request['work_id'] == work['work_id']
    assert request['experiment_plan']['claim_under_test'] == plan['claim_under_test']
    assert resolve_source_files(thread, request['experiment_plan']['source_files'])[0]['content'] == args['plan_metadata']['source_files'][0]['content']
    handoff = execution_handoff(thread, complete)
    assert json.loads(Path(handoff['arguments']['request_path']).read_text()) == request
    assert not list(tree.rglob('runner_result.json'))
    bind_work(thread, work['work_id'], node['id'], plan)
    assert current_work(thread)['status'] == 'running'
    assert 'prepared_implementation' not in current_work(thread)
    assert current_work(thread)['prior_prepared_implementation'] == complete_revision


def test_exploration_retains_solution_lineage_without_invented_predictions(tmp_path):
    from research_harness.orchestrator.research_knowledge import research_brief, validate_solution_path
    thread, _, _, _, _ = fixture(tmp_path)
    class Explorer(Planner):
        def complete(self, request):
            response = super().complete(request)
            decision = json.loads(response.text)
            decision.update(inquiry_mode='exploration', alternatives=[])
            from dataclasses import replace
            return replace(response, text=json.dumps(decision))
    first = plan_research_work(REPO, thread, transport=Explorer('analysis'))
    brief = research_brief(thread)
    assert brief['current_solution']['work_id'] == first['work_id']
    assert brief['current_solution']['path']['next_solution_decision']
    with pytest.raises(ValueError, match='current explanation'):
        validate_solution_path(first['decision'], brief)
    next_decision = copy.deepcopy(first['decision'])
    next_decision['solution_path']['parent_work_id'] = first['work_id']
    validate_solution_path(next_decision, brief)
    next_decision['inquiry_mode'] = 'intervention'
    with pytest.raises(ValueError, match='competing predictions'):
        validate_solution_path(next_decision, brief)


def test_large_planning_context_retains_exact_sources_and_latest_failure(tmp_path):
    from research_harness.orchestrator.research_knowledge import compact_planning_context
    source = {'node': {'source': 'x' * 13000}}
    packet = {'implementation_context': source, 'previous_work': {
        'work_id': 'old', 'status': 'completed', 'source_observations': {'duplicated': True},
        'decision': {'test': 'Repair the earlier serialization defect'},
        'outcome': {'observation': {'measurement_error': 'New output schema defect'}}}}
    compact = compact_planning_context(tmp_path, packet)
    reference = compact['implementation_context']
    assert json.loads(Path(reference['context_reference']).read_text()) == source
    assert compact['authoritative_previous_result']['outcome'] == packet['previous_work']['outcome']
    assert 'source_observations' not in compact['previous_work']
    assert 'source_observations' in packet['previous_work']




@pytest.mark.parametrize('identified', [True, False])
def test_citation_repair_preserves_scientific_decision_and_caches_result(tmp_path, identified):
    from research_harness.orchestrator.research_control import recover_citation_error
    thread, _, _, _, _ = fixture(tmp_path)
    work = plan_research_work(REPO, thread, transport=Planner())
    original = copy.deepcopy(work['decision'])
    original['evidence_ids'] = ['mistyped-reference']
    directory = tmp_path / 'repair'
    directory.mkdir()
    (directory / 'rejected_response.json').write_text(json.dumps({'decision': original, 'error': 'Invalid citation.'}))
    calls = []
    class Repair:
        def complete(self, request):
            calls.append(request)
            assert request.allow_local_tools is False
            schema = json.loads(request.output_schema.read_text())
            assert list(schema['properties']) == ['mistyped-reference']
            return CompletionResult(json.dumps({'mistyped-reference': 'actual-reference' if identified else None}), AgentUsage(), None)
    for _ in range(2):
        result = recover_citation_error(directory, {'actual-reference'}, transport=Repair())
        if identified:
            expected = {**original, 'evidence_ids': ['actual-reference']}
            assert result == expected
        else:
            assert result is None
    assert len(calls) == 1
    assert json.loads((directory / 'rejected_response.json').read_text())['decision'] == original


def test_rejected_planner_response_cannot_restart_direction_generation(tmp_path, monkeypatch):
    from research_harness import mcp_server
    from research_harness.orchestrator.research_control import pending_planning_failure
    thread, _, _, _, _ = fixture(tmp_path)
    work = plan_research_work(REPO, thread, transport=Planner())
    work['status'] = 'completed'
    (thread / 'production/research_control/current.json').write_text(json.dumps(work))
    directory = thread / 'production/research_control/decisions/invalid-citation'
    directory.mkdir()
    (directory / 'request.json').write_text(json.dumps({'previous_work': work}))
    (directory / 'rejected_response.json').write_text(json.dumps({'error': 'Unavailable citation.'}))
    monkeypatch.setattr(mcp_server, '_thread_dir', lambda _: thread)
    def forbidden_restart(tid):
        raise AssertionError('A planner contract error must not restart the research direction.')
    monkeypatch.setattr(mcp_server, '_build_blind_research_engine', forbidden_restart)
    result = mcp_server.handle_advance_research({'thread_id': 'thread', 'command_id': 'restart-after-error'}, {})
    assert result['status'] == 'planning_required'
    assert result['next_tool_to_call'] == 'plan_research_work'
    assert result['work_id'] == work['work_id']
    assert result['new_observation'] is False
    assert current_work(thread) == work
    (directory / 'response.json').write_text('{}')
    assert pending_planning_failure(thread) is None


def test_planner_schema_binds_failed_receipt_before_generation(tmp_path):
    from research_harness.orchestrator.research_knowledge import planning_response_schema
    from research_harness.schemas.validator import validate_schema
    thread, _, _, _, _ = fixture(tmp_path)
    previous = plan_research_work(REPO, thread, transport=Planner())
    previous.update(status='completed', outcome={'execution_result': 'execution_failed'})
    generated = planning_response_schema(previous, available_evidence={'work_' + previous['work_id']},
                                         available_hypotheses={'recorded-hypothesis'})
    with pytest.raises(ValueError):
        validate_schema(generated['properties']['hypothesis_ids'], ['mistyped-hypothesis'])
    validate_schema(generated['properties']['hypothesis_ids'], ['recorded-hypothesis'])
    schema = generated['properties']['previous_result']
    assessment = {'work_id': previous['work_id'], 'result_kind': 'execution_failure',
                  'evidence_ids': ['work_' + previous['work_id']], 'missing_evidence': ['valid measurement'],
                  'next_decision': 'Repair the actual output failure',
                  'prediction_updates': [{'alternative_index': i, 'effect': 'unresolved', 'observation_ids': [],
                                          'reason': 'Measurement failed'} for i in range(2)]}
    validate_schema(schema, assessment)
    assessment['evidence_ids'].append('analysis_malformed_hash')
    with pytest.raises(ValueError):
        validate_schema(schema, assessment)
    assessment['evidence_ids'].pop()
    assessment['prediction_updates'][1]['effect'] = 'supported'
    with pytest.raises(ValueError):
        validate_schema(schema, assessment)

    analysis = copy.deepcopy(previous)
    analysis.update(outcome={'execution_result': 'analysis_completed'})
    analysis['decision'].update(kind='analysis', required_observations=[])
    for alternative in analysis['decision']['alternatives']:
        alternative['required_observations'] = []
    source_schema = planning_response_schema(analysis, available_evidence={'work_' + analysis['work_id']}, available_hypotheses=set())['properties']['previous_result']
    assessment.update(work_id=previous['work_id'], result_kind='inconclusive')
    assessment['prediction_updates'][1].update(effect='unresolved', observation_ids=['/raw/artifact/pointer'])
    with pytest.raises(ValueError):
        validate_schema(source_schema, assessment)
    assessment['prediction_updates'][1]['observation_ids'] = []
    validate_schema(source_schema, assessment)
    from research_harness.orchestrator.research_knowledge import validate_previous_result
    from research_harness.orchestrator.research_observations import prediction_support
    assessment['result_kind'] = 'informative'
    assessment['prediction_updates'][1]['effect'] = 'supported'
    validate_schema(source_schema, assessment)
    validate_previous_result({'previous_result': assessment}, analysis, {'work_' + analysis['work_id']})
    assert prediction_support(analysis)[1]['support_basis'] == 'answered_source_analysis'
    assessment['prediction_updates'][1]['effect'] = 'unresolved'
    assessment['work_id'] = 'stale-work'
    with pytest.raises(ValueError):
        validate_schema(schema, assessment)


def test_planned_work_is_reconsidered_when_model_changes(tmp_path, monkeypatch):
    thread, _, _, _, _ = fixture(tmp_path)
    monkeypatch.setenv('RESEARCH_HARNESS_MODEL', 'gpt-5.6-sol')
    first = plan_research_work(REPO, thread, transport=Planner())
    monkeypatch.setenv('RESEARCH_HARNESS_MODEL', 'gpt-5.6-luna')
    planner = Planner()
    second = plan_research_work(REPO, thread, transport=planner)
    assert planner.calls
    assert second['work_id'] != first['work_id']
    assert second['planning_model'] == 'gpt-5.6-luna'
    prior = json.loads((thread / 'production/research_control/work' / first['work_id'] / 'work.json').read_text())
    assert prior['status'] == 'superseded'
    assert prior['superseded_by'] == second['work_id']


def test_bounded_mcp_transport_failure_returns_checkpoint(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from research_harness import mcp_server
    from research_harness.orchestrator import research_control
    from research_harness.adapters.codex_cli import CodexCliError
    monkeypatch.setenv('RESEARCH_HARNESS_CALL_BUDGET', str(tmp_path / 'budget.json'))
    monkeypatch.setattr(mcp_server, '_exclusive_adaptive_writer', lambda tid: nullcontext())
    monkeypatch.setattr(mcp_server, '_repo_root', lambda: REPO)
    monkeypatch.setattr(mcp_server, '_thread_dir', lambda tid: tmp_path)
    def fail(*args, **kwargs):
        raise CodexCliError('measured planner timeout')
    monkeypatch.setattr(research_control, 'plan_research_work', fail)
    response = mcp_server.handle_plan_research_work({'thread_id': 'probe'})
    assert response['status'] == 'checkpoint'
    assert response['next_tool_to_call'] is None


def test_selector_returns_work_without_source_tools_and_retains_findings(tmp_path):
    thread, _, _, _, _ = fixture(tmp_path)
    class DecisionOnlyPlanner(Planner):
        def complete(self, request):
            assert not request.allow_local_tools
            return super().complete(request)
    work = plan_research_work(REPO, thread, transport=DecisionOnlyPlanner())
    assert work['status'] == 'planned'
    from research_harness.orchestrator.research_knowledge import compact_planning_context
    finding = {'status': 'answered', 'conclusion_excerpt': 'The prior question is resolved.',
               'limitations': ['No causal effect was measured.'], 'receipt_path': '/exact/receipt',
               'next_steps': ['Measure the new distinction.'], 'question': 'What is observed?'}
    packet = {'analysis_findings': {f'analysis_{i}': finding for i in range(80)}}
    projected = compact_planning_context(thread, packet)
    assert projected['analysis_findings']['analysis_79'] == finding


def test_ineligible_confirmation_is_not_cached_as_valid_work(tmp_path):
    thread, _, _, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match='Confirmation requires'):
        plan_research_work(REPO, thread, transport=Planner('confirmation'))
    decisions = thread / 'production/research_control/decisions'
    assert not list(decisions.glob('*/response.json'))
    corrected = Planner()
    plan_research_work(REPO, thread, transport=corrected)
    assert 'Confirmation requires' in corrected.calls[0]['previous_response_rejection']['error']


def test_repair_handoff_binds_predecessor_bytes_and_fresh_identity(tmp_path):
    from research_harness.orchestrator.research_control import execution_handoff, _write
    from research_harness.mcp_server import _experiment_plan_input_schema
    from research_harness.schemas.validator import validate_schema
    thread, _, node, plan, _ = fixture(tmp_path)
    previous = plan_research_work(REPO, thread, transport=Planner())
    previous.update(status='completed', outcome={'execution_result': 'execution_failed'},
                    binding={'node_id': node['id'], 'scope': 'baseline_preflight'})
    _write(thread / 'production/research_control/current.json', previous)
    _write(thread / 'production/research_control/work' / previous['work_id'] / 'work.json', previous)
    base = thread / 'production/tree/baseline_preflight' / node['id']
    _write(base / 'experiment_plan.json', plan)
    _write(base / 'node.json', node)
    for source in plan['source_files']:
        path = Path(plan['workspace']) / source['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source['content'])
    work = plan_research_work(REPO, thread, transport=Planner())
    handoff = execution_handoff(thread, work)
    request = handoff['request_draft']
    assert request['work_id'] == work['work_id']
    assert request['node']['id'] == request['experiment_plan']['node_id'] != node['id']
    validate_schema(_experiment_plan_input_schema(), request['experiment_plan'])
    reference = request['experiment_plan']['source_files'][0]
    assert reference['replacements'] == []
    assert Path(reference['from_path']).read_text() == plan['source_files'][0]['content']
    Path(reference['from_path']).write_text('changed after execution')
    assert 'request_draft' not in execution_handoff(thread, work)


def test_output_bindings_and_partial_interpretation_preserve_family_boundaries(tmp_path):
    from research_harness.orchestrator.research_observations import observation_contract, collect_observation_support, measurement_facts
    from research_harness.orchestrator.research_knowledge import validate_previous_result, planning_response_schema
    thread = tmp_path
    workspace = thread / 'workspace'
    workspace.mkdir()
    artifact = workspace / 'metrics.json'
    artifact.write_text(json.dumps({'details': {'families': [
        {'name': 'a', 'count': 216, 'effect': 0}, {'name': 'b', 'count': 0, 'effect': 0}]}}))
    decision = {'required_observations': ['a_count', 'b_count'],
                'alternatives': [{'required_observations': ['a_count']}, {'required_observations': ['b_count']}]}
    plan = {'workspace': str(workspace), 'expected_outputs': {'metrics_files': ['metrics.json']}}
    with pytest.raises(ValueError, match='Bind every'):
        observation_contract(decision, plan)
    plan['observation_bindings'] = {name: {'artifact_path': 'metrics.json',
        'json_pointer': '/details/families/' + str(i) + '/count', 'producer': 'diagnostic.py report'}
        for i, name in enumerate(decision['required_observations'])}
    observation_contract(decision, plan)
    support = collect_observation_support(decision, plan, {}, thread)
    assert support['counts'] == {'a_count': 216, 'b_count': 0}
    assert not support['evaluable']
    assert measurement_facts(artifact)['facts']['/details/families/1/count'] == 0
    previous = {'work_id': 'previous', 'status': 'completed', 'decision': decision,
                'outcome': {'execution_result': 'executed', 'measurement_support': support}}
    assessment = {'work_id': 'previous', 'result_kind': 'partial', 'evidence_ids': ['work_previous'],
        'prediction_updates': [{'alternative_index': 0, 'effect': 'weakened', 'observation_ids': ['a_count'],
                                'reason': 'Illustrative bounded prediction update, not inferred by the host.'},
                               {'alternative_index': 1, 'effect': 'unresolved', 'observation_ids': [],
                                'reason': 'No eligible observations for b.'}],
        'missing_evidence': ['Eligible b observations'], 'next_decision': 'Choose a different b probe.'}
    validate_previous_result({'previous_result': assessment}, previous, {'work_previous'})
    assert 'partial' in planning_response_schema(previous, available_evidence={'work_' + previous['work_id']}, available_hypotheses=set())['properties']['previous_result']['properties']['result_kind']['enum']
    assessment['prediction_updates'][1].update(effect='weakened', observation_ids=['b_count'])
    with pytest.raises(ValueError, match='another family'):
        validate_previous_result({'previous_result': assessment}, previous, {'work_previous'})
    assessment['prediction_updates'][1].update(effect='unresolved', observation_ids=[])
    assessment['prediction_updates'][0]['observation_ids'] = ['a_count', 'a_count']
    with pytest.raises(ValueError, match='citations must be unique'):
        validate_previous_result({'previous_result': assessment}, previous, {'work_previous'})
    # A source path cannot manufacture the missing count by falling back to another field.
    plan['observation_bindings']['a_count']['json_pointer'] = '/missing_count'
    assert collect_observation_support(decision, plan, {'a_count': 999}, thread)['counts']['a_count'] is None
    plan['observation_bindings']['a_count']['artifact_path'] = '../outside.json'
    with pytest.raises(ValueError, match='declared metrics file'):
        observation_contract(decision, plan)
