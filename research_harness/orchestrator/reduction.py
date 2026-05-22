from __future__ import annotations

from statistics import mean
from typing import Any


class ReductionError(ValueError):
    """Raised when orchestrator reduction lacks required evidence."""


def reduce_node(
    node: dict[str, Any],
    worker_report: dict[str, Any],
    critic_reviews: list[dict[str, Any]],
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
        "child_branch_suggestions": [
            {
                "type": "validity",
                "reason": "Unexpected observation should be checked without letting the worker pursue it.",
                "source": "worker_report.unexpected_observations",
            }
        ]
        if worker_report.get("unexpected_observations")
        else [],
    }
    return reduction
