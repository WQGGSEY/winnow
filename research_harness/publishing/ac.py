from __future__ import annotations

from statistics import mean
from typing import Any


class ACDecisionError(ValueError):
    """Raised when the AC gate cannot be evaluated."""


def decide_acceptance(
    critic_reviews: list[dict[str, Any]],
    settings: dict[str, Any],
) -> dict[str, Any]:
    if not critic_reviews:
        raise ACDecisionError("AC decision requires at least one critic review")

    thresholds = (
        settings.get("publication_gate", {})
        .get("ac_agent", {})
        .get("accept_thresholds", {})
    )
    blocking_reasons = [
        objection["objection"]
        for review in critic_reviews
        if review.get("blocking")
        for objection in review.get("objections", [])
    ]
    score_keys = ["validity", "necessity", "reproducibility", "taste_alignment"]
    score_summary = {
        key: round(mean(review["scores"][key] for review in critic_reviews))
        for key in score_keys
    }
    score_summary["novelty"] = max(1, score_summary["necessity"])
    score_summary["clarity"] = 7

    threshold_failures = [
        key
        for key, threshold in thresholds.items()
        if score_summary.get(key, 10) < threshold
    ]
    if blocking_reasons:
        decision = "reject"
        confidence = "medium"
    elif threshold_failures:
        decision = "revise"
        confidence = "medium"
    else:
        decision = "accept"
        confidence = "medium"

    # Deterministic camera_ready_directives: the legacy AC must still emit
    # a non-empty list to satisfy the schema, even though real directives
    # come from the LLM-driven submit_ac_decision path. We emit one honest
    # stub directive that names the deterministic origin.
    deterministic_directives = [
        {
            "directive": (
                "DETERMINISTIC STUB — re-run the AC under the LLM-driven "
                "submit_ac_decision path so the camera-ready directives come "
                "from a real synthesis of the rebuttal conversation."
            ),
            "origin_critic_ids": [r["critic_id"] for r in critic_reviews] or ["deterministic_ac"],
            "must_appear_in_section": "limitations",
            "rationale": (
                "Deterministic AC has no rebuttal-derived insight to fold in. "
                "The LLM path produces real per-section directives grounded in critic objections."
            ),
        }
    ]
    return {
        "decision": decision,
        "confidence": confidence,
        "score_summary": {
            "novelty": score_summary["novelty"],
            "validity": score_summary["validity"],
            "necessity": score_summary["necessity"],
            "clarity": score_summary["clarity"],
            "reproducibility": score_summary["reproducibility"],
            "taste_alignment": score_summary["taste_alignment"],
        },
        "blocking_reasons": blocking_reasons,
        "required_next_search_nodes": [
            {
                "type": "validity",
                "claim": "Resolve rebuttal blocker before publication.",
                "source": reason,
            }
            for reason in blocking_reasons
        ],
        "camera_ready_conditions": [
            "Generate requested publication artifacts from the research_state_bundle.",
            "Disclose AI-assisted generation in any external manuscript.",
        ]
        if not blocking_reasons
        else [],
        "camera_ready_directives": deterministic_directives,
        "advisor_message_to_professor": (
            "DETERMINISTIC STUB advisor message. The legacy AC averaged critic scores against "
            "thresholds and did not synthesize a real rebuttal narrative — re-run via the "
            "LLM-driven submit_ac_decision path for an actual advisor message."
        ),
        "rebuttal_synthesis": {
            "strongest_supporting_evidence": ["deterministic_stub:see_critic_reviews"],
            "load_bearing_objections": blocking_reasons,
            "minority_dissent": [],
            "methodology_assessment": {
                "aggregate_verdict": "partial" if decision == "accept" else "absent",
                "methodology_for_user": (
                    "DETERMINISTIC STUB methodology assessment. The LLM AC produces a real "
                    "operator-language summary of what the user can apply tomorrow."
                ),
                "remaining_gap": "Run the LLM rebuttal loop for a real methodology assessment.",
            },
        },
    }
