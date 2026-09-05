import json
from pathlib import Path
from unittest.mock import patch

import pytest

from research_harness.agent_runtime import AgentUsage, CompletionResult
from research_harness.orchestrator.research_review import review_research_packet


def test_independent_review_reuses_only_matching_evidence_and_rejects_unresolved_approval(tmp_path):
    assessment = {"decision": "reject", "reason": "incorrect method", "evidence": ["experiment.py:12"], "required_work": ["fix implementation"]}
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
