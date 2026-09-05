"""PR10 tests for the supervisor start/stop frontend routes + state read."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import unittest

from fastapi.testclient import TestClient

from research_harness.frontend import server as fserver


def _setup_repo(tmp: Path, tid: str = "thread_t1") -> Path:
    tdir = tmp / "runs" / "threads" / tid
    (tdir / "grilling").mkdir(parents=True)
    (tdir / "market").mkdir(parents=True)
    (tdir / "production").mkdir(parents=True)
    minimal = {
        "thread_id": tid,
        "title": "t",
        "created_at": "2026-05-26T00:00:00Z",
        "updated_at": "2026-05-26T00:00:00Z",
        "current_phase": "production",
        "phase_status": "running",
        "user_goal": "x",
    }
    (tdir / "thread.json").write_text(json.dumps(minimal), encoding="utf-8")
    (tmp / "settings.json").write_text(
        json.dumps({"data_adapters": {"registered": []}}), encoding="utf-8"
    )
    return tmp


class SupervisorStateTests(unittest.TestCase):
    def test_state_returns_idle_when_no_lock(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            state = fserver._read_supervisor_state(repo, "thread_t1")
            self.assertFalse(state["running"])
            self.assertIsNone(state["pid"])
            self.assertEqual(state["log_tail"], [])
            self.assertEqual(state["needed_resources"], [])

    def test_state_returns_running_when_alive_lock(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            lock_path = repo / "runs" / "threads" / "thread_t1" / ".supervisor.lock"
            lock_path.write_text(str(os.getpid()), encoding="utf-8")
            state = fserver._read_supervisor_state(repo, "thread_t1")
            self.assertTrue(state["running"])
            self.assertEqual(state["pid"], os.getpid())

    def test_state_returns_idle_when_stale_lock(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            lock_path = repo / "runs" / "threads" / "thread_t1" / ".supervisor.lock"
            lock_path.write_text("99999999", encoding="utf-8")
            state = fserver._read_supervisor_state(repo, "thread_t1")
            self.assertFalse(state["running"])

    def test_state_reads_needed_resources_yaml(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            (repo / "runs" / "threads" / "thread_t1" / "needed_resources.yaml").write_text(
                "needs:\n  - type: \"data_adapter\"\n    spec: \"x\"\n",
                encoding="utf-8",
            )
            state = fserver._read_supervisor_state(repo, "thread_t1")
            self.assertEqual(len(state["needed_resources"]), 1)
            self.assertEqual(state["needed_resources"][0]["type"], "data_adapter")

    def test_state_reads_log_tail(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            log = "\n".join(f"line {i}" for i in range(50))
            (repo / "runs" / "threads" / "thread_t1" / "supervisor.log").write_text(log, encoding="utf-8")
            state = fserver._read_supervisor_state(repo, "thread_t1")
            self.assertEqual(len(state["log_tail"]), 30)
            self.assertEqual(state["log_tail"][-1], "line 49")


class SupervisorRoutesTests(unittest.TestCase):
    def _client(self, repo: Path) -> TestClient:
        app = fserver.create_app(repo_root=repo)
        return TestClient(app)

    def test_start_rejects_invalid_scope(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            client = self._client(repo)
            r = client.post(
                "/api/threads/thread_t1/supervisor/start",
                json={"target_scope": "garbage"},
            )
            self.assertEqual(r.status_code, 400)

    def test_deployment_start_rejects_without_ready_adapter(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            client = self._client(repo)

            r = client.post(
                "/api/threads/thread_t1/supervisor/start",
                json={"target_scope": "deployment"},
            )

            self.assertEqual(r.status_code, 400)
            self.assertIn("requires", r.text)

    def test_start_passes_ready_adapter_to_supervisor(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            source = repo / "dataset.json"
            source.write_text("{}", encoding="utf-8")
            (repo / "settings.json").write_text(
                json.dumps(
                    {
                        "data_adapters": {
                            "registered": [
                                {
                                    "id": "local_data",
                                    "materializer_type": "benchmark",
                                    "role": "evaluation",
                                    "source": str(source),
                                    "provenance": "fixture",
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            client = self._client(repo)
            with mock.patch.object(fserver, "subprocess") as proc_mod:
                fake_proc = mock.MagicMock(pid=12345)
                proc_mod.Popen.return_value = fake_proc
                proc_mod.STDOUT = -2
                proc_mod.DEVNULL = -3

                r = client.post(
                    "/api/threads/thread_t1/supervisor/start",
                    json={"target_scope": "deployment"},
                )

            self.assertEqual(r.status_code, 200, r.text)
            command = proc_mod.Popen.call_args.args[0]
            self.assertIn("--data-source-anchor", command)
            self.assertEqual(
                command[command.index("--data-source-anchor") + 1], "local_data"
            )
            self.assertEqual(r.json()["data_source_anchor"], "local_data")

    def test_start_rejects_selection_that_conflicts_with_existing_envelope(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            source = repo / "dataset.json"
            source.write_text("{}", encoding="utf-8")
            (repo / "settings.json").write_text(
                json.dumps(
                    {
                        "data_adapters": {
                            "registered": [
                                {
                                    "id": "local_data",
                                    "materializer_type": "benchmark",
                                    "role": "evaluation",
                                    "source": str(source),
                                    "provenance": "fixture",
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            envelope = repo / "runs" / "threads" / "thread_t1" / "production" / "feasibility_envelope.json"
            envelope.write_text(
                json.dumps(
                    {
                        "operator_intent": {
                            "target_deploy_grade_scope": "directional",
                            "data_source_anchor": "local_data",
                        }
                    }
                ),
                encoding="utf-8",
            )
            client = self._client(repo)

            with mock.patch.object(fserver, "subprocess") as proc_mod:
                proc_mod.Popen.return_value = mock.MagicMock(pid=12345)
                proc_mod.STDOUT = -2
                proc_mod.DEVNULL = -3
                r = client.post(
                    "/api/threads/thread_t1/supervisor/start",
                    json={
                        "target_scope": "deployment",
                        "data_source_anchor": "local_data",
                    },
                )

            self.assertEqual(r.status_code, 409)
            self.assertIn("existing feasibility envelope", r.text)
            proc_mod.Popen.assert_not_called()

    def test_start_spawns_subprocess(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            client = self._client(repo)
            with mock.patch.object(fserver, "subprocess") as proc_mod:
                fake_proc = mock.MagicMock(pid=12345)
                proc_mod.Popen.return_value = fake_proc
                proc_mod.STDOUT = -2
                proc_mod.DEVNULL = -3
                r = client.post(
                    "/api/threads/thread_t1/supervisor/start",
                    json={"target_scope": "directional"},
                )
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertEqual(data["pid"], 12345)
            self.assertEqual(data["target_scope"], "directional")

    def test_start_waits_for_lock_and_reports_running(self):
        # Fix 1: supervisor_start should return only once the subprocess has
        # acquired its lock, so the frontend's immediate panel swap renders the
        # running branch (with logs) without a manual reload.
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            fserver.threads.update_thread(repo, "thread_t1", current_phase="connector", phase_status="complete")
            client = self._client(repo)
            lock_path = repo / "runs" / "threads" / "thread_t1" / ".supervisor.lock"
            with mock.patch.object(fserver, "subprocess") as proc_mod:
                fake_proc = mock.MagicMock(pid=777)
                fake_proc.poll.return_value = None  # alive

                def _spawn(*_a, **_k):
                    # Simulate the subprocess acquiring its lock during startup.
                    lock_path.write_text("777", encoding="utf-8")
                    return fake_proc

                proc_mod.Popen.side_effect = _spawn
                proc_mod.STDOUT = -2
                proc_mod.DEVNULL = -3
                r = client.post(
                    "/api/threads/thread_t1/supervisor/start",
                    json={"target_scope": "directional"},
                )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["running"])
            index = fserver.threads.load_thread(repo, "thread_t1")
            self.assertEqual((index["current_phase"], index["phase_status"]), ("production", "running"))

    def test_start_rejects_if_already_running(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            (repo / "runs" / "threads" / "thread_t1" / ".supervisor.lock").write_text(
                str(os.getpid()), encoding="utf-8"
            )
            client = self._client(repo)
            r = client.post(
                "/api/threads/thread_t1/supervisor/start",
                json={"target_scope": "directional"},
            )
            self.assertEqual(r.status_code, 409)

    def test_stop_404_when_no_lock(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            client = self._client(repo)
            r = client.post("/api/threads/thread_t1/supervisor/stop")
            self.assertEqual(r.status_code, 404)

    def test_stop_signals_alive_pid_and_escalates(self):
        """Stop sends SIGTERM, polls liveness, and escalates to SIGKILL
        if the process is still alive after 3s. With the os.kill mock
        always succeeding, the liveness probe never throws → escalation
        path fires."""
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            lock = repo / "runs" / "threads" / "thread_t1" / ".supervisor.lock"
            lock.write_text(str(os.getpid()), encoding="utf-8")
            client = self._client(repo)
            with mock.patch.object(fserver.os, "kill") as kill:
                # Patch asyncio.sleep so the test doesn't actually wait 3s.
                with mock.patch.object(fserver.asyncio, "sleep",
                                        return_value=None) as _asleep:
                    r = client.post("/api/threads/thread_t1/supervisor/stop")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            # At minimum SIGTERM was sent; SIGKILL may follow.
            self.assertIn("SIGTERM", data["signals_sent"])
            # os.kill called with SIGTERM at least once.
            sigterms = [
                c for c in kill.call_args_list
                if len(c.args) >= 2 and c.args[1] == fserver.signal.SIGTERM
            ]
            self.assertGreaterEqual(len(sigterms), 1)
            self.assertEqual(fserver.threads.load_thread(repo, "thread_t1")["phase_status"], "idle")

    def test_stop_succeeds_when_process_already_dead(self):
        """If SIGTERM raises ProcessLookupError immediately, route
        cleans the stale lock and returns 200 with empty signals."""
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            lock = repo / "runs" / "threads" / "thread_t1" / ".supervisor.lock"
            lock.write_text("99999999", encoding="utf-8")
            client = self._client(repo)
            with mock.patch.object(
                fserver.os, "kill",
                side_effect=ProcessLookupError(),
            ):
                r = client.post("/api/threads/thread_t1/supervisor/stop")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["signals_sent"], [])
            # stale lock removed
            self.assertFalse(lock.exists())
            self.assertEqual(fserver.threads.load_thread(repo, "thread_t1")["phase_status"], "idle")

    def test_start_rejects_when_publication_paper_exists(self):
        # PR10b: resume is disabled once publication artifact exists.
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            pub = repo / "runs" / "threads" / "thread_t1" / "production" / "publication"
            pub.mkdir(parents=True, exist_ok=True)
            (pub / "paper.html").write_text("<html/>", encoding="utf-8")
            client = self._client(repo)
            r = client.post(
                "/api/threads/thread_t1/supervisor/start",
                json={"target_scope": "directional"},
            )
            self.assertEqual(r.status_code, 409)
            self.assertIn("publication", r.text.lower())

    def test_start_allows_retry_when_historical_honest_failure_exists(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            pub = repo / "runs" / "threads" / "thread_t1" / "production" / "publication"
            pub.mkdir(parents=True, exist_ok=True)
            (pub / "honest_failure.html").write_text("<html/>", encoding="utf-8")
            client = self._client(repo)
            with mock.patch.object(fserver, "subprocess") as proc_mod:
                proc_mod.Popen.return_value = mock.MagicMock(pid=12345)
                proc_mod.STDOUT = -2
                proc_mod.DEVNULL = -3
                response = client.post(
                    "/api/threads/thread_t1/supervisor/start",
                    json={"target_scope": "directional"},
                )

            self.assertEqual(response.status_code, 200, response.text)

    def test_force_kill_route_removed(self):
        """PR12b: combined Stop + escalation replaces the separate
        Force kill route. Make sure the old route is gone — a 404 is
        the expected response (route does not exist)."""
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            client = self._client(repo)
            r = client.post("/api/threads/thread_t1/supervisor/force_kill")
            self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
