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

    def test_stop_signals_alive_pid(self):
        with TemporaryDirectory() as tmp:
            repo = _setup_repo(Path(tmp))
            lock = repo / "runs" / "threads" / "thread_t1" / ".supervisor.lock"
            lock.write_text(str(os.getpid()), encoding="utf-8")
            client = self._client(repo)
            with mock.patch.object(fserver.os, "kill") as kill:
                r = client.post("/api/threads/thread_t1/supervisor/stop")
            self.assertEqual(r.status_code, 200, r.text)
            kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
