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


class Planner:
    def __init__(self, kind='diagnostic_experiment', budget=10):
        self.calls = []
        self.kind = kind
        self.budget = budget

    def complete(self, request):
        packet = json.loads(request.prompt.input)
        self.calls.append(packet)
        decision = {
            'kind': self.kind, 'uncertainty': 'Is the measurement implementation valid?',
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
    plan['source_files'] = [{'path': 'experiment.py', 'purpose': 'Reproduce invalid measurement.',
                             'content': "raise ValueError('measurement construction is invalid')\n"}]
    return thread, tree, node, plan, requirement['role']


@pytest.mark.parametrize('explicit_node', [True, False])
def test_actual_execution_failure_changes_next_work_without_refuting_claim(tmp_path, monkeypatch, explicit_node):
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
    from research_harness import mcp_server
    from research_harness import settings_scoped
    from research_harness.orchestrator import research_review
    monkeypatch.setattr(mcp_server, '_repo_root', lambda: tmp_path)
    monkeypatch.setattr(mcp_server, '_thread_dir', lambda tid: thread)
    monkeypatch.setattr(settings_scoped, 'resolve_for_thread', lambda repo, tid: {})
    reviewed = []
    def review(repo, directory, packet, *, purpose):
        reviewed.append(packet)
        return {'request_sha256': 'review', 'assessment': {
            'decision': 'reject' if len(reviewed) == 1 else 'approve',
            'reason': 'Check the actual measurement path.', 'evidence': ['experiment.py'],
            'required_work': ['Use the actual implementation.'] if len(reviewed) == 1 else [],
        }}
    monkeypatch.setattr(research_review, 'review_research_packet', review)
    request_path = thread / 'dispatch.json'
    request_path.write_text(json.dumps({**({'node': node} if explicit_node else {}), 'experiment_plan': plan, 'role': role, 'work_id': work['work_id']}))
    outside = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(tmp_path / 'outside.json')})
    assert outside['status'] == 'rejected'
    revision = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})
    assert revision['status'] == 'rejected'
    assert current_work(thread)['status'] == 'planned'
    assert not (thread / 'production/tree/baseline_preflight' / node['id'] / 'job_manifest.json').exists()
    assert mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})['status'] == 'rejected'
    assert len(reviewed) == 1
    assert revision['reconsideration_available']
    plan['source_files'][0]['content'] += '\n# revised measurement path\n'
    request_path.write_text(json.dumps({'node': node, 'experiment_plan': plan, 'role': role, 'work_id': work['work_id']}))
    result = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})
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
    plan['source_files'][0]['content'] += '\n# corrected request\n'
    request_path.write_text(json.dumps({**({'node': node} if explicit_node else {}), 'experiment_plan': plan, 'role': role, 'work_id': next_work['work_id']}))
    rejected = mcp_server.handle_execute_baseline_preflight({'thread_id': 'thread', 'request_path': str(request_path)})
    assert rejected['status'] == 'rejected'
    assert 'new node ID' in rejected['reason']
    assert current_work(thread)['status'] == 'planned'
    assert current_work(thread)['outcome']['reason'] == rejected['reason']
    assert Path(rejected['dispatch_request_path']).is_absolute()
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
    planner = Planner()
    following = plan_research_work(REPO, thread, transport=planner)
    assert following['status'] == 'planned'
    assert 'DO_NOT_USE' not in json.dumps(planner.calls)
    assert planner.calls[0]['previous_work']['status'] == 'completed'


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
    assert set(findings) == {'analysis_' + work['work_id'], 'analysis_' + second['work_id']}
    assert all(row['conclusion_excerpt'].startswith('The source declares') for row in findings.values())


@pytest.mark.parametrize('replace_holdout,deferred', [(False, False), (True, False), (True, True)])
def test_protocol_amendment_preserves_the_bar_and_requires_independent_review(tmp_path, monkeypatch, replace_holdout, deferred):
    from research_harness.orchestrator import protocol_revision
    from research_harness.publishing.manuscript import ManuscriptError, validate_sections

    thread, _, node, plan, _ = fixture(tmp_path)
    path = thread / 'production/feasibility_envelope.json'
    original = json.loads(path.read_text())
    original.update(notes='Only method A is allowed.', external_falsifier={'predicate': {'threshold': 1.0}})
    path.write_text(json.dumps(original))
    planner = Planner('protocol_revision')
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
    assert protocol_revision.revise_evaluation_protocol(REPO, thread, **kwargs)['status'] == 'rejected'
    rejected = current_work(thread)
    assert rejected['status'] == 'planned'
    assert rejected['protocol_review']['required_work'] == ['Preserve the endpoint meaning.']
    assert rejected['reconsideration_available']
    assert json.loads(path.read_text()) == original
    result = protocol_revision.revise_evaluation_protocol(REPO, thread, **kwargs)
    assert json.loads(path.read_text()) == {**original, 'notes': kwargs['notes']}
    assert result == protocol_revision.revise_evaluation_protocol(REPO, thread, **kwargs)
    assert len(calls) == 2
    assert result['outcome']['new_observation'] is False
    assert result['protocol_review']['decision'] == 'approve'
    assert 'reconsideration_available' not in result
    records = protocol_revision.approved_protocol_revisions(thread)
    assert records[0]['previous_protocol'] == original
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


@pytest.mark.parametrize('review_key', ['implementation_review', 'protocol_review'])
def test_reconsideration_keeps_rejected_plan_and_does_not_create_observations(tmp_path, review_key):
    thread, tree, _, _, _ = fixture(tmp_path)
    work = plan_research_work(REPO, thread, transport=Planner())
    with pytest.raises(ValueError, match='rejected implementation feedback'):
        plan_research_work(REPO, thread, reconsider_reason='Missing telemetry.', transport=Planner('analysis'))
    work[review_key] = {'decision': 'reject', 'reason': 'Missing reset logs cannot establish zero access.'}
    (thread / 'production/research_control/current.json').write_text(json.dumps(work))
    planner = Planner('analysis')
    revised = plan_research_work(REPO, thread, reconsider_reason='Inspect available source provenance instead of inventing telemetry.', transport=planner)
    assert revised['next_tool_to_call'] == 'resolve_research_work'
    assert revised['evidence_digest'] == work['evidence_digest']
    assert planner.calls[0]['previous_work'][review_key] == work[review_key]
    assert planner.calls[0]['reconsider_reason']
    old = json.loads((thread / 'production/research_control/work' / work['work_id'] / 'work.json').read_text())
    assert old['status'] == 'superseded'
    assert old['superseded_by'] == revised['work_id']
    assert not list(tree.rglob('worker_report.json'))


def test_implementation_work_writes_bound_bytes_without_executing(tmp_path, monkeypatch):
    from research_harness import mcp_server
    from research_harness.confirmation_sampling import _hash
    thread, tree, node, plan, _ = fixture(tmp_path)
    work = plan_research_work(REPO, thread, transport=Planner('implementation'))
    assert work['next_tool_to_call'] == 'design_experiment_template'
    with pytest.raises(ValueError, match='design_experiment_template'):
        bind_work(thread, work['work_id'], node['id'], plan)
    monkeypatch.setattr(mcp_server, '_thread_dir', lambda tid: thread)
    monkeypatch.setattr(mcp_server, '_require_authoritative_node', lambda tid, node_id: None)
    args = {'thread_id': 'thread', 'node_id': node['id'], 'work_id': work['work_id'],
            'plan_metadata': {'source_files': [{'path': 'prepared.py', 'content': "raise RuntimeError('must not execute during preparation')\n"}]}}
    prepared = mcp_server.handle_design_experiment_template(args)
    source = prepared['outcome']['source_files'][0]
    assert _hash(Path(source['path'])) == source['sha256']
    assert prepared['outcome']['new_observation'] is False
    assert not list(tree.rglob('runner_result.json'))
    assert mcp_server.handle_design_experiment_template(args) == prepared
    planner = Planner('analysis')
    plan_research_work(REPO, thread, transport=planner)
    assert planner.calls[0]['prepared_implementations'] == {'implementation_' + work['work_id']: prepared['outcome']}
