import json
from pathlib import Path
from unittest.mock import patch

import pytest

from research_harness.agent_runtime import AgentUsage, CompletionResult
from research_harness.orchestrator.research_review import review_research_packet


@pytest.mark.parametrize('scope,path,quote', [
    ('final_confirmation', 'registered_protocol.notes', 'Qualify the final comparator before confirmation.'),
    ('selected_test', 'work_decision.deferred_questions.0', 'Complete the final comparison.'),
    ('selected_test', 'work_decision.test', 'Invented mandatory positive result'),
])
def test_development_review_cannot_install_future_or_invented_requirements(tmp_path, scope, path, quote):
    from research_harness.orchestrator.research_review import ReviewContractError

    packet = {'decision_scope': 'development_execution',
              'work_decision': {'test': 'Count the eligible observations.',
                                'deferred_questions': ['Complete the final comparison.']},
              'registered_protocol': {'notes': 'Qualify the final comparator before confirmation.'}}
    assessment = {'decision': 'reject', 'reason': 'Not ready.', 'evidence': ['work_decision.test'],
        'required_work': ['Complete the final comparison first.'], 'next_steps': [],
        'observation_checks': [], 'blocking_basis': [{'required_work_index': 0, 'scope': scope, 'basis_path': path, 'basis_quote': quote}]}
    repo = Path(__file__).resolve().parents[1]
    with patch('research_harness.orchestrator.research_review.CodexCliAdapter.complete') as complete:
        complete.return_value = CompletionResult(text=json.dumps(assessment), usage=AgentUsage(), thread_id='review')
        with pytest.raises(ReviewContractError):
            review_research_packet(repo, tmp_path, packet, purpose='Run one development diagnostic.')
        assert not list(tmp_path.glob('*/review.json'))
        assessment.update(decision='approve', required_work=[], blocking_basis=[], next_steps=['Complete the final comparison later.'])
        complete.return_value = CompletionResult(text=json.dumps(assessment), usage=AgentUsage(), thread_id='review')
        corrected = review_research_packet(repo, tmp_path, packet, purpose='Run one development diagnostic.')
        assert 'previous_review_rejection' in json.loads(complete.call_args.args[0].prompt.input)
        assert corrected['assessment']['decision'] == 'approve'
        assert review_research_packet(repo, tmp_path, packet, purpose='Run one development diagnostic.') == corrected
        assert complete.call_count == 2


def test_independent_review_reuses_only_matching_evidence_and_rejects_unresolved_approval(tmp_path):
    assessment = {"decision": "reject", "reason": "incorrect method", "evidence": ["experiment.py:12"], "required_work": ["fix implementation"], "next_steps": []}
    result = CompletionResult(text=json.dumps(assessment), usage=AgentUsage(), thread_id="review-session")
    repo = Path(__file__).resolve().parents[1]
    with patch("research_harness.orchestrator.research_review.CodexCliAdapter.complete", return_value=result) as complete:
        first = review_research_packet(repo, tmp_path, {"source": "original"}, purpose="baseline")
        assert first["assessment"]["decision"] == "reject"
        assert review_research_packet(repo, tmp_path, {"source": "original"}, purpose="baseline") == first
        assert complete.call_count == 1
        changed = review_research_packet(repo, tmp_path, {"source": "revised"}, purpose="baseline")
        assert changed["request_sha256"] != first["request_sha256"]
        assert complete.call_count == 2
        assessment["decision"] = "approve"
        complete.return_value = CompletionResult(text=json.dumps(assessment), usage=AgentUsage(), thread_id="review-session")
        with pytest.raises(ValueError, match="unresolved required work"):
            review_research_packet(repo, tmp_path, {"source": "third"}, purpose="baseline")

        assessment.update(required_work=[], next_steps=['Run Q1-Q3 before future training.'])
        complete.return_value = CompletionResult(text=json.dumps(assessment), usage=AgentUsage(), thread_id='review-session')
        future = review_research_packet(repo, tmp_path, {'source': 'fourth'}, purpose='prospective protocol')
        assert future['assessment']['decision'] == 'approve'
        assert future['assessment']['next_steps'] == ['Run Q1-Q3 before future training.']


def test_review_bundle_preserves_evidence_and_denies_self_replay(tmp_path):
    import hashlib
    from research_harness.orchestrator.research_review import review_input_bundle, predecessor_review_delta, analysis_input_bundle

    source = {'path': 'measure.py', 'content': 'print(1)\n'}
    packet = {'decision_scope': 'development_execution',
              'experiment_plan': {'source_files': [source], 'resources': {'timeout_sec': 20}},
              'execution_source_manifest': [{'relative_path': 'measure.py', 'path': '/measure.py', 'sha256': 'digest'}],
              'prepared_implementations': {'prior': {'source': 'unchanged'}},
              'registered_protocol': {'notes': 'Measure honestly.'}}
    destination = tmp_path / 'bundle'
    submitted = review_input_bundle(destination, packet)
    assert 'content' not in submitted['experiment_plan']['source_files'][0]
    assert packet['experiment_plan']['source_files'][0]['content'] == 'print(1)\n'
    reference = submitted['evidence_sections']['prepared_implementations']
    assert json.loads(Path(reference['path']).read_text()) == packet['prepared_implementations']
    assert submitted['registered_protocol'] == packet['registered_protocol']
    analysis = {'question': {'evidence_ids': ['selected']},
                'development_evidence': {'selected': {'count': 7}, 'older': {'count': 3}},
                'measurement_facts': {'selected': {'pointer': '/count', 'value': 7}}}
    focused = analysis_input_bundle(tmp_path / 'analysis', analysis)
    assert focused['development_evidence'] == {'selected': {'count': 7}}
    assert focused['measurement_facts'] == analysis['measurement_facts']
    archived = focused['evidence_sections']['development_evidence']
    assert json.loads(Path(archived['path']).read_text()) == analysis['development_evidence']
    assert hashlib.sha256(Path(archived['path']).read_bytes()).hexdigest() == archived['sha256']
    prior_dir = tmp_path / 'production/research_control/work/old'
    prior_dir.mkdir(parents=True)
    request = json.dumps({'packet': packet}).encode()
    (prior_dir / 'request.json').write_bytes(request)
    (prior_dir / 'review.json').write_text(json.dumps({'request_sha256': hashlib.sha256(request).hexdigest(),
        'assessment': {'decision': 'approve', 'reason': 'Verified old code.'}}))
    (prior_dir / 'work.json').write_text(json.dumps({'implementation_review': {'receipt_path': str(prior_dir / 'review.json')}}))
    work = {'decision': {'previous_result': {'work_id': 'old'}}}
    changed = json.loads(json.dumps(packet))
    changed['experiment_plan']['source_files'][0]['content'] = 'print(2)\n'
    changed['registered_protocol']['notes'] = 'Changed endpoint.'
    delta = predecessor_review_delta(tmp_path, work, changed)
    assert '+print(2)' in delta['source_diffs']['measure.py']
    assert delta['changed_conditions'] == ['registered_protocol']
    assert not delta['bounded_revision']
    assert predecessor_review_delta(tmp_path, work, packet)['bounded_revision']
    (prior_dir / 'request.json').write_text('{}')
    assert predecessor_review_delta(tmp_path, work, changed) is None
    assessment = {'decision': 'approve', 'reason': 'Valid development execution.',
                  'evidence': ['measure.py'], 'required_work': [], 'next_steps': [], 'blocking_basis': [], 'observation_checks': []}
    repo = Path(__file__).resolve().parents[1]
    with patch('research_harness.orchestrator.research_review.CodexCliAdapter.complete') as complete:
        complete.return_value = CompletionResult(text=json.dumps(assessment), usage=AgentUsage(), thread_id=None)
        review_research_packet(repo, tmp_path / 'review', packet, purpose='Measure once.')
        req = complete.call_args.args[0]
        assert req.event_log_path in req.denied_read_paths
        assert req.event_log_path.with_name('request.json') in req.denied_read_paths
        packet['predecessor_review_delta'] = {'bounded_revision': True}
        review_research_packet(repo, tmp_path / 'revision', packet, purpose='Measure once.')
        req = complete.call_args.args[0]
        assert not req.allow_local_tools
        assert json.loads(req.prompt.input)['experiment_plan']['source_files'][0]['content'] == source['content']


def test_execution_review_cannot_omit_declared_output_checks():
    from research_harness.orchestrator.research_review import validate_execution_objections
    packet = {'observation_contract': {'eligible_count': {'json_pointer': '/metrics/eligible_count'}}}
    assessment = {'decision': 'approve', 'required_work': [], 'blocking_basis': [], 'observation_checks': []}
    with pytest.raises(ValueError, match='every declared observation'):
        validate_execution_objections(assessment, packet)
    assessment['observation_checks'] = [{'observation_id': 'eligible_count', 'emitted_by_producer': False,
                                       'reason': 'Producer emits a different key.'}]
    with pytest.raises(ValueError, match='missing declared observation'):
        validate_execution_objections(assessment, packet)
    assessment['observation_checks'][0]['emitted_by_producer'] = True
    validate_execution_objections(assessment, packet)


@pytest.mark.parametrize("role", ["analysis", "execution_review"])
def test_interrupted_analysis_reuses_completed_reads_for_tool_free_synthesis(tmp_path, role):
    from research_harness.orchestrator.research_review import analyze_research_packet
    from research_harness.adapters.codex_cli import CodexCliError
    repo = Path(__file__).resolve().parents[1]
    packet = {'question': {'evidence_ids': []}}
    assess = analyze_research_packet
    if role == 'execution_review':
        packet['decision_scope'] = 'development_execution'
        packet['experiment_plan'] = {'source_files': [{'path': 'measure.py', 'content': 'print(7)'}]}
        assess = review_research_packet
    assessment = {'status': 'answered', 'answer': 'The recorded count is 7.',
                  'evidence': ['metrics.json /count'], 'limitations': [], 'next_steps': []}
    if role == 'execution_review':
        assessment = {'decision': 'approve', 'reason': 'The producer emits the count.',
                      'evidence': ['metrics.json /count'], 'required_work': [], 'next_steps': [],
                      'blocking_basis': [], 'observation_checks': []}
    requests = []
    def complete(request):
        requests.append(request)
        if len(requests) == 1:
            request.event_log_path.write_text(json.dumps({'type': 'item.completed', 'item': {
                'type': 'command_execution', 'command': 'cat metrics.json',
                'exit_code': 0, 'aggregated_output': '{"count":7}'}}) + '\n{"type":')
            raise CodexCliError('timeout')
        assert not request.allow_local_tools
        if role == 'execution_review':
            assert json.loads(request.prompt.input)['experiment_plan']['source_files'][0]['content'] == 'print(7)'
        recovered = json.loads(request.prompt.input)['completed_inspection']
        assert recovered['observations'][0]['output'] == '{"count":7}'
        assert Path(recovered['events_path']).read_text().endswith('{"type":')
        return CompletionResult(json.dumps(assessment), AgentUsage(), None)
    with patch('research_harness.orchestrator.research_review.CodexCliAdapter.complete', side_effect=complete):
        with pytest.raises(CodexCliError):
            assess(repo, tmp_path, packet, purpose='Read the count.')
        result = assess(repo, tmp_path, packet, purpose='Read the count.')
        assert result['assessment'] == assessment
        assert assess(repo, tmp_path, packet, purpose='Read the count.') == result
        assert len(requests) == 2


def test_inspection_budget_prioritizes_experiment_source_over_recent_framework_reads(tmp_path):
    from research_harness.orchestrator.research_review import completed_inspection
    path = tmp_path / 'events.jsonl'
    path.write_text('\n'.join(json.dumps({'type': 'item.completed', 'item': {
        'type': 'command_execution', 'exit_code': 0, 'command': command, 'aggregated_output': output}})
        for command, output in [('cat /workspace/experiment.py', 'source' * 700),
                                ('cat /framework/validator.py', 'framework' * 8600)]))
    recovered = completed_inspection(path, ('/workspace/experiment.py',))
    assert recovered['observations'][0]['output'] == 'source' * 700
    assert recovered['omitted_output_count'] == 1


@pytest.mark.parametrize('scope_status', ['answered', 'unresolved'])
def test_protocol_interpretation_is_preserved_across_implementation_revisions(tmp_path, scope_status):
    from research_harness.orchestrator.research_review import ProtocolScopeUnresolved
    packet = {'decision_scope': 'development_execution', 'work_decision': {'test': 'Compare matched controls.'},
              'development_executions': {'prior_control': {'execution_status': 'completed'}},
              'registered_protocol': {'notes': 'Keep the original outcome.'},
              'protocol_note_history': {'entries': [{'notes': 'Keep the original outcome.'},
                                                    {'notes': 'A later confirmation must use unused inputs.'}]},
              'experiment_plan': {'source_files': [{'path': 'measure.py', 'content': 'print(1)'}]}}
    repo = Path(__file__).resolve().parents[1]
    seen = []
    def complete(request):
        value = json.loads(request.prompt.input)
        seen.append(value)
        if 'amendment_history' in value:
            assert value['development_executions'] == packet['development_executions']
            assert not request.allow_local_tools
            result = {'status': scope_status, 'answer': 'Preserve outcome; confirmation is later.',
                      'evidence': ['amendment_history.entries.0.notes: “Keep the original outcome.”'], 'limitations': [], 'next_steps': []}
        else:
            assert value['protocol_scope_analysis']['assessment']['status'] == 'answered'
            assert value['protocol_scope_analysis']['verified_clause_citations'] == [
                {'basis_path': 'protocol_note_history.entries.0.notes', 'basis_quote': 'Keep the original outcome.'}]
            reference = value['evidence_sections']['protocol_note_history']
            assert json.loads(Path(reference['path']).read_text()) == packet['protocol_note_history']
            result = {'decision': 'approve', 'reason': 'Valid selected test.', 'evidence': ['measure.py'],
                      'required_work': [], 'next_steps': [], 'blocking_basis': [], 'observation_checks': []}
        return CompletionResult(json.dumps(result), AgentUsage(), None)
    with patch('research_harness.orchestrator.research_review.CodexCliAdapter.complete', side_effect=complete):
        if scope_status == 'unresolved':
            for _ in range(2):
                with pytest.raises(ProtocolScopeUnresolved) as unresolved:
                    review_research_packet(repo, tmp_path, packet, purpose='Development test.')
                assert unresolved.value.analysis['assessment']['status'] == 'unresolved'
            assert len(seen) == 1
            return
        first = review_research_packet(repo, tmp_path, packet, purpose='Development test.')
        assert review_research_packet(repo, tmp_path, packet, purpose='Development test.') == first
        packet['experiment_plan']['source_files'][0]['content'] = 'print(2)'
        review_research_packet(repo, tmp_path, packet, purpose='Development test.')
    assert len(seen) == 3
    assert sum('amendment_history' in value for value in seen) == 1
