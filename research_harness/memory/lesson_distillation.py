from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from research_harness.config import (
    load_lessons,
    load_settings,
    resolve_agent_budget,
    resolve_agent_model,
)
from research_harness.schemas.validator import validate_named_schema


BILLING_ACK_ENV = "RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE"
BILLING_ACK_VALUE = "subscription_ack"
EXECUTION_ACK_ENV = "RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE"
EXECUTION_ACK_VALUE = "live_smoke_ack"
DEFAULT_DISTILL_TIMEOUT_SECONDS = 180


CommandRunner = Callable[..., subprocess.CompletedProcess]


class LessonDistillationError(ValueError):
    """Raised when distillation cannot proceed safely."""


@dataclass
class _DistillCall:
    distilled: list[dict[str, Any]]
    cost_usd: float
    input_tokens: int
    output_tokens: int


def compute_active_lessons_bytes(lessons: dict[str, Any]) -> int:
    total = 0
    for lesson in lessons.get("active_lessons", []):
        text = str(lesson.get("text") or "")
        total += len(text.encode("utf-8"))
    return total


def should_trigger_distillation(
    lessons: dict[str, Any], settings: dict[str, Any]
) -> tuple[bool, int, int]:
    """Return (triggered, current_bytes, threshold_bytes)."""

    threshold = int(
        settings.get("memory", {})
        .get("lessons", {})
        .get("distillation_token_threshold_bytes", 2000)
    )
    current = compute_active_lessons_bytes(lessons)
    return (current > threshold, current, threshold)


def run_lesson_distillation(
    repo_root: Path,
    *,
    summary_dir: Path | None = None,
    approve: bool = False,
    claude_path: str | None = None,
    billing_ack: bool | None = None,
    execution_ack: bool | None = None,
    timeout_seconds: int = DEFAULT_DISTILL_TIMEOUT_SECONDS,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Periodic deterministic-triggered lesson distillation with approval gate."""

    repo_root = repo_root.resolve()
    settings = load_settings(repo_root)
    lessons = load_lessons(repo_root)
    lessons_path = repo_root / "lessons.yaml"
    triggered, current_bytes, threshold = should_trigger_distillation(lessons, settings)
    summary_dir = (summary_dir or repo_root / "runs" / "lesson_distillation").resolve()
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / f"distillation_{uuid.uuid4().hex[:8]}.json"

    live_backend = (
        settings.get("runtime", {})
        .get("worker_backends", {})
        .get("claude_code_live", {})
    )
    model = resolve_agent_model(settings, "lesson_distillation_agent")
    max_budget = resolve_agent_budget(settings, "lesson_distillation_agent")

    base = {
        "type": "lesson_distillation_summary",
        "triggered": triggered,
        "threshold_bytes": threshold,
        "current_bytes": current_bytes,
        "lesson_count_before": len(lessons.get("active_lessons", [])),
        "lesson_count_after": len(lessons.get("active_lessons", [])),
        "approval_required": True,
        "approved": bool(approve),
        "summary_path": str(summary_path),
        "lessons_path": str(lessons_path),
        "archive_path": None,
        "distilled_lessons": [],
        "model": model,
        "usage": {"cost_usd": None, "input_tokens": 0, "output_tokens": 0},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }

    if not triggered:
        return _write_summary(summary_path, {**base, "status": "not_triggered"})

    if not _billing_ack_ok(billing_ack):
        return _write_summary(
            summary_path,
            {
                **base,
                "status": "triggered_blocked_by_gate",
                "error": f"set {BILLING_ACK_ENV}={BILLING_ACK_VALUE} to enable live distillation",
            },
        )
    if not _execution_ack_ok(execution_ack):
        return _write_summary(
            summary_path,
            {
                **base,
                "status": "triggered_blocked_by_execution_ack",
                "error": f"set {EXECUTION_ACK_ENV}={EXECUTION_ACK_VALUE} to actually invoke Claude for distillation",
            },
        )

    detected_claude = claude_path or shutil.which("claude") or "claude"
    runner = command_runner or subprocess.run

    try:
        call = _call_distillation_claude(
            runner=runner,
            claude_path=detected_claude,
            model=model,
            max_budget=max_budget,
            lessons=lessons,
            timeout_seconds=timeout_seconds,
        )
    except LessonDistillationError as exc:
        return _write_summary(
            summary_path,
            {**base, "status": "failed", "error": str(exc)},
        )

    distilled = _normalize_distilled(call.distilled, original=lessons.get("active_lessons", []))
    base["distilled_lessons"] = distilled
    base["usage"] = {
        "cost_usd": call.cost_usd,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
    }
    if not approve:
        return _write_summary(
            summary_path,
            {
                **base,
                "status": "triggered_blocked_by_approval",
                "error": "pass approve=True or --approve to write lessons.yaml",
            },
        )

    archive_path = _archive_existing_lessons(repo_root, lessons_path)
    new_lessons_doc = {
        "active_lessons": distilled,
        "archive": lessons.get("archive") or {"path": "memory/lessons/archive"},
    }
    _write_lessons_yaml(lessons_path, new_lessons_doc)
    # confirm load round-trip
    reloaded = load_lessons(repo_root)
    return _write_summary(
        summary_path,
        {
            **base,
            "status": "applied",
            "archive_path": str(archive_path),
            "lesson_count_after": len(reloaded.get("active_lessons", [])),
        },
    )


def _call_distillation_claude(
    *,
    runner: CommandRunner,
    claude_path: str,
    model: str,
    max_budget: str,
    lessons: dict[str, Any],
    timeout_seconds: int,
) -> _DistillCall:
    user_prompt = _distillation_user_prompt(lessons)
    cmd = [
        claude_path,
        "-p",
        "--model",
        model,
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--system-prompt",
        _distillation_system_prompt(),
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--no-session-persistence",
        "--max-budget-usd",
        max_budget,
    ]
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        completed = runner(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise LessonDistillationError(
            f"claude CLI timed out after {timeout_seconds}s during distillation"
        ) from exc
    if completed.returncode not in (0, None):
        raise LessonDistillationError(
            f"claude CLI exited with code {completed.returncode}: "
            f"{(completed.stderr or '').strip()[:200]}"
        )
    try:
        cli_result = json.loads(completed.stdout or "")
    except json.JSONDecodeError as exc:
        raise LessonDistillationError(
            f"claude CLI did not return JSON: {(completed.stdout or '')[:200]!r}"
        ) from exc
    if not isinstance(cli_result, dict) or cli_result.get("type") != "result":
        raise LessonDistillationError(
            f"claude CLI returned unexpected payload: {(completed.stdout or '')[:200]!r}"
        )
    if cli_result.get("is_error"):
        raise LessonDistillationError(
            f"claude CLI reported error subtype {cli_result.get('subtype')!r}"
        )
    inner = cli_result.get("result")
    if not isinstance(inner, str) or not inner.strip():
        raise LessonDistillationError("claude CLI returned empty assistant text")
    payload_text = _strip_fence(inner.strip())
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise LessonDistillationError(
            f"distillation output was not parseable JSON: {payload_text[:200]!r}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("distilled_lessons"), list):
        raise LessonDistillationError("distillation output must include distilled_lessons array")
    if not payload["distilled_lessons"]:
        raise LessonDistillationError("distillation produced zero lessons")
    usage = cli_result.get("usage") if isinstance(cli_result.get("usage"), dict) else {}
    return _DistillCall(
        distilled=payload["distilled_lessons"],
        cost_usd=float(cli_result.get("total_cost_usd") or 0.0),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
    )


def _distillation_system_prompt() -> str:
    return (
        "You are a research-memory distillation agent. The user gives you the "
        "current set of one-line lessons. Your job is to compress them: merge "
        "lessons that share a tag or principle, deduplicate near-duplicates, "
        "and keep every retained lesson to a single line of <=240 characters. "
        "Preserve provenance: if you merge lesson_a and lesson_b, list both in "
        "merges. Output ONE JSON object only, no prose, no markdown. Shape: "
        '{"distilled_lessons":[{"id":"lesson_xxx","text":"...","tags":["..."],'
        '"evidence_refs":["..."],"merges":["lesson_001","lesson_002"]}]}.'
    )


def _distillation_user_prompt(lessons: dict[str, Any]) -> str:
    payload = {
        "active_lessons": lessons.get("active_lessons", []),
    }
    return (
        "## Current Lessons\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        "## Task\n"
        "Compress duplicates and adjacent principles. Keep <=20 lessons after "
        "distillation. Preserve tags and evidence_refs. Output one JSON object."
    )


def _normalize_distilled(
    distilled: list[dict[str, Any]],
    *,
    original: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for entry in distilled:
        text = " ".join(str(entry.get("text") or "").split())
        if not text:
            continue
        if "\n" in text:
            text = text.replace("\n", " ")
        if len(text) > 240:
            text = text[:237] + "..."
        cid = str(entry.get("id") or "")
        if not cid or cid in seen_ids:
            cid = "lesson_distilled_" + uuid.uuid4().hex[:8]
        seen_ids.add(cid)
        tags = entry.get("tags") or []
        evidence = entry.get("evidence_refs") or []
        merges = entry.get("merges") or []
        normalized.append(
            {
                "id": cid,
                "text": text,
                "tags": [str(t) for t in tags],
                "evidence_refs": [str(r) for r in evidence],
                "merges": [str(m) for m in merges],
            }
        )
    if not normalized:
        raise LessonDistillationError("normalized distilled lessons came out empty")
    return normalized


def _archive_existing_lessons(repo_root: Path, lessons_path: Path) -> Path:
    archive_dir = repo_root / "memory" / "lessons" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = archive_dir / f"lessons_{timestamp}.yaml"
    shutil.copy2(lessons_path, archive_path)
    return archive_path


def _write_lessons_yaml(path: Path, lessons: dict[str, Any]) -> None:
    lines = ["active_lessons:"]
    for lesson in lessons.get("active_lessons", []):
        lines.append(f"  - id: {lesson['id']}")
        lines.append(f"    text: {json.dumps(str(lesson['text']))}")
        lines.append(f"    tags: {_inline_string_list(lesson.get('tags') or [])}")
        lines.append(
            f"    evidence_refs: {_inline_string_list(lesson.get('evidence_refs') or [])}"
        )
    archive = lessons.get("archive") or {"path": "memory/lessons/archive"}
    lines.extend(
        [
            "",
            "archive:",
            f"  path: {json.dumps(str(archive.get('path') or 'memory/lessons/archive'))}",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _inline_string_list(items: list[Any]) -> str:
    return "[" + ", ".join(json.dumps(str(item)) for item in items) + "]"


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def _billing_ack_ok(billing_ack: bool | None) -> bool:
    if billing_ack is not None:
        return bool(billing_ack)
    return os.environ.get(BILLING_ACK_ENV) == BILLING_ACK_VALUE


def _execution_ack_ok(execution_ack: bool | None) -> bool:
    if execution_ack is not None:
        return bool(execution_ack)
    return os.environ.get(EXECUTION_ACK_ENV) == EXECUTION_ACK_VALUE


def _write_summary(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    validate_named_schema("lesson_distillation_summary", summary)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary
