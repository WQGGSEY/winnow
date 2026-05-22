from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from research_harness.config import load_yaml
from research_harness.schemas.validator import validate_named_schema


class BaselineDossierError(ValueError):
    """Raised when a baseline dossier is incomplete or unsafe to consume."""


REQUIRED_DECISIONS = {
    "selected",
    "selected_as_naive",
    "selected_as_random_or_null",
}


def dossier_path(repo_root: Path, dossier_id: str) -> Path:
    return repo_root / "memory" / "baseline_dossiers" / f"{dossier_id}.yaml"


def load_baseline_dossier(repo_root: Path, dossier_id: str) -> dict[str, Any]:
    path = dossier_path(repo_root, dossier_id)
    if not path.exists():
        raise BaselineDossierError(f"baseline dossier not found: {dossier_id}")
    dossier = load_yaml(path)
    if not isinstance(dossier, dict):
        raise BaselineDossierError("baseline dossier must be a map")
    validate_baseline_dossier(repo_root, dossier)
    return dossier


def validate_baseline_dossier(repo_root: Path, dossier: dict[str, Any]) -> None:
    validate_named_schema("baseline_dossier", dossier)

    try:
        date.fromisoformat(str(dossier["created_at"]))
    except ValueError as exc:
        raise BaselineDossierError("created_at must be ISO date YYYY-MM-DD") from exc

    base_dir = repo_root / "memory" / "baseline_dossiers"
    candidate_ids = {candidate["id"] for candidate in dossier["candidates_index"]}
    selected_id = dossier["selected"]["candidate_id"]
    if selected_id not in candidate_ids:
        raise BaselineDossierError(f"selected candidate_id not in candidates_index: {selected_id}")

    decisions = {candidate["decision"] for candidate in dossier["candidates_index"]}
    missing_decisions = sorted(REQUIRED_DECISIONS - decisions)
    if missing_decisions:
        raise BaselineDossierError(
            "baseline dossier missing required candidate decisions: "
            + ", ".join(missing_decisions)
        )

    for candidate in dossier["candidates_index"]:
        detail_file = candidate["detail_file"]
        if Path(detail_file).is_absolute() or ".." in Path(detail_file).parts:
            raise BaselineDossierError(f"candidate detail_file must be relative: {detail_file}")
        if not (base_dir / detail_file).exists():
            raise BaselineDossierError(f"candidate detail_file missing: {detail_file}")

    for source in dossier["source_index"]:
        if not str(source["url"]).startswith(("https://", "http://")):
            raise BaselineDossierError(f"source url must be http(s): {source['id']}")
        try:
            date.fromisoformat(str(source["accessed_at"]))
        except ValueError as exc:
            raise BaselineDossierError(
                f"source accessed_at must be ISO date: {source['id']}"
            ) from exc


def build_baseline_resolution_report(
    repo_root: Path,
    dossier_id: str,
    output_path: Path,
) -> dict[str, Any]:
    dossier = load_baseline_dossier(repo_root, dossier_id)
    candidates_by_decision = {
        candidate["decision"]: candidate["id"] for candidate in dossier["candidates_index"]
    }
    report = {
        "dossier_id": dossier["id"],
        "query": dossier["query"],
        "selected_current_best_known": dossier["selected"]["candidate_id"],
        "naive": candidates_by_decision["selected_as_naive"],
        "random_or_null": candidates_by_decision["selected_as_random_or_null"],
        "evidence_tags": dossier["selected"]["evidence_tags"],
        "risk_tags": dossier["selected"]["risk_tags"],
        "source_count": len(dossier["source_index"]),
        "refresh_required_before": dossier["refresh_policy"]["required_before"],
        "webfetch_executed": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Baseline Resolution Dry Run",
        "",
        f"- Dossier: {report['dossier_id']}",
        f"- Current best-known: {report['selected_current_best_known']}",
        f"- Naive: {report['naive']}",
        f"- Random/null: {report['random_or_null']}",
        f"- Webfetch executed: {report['webfetch_executed']}",
        "",
        "## Evidence Tags",
    ]
    lines.extend(f"- {tag}" for tag in report["evidence_tags"])
    lines.extend(["", "## Risk Tags"])
    lines.extend(f"- {tag}" for tag in report["risk_tags"])
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report

