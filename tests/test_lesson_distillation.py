from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from research_harness.config import load_lessons
from research_harness.memory.lesson_distillation import (
    compute_active_lessons_bytes,
    run_lesson_distillation,
    should_trigger_distillation,
)
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _stage_repo(tmp: Path) -> Path:
    """Copy the minimum tree we need to mutate lessons without touching real repo."""

    staged = tmp / "repo"
    staged.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "lessons.yaml", "research_profile.md"):
        shutil.copy2(REPO_ROOT / name, staged / name)
    for subdir in ("configs", "research_harness", "critics"):
        shutil.copytree(REPO_ROOT / subdir, staged / subdir, dirs_exist_ok=True)
    return staged


def _make_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=""
    )


def _wrap_assistant_text(text: str, cost: float = 0.002) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "result": text,
            "total_cost_usd": cost,
            "usage": {"input_tokens": 800, "output_tokens": 120},
        }
    )


class FakeClaudeRunner:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict] = []

    def __call__(self, cmd, *, input, capture_output, text, timeout, check, env):
        self.calls.append({"cmd": cmd, "input": input, "env_keys": list(env.keys())})
        return _make_completed(self.response)


def _stuff_lessons(repo: Path, total_bytes_target: int) -> None:
    """Bloat lessons.yaml until it crosses the threshold."""

    lessons_path = repo / "lessons.yaml"
    existing = load_lessons(repo)
    lessons = list(existing.get("active_lessons", []))
    base_text = (
        "Synthetic lesson padding to trigger distillation deterministically and "
        "exceed the byte threshold without changing real lessons. "
    )
    counter = 0
    while compute_active_lessons_bytes({"active_lessons": lessons}) < total_bytes_target:
        counter += 1
        lessons.append(
            {
                "id": f"lesson_pad_{counter}",
                "text": (base_text * 2).strip()[:230],
                "tags": ["synthetic"],
                "evidence_refs": [],
            }
        )
    lines = ["active_lessons:"]
    for lesson in lessons:
        lines.append(f"  - id: {lesson['id']}")
        lines.append(f"    text: {json.dumps(str(lesson['text']))}")
        lines.append(f"    tags: {json.dumps(lesson.get('tags', []))}")
        lines.append(f"    evidence_refs: {json.dumps(lesson.get('evidence_refs', []))}")
    archive = existing.get("archive") or {"path": "memory/lessons/archive"}
    lines.extend(["", "archive:", f"  path: {json.dumps(archive.get('path'))}"])
    lessons_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class LessonDistillationTriggerTests(unittest.TestCase):
    def test_below_threshold_does_not_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _stage_repo(Path(tmp))
            summary = run_lesson_distillation(repo, approve=False, billing_ack=True, execution_ack=True)
            self.assertEqual(summary["status"], "not_triggered")
            self.assertFalse(summary["triggered"])
            validate_named_schema("lesson_distillation_summary", summary)


class LessonDistillationLiveTests(unittest.TestCase):
    def _prepare(self, tmp: Path) -> Path:
        repo = _stage_repo(tmp)
        _stuff_lessons(repo, total_bytes_target=2500)
        return repo

    def test_above_threshold_blocked_by_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._prepare(Path(tmp))
            summary = run_lesson_distillation(repo, approve=False, billing_ack=False)
            self.assertEqual(summary["status"], "triggered_blocked_by_gate")
            self.assertIn("RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE", summary["error"])

    def test_above_threshold_blocked_by_execution_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._prepare(Path(tmp))
            summary = run_lesson_distillation(
                repo, approve=False, billing_ack=True, execution_ack=False
            )
            self.assertEqual(summary["status"], "triggered_blocked_by_execution_ack")

    def test_above_threshold_blocked_by_missing_approval_even_after_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._prepare(Path(tmp))
            distilled_payload = {
                "distilled_lessons": [
                    {
                        "id": "lesson_merged_1",
                        "text": "Merged lesson covering synthetic padding lessons.",
                        "tags": ["synthetic"],
                        "evidence_refs": [],
                        "merges": ["lesson_pad_1", "lesson_pad_2"],
                    }
                ]
            }
            runner = FakeClaudeRunner(
                _wrap_assistant_text(json.dumps(distilled_payload))
            )
            before = (repo / "lessons.yaml").read_text()
            summary = run_lesson_distillation(
                repo,
                approve=False,
                billing_ack=True,
                execution_ack=True,
                command_runner=runner,
            )
            self.assertEqual(summary["status"], "triggered_blocked_by_approval")
            self.assertTrue(summary["triggered"])
            self.assertIsNone(summary["archive_path"])
            self.assertEqual(len(summary["distilled_lessons"]), 1)
            # lessons.yaml unchanged
            self.assertEqual((repo / "lessons.yaml").read_text(), before)

    def test_approved_distillation_replaces_lessons_and_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._prepare(Path(tmp))
            distilled_payload = {
                "distilled_lessons": [
                    {
                        "id": "lesson_merged_synthetic",
                        "text": "Merged lesson for synthetic padding.",
                        "tags": ["synthetic", "distilled"],
                        "evidence_refs": [],
                        "merges": ["lesson_pad_1", "lesson_pad_2"],
                    },
                    {
                        "id": "lesson_real_001",
                        "text": "Real lesson kept verbatim from existing set.",
                        "tags": ["retrieval"],
                        "evidence_refs": [],
                        "merges": ["lesson_001"],
                    },
                ]
            }
            runner = FakeClaudeRunner(
                _wrap_assistant_text(json.dumps(distilled_payload))
            )
            summary = run_lesson_distillation(
                repo,
                approve=True,
                billing_ack=True,
                execution_ack=True,
                command_runner=runner,
            )
            self.assertEqual(summary["status"], "applied")
            self.assertEqual(summary["lesson_count_after"], 2)
            self.assertIsNotNone(summary["archive_path"])
            self.assertTrue(Path(summary["archive_path"]).exists())
            reloaded = load_lessons(repo)
            ids = [lesson["id"] for lesson in reloaded["active_lessons"]]
            self.assertIn("lesson_merged_synthetic", ids)
            self.assertIn("lesson_real_001", ids)

    def test_claude_invalid_json_fails_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._prepare(Path(tmp))
            runner = FakeClaudeRunner(_wrap_assistant_text("not json at all"))
            before = (repo / "lessons.yaml").read_text()
            summary = run_lesson_distillation(
                repo,
                approve=True,
                billing_ack=True,
                execution_ack=True,
                command_runner=runner,
            )
            self.assertEqual(summary["status"], "failed")
            self.assertIn("not parseable JSON", summary["error"])
            self.assertEqual((repo / "lessons.yaml").read_text(), before)


class ThresholdComputeTests(unittest.TestCase):
    def test_compute_bytes_sums_active_lessons(self) -> None:
        lessons = {
            "active_lessons": [
                {"text": "abc"},
                {"text": "defgh"},
            ]
        }
        self.assertEqual(compute_active_lessons_bytes(lessons), 8)

    def test_should_trigger_respects_settings_threshold(self) -> None:
        lessons = {"active_lessons": [{"text": "x" * 100}]}
        settings = {"memory": {"lessons": {"distillation_token_threshold_bytes": 50}}}
        triggered, current, threshold = should_trigger_distillation(lessons, settings)
        self.assertTrue(triggered)
        self.assertEqual(current, 100)
        self.assertEqual(threshold, 50)

    def test_should_not_trigger_when_within_threshold(self) -> None:
        lessons = {"active_lessons": [{"text": "x" * 10}]}
        settings = {"memory": {"lessons": {"distillation_token_threshold_bytes": 100}}}
        triggered, _, _ = should_trigger_distillation(lessons, settings)
        self.assertFalse(triggered)


if __name__ == "__main__":
    unittest.main()
