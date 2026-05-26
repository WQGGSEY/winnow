"""Tests for thread_supervisor — hands-free e2e daemon.

We don't actually spawn `claude` subprocesses in tests; we exercise:
  - is_terminal() against various production_run_summary.json shapes
  - mcp_idle_seconds() against various mtime patterns
  - build_resume_prompt() output shape
  - SupervisorLock contention + stale lock recovery
  - watch_thread() loop with mocked spawn (verify terminate, idle-spawn,
    max-cycles, SIGINT)
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from research_harness import thread_supervisor as ts


def _make_thread(repo: Path, tid: str) -> Path:
    tdir = repo / "runs" / "threads" / tid
    (tdir / "production").mkdir(parents=True, exist_ok=True)
    return tdir


class TerminalDetectionTests(unittest.TestCase):
    def test_no_summary_file_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)
            self.assertIsNone(outcome)

    def test_honest_failure_outcome_is_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({"outcome": "honest_failure"}), encoding="utf-8"
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertTrue(t)
            self.assertEqual(outcome, "honest_failure")

    def test_accept_with_rendered_artifacts_is_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {
                        "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                    },
                }),
                encoding="utf-8",
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertTrue(t)
            self.assertEqual(outcome, "accept")

    def test_accept_without_artifacts_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {"rendered_artifacts": []},
                }),
                encoding="utf-8",
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)

    def test_corrupt_summary_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                "not json", encoding="utf-8"
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)


class IdleDetectionTests(unittest.TestCase):
    def test_empty_production_dir_idle_is_inf(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            self.assertEqual(ts.mcp_idle_seconds(repo, "t1"), float("inf"))

    def test_missing_production_dir_idle_is_inf(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(ts.mcp_idle_seconds(Path(tmp), "no_thread"), float("inf"))

    def test_recent_file_yields_small_idle(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            f = tdir / "production" / "tree" / "search_state.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("{}", encoding="utf-8")
            idle = ts.mcp_idle_seconds(repo, "t1")
            self.assertLess(idle, 5.0)

    def test_old_file_yields_large_idle(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            f = tdir / "production" / "tree" / "search_state.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("{}", encoding="utf-8")
            past = time.time() - 7200
            os.utime(f, (past, past))
            idle = ts.mcp_idle_seconds(repo, "t1")
            self.assertGreater(idle, 3600)


class ResumePromptTests(unittest.TestCase):
    def test_resume_prompt_carries_thread_id_and_anti_lazy_brief(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            prompt = ts.build_resume_prompt(repo, "t1", cycle=3)
            self.assertIn("t1", prompt)
            self.assertIn("cycle #3", prompt)
            self.assertIn("dual-gate", prompt.lower())
            self.assertIn("honest_failure", prompt)
            self.assertIn("anti-laziness", prompt.lower())

    def test_resume_prompt_reads_state_when_present(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            state = {
                "promoted_node_ids": ["n_root"],
                "pruned_node_ids": [],
                "status": "running",
                "nodes": [
                    {"id": "n_root", "status": "promoted"},
                    {"id": "n_a", "status": "ready"},
                ],
            }
            (tdir / "production" / "tree").mkdir(parents=True, exist_ok=True)
            (tdir / "production" / "tree" / "search_state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            prompt = ts.build_resume_prompt(repo, "t1", cycle=1)
            self.assertIn("n_root", prompt)


class LockTests(unittest.TestCase):
    def test_lock_acquire_and_release(self):
        with TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            lock = ts.SupervisorLock(tdir)
            lock.acquire()
            self.assertTrue((tdir / ".supervisor.lock").exists())
            lock.release()
            self.assertFalse((tdir / ".supervisor.lock").exists())

    def test_stale_lock_is_cleared(self):
        with TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            # 99999999 is virtually guaranteed to be a dead PID.
            (tdir / ".supervisor.lock").write_text("99999999", encoding="utf-8")
            lock = ts.SupervisorLock(tdir)
            lock.acquire()
            # After acquire, the lock should hold THIS process's pid.
            self.assertEqual(
                (tdir / ".supervisor.lock").read_text().strip(), str(os.getpid())
            )
            lock.release()

    def test_live_lock_blocks(self):
        with TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            # current process's pid is alive -> should block.
            (tdir / ".supervisor.lock").write_text(str(os.getpid()), encoding="utf-8")
            lock = ts.SupervisorLock(tdir)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                lock.acquire()
            # cleanup
            (tdir / ".supervisor.lock").unlink()


class WatchLoopTests(unittest.TestCase):
    def test_terminal_thread_exits_without_spawn(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({"outcome": "honest_failure"}), encoding="utf-8"
            )
            with mock.patch.object(ts, "spawn_claude_session") as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01, max_cycles=3
                )
            spawn.assert_not_called()
            self.assertEqual(result["status"], "terminal")
            self.assertEqual(result["outcome"], "honest_failure")

    def test_idle_thread_triggers_spawn_then_terminates_on_outcome(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            # production dir exists but is empty -> idle = inf
            # We make spawn flip the thread to terminal so the loop exits.
            summary_path = tdir / "production" / "production_run_summary.json"

            def fake_spawn(prompt, **kw):  # noqa: ARG001
                summary_path.write_text(
                    json.dumps({"outcome": "honest_failure"}), encoding="utf-8"
                )
                return 0

            with mock.patch.object(ts, "spawn_claude_session", side_effect=fake_spawn) as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01, max_cycles=3
                )
            spawn.assert_called_once()
            self.assertEqual(result["status"], "terminal")
            self.assertEqual(result["cycles"], 1)

    def test_max_cycles_exhaustion_exits(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            # production dir empty -> always idle, never terminal.
            with mock.patch.object(ts, "spawn_claude_session", return_value=0) as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01, max_cycles=2
                )
            self.assertEqual(spawn.call_count, 2)
            self.assertEqual(result["status"], "max_cycles_exceeded")
            self.assertEqual(result["cycles"], 2)

    def test_thread_dir_missing_raises(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "not found"):
                ts.watch_thread(Path(tmp), "no_such_thread")


class WhichTests(unittest.TestCase):
    def test_which_finds_python(self):
        # Python is always on PATH in CI; sanity-check _which.
        self.assertIsNotNone(ts._which("python3") or ts._which("python"))

    def test_which_returns_none_for_missing_binary(self):
        self.assertIsNone(ts._which("definitely-not-a-real-binary-xyz123"))


if __name__ == "__main__":
    unittest.main()
