from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_harness.config import load_yaml, split_frontmatter
from research_harness.workers.workspace import WorkspaceGuardError, ensure_path_inside


class FailureRetrievalError(ValueError):
    """Raised when failure memory cannot be safely retrieved."""


@dataclass(frozen=True)
class FailureSummary:
    file: str
    category: str
    tags: list[str]
    lesson: str
    reason: str
    score: int
    explicit: bool = False


def retrieve_failure_summaries(
    repo_root: Path,
    *,
    query_tags: list[str],
    selected_fail_files: list[str],
    top_k: int = 5,
) -> list[FailureSummary]:
    """Select a small set of relevant failure summaries.

    The index controls candidate filenames. Explicit `selected_fail_files` are
    always included after path validation; the remaining slots are filled by
    tag/text overlap against one-line lessons and reasons.
    """

    if top_k < 1:
        return []
    base_dir = repo_root / "memory" / "failures"
    index = load_yaml(base_dir / "index.yaml")
    indexed_files = _indexed_files(index)
    explicit_files = _dedupe(selected_fail_files)
    unknown_explicit = sorted(set(explicit_files) - set(indexed_files))
    if unknown_explicit:
        raise FailureRetrievalError(
            "selected_fail_files must be listed in failure index: "
            + ", ".join(unknown_explicit)
        )

    query_terms = {str(tag).lower() for tag in query_tags if str(tag).strip()}
    explicit_summaries = [
        _read_failure_summary(base_dir, relative_path, query_terms, explicit=True)
        for relative_path in explicit_files[:top_k]
    ]
    remaining = top_k - len(explicit_summaries)
    if remaining <= 0:
        return explicit_summaries

    candidates = []
    explicit_set = set(explicit_files)
    for relative_path in indexed_files:
        if relative_path in explicit_set:
            continue
        summary = _read_failure_summary(base_dir, relative_path, query_terms, explicit=False)
        if summary.score > 0:
            candidates.append(summary)
    candidates.sort(key=lambda item: (-item.score, item.category, item.file))
    return explicit_summaries + candidates[:remaining]


def format_failure_summaries(summaries: list[FailureSummary]) -> str:
    if not summaries:
        return "- selected_fail_files: none\n- relevant_failures: none"
    lines = ["- relevant_failures:"]
    for summary in summaries:
        explicit = "explicit" if summary.explicit else "retrieved"
        tags = ", ".join(summary.tags)
        lines.extend(
            [
                f"  - file: {summary.file}",
                f"    mode: {explicit}",
                f"    category: {summary.category}",
                f"    tags: [{tags}]",
                f"    score: {summary.score}",
                f"    lesson: {summary.lesson}",
                f"    reason: {summary.reason}",
            ]
        )
    return "\n".join(lines)


def _indexed_files(index: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for category, spec in index.get("categories", {}).items():
        for relative_path in spec.get("files", []) or []:
            relative = str(relative_path)
            if not relative.startswith(f"{category}/"):
                raise FailureRetrievalError(
                    f"failure file {relative} must live under category {category}"
                )
            files.append(relative)
    return _dedupe(files)


def _read_failure_summary(
    base_dir: Path,
    relative_path: str,
    query_terms: set[str],
    *,
    explicit: bool,
) -> FailureSummary:
    path = _safe_failure_path(base_dir, relative_path)
    frontmatter, body = split_frontmatter(path)
    tags = [str(tag) for tag in frontmatter.get("tags", [])]
    category = str(frontmatter.get("category") or relative_path.split("/", 1)[0])
    lesson = _one_line(str(frontmatter.get("lesson") or _section(body, "One-Line Lesson")))
    reason = _one_line(_section(body, "Reason"))
    searchable = " ".join(
        [relative_path, category, lesson, reason, " ".join(tags)]
    ).lower()
    score = 999 if explicit else sum(1 for term in query_terms if term in searchable)
    return FailureSummary(
        file=relative_path,
        category=category,
        tags=tags,
        lesson=lesson or "No lesson recorded.",
        reason=reason or "No reason recorded.",
        score=score,
        explicit=explicit,
    )


def _safe_failure_path(base_dir: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise FailureRetrievalError(f"unsafe failure path: {relative_path}")
    resolved = (base_dir / path).resolve()
    try:
        ensure_path_inside(resolved, base_dir.resolve(), "failure_file")
    except WorkspaceGuardError as exc:
        raise FailureRetrievalError(str(exc)) from exc
    if not resolved.exists():
        raise FailureRetrievalError(f"failure file missing: {relative_path}")
    return resolved


def _section(body: str, heading: str) -> str:
    marker = f"## {heading}"
    if marker not in body:
        return ""
    section = body.split(marker, 1)[1]
    if "## " in section:
        section = section.split("## ", 1)[0]
    return section.strip()


def _one_line(text: str, max_chars: int = 240) -> str:
    line = " ".join(text.split())
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 3].rstrip() + "..."


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        text = str(item)
        if text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped
