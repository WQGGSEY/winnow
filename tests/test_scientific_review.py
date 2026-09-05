from __future__ import annotations

import copy

import pytest

from research_harness.publishing.integrity import json_digest
from research_harness.publishing.scientific_review import (
    ScientificReviewError,
    assess_readiness,
)


def _ledger() -> dict:
    return {
        "sections": {
            "experiments": [{
                "anchor": "worker_report.metrics.return",
                "source_sha256": "a" * 64,
                "value_sha256": "b" * 64,
            }],
        },
        "citations": {
            "closest": {"source": {"title": "Prior work"}, "sha256": "c" * 64},
        },
    }


def _records(manuscript_digest: str, ledger: dict) -> list[dict]:
    categories = ["importance", "closest_work", "argument_completeness",
                  "reproducibility", "limitations"]
    records = []
    for index in range(2):
        records.append({
            "version": 1,
            "review_id": f"review-{index}",
            "manuscript_sha256": manuscript_digest,
            "evidence_ledger_sha256": json_digest(ledger),
            "reviewer": {
                "reviewer_id": f"reviewer-{index}",
                "provider": "independent-runner",
                "model": f"model-{index}",
                "invocation_sha256": str(index + 1) * 64,
            },
            "assessments": [{
                "category": category,
                "judgment": f"Evidence-bound judgment for {category}",
                "evidence_ids": ["worker_report.metrics.return"],
                "citation_ids": ["closest"],
                "difference_from_closest_work": (
                    "Unlike Prior work, this manuscript evaluates the frozen symmetric setting."
                    if category == "closest_work" else ""
                ),
            } for category in categories[index::2]],
            "objections": [],
        })
    return records


def test_valid_independent_reviews_are_ready() -> None:
    ledger = _ledger()
    result = assess_readiness("d" * 64, ledger, _records("d" * 64, ledger))

    assert result["ready"] is True
    assert result["categories"] == sorted([
        "importance", "closest_work", "argument_completeness",
        "reproducibility", "limitations",
    ])
    assert "does not prove novelty" in result["scope"]


def test_missing_records_and_categories_are_rejected() -> None:
    ledger = _ledger()
    with pytest.raises(ScientificReviewError, match="records are required"):
        assess_readiness("d" * 64, ledger, [])

    records = _records("d" * 64, ledger)
    records[1]["assessments"] = records[1]["assessments"][:-1]
    with pytest.raises(ScientificReviewError, match="missing scientific review categories"):
        assess_readiness("d" * 64, ledger, records)


@pytest.mark.parametrize("binding", ["manuscript_sha256", "evidence_ledger_sha256"])
def test_stale_review_binding_is_rejected(binding: str) -> None:
    ledger = _ledger()
    records = _records("d" * 64, ledger)
    records[0][binding] = "e" * 64

    with pytest.raises(ScientificReviewError, match="stale"):
        assess_readiness("d" * 64, ledger, records)


def test_nonexistent_artifact_reference_is_rejected() -> None:
    ledger = _ledger()
    records = _records("d" * 64, ledger)
    records[0]["assessments"][0]["evidence_ids"] = ["invented.metric"]

    with pytest.raises(ScientificReviewError, match="nonexistent artifacts"):
        assess_readiness("d" * 64, ledger, records)


def test_unresolved_blocker_rejects_readiness() -> None:
    ledger = _ledger()
    records = _records("d" * 64, ledger)
    records[0]["objections"] = [{
        "objection_id": "closest-work-gap",
        "category": "closest_work",
        "blocking": True,
        "status": "open",
        "statement": "The nearest comparison is incomplete.",
        "resolution": "",
        "evidence_ids": [],
        "citation_ids": ["closest"],
        "changed_section_ids": [],
        "resolution_evidence_ids": [],
        "claim_scope_change": "",
    }]

    result = assess_readiness("d" * 64, ledger, records)
    assert result["ready"] is False
    assert result["unresolved_blocking_objection_ids"] == ["closest-work-gap"]


def test_resolved_objection_requires_resolution_text() -> None:
    ledger = _ledger()
    records = _records("d" * 64, ledger)
    records[0]["objections"] = [{
        "objection_id": "limitation",
        "category": "limitations",
        "blocking": True,
        "status": "accepted_limitation",
        "statement": "Scope is narrow.",
        "resolution": "",
        "evidence_ids": ["worker_report.metrics.return"],
        "citation_ids": [],
        "changed_section_ids": [],
        "resolution_evidence_ids": [],
        "claim_scope_change": "",
    }]

    with pytest.raises(ScientificReviewError, match="lacks resolution"):
        assess_readiness("d" * 64, ledger, records)


def test_duplicate_reviewer_provenance_is_not_independent() -> None:
    ledger = _ledger()
    records = _records("d" * 64, ledger)
    records[1]["reviewer"] = copy.deepcopy(records[0]["reviewer"])

    with pytest.raises(ScientificReviewError, match="identities must be distinct"):
        assess_readiness("d" * 64, ledger, records)


def test_closest_work_requires_literature_and_difference() -> None:
    ledger = _ledger()
    records = _records("d" * 64, ledger)
    closest = next(
        assessment for record in records for assessment in record["assessments"]
        if assessment["category"] == "closest_work"
    )
    closest["citation_ids"] = []
    with pytest.raises(ScientificReviewError, match="retrieved literature"):
        assess_readiness("d" * 64, ledger, records)

    closest["citation_ids"] = ["closest"]
    closest["difference_from_closest_work"] = ""
    with pytest.raises(ScientificReviewError, match="claimed difference"):
        assess_readiness("d" * 64, ledger, records)


def test_importance_requires_evidence_and_literature() -> None:
    ledger = _ledger()
    records = _records("d" * 64, ledger)
    importance = next(
        assessment for record in records for assessment in record["assessments"]
        if assessment["category"] == "importance"
    )
    importance["evidence_ids"] = []
    with pytest.raises(ScientificReviewError, match="both research evidence and literature"):
        assess_readiness("d" * 64, ledger, records)

def test_declared_hashes_are_not_reported_as_verified_runs() -> None:
    ledger = _ledger()
    result = assess_readiness("d" * 64, ledger, _records("d" * 64, ledger))

    assert result["review_execution_provenance"] == "declared_only"
    assert result["verified_review_run_ids"] == []


def test_verified_runs_can_be_required_at_integration_boundary() -> None:
    ledger = _ledger()
    records = _records("d" * 64, ledger)
    with pytest.raises(ScientificReviewError, match="lack harness-verified"):
        assess_readiness(
            "d" * 64, ledger, records, require_verified_runs=True,
        )

    artifacts = [{
        "kind": "verified_harness_review_run",
        "review_id": record["review_id"],
        "reviewer_id": record["reviewer"]["reviewer_id"],
        "prompt_sha256": "a" * 64,
        "response_sha256": "b" * 64,
        "review_record_sha256": json_digest(record),
        "runner_receipt_sha256": str(index + 3) * 64,
    } for index, record in enumerate(records)]
    result = assess_readiness(
        "d" * 64, ledger, records,
        review_run_artifacts=artifacts,
        require_verified_runs=True,
    )
    assert result["ready"] is True
    assert result["review_execution_provenance"] == "harness_verified"

    artifacts[0]["review_record_sha256"] = "f" * 64
    with pytest.raises(ScientificReviewError, match="response differs"):
        assess_readiness(
            "d" * 64, ledger, records,
            review_run_artifacts=artifacts,
            require_verified_runs=True,
        )


def test_resolved_objection_requires_changed_section_evidence_and_scope() -> None:
    ledger = _ledger()
    records = _records("d" * 64, ledger)
    objection = {
        "objection_id": "scope-gap",
        "category": "limitations",
        "blocking": True,
        "status": "resolved",
        "statement": "The claim exceeds the measured population.",
        "resolution": "Narrowed the claim.",
        "evidence_ids": ["worker_report.metrics.return"],
        "citation_ids": [],
        "changed_section_ids": [],
        "resolution_evidence_ids": [],
        "claim_scope_change": "",
    }
    records[0]["objections"] = [objection]
    with pytest.raises(ScientificReviewError, match="changed sections"):
        assess_readiness("d" * 64, ledger, records)

    objection["changed_section_ids"] = ["experiments"]
    with pytest.raises(ScientificReviewError, match="resolution evidence"):
        assess_readiness("d" * 64, ledger, records)

    objection["resolution_evidence_ids"] = ["worker_report.metrics.return"]
    with pytest.raises(ScientificReviewError, match="claim-scope"):
        assess_readiness("d" * 64, ledger, records)

    objection["claim_scope_change"] = "Limited the claim to the evaluated tasks."
    assert assess_readiness("d" * 64, ledger, records)["ready"] is True
