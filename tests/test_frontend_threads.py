from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_harness.frontend import threads


class ThreadsCrudTests(unittest.TestCase):
    def test_create_thread_writes_thread_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            t = threads.create_thread(repo, user_goal="test goal")
            self.assertTrue(t["thread_id"].startswith("thread_"))
            self.assertEqual(t["current_phase"], "grilling")
            self.assertEqual(t["phase_status"], "idle")
            self.assertEqual(t["user_goal"], "test goal")
            self.assertEqual(t["title"], "test goal")
            self.assertIsNone(t["outcome"])
            self.assertTrue(
                (repo / "runs" / "threads" / t["thread_id"] / "thread.json").exists()
            )

    def test_create_thread_rejects_empty_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(threads.ThreadError):
                threads.create_thread(Path(tmp), user_goal="")

    def test_load_and_update_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            t = threads.create_thread(repo, user_goal="x")
            updated = threads.update_thread(
                repo,
                t["thread_id"],
                phase_status="awaiting_input",
                domain="retrieval",
            )
            self.assertEqual(updated["phase_status"], "awaiting_input")
            self.assertEqual(updated["domain"], "retrieval")
            reloaded = threads.load_thread(repo, t["thread_id"])
            self.assertEqual(reloaded["phase_status"], "awaiting_input")

    def test_update_thread_rejects_unknown_phase_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            t = threads.create_thread(repo, user_goal="x")
            with self.assertRaises(threads.ThreadError):
                threads.update_thread(repo, t["thread_id"], phase_status="bogus")

    def test_list_threads_orders_by_updated_at_desc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            t1 = threads.create_thread(repo, user_goal="first")
            t2 = threads.create_thread(repo, user_goal="second")
            # Bump t1's updated_at by touching it
            threads.update_thread(repo, t1["thread_id"], title="updated first")
            listed = threads.list_threads(repo)
            self.assertEqual([d["thread_id"] for d in listed], [t1["thread_id"], t2["thread_id"]])

    def test_list_threads_skips_unrelated_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            t = threads.create_thread(repo, user_goal="x")
            # Drop an unrelated dir under runs/threads/
            stray = repo / "runs" / "threads" / "not_a_thread"
            stray.mkdir()
            (stray / "garbage.txt").write_text("nope")
            listed = threads.list_threads(repo)
            self.assertEqual([d["thread_id"] for d in listed], [t["thread_id"]])

    def test_boot_repair_demotes_running_to_awaiting_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            t = threads.create_thread(repo, user_goal="x")
            threads.update_thread(repo, t["thread_id"], phase_status="running")
            repaired = threads.boot_repair(repo)
            self.assertEqual(repaired, [t["thread_id"]])
            reloaded = threads.load_thread(repo, t["thread_id"])
            self.assertEqual(reloaded["phase_status"], "awaiting_input")

    def test_boot_repair_demotes_orphan_aborted_to_failed(self) -> None:
        """When the previous launcher couldn't write phase_status=failed
        before the server died, the thread is left in awaiting_input but
        the on-disk artifact has status=aborted. boot_repair should flip
        it to failed so the Retry button lights up."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            t = threads.create_thread(repo, user_goal="orphan repro")
            tid = t["thread_id"]
            # Simulate orphan: phase_status=awaiting_input + session aborted.
            grilling_dir = threads.phase_dir(repo, tid, "grilling")
            grilling_dir.mkdir(parents=True, exist_ok=True)
            (grilling_dir / "grilling_session.json").write_text(
                json.dumps(
                    {"status": "aborted", "error": "claude reported error"}
                )
            )
            threads.update_thread(
                repo,
                tid,
                current_phase="grilling",
                phase_status="awaiting_input",
            )
            repaired = threads.boot_repair(repo)
            self.assertEqual(repaired, [tid])
            reloaded = threads.load_thread(repo, tid)
            self.assertEqual(reloaded["phase_status"], "failed")

    def test_append_execute_ack_audit_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            t = threads.create_thread(repo, user_goal="x")
            threads.update_thread(
                repo,
                t["thread_id"],
                append_execute_ack={
                    "phase": "grilling",
                    "at": "2026-05-25T10:00:00Z",
                    "mode": "manual",
                },
            )
            reloaded = threads.load_thread(repo, t["thread_id"])
            self.assertEqual(len(reloaded["execute_acks"]), 1)
            self.assertEqual(reloaded["execute_acks"][0]["mode"], "manual")


if __name__ == "__main__":
    unittest.main()
