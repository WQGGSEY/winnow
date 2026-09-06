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
