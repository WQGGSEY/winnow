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
              'work_decision': {'evidence_ids': ['selected']},
              'development_executions': {'selected': {'count': 7}, 'older': {'count': 3}},
              'protocol_scope_analysis': {'assessment': {'answer': 'A development test may be source-reviewed.'}},
              'registered_protocol': {'notes': 'Measure honestly.'}}
    destination = tmp_path / 'bundle'
    submitted = review_input_bundle(destination, packet)
    assert 'content' not in submitted['experiment_plan']['source_files'][0]
    assert packet['experiment_plan']['source_files'][0]['content'] == 'print(1)\n'
    reference = submitted['evidence_sections']['prepared_implementations']
    assert json.loads(Path(reference['path']).read_text()) == packet['prepared_implementations']
    assert submitted['registered_protocol'] == packet['registered_protocol']
    assert submitted['development_executions'] == {'selected': {'count': 7}}
    history = submitted['evidence_sections']['development_executions']
    assert json.loads(Path(history['path']).read_text()) == packet['development_executions']
    assert hashlib.sha256(Path(history['path']).read_bytes()).hexdigest() == history['sha256']
    analysis = {'question': {'evidence_ids': ['selected']},
                'execution_inventory': {'base_path': '/tree', 'scope': 'Reservations and actual runner states.',
                    'groups': {'baseline_preflight': {
                        'selected': {'runner_status': 'timeout', 'runner_receipt_exists': True},
                        'older': {'runner_status': 'completed', 'runner_receipt_exists': True}}, 'nodes': {}}},
                'development_evidence': {'selected': {'count': 7}, 'older': {'count': 3}},
                'measurement_facts': {'selected': {'pointer': '/count', 'value': 7}}}
    focused = analysis_input_bundle(tmp_path / 'analysis', analysis)
    assert focused['development_evidence'] == {'selected': {'count': 7}}
    assert focused['measurement_facts'] == analysis['measurement_facts']
    inventory = focused['execution_inventory']
    assert inventory['base_path'] == '/tree'
    assert inventory['groups']['baseline_preflight'] == {
        'selected': {'runner_status': 'timeout', 'runner_receipt_exists': True}}
    archived_inventory = focused['evidence_sections']['execution_inventory']
    assert json.loads(Path(archived_inventory['path']).read_text()) == analysis['execution_inventory']
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
    assert delta['prior_work_decision'] == packet['work_decision']
    assert delta['prior_protocol_scope_analysis'] == packet['protocol_scope_analysis']
    assert not delta['bounded_revision']
    assert predecessor_review_delta(tmp_path, work, packet)['bounded_revision']
    unrelated = json.loads(json.dumps(packet))
    unrelated['experiment_plan']['source_files'][0]['path'] = 'another_experiment.py'
    assert predecessor_review_delta(tmp_path, work, unrelated) is None
    (prior_dir / 'review.json').write_text(json.dumps({'request_sha256': hashlib.sha256(request).hexdigest(),
        'assessment': {'decision': 'reject', 'reason': 'Input coordinate type mismatch.'}}))
    same_work = {**work, 'implementation_review': {'receipt_path': str(prior_dir / 'review.json')}}
    repaired = predecessor_review_delta(tmp_path, same_work, changed)
    assert repaired['prior_assessment']['decision'] == 'reject'
    assert '+print(2)' in repaired['source_diffs']['measure.py']
    assert not repaired['bounded_revision']
    assert predecessor_review_delta(tmp_path, same_work, packet) is None
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
    packet['experiment_plan'] = {'success_criteria': ['Complete the cells.']}
    rejection = {'decision': 'reject', 'required_work': ['Repair the reachable input exception.'],
                 'observation_checks': [], 'blocking_basis': [{'required_work_index': 0,
                     'scope': 'selected_test', 'basis_path': 'experiment_plan.success_criteria.0',
                     'basis_quote': 'Complete the cells.'}]}
    validate_execution_objections(rejection, packet)
    rejection['observation_checks'] = [{'observation_id': 'invented', 'emitted_by_producer': True, 'reason': 'Unknown output.'}]
    with pytest.raises(ValueError, match='distinct declared'):
        validate_execution_objections(rejection, packet)


def test_valid_saved_objection_survives_host_citation_validator_repair(tmp_path):
    from research_harness.orchestrator.research_review import ReviewContractError, validate_execution_objections
    packet = {'decision_scope': 'development_execution',
              'experiment_plan': {'success_criteria': ['Complete the declared cells.']},
              'node': {'claim_contract': {'disproof_conditions': ['The declared cells are incomplete.']}},
              'work_decision': {'test': 'Use the selected intervention.'},
              'predecessor_review_delta': {'bounded_revision': False, 'source_diffs': {'old.py': 'irrelevant old source'}},
              'measurement_output_contract': {'unexpected_observations': 'Evidence must be a string.'}}
    assessment = {'decision': 'reject', 'reason': 'Reachable input and output defects.',
                  'evidence': ['Inspected engine and output producer.'], 'next_steps': [],
                  'required_work': ['Accept actual engine coordinates.', 'Use the selected intervention.', 'Emit string evidence.'],
                  'observation_checks': [], 'blocking_basis': [
                      {'required_work_index': 0, 'scope': 'selected_test',
                       'basis_path': 'node.claim_contract.disproof_conditions[0]', 'basis_quote': 'The declared cells are incomplete.'},
                      {'required_work_index': 0, 'scope': 'selected_test',
                       'basis_path': 'experiment_plan.success_criteria[0]', 'basis_quote': 'Complete the declared cells.'},
                      {'required_work_index': 1, 'scope': 'method_semantics',
                       'basis_path': 'work_decision.test', 'basis_quote': 'Use the selected intervention.'},
                      {'required_work_index': 2, 'scope': 'runtime_contract',
                       'basis_path': 'measurement_output_contract.unexpected_observations', 'basis_quote': 'Evidence must be a string.'}]}
    repo = Path(__file__).resolve().parents[1]
    with patch('research_harness.orchestrator.research_review.CodexCliAdapter.complete',
               return_value=CompletionResult(json.dumps(assessment), AgentUsage(input_tokens=71), 'paid-review')) as complete:
        with patch('research_harness.orchestrator.research_review.validate_execution_objections',
                   side_effect=ValueError('Old validator omitted the host output contract.')):
            with pytest.raises(ReviewContractError):
                review_research_packet(repo, tmp_path, packet, purpose='Development test.')
        packet.pop('predecessor_review_delta')
        recovered = review_research_packet(repo, tmp_path, packet, purpose='Development test.')
        assert recovered['assessment'] == assessment
        assert recovered['usage']['input_tokens'] == 71
        assert recovered['thread_id'] == 'paid-review'
        assert recovered['recovered_after_contract_validation']
        complete.assert_called_once()
    assessment['blocking_basis'][1]['basis_quote'] = 'Invented extra requirement.'
    with pytest.raises(ValueError, match='quote does not match'):
        validate_execution_objections(assessment, packet)
    assessment['blocking_basis'] = assessment['blocking_basis'][:1]
    with pytest.raises(ValueError, match='Every blocking change'):
        validate_execution_objections(assessment, packet)


@pytest.mark.parametrize("role", ["analysis", "execution_review", "protocol_review"])
def test_interrupted_analysis_reuses_completed_reads_for_tool_free_synthesis(tmp_path, role, monkeypatch):
    from research_harness.orchestrator.research_review import analyze_research_packet
    from research_harness.adapters.codex_cli import CodexCliError
    repo = Path(__file__).resolve().parents[1]
    packet = {'question': {'evidence_ids': []}}
    assess = analyze_research_packet
    if role == 'execution_review':
        packet['decision_scope'] = 'development_execution'
        packet['experiment_plan'] = {'source_files': [{'path': 'measure.py', 'content': 'print(7)'}]}
        assess = review_research_packet
    if role == 'protocol_review':
        packet.update(proposal={'notes': 'Preserve endpoints.'}, work_decision={'kind': 'protocol_revision', 'evidence_ids': ['selected']},
                      protocol_note_history={'entries': [{'notes': 'Original endpoints.'}]},
                      analysis_findings={'selected': {'answer': 'Relevant finding.'}, 'older': {'answer': 'Large unrelated history.'}},
                      prior_review_context={'unchanged_packet_fields': ['protocol_note_history']})
        assess = review_research_packet
    assessment = {'status': 'answered', 'answer': 'The recorded count is 7.',
                  'evidence': ['metrics.json /count'], 'limitations': [], 'next_steps': []}
    if role == 'execution_review':
        assessment = {'decision': 'approve', 'reason': 'The producer emits the count.',
                      'evidence': ['metrics.json /count'], 'required_work': [], 'next_steps': [],
                      'blocking_basis': [], 'observation_checks': []}
    if role == 'protocol_review':
        assessment = {'decision': 'approve', 'reason': 'The amendment preserves the endpoint.',
                      'evidence': ['proposal.notes'], 'required_work': [], 'next_steps': []}
    requests = []
    budget = tmp_path / 'call_budget.json'
    budget.write_text(json.dumps({'max_prompt_bytes': 100000, 'max_call_prompt_bytes': 16000}))
    monkeypatch.setenv('RESEARCH_HARNESS_CALL_BUDGET', str(budget))
    def complete(request):
        requests.append(request)
        assert len((request.prompt.instructions + request.prompt.input).encode()) < 16000
        assert request.timeout_seconds == (1200 if len(requests) > 1 or role in {'execution_review', 'protocol_review'} else 600)
        if role == 'protocol_review':
            submitted = json.loads(request.prompt.input)
            assert submitted['analysis_findings'] == {'selected': {'answer': 'Relevant finding.'}}
            history = submitted['evidence_sections']['protocol_note_history']
            assert json.loads(Path(history['path']).read_text()) == packet['protocol_note_history']
            reference = submitted['evidence_sections']['analysis_findings']
            assert json.loads(Path(reference['path']).read_text()) == packet['analysis_findings']
        if len(requests) == 1:
            request.event_log_path.write_text(json.dumps({'type': 'item.completed', 'item': {
                'type': 'command_execution', 'command': 'cat history.json',
                'exit_code': 0, 'aggregated_output': 'x' * 20000}}) + '\n' + json.dumps({'type': 'item.completed', 'item': {
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
        result = assess(repo, tmp_path, packet, purpose='Finish the same count from inspected evidence.')
        assert result['assessment'] == assessment
        assert assess(repo, tmp_path, packet, purpose='Finish the same count from inspected evidence.') == result
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
    path.write_text('\n'.join(json.dumps({'type': 'item.completed', 'item': {
        'type': 'command_execution', 'exit_code': 0, 'command': command, 'aggregated_output': output}})
        for command, output in [('cat /workspace/experiment.py', 'source' * 10000),
                                ('cat /workspace/inputs/engine.py', 'engine' * 5000),
                                ('cat /framework/validator.py', 'framework' * 8600),
                                ('python /workspace/experiment.py', '{"failed_step":92}')]))
    recovered = completed_inspection(path, ('/workspace/experiment.py',), source_inlined=True)
    assert [row['output'] for row in recovered['observations']] == ['engine' * 5000, '{"failed_step":92}']
    assert recovered['source_supplied_inline']


def test_interrupted_large_source_review_keeps_bounded_reader_and_exact_references(tmp_path, monkeypatch):
    import hashlib
    import shutil
    from research_harness.adapters.codex_cli import CodexCliError

    repo = tmp_path / 'repo'
    schemas = repo / 'research_harness/schemas'
    schemas.mkdir(parents=True)
    shutil.copy(Path(__file__).resolve().parents[1] / 'research_harness/schemas/research_execution_review_response.schema.json', schemas)
    thread = repo / 'runs/threads/t'
    thread.mkdir(parents=True)
    source = thread / 'saved_table.json'
    source.write_text(json.dumps({'rows': ['saved measurement'] * 60000}))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    packet = {'decision_scope': 'development_execution',
              'experiment_plan': {'source_files': [{'path': source.name, 'content': source.read_text()}]},
              'execution_source_manifest': [{'relative_path': source.name, 'path': str(source), 'sha256': digest}]}
    budget = tmp_path / 'budget.json'
    budget.write_text(json.dumps({'max_prompt_bytes': 100000, 'max_call_prompt_bytes': 16000}))
    monkeypatch.setenv('RESEARCH_HARNESS_CALL_BUDGET', str(budget))
    requests = []
    def complete(request):
        requests.append(request)
        assert len((request.prompt.instructions + request.prompt.input).encode()) < 16000
        assert not request.allow_local_tools
        assert request.mcp is not None and '--read-only-thread' in request.mcp.args
        assert '--deny-read-path' in request.mcp.args
        submitted = json.loads(request.prompt.input)
        reference = submitted['experiment_plan']['source_files'][0]
        assert 'content' not in reference
        assert reference['materialized_source']['sha256'] == digest
        if len(requests) == 1:
            observation = {'type': 'item.completed', 'item': {'type': 'mcp_tool_call',
                'server': 'research_harness', 'tool': 'read_research_artifact',
                'arguments': {'path': str(source), 'json_pointer': '/rows/0'}, 'error': None,
                'result': {'content': [{'type': 'text', 'text': '{"value":"saved measurement"}'}]}}}
            failed = {'type': 'item.completed', 'item': {**observation['item'],
                      'error': {'message': 'failed read'}, 'result': None}}
            request.event_log_path.write_text(json.dumps(observation) + '\n' + json.dumps(failed) + '\n{"type":')
            raise CodexCliError('interrupted')
        assert submitted['completed_inspection']['observations'][0]['output'] == '{"value":"saved measurement"}'
        assert len(submitted['completed_inspection']['observations']) == 1
        return CompletionResult(json.dumps({'decision': 'approve', 'reason': 'Bound recorded table.',
            'evidence': ['saved_table.json'], 'required_work': [], 'next_steps': [],
            'blocking_basis': [], 'observation_checks': []}), AgentUsage(), None)
    with patch('research_harness.orchestrator.research_review.CodexCliAdapter.complete', side_effect=complete):
        with pytest.raises(CodexCliError):
            review_research_packet(repo, thread / 'review', packet, purpose='Inspect saved data binding.')
        result = review_research_packet(repo, thread / 'review', packet, purpose='Inspect saved data binding.')
        assert result['assessment']['decision'] == 'approve'
        assert review_research_packet(repo, thread / 'review', packet, purpose='Inspect saved data binding.') == result
    assert len(requests) == 2


@pytest.mark.parametrize('scope_status,execution_scope', [('answered', 'eligible_for_source_review'),
    ('answered', 'blocked'), ('unresolved', 'unresolved')])
def test_protocol_interpretation_is_preserved_across_implementation_revisions(tmp_path, scope_status, execution_scope):
    from research_harness.orchestrator.research_review import ProtocolScopeNeedsReplanning
    packet = {'decision_scope': 'development_execution', 'work_decision': {'test': 'Compare matched controls.'},
              'development_executions': {'prior_control': {'execution_status': 'completed'}},
              'preparation_execution_disclosure': {'attempts': [{'stage': 'fit', 'registered': False}]},
              'registered_protocol': {'notes': 'Keep the original outcome.'},
              'protocol_note_history': {'entries': [{'notes': 'Keep the original outcome.'},
                                                    {'notes': 'A later confirmation must use unused inputs.'}]},
              'experiment_plan': {'source_files': [{'path': 'measure.py', 'content': 'print(1)'}]}}
    prior_scope = {'prior_receipt_path': '/prior/review.json', 'prior_request_sha256': 'prior-digest',
                   'prior_assessment': {'decision': 'approve', 'reason': 'Earlier test passed source review.'},
                   'prior_work_decision': {'test': 'Earlier matched control.'},
                   'prior_protocol_scope_analysis': {'assessment': {'execution_scope': 'eligible_for_source_review'}},
                   'changed_conditions': []}
    packet['predecessor_review_delta'] = {**prior_scope, 'bounded_revision': False}
    repo = Path(__file__).resolve().parents[1]
    seen = []
    def complete(request):
        value = json.loads(request.prompt.input)
        seen.append(value)
        if 'amendment_history' in value:
            assert value['development_executions'] == packet['development_executions']
            assert value['preparation_execution_disclosure'] == packet['preparation_execution_disclosure']
            assert value['prior_scope_context'] == prior_scope
            assert not request.allow_local_tools
            assert request.timeout_seconds == 1200
            result = {'status': scope_status, 'execution_scope': execution_scope, 'answer': 'Preserve outcome; confirmation is later.',
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
        if execution_scope != 'eligible_for_source_review':
            for _ in range(2):
                with pytest.raises(ProtocolScopeNeedsReplanning) as unresolved:
                    review_research_packet(repo, tmp_path, packet, purpose='Development test.')
                assert unresolved.value.analysis['assessment']['execution_scope'] == execution_scope
            assert len(seen) == 1
            return
        first = review_research_packet(repo, tmp_path, packet, purpose='Development test.')
        assert review_research_packet(repo, tmp_path, packet, purpose='Development test.') == first
        packet['experiment_plan']['source_files'][0]['content'] = 'print(2)'
        packet['predecessor_review_delta']['prior_assessment'] = {
            'decision': 'reject', 'reason': 'Repair source aggregation; selected scope unchanged.'}
        review_research_packet(repo, tmp_path, packet, purpose='Development test.')
    assert len(seen) == 3
    assert sum('amendment_history' in value for value in seen) == 1


def test_changed_execution_history_requires_new_scope_judgment(tmp_path):
    from research_harness.orchestrator.research_review import _complete_packet
    repo = Path(__file__).resolve().parents[1]
    packet = {'work_decision': {'test': 'One prospective cell.'},
              'registered_protocol': {'notes': 'Only once.'},
              'amendment_history': {'entries': []}, 'development_executions': {}}
    assessment = {'status': 'answered', 'execution_scope': 'eligible_for_source_review',
                  'answer': 'Not consumed.', 'evidence': [], 'limitations': [], 'next_steps': []}
    with patch('research_harness.orchestrator.research_review.CodexCliAdapter.complete',
               return_value=CompletionResult(json.dumps(assessment), AgentUsage(), None)) as complete:
        def review():
            return _complete_packet(repo, tmp_path, packet, instructions='Inspect scope.',
                                    schema_name='research_protocol_scope_response', source_inspection=False)
        first = review()
        packet['prior_scope_context'] = {'prior_assessment': assessment}
        assert review() == first
        assert complete.call_count == 1
        packet['development_executions']['cell'] = {'execution_status': 'completed'}
        review()
        assert complete.call_count == 2
