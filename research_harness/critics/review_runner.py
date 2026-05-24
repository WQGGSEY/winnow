from __future__ import annotations

from typing import Any


def _has_role(node: dict[str, Any], role: str) -> bool:
    for ref in node.get("baseline_refs", []):
        if role in ref.get("roles", []):
            return True
    return False


def _missing_baseline_roles(node: dict[str, Any]) -> list[str]:
    required = {"current_best_known", "naive", "random_or_null"}
    present: set[str] = set()
    for ref in node.get("baseline_refs", []):
        present.update(ref.get("roles", []))
    return sorted(required - present)


def _quant_review(
    worker_report: dict[str, Any],
    node: dict[str, Any],
    scores: dict[str, int],
    objections: list[dict[str, str]],
) -> None:
    metrics = worker_report.get("metrics") or {}
    baselines = worker_report.get("baselines") or {}
    if not isinstance(metrics, dict) or not isinstance(baselines, dict):
        return
    numeric_metrics = {
        k: v for k, v in metrics.items() if isinstance(v, (int, float))
    }
    current_best_candidates = [
        v
        for k, v in baselines.items()
        if isinstance(v, (int, float))
        and ("current_best" in k or k == "current_best_known")
    ]
    if not numeric_metrics or not current_best_candidates:
        return
    main_metric = numeric_metrics[sorted(numeric_metrics.keys())[0]]
    current_best = current_best_candidates[0]
    if current_best <= 0:
        return
    delta = (main_metric - current_best) / current_best
    if 0 < delta < 0.01:
        scores["validity"] = min(scores["validity"], 6)
        objections.append(
            {
                "claim_affected": node["claim_contract"]["claim_under_test"],
                "objection": (
                    f"Improvement over current_best is {delta * 100:.2f}% — "
                    "likely within noise without explicit variance / "
                    "significance reporting."
                ),
                "required_resolution": (
                    "Report variance across seeds and a significance test "
                    "before claiming an improvement."
                ),
            }
        )


def _reproducibility_review(
    worker_report: dict[str, Any],
    node: dict[str, Any],
    scores: dict[str, int],
    objections: list[dict[str, str]],
) -> None:
    artifacts = worker_report.get("artifacts") or []
    metrics_present = any(
        isinstance(a, str) and a.endswith(".json") for a in artifacts
    )
    if not metrics_present:
        scores["reproducibility"] = min(scores["reproducibility"], 5)
        objections.append(
            {
                "claim_affected": node["claim_contract"]["claim_under_test"],
                "objection": (
                    "No machine-readable JSON metrics artifact recorded by "
                    "the worker; reproducibility audit cannot read numbers."
                ),
                "required_resolution": (
                    "Have the experiment write a JSON metrics file under "
                    "the node workspace artifacts/."
                ),
            }
        )


def run_critic_reviews(
    node: dict[str, Any],
    worker_report: dict[str, Any],
    critic_bundle: dict[str, Any],
) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for critic in critic_bundle["applied_critics"]:
        critic_id = critic["critic_id"]
        objections: list[dict[str, str]] = []
        blocking = False
        verdict = "supported"
        scores = {
            "validity": 8,
            "necessity": 7,
            "reproducibility": 7,
            "taste_alignment": 8,
        }

        if worker_report["status"] != "completed":
            blocking = True
            verdict = "blocked_by_operational_issue"
            scores["validity"] = 3
            objections.append(
                {
                    "claim_affected": node["claim_contract"]["claim_under_test"],
                    "objection": f"Worker status is {worker_report['status']}.",
                    "required_resolution": "Rerun with a valid bounded worker report or split the node.",
                }
            )

        if "invariants" in critic_id and not node["claim_contract"]["mandatory_baselines"]:
            blocking = True
            verdict = "confounded_or_not_evaluable"
            scores["validity"] = 2
            objections.append(
                {
                    "claim_affected": node["claim_contract"]["claim_under_test"],
                    "objection": "Mandatory baselines are missing.",
                    "required_resolution": "Add current-best, naive, and random/null baseline plan.",
                }
            )

        if "invariants" in critic_id:
            missing_roles = _missing_baseline_roles(node)
            if missing_roles:
                blocking = True
                verdict = "confounded_or_not_evaluable"
                scores["validity"] = min(scores["validity"], 2)
                objections.append(
                    {
                        "claim_affected": node["claim_contract"]["claim_under_test"],
                        "objection": "Missing baseline roles: " + ", ".join(missing_roles),
                        "required_resolution": "Attach current-best, naive, and random/null baseline dossier roles.",
                    }
                )

        if "necessity" in critic_id and not _has_role(node, "current_best_known"):
            blocking = True
            verdict = "confounded_or_not_evaluable"
            scores["necessity"] = 3
            objections.append(
                {
                    "claim_affected": node["claim_contract"]["claim_under_test"],
                    "objection": "No current-best-known baseline dossier role is attached.",
                    "required_resolution": "Run or refresh a baseline_resolution_node.",
                }
            )

        if "runtime_safety" in critic_id or "nested_agent" in critic_id:
            if node["runtime_profile"]["worker_type"] == "experiment_worker":
                scores["validity"] = min(scores["validity"], 7)

        if "senior_quant" in critic_id:
            _quant_review(worker_report, node, scores, objections)

        if "reproducibility_auditor" in critic_id:
            _reproducibility_review(worker_report, node, scores, objections)

        lesson_candidates: list[str] = []
        if worker_report.get("unexpected_observations"):
            lesson_candidates.append(
                "In agent-harness ports, runtime envelopes are often the necessity claim, not an implementation detail."
            )

        reviews.append(
            {
                "critic_id": critic_id,
                "node_id": node["id"],
                "verdict_candidate": verdict,
                "blocking": blocking,
                "scores": scores,
                "objections": objections,
                "lesson_candidates": lesson_candidates,
                "failure_record_candidate": None,
            }
        )
    return reviews
