"""Evidence-bound scholarly review gate for publication readiness.

The gate checks consistency, provenance, and resolution of recorded judgments.
It does not prove novelty, importance, or acceptance; those remain scholarly
judgments. Numeric review scores are intentionally outside this contract.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_harness.publishing.integrity import json_digest
from research_harness.schemas.validator import validate_schema


REQUIRED_CATEGORIES = frozenset({
    "importance",
    "closest_work",
    "argument_completeness",
    "reproducibility",
    "limitations",
})


class ScientificReviewError(ValueError):
    """Raised when review records cannot support a readiness decision."""


def _schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "schemas" / "scientific_review.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_ids(ledger: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return evidence and citation IDs that resolve in a manuscript ledger."""
    evidence: set[str] = set()
    sections = ledger.get("sections")
    citations = ledger.get("citations")
    if not isinstance(sections, dict) or not isinstance(citations, dict):
        raise ScientificReviewError("malformed manuscript evidence ledger")
    for anchors in sections.values():
        if not isinstance(anchors, list):
            raise ScientificReviewError("malformed manuscript evidence anchors")
        for anchor in anchors:
            if not isinstance(anchor, dict):
                raise ScientificReviewError("malformed manuscript evidence anchor")
            for key in ("anchor", "source_sha256", "value_sha256"):
                value = anchor.get(key)
                if isinstance(value, str) and value:
                    evidence.add(value)
    citation_ids = {key for key in citations if isinstance(key, str) and key}
    return evidence, citation_ids


def _check_references(
    item: dict[str, Any], evidence_ids: set[str], citation_ids: set[str], label: str,
) -> None:
    evidence = item["evidence_ids"]
    citations = item["citation_ids"]
    if not evidence and not citations:
        raise ScientificReviewError(f"{label} cites no manuscript artifact")
    unknown_evidence = sorted(set(evidence) - evidence_ids)
    unknown_citations = sorted(set(citations) - citation_ids)
    if unknown_evidence or unknown_citations:
        raise ScientificReviewError(
            f"{label} cites nonexistent artifacts: "
            f"evidence={unknown_evidence}, citations={unknown_citations}"
        )


def assess_readiness(
    manuscript_digest: str,
    ledger: dict[str, Any],
    review_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate evidence-bound reviews and return an auditable readiness result.

    A ready result means only that the required judgments were recorded against
    the current artifacts by independent reviewers and have no open blocker.
    """
    if not review_records:
        raise ScientificReviewError("scientific review records are required")
    ledger_digest = json_digest(ledger)
    evidence_ids, citation_ids = _artifact_ids(ledger)
    categories: set[str] = set()
    reviewer_ids: set[str] = set()
    invocation_ids: set[str] = set()
    objection_ids: set[str] = set()
    open_blockers: list[str] = []

    schema = _schema()
    for record in review_records:
        try:
            validate_schema(schema, record)
        except ValueError as exc:
            raise ScientificReviewError(f"invalid scientific review record: {exc}") from exc
        if record["manuscript_sha256"] != manuscript_digest:
            raise ScientificReviewError(f"stale manuscript binding in review {record['review_id']}")
        if record["evidence_ledger_sha256"] != ledger_digest:
            raise ScientificReviewError(f"stale evidence ledger binding in review {record['review_id']}")

        reviewer = record["reviewer"]
        if reviewer["reviewer_id"] in reviewer_ids:
            raise ScientificReviewError("reviewer identities must be independent")
        if reviewer["invocation_sha256"] in invocation_ids:
            raise ScientificReviewError("review invocations must be independent")
        reviewer_ids.add(reviewer["reviewer_id"])
        invocation_ids.add(reviewer["invocation_sha256"])

        local_categories: set[str] = set()
        for assessment in record["assessments"]:
            category = assessment["category"]
            if category in local_categories:
                raise ScientificReviewError(
                    f"duplicate {category} assessment in review {record['review_id']}"
                )
            local_categories.add(category)
            categories.add(category)
            _check_references(assessment, evidence_ids, citation_ids,
                              f"{category} assessment")

        for objection in record["objections"]:
            objection_id = objection["objection_id"]
            if objection_id in objection_ids:
                raise ScientificReviewError(f"duplicate objection id: {objection_id}")
            objection_ids.add(objection_id)
            _check_references(objection, evidence_ids, citation_ids,
                              f"objection {objection_id}")
            if objection["status"] != "open" and not objection["resolution"].strip():
                raise ScientificReviewError(f"resolved objection lacks resolution: {objection_id}")
            if objection["blocking"] and objection["status"] == "open":
                open_blockers.append(objection_id)

    if len(reviewer_ids) < 2:
        raise ScientificReviewError("at least two independent scientific reviewers are required")
    missing = sorted(REQUIRED_CATEGORIES - categories)
    if missing:
        raise ScientificReviewError(f"missing scientific review categories: {missing}")

    return {
        "version": 1,
        "kind": "evidence_bound_scientific_readiness",
        "ready": not open_blockers,
        "manuscript_sha256": manuscript_digest,
        "evidence_ledger_sha256": ledger_digest,
        "review_record_sha256s": [json_digest(record) for record in review_records],
        "reviewer_ids": sorted(reviewer_ids),
        "categories": sorted(categories),
        "unresolved_blocking_objection_ids": sorted(open_blockers),
        "scope": (
            "Consistent evidence-bound scholarly judgments; this does not prove "
            "novelty, importance, correctness, or conference acceptance."
        ),
    }
