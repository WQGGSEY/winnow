from __future__ import annotations

from statistics import mean
from typing import Any


class ReductionError(ValueError):
    """Raised when orchestrator reduction lacks required evidence."""


def reduce_node(
    node: dict[str, Any],
    worker_report: dict[str, Any],
    critic_reviews: list[dict[str, Any]],
    branch_prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not critic_reviews:
        raise ReductionError("orchestrator reduction requires at least one critic review")

    blocking_reviews = [review for review in critic_reviews if review["blocking"]]
    all_scores = [review["scores"] for review in critic_reviews]
    score_summary = {
        key: round(mean(score[key] for score in all_scores), 2)
        for key in ("validity", "necessity", "reproducibility", "taste_alignment")
    }

    if blocking_reviews:
        final_verdict = "confounded_or_not_evaluable"
        research_status = "confounded_or_not_evaluable"
        next_transition = "needs_child_branch"
    elif _failure_category(worker_report) == "confounded_result":
        final_verdict = "confounded_or_not_evaluable"
        research_status = "confounded_or_not_evaluable"
        next_transition = "needs_child_branch"
    elif worker_report["claim_verdict_candidate"] == "supported":
        final_verdict = "supported_with_scope_narrowing"
        research_status = "supported_with_scope_narrowing"
        next_transition = "promoted"
    else:
        final_verdict = worker_report["claim_verdict_candidate"]
        research_status = "interpretable_negative_result"
        next_transition = "needs_child_branch"

    lesson_candidates: list[str] = []
    for review in critic_reviews:
        lesson_candidates.extend(review.get("lesson_candidates", []))

    child_branch_suggestions = (
        [
            {
                "type": "validity",
                "reason": "Unexpected observation should be checked without letting the worker pursue it.",
                "source": "worker_report.unexpected_observations",
            }
        ]
        if worker_report.get("unexpected_observations")
        else []
    )
    if next_transition == "needs_child_branch" and branch_prior:
        child_branch_suggestions.extend(branch_prior.get("branch_suggestions", []))
    child_branch_suggestions.extend(_baseline_branch_suggestions(worker_report))

    reduction = {
        "node_id": node["id"],
        "final_verdict": final_verdict,
        "research_status": research_status,
        "next_transition": next_transition,
        "score_summary": score_summary,
        "blocking_objections": [
            objection
            for review in blocking_reviews
            for objection in review.get("objections", [])
        ],
        "accepted_lesson_candidates": sorted(set(lesson_candidates)),
        "failure_branch_prior": branch_prior or {
            "source": "failure_memory",
            "query_tags": [],
            "selected_failure_files": [],
            "risk_controls": [],
            "branch_suggestions": [],
        },
        "child_branch_suggestions": child_branch_suggestions,
    }
    return reduction


def _failure_category(worker_report: dict[str, Any]) -> str | None:
    candidate = worker_report.get("failure_record_candidate")
    if not isinstance(candidate, dict):
        return None
    return str(candidate.get("category") or "")


def _baseline_branch_suggestions(worker_report: dict[str, Any]) -> list[dict[str, str]]:
    baseline_status = worker_report.get("baseline_evidence_status")
    if not isinstance(baseline_status, dict):
        return []
    if baseline_status.get("overall") == "passed":
        return []
    return [
        {
            "type": "necessity"
            if baseline_status.get("overall") == "failed"
            else "validity",
            "reason": "Mandatory baseline evidence did not support promotion: "
            + _baseline_status_reason(baseline_status),
            "source": "worker_report.baseline_evidence_status",
        }
    ]


def _baseline_status_reason(baseline_status: dict[str, Any]) -> str:
    failures = [
        result
        for result in baseline_status.get("results", [])
        if result.get("status") != "passed"
    ]
    if not failures:
        return "no failing requirement recorded"
    return "; ".join(str(result.get("reason")) for result in failures)
