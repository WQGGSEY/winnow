from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_harness.agent_runtime import AgentUsage, CompletionResult
from research_harness.publishing.review_runner import (
    ScientificReviewRunError,
    replay_scientific_reviews,
    run_scientific_reviews,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class _Transport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return CompletionResult(
            text=json.dumps(self.response),
            usage=AgentUsage(input_tokens=100, output_tokens=50),
            thread_id=f"thread-{len(self.requests)}",
        )


def _response() -> dict:
    categories = ["importance", "closest_work", "argument_completeness", "reproducibility", "limitations"]
    return {"assessments": [{
        "category": category,
        "judgment": f"Artifact-grounded judgment for {category}.",
        "evidence_ids": ["worker_report.metrics.return"],
        "citation_ids": ["closest"],
        "difference_from_closest_work": "Evaluates a different frozen setting." if category == "closest_work" else "",
    } for category in categories], "objections": []}


def _publication(tmp_path: Path) -> Path:
    publication = tmp_path / "publication"
    (publication / "_drafts").mkdir(parents=True)
    (publication / "paper.html").write_text("<html><p>Complete paper</p></html>")
    ledger = {
        "sections": {"experiments": [{"anchor": "worker_report.metrics.return", "source_sha256": "a" * 64, "value_sha256": "b" * 64}]},
        "citations": {"closest": {"source": {"title": "Prior work"}, "sha256": "c" * 64}},
    }
    (publication / "_drafts" / "evidence_ledger.json").write_text(json.dumps(ledger))
    return publication


def test_runs_two_isolated_reviews_and_replays_readiness(tmp_path: Path) -> None:
    publication = _publication(tmp_path)
    transport = _Transport(_response())

    result = run_scientific_reviews(REPO_ROOT, publication, "test-model", transport)

    assert len(transport.requests) == 2
    assert all(request.allow_local_tools is False for request in transport.requests)
    assert transport.requests[0].cwd != transport.requests[1].cwd
    assert all("Complete paper" in request.prompt.input for request in transport.requests)
    assert result["readiness"]["ready"] is True
    assert result["readiness"]["review_execution_provenance"] == "harness_verified"


@pytest.mark.parametrize("filename", ["prompt.json", "raw_response.txt", "record.json"])
def test_replay_rejects_mutated_review_artifacts(tmp_path: Path, filename: str) -> None:
    publication = _publication(tmp_path)
    run_scientific_reviews(REPO_ROOT, publication, "test-model", _Transport(_response()))
    path = publication / "scientific_reviews" / "review-1" / filename
    path.write_text(path.read_text() + " ")

    with pytest.raises(ScientificReviewRunError, match="changed review artifact"):
        replay_scientific_reviews(publication)
