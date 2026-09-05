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


def _verified_run_ids(review_run_artifacts: list[dict[str, Any]] | None) -> set[str]:
    """Validate bindings supplied by the harness review-run verifier.

    This function checks receipt structure and correlations only. The caller
    must derive these records by reopening runner artifacts; arbitrary caller
    hashes do not establish that a review ran.
    """
    verified: set[str] = set()
    for artifact in review_run_artifacts or []:
        required = {
            "kind", "review_id", "reviewer_id", "prompt_sha256",
            "response_sha256", "review_record_sha256", "runner_receipt_sha256",
        }
        if set(artifact) != required or artifact.get("kind") != "verified_harness_review_run":
            raise ScientificReviewError("invalid verified review-run artifact")
        for key in ("prompt_sha256", "response_sha256", "review_record_sha256",
                    "runner_receipt_sha256"):
            value = artifact.get(key)
            if not isinstance(value, str) or len(value) != 64 or any(
                char not in "0123456789abcdef" for char in value
            ):
                raise ScientificReviewError(f"invalid review-run {key}")
        review_id = artifact.get("review_id")
        reviewer_id = artifact.get("reviewer_id")
        if not isinstance(review_id, str) or not review_id or not isinstance(reviewer_id, str) or not reviewer_id:
            raise ScientificReviewError("review-run artifact lacks review identity")
        if review_id in verified:
            raise ScientificReviewError(f"duplicate verified review-run artifact: {review_id}")
        verified.add(review_id)
    return verified


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
    *,
    review_run_artifacts: list[dict[str, Any]] | None = None,
    require_verified_runs: bool = False,
) -> dict[str, Any]:
    """Validate evidence-bound reviews and return an auditable readiness result.

    A ready result means only that the required judgments were recorded against
    the current artifacts by independent reviewers and have no open blocker.
    """
    if not review_records:
        raise ScientificReviewError("scientific review records are required")
    ledger_digest = json_digest(ledger)
    evidence_ids, citation_ids = _artifact_ids(ledger)
    section_ids = set(ledger["sections"])
    verified_run_ids = _verified_run_ids(review_run_artifacts)
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
            raise ScientificReviewError("declared reviewer identities must be distinct")
        if reviewer["invocation_sha256"] in invocation_ids:
            raise ScientificReviewError("declared review invocation hashes must be distinct")
        reviewer_ids.add(reviewer["reviewer_id"])
        invocation_ids.add(reviewer["invocation_sha256"])
        matching_runs = [
            artifact for artifact in review_run_artifacts or []
            if artifact["review_id"] == record["review_id"]
        ]
        if matching_runs and matching_runs[0]["reviewer_id"] != reviewer["reviewer_id"]:
            raise ScientificReviewError(
                f"review-run reviewer differs for review {record['review_id']}"
            )
        if matching_runs and matching_runs[0]["review_record_sha256"] != json_digest(record):
            raise ScientificReviewError(
                f"review-run response differs from review {record['review_id']}"
            )

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
            if category == "closest_work":
                if not assessment["citation_ids"]:
                    raise ScientificReviewError("closest_work must cite retrieved literature")
                if not assessment["difference_from_closest_work"].strip():
                    raise ScientificReviewError("closest_work must explain the claimed difference")
            if category == "importance" and (
                not assessment["evidence_ids"] or not assessment["citation_ids"]
            ):
                raise ScientificReviewError(
                    "importance must cite both research evidence and literature"
                )

        for objection in record["objections"]:
            objection_id = objection["objection_id"]
            if objection_id in objection_ids:
                raise ScientificReviewError(f"duplicate objection id: {objection_id}")
            objection_ids.add(objection_id)
            _check_references(objection, evidence_ids, citation_ids,
                              f"objection {objection_id}")
            if objection["status"] != "open" and not objection["resolution"].strip():
                raise ScientificReviewError(f"resolved objection lacks resolution: {objection_id}")
            if objection["status"] != "open":
                changed_sections = objection["changed_section_ids"]
                resolution_evidence = objection["resolution_evidence_ids"]
                if (not changed_sections
                        or not set(changed_sections) <= section_ids):
                    raise ScientificReviewError(
                        f"resolved objection lacks concrete changed sections: {objection_id}"
                    )
                if (not resolution_evidence
                        or not set(resolution_evidence) <= evidence_ids):
                    raise ScientificReviewError(
                        f"resolved objection lacks concrete resolution evidence: {objection_id}"
                    )
                if not objection["claim_scope_change"].strip():
                    raise ScientificReviewError(
                        f"resolved objection lacks claim-scope explanation: {objection_id}"
                    )
            if objection["blocking"] and objection["status"] == "open":
                open_blockers.append(objection_id)

    if len(reviewer_ids) < 2:
        raise ScientificReviewError("at least two distinct declared scientific reviewers are required")
    missing = sorted(REQUIRED_CATEGORIES - categories)
    if missing:
        raise ScientificReviewError(f"missing scientific review categories: {missing}")
    if require_verified_runs:
        missing_runs = sorted(
            record["review_id"] for record in review_records
            if record["review_id"] not in verified_run_ids
        )
        if missing_runs:
            raise ScientificReviewError(
                f"reviews lack harness-verified run artifacts: {missing_runs}"
            )

    return {
        "version": 1,
        "kind": "evidence_bound_scientific_readiness",
        "ready": not open_blockers,
        "manuscript_sha256": manuscript_digest,
        "evidence_ledger_sha256": ledger_digest,
        "review_record_sha256s": [json_digest(record) for record in review_records],
        "declared_reviewer_ids": sorted(reviewer_ids),
        "verified_review_run_ids": sorted(verified_run_ids),
        "review_execution_provenance": (
            "harness_verified" if require_verified_runs else "declared_only"
        ),
        "categories": sorted(categories),
        "unresolved_blocking_objection_ids": sorted(open_blockers),
        "scope": (
            "Consistent evidence-bound scholarly judgments; this does not prove "
            "novelty, importance, correctness, or conference acceptance."
        ),
    }
