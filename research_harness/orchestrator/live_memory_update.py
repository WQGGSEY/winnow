from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from research_harness.config import load_lessons
from research_harness.memory.failure_memory import (
    FailureMemoryResult,
    record_failure_candidate,
)
from research_harness.schemas.validator import validate_named_schema


class LiveMemoryUpdateError(ValueError):
    """Raised when live memory update input is invalid."""


def apply_live_memory_update(
    repo_root: Path,
    bundle_path: Path,
    *,
    approve: bool = False,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Promote approved live failure and lesson candidates into memory."""

    repo_root = repo_root.resolve()
    bundle_path = bundle_path.resolve()
    bundle = _load_json(bundle_path)
    validate_named_schema("live_reduction_bundle", bundle)
    lessons_path = repo_root / "lessons.yaml"
    summary_path = (output_path or bundle_path.with_name("live_memory_update_summary.json")).resolve()
    base_summary = {
        "type": "live_memory_update_summary",
        "node_id": bundle["node_id"],
        "bundle_path": str(bundle_path),
        "repo_root": str(repo_root),
        "approval_required": True,
        "approved": bool(approve),
        "failure_memory": None,
        "recorded_lessons": [],
        "lessons_path": str(lessons_path),
        "summary_path": str(summary_path),
        "error": None,
    }
    if not approve:
        return _write_summary(
            summary_path,
            {
                **base_summary,
                "status": "blocked_by_missing_approval",
                "error": "pass approve=True or --approve to write shared memory",
            },
        )

    failure_result = record_failure_candidate(
        repo_root,
        bundle["node"],
        bundle["worker_report"],
        source_artifact=bundle["worker_report_path"],
    )
    lesson_texts = _candidate_lessons(bundle, failure_result)
    recorded_lessons = _append_active_lessons(
        repo_root,
        bundle,
        lesson_texts,
    )
    return _write_summary(
        summary_path,
        {
            **base_summary,
            "status": "applied",
            "failure_memory": _failure_memory_summary(failure_result),
            "recorded_lessons": recorded_lessons,
        },
    )


def _candidate_lessons(
    bundle: dict[str, Any],
    failure_result: FailureMemoryResult | None,
) -> list[str]:
    lessons: list[str] = []
    if failure_result is not None:
        lessons.append(failure_result.lesson)
    lessons.extend(bundle["orchestrator_reduction"].get("accepted_lesson_candidates", []))
    result: list[str] = []
    seen: set[str] = set()
    for lesson in lessons:
        text = _one_line(str(lesson))
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _append_active_lessons(
    repo_root: Path,
    bundle: dict[str, Any],
    lesson_texts: list[str],
) -> list[dict[str, Any]]:
    if not lesson_texts:
        return []
    lessons = load_lessons(repo_root)
    active = lessons.setdefault("active_lessons", [])
    existing_texts = {str(lesson.get("text")) for lesson in active}
    recorded: list[dict[str, Any]] = []
    for text in lesson_texts:
        if text in existing_texts:
            continue
        lesson = {
            "id": _lesson_id(bundle["node_id"], text),
            "text": text,
            "tags": _lesson_tags(bundle),
            "evidence_refs": [bundle["bundle_path"]],
        }
        active.append(lesson)
        existing_texts.add(text)
        recorded.append(lesson)
    if recorded:
        _write_lessons(repo_root / "lessons.yaml", lessons)
        load_lessons(repo_root)
    return recorded


def _lesson_id(node_id: str, text: str) -> str:
    digest = hashlib.sha256(f"{node_id}:{text}".encode("utf-8")).hexdigest()[:10]
    return f"lesson_live_{digest}"


def _lesson_tags(bundle: dict[str, Any]) -> list[str]:
    node = bundle["node"]
    tags = [
        "live_reduction",
        str(node.get("domain") or "unknown_domain"),
        str(node.get("type") or "unknown_type"),
    ]
    tags.extend(str(tag) for tag in node.get("failure_retrieval", {}).get("query_tags", []))
    return _dedupe(tags)


def _write_lessons(path: Path, lessons: dict[str, Any]) -> None:
    lines = ["active_lessons:"]
    for lesson in lessons.get("active_lessons", []):
        lines.append(f"  - id: {lesson['id']}")
        lines.append(f"    text: {json.dumps(str(lesson['text']))}")
        lines.append(f"    tags: {_inline_string_list(lesson.get('tags', []))}")
        lines.append(f"    evidence_refs: {_inline_string_list(lesson.get('evidence_refs', []))}")
    archive = lessons.get("archive", {}) if isinstance(lessons.get("archive"), dict) else {}
    archive_path = str(archive.get("path") or "memory/lessons/archive")
    lines.extend(
        [
            "",
            "archive:",
            f"  path: {json.dumps(archive_path)}",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _inline_string_list(items: list[Any]) -> str:
    return "[" + ", ".join(json.dumps(str(item)) for item in items) + "]"


def _failure_memory_summary(result: FailureMemoryResult | None) -> dict[str, str] | None:
    if result is None:
        return None
    return {
        "record_path": str(result.record_path),
        "index_path": str(result.index_path),
        "lesson": result.lesson,
    }


def _one_line(text: str, max_chars: int = 240) -> str:
    line = " ".join(text.split())
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 3].rstrip() + "..."


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise LiveMemoryUpdateError(f"expected JSON object: {path}")
    return data


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_summary(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    validate_named_schema("live_memory_update_summary", summary)
    _write_json(path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply approved live failure and lesson candidates to memory."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        summary = apply_live_memory_update(
            args.repo_root,
            args.bundle,
            approve=args.approve,
            output_path=args.output,
        )
    except LiveMemoryUpdateError as exc:
        print(
            json.dumps(
                {
                    "type": "live_memory_update_error",
                    "status": "blocked",
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(2) from exc
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
