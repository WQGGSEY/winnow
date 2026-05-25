"""Server smoke tests via FastAPI's TestClient.

These do NOT invoke live Claude. They exercise the routing, template
rendering, thread CRUD, subscription_ack gating, and the file-serving
path traversal guard.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from research_harness.frontend import threads
from research_harness.frontend.server import create_app


class ServerSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        # Copy essential files the harness expects (settings.json, schemas).
        # The server doesn't actually need them for these smoke tests — only
        # the threads.py + acks.py + jinja layer is exercised.
        self.client = TestClient(create_app(self.repo))

    def test_index_renders_empty_state(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Pick or create a thread", resp.text)

    def test_create_thread_redirects(self) -> None:
        resp = self.client.post(
            "/api/threads",
            data={"user_goal": "test goal for server smoke"},
        )
        self.assertEqual(resp.status_code, 200)
        # HX-Redirect header triggers a full client-side navigation to the new
        # thread page (needed because the form lives inside a <dialog>; an
        # ajax-style swap via HX-Location leaves the dialog open).
        self.assertIn("HX-Redirect", resp.headers)
        self.assertTrue(resp.headers["HX-Redirect"].startswith("/threads/thread_"))

    def test_create_thread_rejects_empty(self) -> None:
        resp = self.client.post("/api/threads", data={"user_goal": ""})
        self.assertEqual(resp.status_code, 400)

    def test_thread_page_renders_accordion(self) -> None:
        t = threads.create_thread(self.repo, user_goal="thread for accordion")
        resp = self.client.get(f"/threads/{t['thread_id']}")
        self.assertEqual(resp.status_code, 200)
        # Accordion must list all four phases.
        for phase in ("Grilling", "Market research", "Refiner", "Production"):
            self.assertIn(phase, resp.text)

    def test_thread_page_404_for_missing(self) -> None:
        resp = self.client.get("/threads/thread_doesnotexist")
        self.assertEqual(resp.status_code, 404)

    def test_grilling_start_requires_subscription_ack(self) -> None:
        t = threads.create_thread(self.repo, user_goal="needs ack")
        resp = self.client.post(
            f"/api/threads/{t['thread_id']}/grilling/start",
            json={},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("subscription_ack", resp.text)

    def test_subscription_ack_grant_and_revoke(self) -> None:
        grant = self.client.post("/api/settings/subscription_ack")
        self.assertEqual(grant.status_code, 200)
        self.assertTrue(grant.json()["ok"])
        revoke = self.client.post("/api/settings/subscription_ack/revoke")
        self.assertEqual(revoke.status_code, 200)

    def test_full_auto_toggle(self) -> None:
        resp = self.client.post("/api/settings/full_auto", json={"enabled": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["full_auto_mode"])
        state = self.client.get("/api/state").json()
        self.assertTrue(state["full_auto_mode"])

    def test_partial_route_for_completed_grilling(self) -> None:
        t = threads.create_thread(self.repo, user_goal="show extracted")
        # Drop a synthetic grilling_session.json into the phase dir.
        phase_dir = threads.phase_dir(self.repo, t["thread_id"], "grilling")
        phase_dir.mkdir(parents=True, exist_ok=True)
        session = {
            "session_id": "grill_test001",
            "status": "done",
            "user_goal": "show extracted",
            "max_rounds": 8,
            "model": "sonnet",
            "created_at": "2026-05-25T00:00:00Z",
            "rounds": [],
            "extracted": {
                "root_goal_id": "rg_show_extracted",
                "domain": "retrieval",
                "node_type": "capability",
                "claim_under_test": "x beats y",
                "mandatory_baselines": ["a"],
                "success_criteria": ["b"],
                "disproof_conditions": ["c"],
                "goal_facets": [],
                "taste_constraints": [],
                "search_query_seed": "x",
            },
            "usage_estimate": {
                "rounds_used": 1,
                "total_cost_usd": 0.01,
                "total_input_tokens": 10,
                "total_output_tokens": 5,
            },
        }
        (phase_dir / "grilling_session.json").write_text(json.dumps(session))
        resp = self.client.get(
            f"/partials/thread/{t['thread_id']}/phase/grilling"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("x beats y", resp.text)
        self.assertIn("Extracted", resp.text)

    def test_thread_page_survives_running_without_session_file(self) -> None:
        """Regression: between grilling/start and the first _flush_in_progress,
        the phase directory exists but grilling_session.json does not. The
        thread page rendered for the running thread used to crash with
        UndefinedError accessing session.session_id."""
        from research_harness.frontend.server import LiveSession

        t = threads.create_thread(self.repo, user_goal="race-condition test")
        # Simulate the post-launch / pre-flush state: phase dir exists,
        # thread status is "running", but no grilling_session.json yet —
        # and the LiveSession DOES exist in memory (launcher just created
        # it, the agent task hasn't reached the first flush yet).
        threads.phase_dir(self.repo, t["thread_id"], "grilling").mkdir(
            parents=True, exist_ok=True
        )
        threads.update_thread(
            self.repo, t["thread_id"], phase_status="running"
        )
        self.client.app.state.s.sessions[t["thread_id"]] = LiveSession(
            thread_id=t["thread_id"], phase="grilling"
        )
        try:
            resp = self.client.get(f"/threads/{t['thread_id']}")
            self.assertEqual(resp.status_code, 200)
            # The chat panel should pre-render with a disabled reply form, the
            # "agent is contacting Claude" placeholder, and the chat list
            # ready for SSE-driven population — so the user sees progress
            # instead of a 500 or an empty "not yet run" state.
            self.assertIn("agent is contacting Claude", resp.text)
            self.assertIn(f'id="chat-{t["thread_id"]}"', resp.text)
            self.assertIn("Waiting for the first question", resp.text)
            # And critically — the reply form is present but disabled
            # (because no ASK has arrived yet, NOT because the server
            # restarted).
            self.assertIn("disabled", resp.text)
            self.assertNotIn("Server was restarted", resp.text)
        finally:
            self.client.app.state.s.sessions.pop(t["thread_id"], None)

    def test_launch_refuses_when_another_thread_holds_lock(self) -> None:
        """Regression: clicking Start grilling on thread B while thread A is
        live used to return 200 (s.sessions[B] populated) and then the run
        loop silently failed with LockBusyError, leaving B in a confused
        state. The launch must refuse synchronously with 409."""
        from research_harness.frontend.lock import LockHolder

        t = threads.create_thread(self.repo, user_goal="contender")
        self.client.post("/api/settings/subscription_ack")
        app_state = self.client.app.state.s
        # Simulate another thread holding the lock.
        app_state.lock._holder = LockHolder(
            thread_id="thread_other_already_live",
            phase="grilling",
            acquired_at="2026-05-25T05:00:00Z",
        )
        try:
            resp = self.client.post(
                f"/api/threads/{t['thread_id']}/grilling/start",
                json={"mode": "manual"},
            )
            self.assertEqual(resp.status_code, 409)
            self.assertIn("another thread is currently live", resp.text)
            self.assertIn("thread_other_already_live", resp.text)
            # And critically — B was NOT added to s.sessions.
            self.assertNotIn(t["thread_id"], app_state.sessions)
        finally:
            app_state.lock._holder = None

    def test_resume_button_hidden_when_live_session_present(self) -> None:
        """The Resume / reconnect button is only useful post-server-restart
        (when the in-memory LiveSession is gone). When the agent task is
        alive, SSE auto-reconnect handles transient drops and the button
        would just be visual noise."""
        from research_harness.frontend.server import LiveSession

        t = threads.create_thread(self.repo, user_goal="hide resume when alive")
        # Drop a synthetic in-progress session so the chat panel renders.
        phase_dir = threads.phase_dir(self.repo, t["thread_id"], "grilling")
        phase_dir.mkdir(parents=True, exist_ok=True)
        session = {
            "session_id": "grill_alive",
            "status": "in_progress",
            "user_goal": "hide resume when alive",
            "max_rounds": 8,
            "model": "sonnet",
            "created_at": "2026-05-25T00:00:00Z",
            "rounds": [],
            "extracted": {
                "root_goal_id": "rg_alive",
                "domain": "x",
                "node_type": "capability",
                "claim_under_test": "x",
                "mandatory_baselines": ["a"],
                "success_criteria": ["b"],
                "disproof_conditions": ["c"],
                "goal_facets": [],
                "taste_constraints": [],
                "search_query_seed": "x",
            },
            "usage_estimate": {
                "rounds_used": 0,
                "total_cost_usd": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
            },
        }
        (phase_dir / "grilling_session.json").write_text(json.dumps(session))
        threads.update_thread(
            self.repo, t["thread_id"], phase_status="awaiting_input"
        )
        self.client.app.state.s.sessions[t["thread_id"]] = LiveSession(
            thread_id=t["thread_id"], phase="grilling"
        )
        try:
            resp = self.client.get(f"/threads/{t['thread_id']}")
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("Resume / reconnect", resp.text)
            # Abandon still available (different concern: bailing out
            # while alive).
            self.assertIn("Abandon", resp.text)
        finally:
            self.client.app.state.s.sessions.pop(t["thread_id"], None)

    def test_resume_button_visible_when_no_live_session(self) -> None:
        """Conversely, when the LiveSession is missing (e.g. post-server-
        restart) and phase is still live, Resume must show."""
        t = threads.create_thread(self.repo, user_goal="show resume when dead")
        phase_dir = threads.phase_dir(self.repo, t["thread_id"], "grilling")
        phase_dir.mkdir(parents=True, exist_ok=True)
        # No grilling_session.json on disk — but is_live is True via
        # phase_status, so the chat panel still renders the placeholder
        # and SHOULD show Resume.
        threads.update_thread(
            self.repo, t["thread_id"], phase_status="awaiting_input"
        )
        resp = self.client.get(f"/threads/{t['thread_id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Resume / reconnect", resp.text)

    def test_market_and_production_streams_404_without_live_session(self) -> None:
        """Market and production SSE endpoints exist so the page can
        auto-reload on phase_complete. When there's no live session they
        cleanly 409 (same as grilling/refine)."""
        t = threads.create_thread(self.repo, user_goal="no live session")
        for phase in ("market", "production"):
            with self.subTest(phase=phase):
                resp = self.client.get(
                    f"/api/threads/{t['thread_id']}/{phase}/stream"
                )
                self.assertEqual(resp.status_code, 409)

    def test_market_and_production_stream_endpoints_registered(self) -> None:
        """Smoke: the routes exist (would 404 otherwise) and require
        a real thread."""
        for phase in ("market", "production"):
            resp = self.client.get(
                f"/api/threads/thread_doesnotexist/{phase}/stream"
            )
            self.assertEqual(resp.status_code, 404)

    def test_rename_thread_updates_title(self) -> None:
        t = threads.create_thread(self.repo, user_goal="original title")
        resp = self.client.post(
            f"/api/threads/{t['thread_id']}/rename",
            json={"title": "renamed title"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "renamed title")
        reloaded = threads.load_thread(self.repo, t["thread_id"])
        self.assertEqual(reloaded["title"], "renamed title")

    def test_rename_rejects_empty_title(self) -> None:
        t = threads.create_thread(self.repo, user_goal="x")
        resp = self.client.post(
            f"/api/threads/{t['thread_id']}/rename",
            json={"title": "   "},
        )
        self.assertEqual(resp.status_code, 400)

    def test_rename_returns_404_for_missing_thread(self) -> None:
        resp = self.client.post(
            "/api/threads/thread_doesnotexist/rename",
            json={"title": "x"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_delete_thread_removes_directory(self) -> None:
        t = threads.create_thread(self.repo, user_goal="to delete")
        thread_dir = threads.threads_root(self.repo) / t["thread_id"]
        self.assertTrue(thread_dir.exists())
        resp = self.client.post(f"/api/threads/{t['thread_id']}/delete")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(thread_dir.exists())
        # Subsequent thread page request 404s.
        self.assertEqual(
            self.client.get(f"/threads/{t['thread_id']}").status_code, 404
        )

    def test_delete_refuses_when_thread_holds_live_lock(self) -> None:
        from research_harness.frontend.lock import LockHolder

        t = threads.create_thread(self.repo, user_goal="live thread")
        app_state = self.client.app.state.s
        app_state.lock._holder = LockHolder(
            thread_id=t["thread_id"],
            phase="grilling",
            acquired_at="2026-05-25T05:00:00Z",
        )
        try:
            resp = self.client.post(f"/api/threads/{t['thread_id']}/delete")
            self.assertEqual(resp.status_code, 409)
            self.assertIn("Abandon", resp.text)
            # Directory is still there.
            self.assertTrue(
                (threads.threads_root(self.repo) / t["thread_id"]).exists()
            )
        finally:
            app_state.lock._holder = None

    def test_sse_drains_stale_backlog_on_attach(self) -> None:
        """Regression: when a user navigates away mid-grilling and back,
        the agent worker has kept emitting events into the LiveSession's
        out_queue (no SSE consumer was draining). On reconnect, the new
        SSE generator used to replay every stale event, duplicating Q&A
        that the server-rendered page had already shown from disk."""
        from research_harness.frontend.server import LiveSession, _sse_stream

        async def _drive() -> list[dict]:
            session = LiveSession(thread_id="t_drain", phase="grilling")
            # Pre-populate the queue with stale events (the "navigate away"
            # window where no consumer was reading).
            await session.out_queue.put({"type": "ask", "question": "stale Q1"})
            await session.out_queue.put({"type": "user_reply", "text": "stale A1"})
            await session.out_queue.put({"type": "ask", "question": "stale Q2"})

            received: list[dict] = []
            gen = _sse_stream(session)

            # Inject a fresh terminal event after the drain has had a chance
            # to run. The generator's first await on `get()` will pick it up.
            async def _push_fresh() -> None:
                await asyncio.sleep(0.05)
                await session.out_queue.put({"type": "phase_complete"})

            push_task = asyncio.create_task(_push_fresh())
            try:
                async for frame in gen:
                    payload = json.loads(frame.removeprefix("data: ").rstrip("\n"))
                    received.append(payload)
                    if payload.get("type") in {"phase_complete", "phase_failed"}:
                        break
            finally:
                await push_task
            return received

        received = asyncio.run(_drive())
        # Only the fresh phase_complete should come through — stale Q&A drained.
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["type"], "phase_complete")

    def test_post_restart_page_blocks_reply_form_with_resume_banner(self) -> None:
        """Regression: after server restart, the on-disk grilling_session.json
        still has prior rounds and thread.json says phase_status='awaiting_input',
        but app.state.s.sessions has no LiveSession yet. The reply form used
        to render as enabled — a user typing into it submitted, got 409,
        and concluded their grilling history was "lost". The page must
        instead surface a clear "click Resume" banner and disable the form."""
        import json as _json

        t = threads.create_thread(self.repo, user_goal="post-restart UX")
        # Drop a synthetic in-progress session with rounds saved.
        phase_dir = threads.phase_dir(self.repo, t["thread_id"], "grilling")
        phase_dir.mkdir(parents=True, exist_ok=True)
        session = {
            "session_id": "grill_restart001",
            "status": "in_progress",
            "user_goal": "post-restart UX",
            "max_rounds": 8,
            "model": "sonnet",
            "created_at": "2026-05-25T07:00:00Z",
            "rounds": [
                {
                    "round_index": 0,
                    "action": "ASK",
                    "question": "first question",
                    "user_response": "first answer",
                    "raw_action": "{}",
                }
            ],
            "extracted": {
                "root_goal_id": "rg_restart",
                "domain": "x",
                "node_type": "capability",
                "claim_under_test": "x",
                "mandatory_baselines": ["a"],
                "success_criteria": ["b"],
                "disproof_conditions": ["c"],
                "goal_facets": [],
                "taste_constraints": [],
                "search_query_seed": "x",
            },
            "usage_estimate": {
                "rounds_used": 1,
                "total_cost_usd": 0.02,
                "total_input_tokens": 100,
                "total_output_tokens": 30,
            },
        }
        (phase_dir / "grilling_session.json").write_text(_json.dumps(session))
        threads.update_thread(
            self.repo, t["thread_id"], phase_status="awaiting_input"
        )
        # In-memory s.sessions is empty (no LiveSession for this thread).
        resp = self.client.get(f"/threads/{t['thread_id']}")
        self.assertEqual(resp.status_code, 200)
        # Prior rounds still visible.
        self.assertIn("first answer", resp.text)
        self.assertIn("first question", resp.text)
        # Resume banner present.
        self.assertIn("Server was restarted", resp.text)
        self.assertIn("Resume / reconnect", resp.text)
        # Form is disabled (placeholder reflects the gating).
        self.assertIn(
            "Click Resume / reconnect to re-attach the agent first",
            resp.text,
        )

    def test_thread_json_round_trip_includes_domain_state(self) -> None:
        t = threads.create_thread(self.repo, user_goal="domain state field")
        threads.update_thread(
            self.repo,
            t["thread_id"],
            domain="new_scaffolded_domain",
            domain_state="scaffold_complete",
        )
        reloaded = threads.load_thread(self.repo, t["thread_id"])
        self.assertEqual(reloaded["domain"], "new_scaffolded_domain")
        self.assertEqual(reloaded["domain_state"], "scaffold_complete")

    def test_thread_page_renders_scaffolding_badge(self) -> None:
        t = threads.create_thread(self.repo, user_goal="badge render")
        threads.update_thread(
            self.repo, t["thread_id"], domain_state="scaffolding"
        )
        resp = self.client.get(f"/threads/{t['thread_id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("✎ scaffolding", resp.text)

    def test_thread_page_renders_matched_domain_badge(self) -> None:
        t = threads.create_thread(self.repo, user_goal="matched render")
        threads.update_thread(
            self.repo,
            t["thread_id"],
            domain="retrieval",
            domain_state="matched",
        )
        resp = self.client.get(f"/threads/{t['thread_id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("domain: retrieval", resp.text)

    def test_update_thread_rejects_unknown_domain_state(self) -> None:
        t = threads.create_thread(self.repo, user_goal="bad state")
        with self.assertRaises(threads.ThreadError):
            threads.update_thread(
                self.repo, t["thread_id"], domain_state="bogus_state"
            )

    def test_abandon_releases_lock_and_marks_failed(self) -> None:
        from research_harness.frontend.lock import LockHolder
        from research_harness.frontend.server import LiveSession

        t = threads.create_thread(self.repo, user_goal="abandon target")
        threads.update_thread(
            self.repo, t["thread_id"], phase_status="awaiting_input"
        )
        app_state = self.client.app.state.s
        # Simulate a live session holding the lock.
        app_state.sessions[t["thread_id"]] = LiveSession(
            thread_id=t["thread_id"], phase="grilling"
        )
        app_state.lock._holder = LockHolder(
            thread_id=t["thread_id"],
            phase="grilling",
            acquired_at="2026-05-25T05:00:00Z",
        )
        resp = self.client.post(
            f"/api/threads/{t['thread_id']}/grilling/abandon"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(t["thread_id"], app_state.sessions)
        self.assertIsNone(app_state.lock.holder)
        reloaded = threads.load_thread(self.repo, t["thread_id"])
        self.assertEqual(reloaded["phase_status"], "failed")

    def test_atomic_thread_json_write_under_concurrent_reads(self) -> None:
        """Regression: a reply POST used to 404 when its _require_thread
        read raced against the worker thread's phase_status update_thread
        write (mid-write the file briefly had partial / empty content).
        After switching _write_index to temp-file + os.replace, readers
        always see either the old complete JSON or the new complete JSON."""
        import threading as py_threading

        t = threads.create_thread(self.repo, user_goal="atomicity")

        stop = py_threading.Event()
        errors: list[str] = []

        def writer():
            i = 0
            while not stop.is_set():
                threads.update_thread(
                    self.repo, t["thread_id"], domain=f"d_{i}"
                )
                i += 1

        def reader():
            while not stop.is_set():
                try:
                    threads.load_thread(self.repo, t["thread_id"])
                except threads.ThreadError as exc:
                    errors.append(str(exc))

        w = py_threading.Thread(target=writer)
        readers = [py_threading.Thread(target=reader) for _ in range(4)]
        w.start()
        for r in readers:
            r.start()
        import time

        time.sleep(0.5)
        stop.set()
        w.join()
        for r in readers:
            r.join()
        self.assertEqual(errors, [], f"races observed: {errors[:3]}")

    def test_reply_to_nonexistent_thread_returns_404(self) -> None:
        """Reply endpoint used to 409 for missing threads, which is the wrong
        error semantically. After the _require_thread guard it should be 404."""
        resp = self.client.post(
            "/api/threads/thread_doesnotexist/grilling/reply",
            data={"reply": "hi"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_grilling_start_is_idempotent_when_session_already_in_memory(
        self,
    ) -> None:
        """Regression: clicking Resume on a thread whose in-process task is
        still alive used to 409. The launcher must treat that as a no-op so
        the client can just reload and re-open the SSE stream."""
        from research_harness.frontend.server import LiveSession

        t = threads.create_thread(self.repo, user_goal="resume idempotency")
        threads.update_thread(
            self.repo, t["thread_id"], phase_status="awaiting_input"
        )
        # Grant subscription_ack so the launcher passes the gate.
        self.client.post("/api/settings/subscription_ack")
        # Pre-populate s.sessions to simulate an alive in-process task.
        app_state = self.client.app.state.s
        app_state.sessions[t["thread_id"]] = LiveSession(
            thread_id=t["thread_id"], phase="grilling"
        )
        resp = self.client.post(
            f"/api/threads/{t['thread_id']}/grilling/start",
            json={"mode": "manual"},
        )
        self.assertEqual(resp.status_code, 200)
        # And the in-memory session is unchanged (we didn't launch a new one).
        self.assertIn(t["thread_id"], app_state.sessions)

    def test_file_route_blocks_path_traversal(self) -> None:
        t = threads.create_thread(self.repo, user_goal="path traversal test")
        # Create a fake production phase dir + a sensitive file outside.
        production_dir = threads.phase_dir(self.repo, t["thread_id"], "production")
        production_dir.mkdir(parents=True, exist_ok=True)
        secret = self.repo / "SECRET.txt"
        secret.write_text("do not leak")
        resp = self.client.get(
            f"/files/{t['thread_id']}/production/../../../SECRET.txt"
        )
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
