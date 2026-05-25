"""LLM-driven critic reviews.

Sits next to `critics/review_runner.run_critic_reviews` (the deterministic
rule-based critic). When enabled, each applied critic markdown profile is
sent to the LLM as a system prompt + the node + worker_report as user
content. The LLM produces a structured review that goes through the same
`critic_review.schema.json` validation as the deterministic critic.

If the LLM response is malformed, we fall back to the deterministic reviewer
for that specific critic. The harness never gets stuck because of an LLM hiccup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_harness.critics.review_runner import run_critic_reviews as _deterministic
from research_harness.orchestrator.llm_orchestrator.dialog import DialogEntry
from research_harness.orchestrator.llm_orchestrator.llm_client import LLMClient


_CRITIC_RESPONSE_SCHEMA = """{
  "verdict_candidate": "supported | supported_with_scope_narrowing | contradicted | confounded_or_not_evaluable | taste_rejected_local_branch | blocked_by_operational_issue",
  "blocking": true|false,
  "scores": {"validity": 1-10, "necessity": 1-10, "reproducibility": 1-10, "taste_alignment": 1-10},
  "objections": [{"claim_affected": "...", "objection": "...", "required_resolution": "..."}],
  "lesson_candidates": ["..."]
}"""


def run_critic_reviews_llm(
    node: dict[str, Any],
    worker_report: dict[str, Any],
    critic_bundle: dict[str, Any],
    llm: LLMClient,
    *,
    repo_root: Path,
) -> list[dict[str, Any]]:
    deterministic = _deterministic(node, worker_report, critic_bundle)
    enriched: list[dict[str, Any]] = []
    for det_review, critic in zip(deterministic, critic_bundle["applied_critics"]):
        critic_path = repo_root / critic["path"]
        persona = _load_persona(critic_path)
        prompt = _build_critic_prompt(node, worker_report, persona)
        try:
            response = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system=(
                    f"[intent:critic.review:{critic['critic_id']}] "
                    "You are a research critic following the persona below. "
                    "Be specific and grounded in the worker report. Do not over- "
                    "or under-claim. Persona:\n" + persona
                ),
                json_schema_hint=_CRITIC_RESPONSE_SCHEMA,
            )
        except Exception:
            enriched.append(det_review)
            continue
        review = _coerce_review(response.structured, det_review, critic, node)
        enriched.append(review)
    return enriched


def _load_persona(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "(no persona available)"
    # Strip YAML frontmatter for the system prompt — keep markdown body only.
    if text.startswith("---"):
        end = text.find("---", 3)
        if end >= 0:
            text = text[end + 3 :]
    return text.strip()


def _build_critic_prompt(
    node: dict[str, Any],
    worker_report: dict[str, Any],
    persona: str,
) -> str:
    contract = node["claim_contract"]
    return (
        f"=== Node {node['id']} ({node['type']}, stage={node['stage']}) ===\n"
        f"Claim under test: {contract['claim_under_test']}\n"
        f"Mandatory baselines: {contract['mandatory_baselines']}\n"
        f"Success criteria: {contract['success_criteria']}\n"
        f"Disproof conditions: {contract['disproof_conditions']}\n\n"
        f"=== Worker report ===\n"
        f"Status: {worker_report.get('status')}\n"
        f"Claim verdict candidate: {worker_report.get('claim_verdict_candidate')}\n"
        f"Metrics: {json.dumps(worker_report.get('metrics') or {}, ensure_ascii=False)}\n"
        f"Baselines: {json.dumps(worker_report.get('baselines') or {}, ensure_ascii=False)}\n"
        f"Baseline overall: {(worker_report.get('baseline_evidence_status') or {}).get('overall', 'n/a')}\n"
        f"Disproof hit: {worker_report.get('disproof_conditions_hit') or []}\n"
        f"Unexpected obs: {len(worker_report.get('unexpected_observations') or [])}\n\n"
        "Review this node per the persona at the top of the system prompt. "
        "Produce the structured JSON."
    )


def _coerce_review(
    structured: dict[str, Any] | None,
    deterministic: dict[str, Any],
    critic: dict[str, Any],
    node: dict[str, Any],
) -> dict[str, Any]:
    structured = structured or {}
    verdict = str(structured.get("verdict_candidate") or deterministic["verdict_candidate"])
    blocking = bool(structured.get("blocking", deterministic["blocking"]))
    scores = structured.get("scores") or deterministic["scores"]
    if not isinstance(scores, dict):
        scores = deterministic["scores"]
    # Ensure all 4 axes present.
    for axis in ("validity", "necessity", "reproducibility", "taste_alignment"):
        if axis not in scores or not isinstance(scores[axis], (int, float)):
            scores[axis] = deterministic["scores"].get(axis, 7)
    objections_raw = structured.get("objections")
    objections: list[dict[str, str]] = []
    if isinstance(objections_raw, list):
        for o in objections_raw:
            if not isinstance(o, dict):
                continue
            objections.append(
                {
                    "claim_affected": str(o.get("claim_affected") or node["claim_contract"]["claim_under_test"]),
                    "objection": str(o.get("objection") or ""),
                    "required_resolution": str(o.get("required_resolution") or ""),
                }
            )
    if not objections:
        objections = deterministic["objections"]
    lessons_raw = structured.get("lesson_candidates")
    lessons = (
        [str(item) for item in lessons_raw if isinstance(item, str)]
        if isinstance(lessons_raw, list)
        else deterministic["lesson_candidates"]
    )
    return {
        "critic_id": critic["critic_id"],
        "node_id": node["id"],
        "verdict_candidate": verdict,
        "blocking": blocking,
        "scores": {axis: int(scores[axis]) for axis in ("validity", "necessity", "reproducibility", "taste_alignment")},
        "objections": objections,
        "lesson_candidates": lessons,
        "failure_record_candidate": deterministic.get("failure_record_candidate"),
    }
