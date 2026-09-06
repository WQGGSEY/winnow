from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_harness.agent_runtime import AgentUsage, CompletionResult
from research_harness.publishing.manuscript import resolve_anchor
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
        "sections": {"experiments": [resolve_anchor(
            {"worker_report": {"metrics": {"return": 12.5}}}, "worker_report.metrics.return",
        )]},
        "citations": {"closest": {"source": {"title": "Prior work"}, "sha256": "c" * 64}},
    }
    (publication / "_drafts" / "evidence_ledger.json").write_text(json.dumps(ledger))
    return publication


@pytest.mark.parametrize('with_figure', [False, True])
def test_runs_two_isolated_reviews_and_replays_readiness(tmp_path: Path, with_figure: bool) -> None:
    publication = _publication(tmp_path)
    transport = _Transport(_response())
    if with_figure:
        from PIL import Image
        (publication / 'figures').mkdir()
        Image.new('RGB', (8, 8), 'white').save(publication / 'figures/curve.png')
        (publication / 'paper.html').write_text('<html><p>Complete paper</p><img src="figures/curve.png"></html>')

    result = run_scientific_reviews(REPO_ROOT, publication, "test-model", transport)

    assert len(transport.requests) == 2
    assert all(request.allow_local_tools is with_figure for request in transport.requests)
    assert transport.requests[0].cwd != transport.requests[1].cwd
    assert all("Complete paper" in request.prompt.input for request in transport.requests)
    for request in transport.requests:
        evidence = json.loads(request.prompt.input)["evidence_ledger"]["sections"]["experiments"]
        assert evidence[0]["value"] == 12.5
        if with_figure:
            import hashlib
            figure = json.loads(request.prompt.input)['figure_files'][0]
            assert Path(figure['path']).read_bytes() == (publication / 'figures/curve.png').read_bytes()
            assert hashlib.sha256(Path(figure['path']).read_bytes()).hexdigest() == figure['sha256']
    assert result["readiness"]["ready"] is True
    assert result["readiness"]["review_execution_provenance"] == "harness_verified"
    assert run_scientific_reviews(REPO_ROOT, publication, "test-model", transport) == result
    assert len(transport.requests) == 2
    if with_figure:
        Image.new('RGB', (8, 8), 'black').save(publication / 'figures/curve.png')
        with pytest.raises(ScientificReviewRunError, match='stale'):
            replay_scientific_reviews(publication)


@pytest.mark.parametrize("filename", ["prompt.json", "raw_response.txt", "record.json"])
def test_replay_rejects_mutated_review_artifacts(tmp_path: Path, filename: str) -> None:
    publication = _publication(tmp_path)
    run_scientific_reviews(REPO_ROOT, publication, "test-model", _Transport(_response()))
    path = publication / "scientific_reviews" / "review-1" / filename
    path.write_text(path.read_text() + " ")

    with pytest.raises(ScientificReviewRunError, match="changed review artifact"):
        replay_scientific_reviews(publication)
