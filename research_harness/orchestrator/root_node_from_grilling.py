from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_harness.orchestrator.validation import (
    ValidationError,
    validate_node_invariants,
)
from research_harness.schemas.validator import validate_named_schema


PLACEHOLDER_BASELINE_DOSSIER_ID = "bd_pending_market_research"


def build_root_node_from_grilling(
    grilling_session: dict[str, Any],
    *,
    baseline_dossier_id: str | None = None,
    candidate_ids: list[str] | None = None,
    node_id_suffix: str = "root",
    runtime_turn_budget: int = 6,
    extra_failure_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministically convert a grilling_session into a claim-bearing root node.

    market research is expected to replace the placeholder baseline_dossier_id
    with a real dossier id once paper search and dossier candidate generation
    finish. The node is therefore safe to schedule only after that pass; the
    placeholder allows schema validation to pass at this stage.
    """

    validate_named_schema("grilling_session", grilling_session)
    extracted = grilling_session["extracted"]

    node_id = _build_node_id(extracted["root_goal_id"], node_id_suffix)
    failure_tags = _collect_failure_tags(extracted, extra_failure_tags)
    dossier_id = baseline_dossier_id or PLACEHOLDER_BASELINE_DOSSIER_ID
    candidate_ids = list(candidate_ids or [])

    node = {
        "id": node_id,
        "type": extracted["node_type"],
        "status": "ready",
        "domain": extracted["domain"],
        "stage": "promotion",
        "parent": None,
        "lineage": {
            "root_goal_id": extracted["root_goal_id"],
            "covers_goal_facets": list(extracted.get("goal_facets") or []),
            "inherited_assumptions": [
                "Outer orchestrator owns search policy.",
                "Workers are bounded tools.",
            ],
            "introduced_assumptions": [
                f"Grilling session {grilling_session['session_id']} produced this root node.",
            ],
            "taste_constraints_applied": list(extracted.get("taste_constraints") or []),
        },
        "claim_contract": {
            "claim_under_test": extracted["claim_under_test"],
            "mandatory_baselines": list(extracted["mandatory_baselines"]),
            "success_criteria": list(extracted["success_criteria"]),
            "disproof_conditions": list(extracted["disproof_conditions"]),
        },
        "baseline_refs": [
            {
                "baseline_dossier_id": dossier_id,
                "candidate_ids": candidate_ids,
                "roles": ["current_best_known", "naive", "random_or_null"],
            }
        ],
        "runtime_profile": {
            "worker_type": "experiment_worker",
            "timeout_policy": "task_class_dependent",
            "turn_budget": int(runtime_turn_budget),
        },
        "failure_retrieval": {
            "query_tags": failure_tags,
            "selected_fail_files": [],
        },
        "outputs": {
            "artifacts": [],
            "verdict": None,
        },
    }
    validate_node_invariants(node)
    return node


def write_root_node(
    grilling_session_path: Path,
    output_path: Path,
    *,
    baseline_dossier_id: str | None = None,
    candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    grilling_session = json.loads(grilling_session_path.read_text(encoding="utf-8"))
    node = build_root_node_from_grilling(
        grilling_session,
        baseline_dossier_id=baseline_dossier_id,
        candidate_ids=candidate_ids,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(node, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return node


def has_placeholder_baseline(node: dict[str, Any]) -> bool:
    for ref in node.get("baseline_refs", []):
        if ref.get("baseline_dossier_id") == PLACEHOLDER_BASELINE_DOSSIER_ID:
            return True
    return False


def attach_market_research_dossier(
    node: dict[str, Any],
    *,
    baseline_dossier_id: str,
    candidate_ids: list[str],
    baseline_analysis_md_path: str | None = None,
) -> dict[str, Any]:
    """Replace the placeholder dossier with the market-research-produced one.

    If baseline_analysis_md_path is given, also records it under
    node.lineage.inherited_assumptions so downstream orchestrator / template
    code can find the analysis brief.
    """

    if not baseline_dossier_id:
        raise ValidationError("baseline_dossier_id must be a non-empty string")
    refs = node.get("baseline_refs") or []
    if not refs:
        raise ValidationError("node is missing baseline_refs")
    new_refs = []
    replaced = False
    for ref in refs:
        if (
            not replaced
            and ref.get("baseline_dossier_id") == PLACEHOLDER_BASELINE_DOSSIER_ID
        ):
            new_refs.append(
                {
                    "baseline_dossier_id": baseline_dossier_id,
                    "candidate_ids": list(candidate_ids),
                    "roles": list(ref.get("roles") or []),
                }
            )
            replaced = True
        else:
            new_refs.append(ref)
    if not replaced:
        raise ValidationError(
            "node has no placeholder baseline_dossier_id to attach market research to"
        )
    node = dict(node)
    node["baseline_refs"] = new_refs
    if baseline_analysis_md_path:
        lineage = dict(node.get("lineage") or {})
        inherited = list(lineage.get("inherited_assumptions") or [])
        note = f"market_research_baseline_analysis_md: {baseline_analysis_md_path}"
        if note not in inherited:
            inherited.append(note)
        lineage["inherited_assumptions"] = inherited
        node["lineage"] = lineage
    validate_node_invariants(node)
    return node


def _build_node_id(root_goal_id: str, suffix: str) -> str:
    base = root_goal_id
    if base.startswith("rg_"):
        base = base[3:]
    return f"n_{base}_{suffix}"


def _collect_failure_tags(
    extracted: dict[str, Any],
    extras: list[str] | None,
) -> list[str]:
    tags = [extracted["domain"], extracted["node_type"]]
    tags.extend(str(item) for item in (extracted.get("goal_facets") or []))
    tags.extend(str(item) for item in (extras or []))
    seen: set[str] = set()
    deduped: list[str] = []
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            deduped.append(tag)
    return deduped
