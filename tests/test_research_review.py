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
        'blocking_basis': [{'required_work_index': 0, 'scope': scope, 'basis_path': path, 'basis_quote': quote}]}
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
    from research_harness.orchestrator.research_review import review_input_bundle, predecessor_review_delta

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
                  'evidence': ['measure.py'], 'required_work': [], 'next_steps': [], 'blocking_basis': []}
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
