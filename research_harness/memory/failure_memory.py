from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_harness.config import load_yaml
from research_harness.workers.workspace import WorkspaceGuardError, ensure_path_inside


class FailureMemoryError(ValueError):
    """Raised when a failure candidate cannot be promoted into shared memory."""


@dataclass(frozen=True)
class FailureMemoryResult:
    record_path: Path
    index_path: Path
    lesson: str


def record_failure_candidate(
    repo_root: Path,
    node: dict[str, Any],
    worker_report: dict[str, Any],
    source_artifact: str | None = None,
) -> FailureMemoryResult | None:
    """Persist an orchestrator-approved failure candidate.

    Workers may propose `failure_record_candidate`, but only this memory layer
    writes shared failure documents. The generated lesson is intentionally one
    line so it can be included in future prompts without large token growth.
    """

    candidate = worker_report.get("failure_record_candidate")
    if not candidate:
        return None
    if not isinstance(candidate, dict):
        raise FailureMemoryError("failure_record_candidate must be an object")

    index_path = repo_root / "memory" / "failures" / "index.yaml"
    failure_index = load_yaml(index_path)
    categories = failure_index.get("categories", {})
    category = str(candidate.get("category") or "")
    if category not in categories:
        raise FailureMemoryError(f"unknown failure category: {category}")

    tags = [str(tag) for tag in candidate.get("tags", [])]
    reason = _one_line(
        str(candidate.get("reason") or _first_observation(worker_report) or "No reason provided.")
    )
    lesson = _distill_lesson(node, category, reason)
    record_id = _record_id(node["id"], category, reason, tags)
    relative_path = f"{category}/{record_id}.md"
    record_path = repo_root / "memory" / "failures" / relative_path
    ensure_path_inside(record_path, repo_root / "memory" / "failures", "failure_record")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        _render_failure_record(
            record_id=record_id,
            node=node,
            worker_report=worker_report,
            category=category,
            tags=tags,
            reason=reason,
            lesson=lesson,
            source_artifact=source_artifact,
        ),
        encoding="utf-8",
    )

    files = categories[category].setdefault("files", [])
    if relative_path not in files:
        files.append(relative_path)
    _write_failure_index(index_path, failure_index)
    return FailureMemoryResult(
        record_path=record_path,
        index_path=index_path,
        lesson=lesson,
    )


def _record_id(node_id: str, category: str, reason: str, tags: list[str]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "node_id": node_id,
                "category": category,
                "reason": reason,
                "tags": tags,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:10]
    return f"{node_id}__{category}__{digest}"


def _distill_lesson(node: dict[str, Any], category: str, reason: str) -> str:
    domain = node.get("domain") or "this regime"
    lesson = (
        f"In {domain}, treat {category} as non-promotable until the orchestrator "
        f"addresses: {reason}"
    )
    return _one_line(lesson, max_chars=240)


def _first_observation(worker_report: dict[str, Any]) -> str | None:
    observations = worker_report.get("unexpected_observations") or []
    if not observations:
        return None
    first = observations[0]
    if not isinstance(first, dict):
        return None
    return str(first.get("evidence") or first.get("observation") or "")


def _one_line(text: str, max_chars: int = 240) -> str:
    line = " ".join(text.split())
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 3].rstrip() + "..."


def _render_failure_record(
    *,
    record_id: str,
    node: dict[str, Any],
    worker_report: dict[str, Any],
    category: str,
    tags: list[str],
    reason: str,
    lesson: str,
    source_artifact: str | None,
) -> str:
    tags_json = json.dumps(tags, ensure_ascii=True)
    source = source_artifact or "unspecified"
    claim = node.get("claim_contract", {}).get("claim_under_test", "")
    return (
        "---\n"
        f"id: {record_id}\n"
        f"category: {category}\n"
        f"tags: {tags_json}\n"
        f"node_id: {node.get('id')}\n"
        f"worker_status: {worker_report.get('status')}\n"
        f"claim_verdict_candidate: {worker_report.get('claim_verdict_candidate')}\n"
        f"source_artifact: {source}\n"
        f"lesson: \"{lesson}\"\n"
        "---\n"
        f"# Failure Record: {record_id}\n\n"
        "## Claim\n\n"
        f"{claim}\n\n"
        "## Reason\n\n"
        f"{reason}\n\n"
        "## One-Line Lesson\n\n"
        f"{lesson}\n"
    )


def _write_failure_index(index_path: Path, failure_index: dict[str, Any]) -> None:
    lines = ["categories:"]
    for category, spec in failure_index.get("categories", {}).items():
        lines.append(f"  {category}:")
        description = str(spec.get("description", "")).replace('"', '\\"')
        lines.append(f"    description: \"{description}\"")
        files = spec.get("files", []) or []
        if files:
            lines.append("    files:")
            for file_path in files:
                lines.append(f"      - {json.dumps(str(file_path))}")
        else:
            lines.append("    files: []")
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Promote a worker failure candidate into shared failure memory."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--worker-report", type=Path, required=True)
    parser.add_argument("--source-artifact")
    args = parser.parse_args()

    node = json.loads(args.node.read_text(encoding="utf-8"))
    worker_report = json.loads(args.worker_report.read_text(encoding="utf-8"))
    result = record_failure_candidate(
        args.repo_root,
        node,
        worker_report,
        source_artifact=args.source_artifact,
    )
    if result is None:
        summary = {"recorded": False}
    else:
        summary = {
            "recorded": True,
            "record_path": str(result.record_path),
            "index_path": str(result.index_path),
            "lesson": result.lesson,
        }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
