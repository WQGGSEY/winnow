"""Phase 3+4 tests (ADR 0005).

Covers:
  - Settings page rendering for each scope tab.
  - Project / operator bulk save round-trips.
  - Thread override add + remove.
  - ``thread_id_from_run_dir`` recognition of the runs/threads layout.
  - ``frontend.acks.full_auto_mode`` thread-scope tightening (the
    cross-cut invariant: thread may *disable* but not *enable*).
  - Migrated agent path code reads settings via ResolvedSettings
    (smoke import + Mapping behaviour).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from research_harness.frontend import acks
from research_harness.frontend.server import create_app
from research_harness.settings_scoped import (
    FIELD_REGISTRY,
    list_thread_overrides,
    resolve_for_thread,
    thread_id_from_run_dir,
    write_setting,
)


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Settings page rendering
# ---------------------------------------------------------------------------


class SettingsPageRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        _write(
            self.repo / "settings.json",
            {
                "runtime": {
                    "default_backend": "mock",
                    "llm_orchestrator": {
                        "backend": "mcp",
                        "mcp": {
                            "default_model": "claude-opus-4-7",
                            "allowed_models": ["claude-opus-4-7", "claude-sonnet-4-6"],
                        },
                    },
                    "agent_max_rounds": {"grilling_agent": 8},
                },
            },
        )
        self.client = TestClient(create_app(self.repo))

    def test_project_tab_renders(self) -> None:
        resp = self.client.get("/settings?scope=project")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("runtime.default_backend", resp.text)
        self.assertIn("Save project settings", resp.text)
        # active tab marker
        self.assertIn('href="/settings?scope=project"', resp.text)

    def test_operator_tab_renders(self) -> None:
        resp = self.client.get("/settings?scope=operator")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Save operator settings", resp.text)
        # operator-only fields must appear on this tab
        self.assertIn("frontend.full_auto_mode", resp.text)

    def test_thread_tab_requires_thread_id(self) -> None:
        # No thread_id provided -> falls back to project (we don't 404)
        resp = self.client.get("/settings?scope=thread")
        self.assertEqual(resp.status_code, 200)
        # The thread tab itself is shown as disabled in the tab strip.
        self.assertIn("none selected", resp.text)

    def test_thread_tab_with_thread_id_renders_override_view(self) -> None:
        resp = self.client.get("/settings?scope=thread&thread_id=thread_xyz")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No thread-scope overrides set", resp.text)
        self.assertIn("Add override", resp.text)

    def test_thread_tab_lists_current_overrides(self) -> None:
        tid = "thread_xyz"
        _write(
            self.repo / "runs" / "threads" / tid / "thread_settings.json",
            {"runtime.agent_max_rounds.grilling_agent": 12},
        )
        resp = self.client.get(f"/settings?scope=thread&thread_id={tid}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("runtime.agent_max_rounds.grilling_agent", resp.text)
        self.assertIn("Current overrides", resp.text)


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------


class SettingsWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        _write(
            self.repo / "settings.json",
            {
                "runtime": {
                    "default_backend": "mock",
                    "llm_orchestrator": {
                        "backend": "mcp",
                        "mcp": {
                            "default_model": "claude-opus-4-7",
                            "allowed_models": ["claude-opus-4-7", "claude-sonnet-4-6"],
                        },
                    },
                },
            },
        )
        self.client = TestClient(create_app(self.repo))

    def test_project_bulk_save_writes_to_settings_json(self) -> None:
        # Send the form values for fields the project scope is allowed to edit.
        resp = self.client.post(
            "/api/settings/project",
            data={
                "runtime.default_backend": "claude_code_dry_run",
                "publication_gate.enabled": "true",
                "__bool__publication_gate.enabled": "present",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Saved", resp.text)
        # On disk
        on_disk = json.loads((self.repo / "settings.json").read_text())
        self.assertEqual(on_disk["runtime"]["default_backend"], "claude_code_dry_run")
        self.assertEqual(on_disk["publication_gate"]["enabled"], True)

    def test_project_bulk_save_rejects_invalid_enum(self) -> None:
        resp = self.client.post(
            "/api/settings/project",
            data={"runtime.default_backend": "definitely_not_a_backend"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Validation error", resp.text)
        # Settings.json untouched
        on_disk = json.loads((self.repo / "settings.json").read_text())
        self.assertEqual(on_disk["runtime"]["default_backend"], "mock")

    def test_operator_bulk_save_writes_to_local(self) -> None:
        resp = self.client.post(
            "/api/settings/operator",
            data={
                "frontend.full_auto_mode": "true",
                "__bool__frontend.full_auto_mode": "present",
            },
        )
        self.assertEqual(resp.status_code, 200)
        local = json.loads((self.repo / "settings.local.json").read_text())
        self.assertEqual(local["frontend"]["full_auto_mode"], True)

    def test_thread_override_add_and_remove(self) -> None:
        tid = "thread_x"
        # Add
        resp = self.client.post(
            f"/api/settings/thread/{tid}",
            data={
                "path": "runtime.agent_max_rounds.grilling_agent",
                "value": "20",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Added override", resp.text)
        overrides = list_thread_overrides(self.repo, tid)
        self.assertEqual(overrides["runtime.agent_max_rounds.grilling_agent"], 20)

        # Remove
        resp = self.client.post(
            f"/api/settings/thread/{tid}/remove",
            data={"path": "runtime.agent_max_rounds.grilling_agent"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Removed override", resp.text)
        overrides = list_thread_overrides(self.repo, tid)
        self.assertNotIn("runtime.agent_max_rounds.grilling_agent", overrides)

    def test_thread_override_rejects_non_thread_scope_field(self) -> None:
        # auth_policy.provider is project-only; thread cannot write it.
        resp = self.client.post(
            "/api/settings/thread/thread_x",
            data={"path": "runtime.auth_policy.provider", "value": "claude_code"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("not thread-overridable", resp.text)


# ---------------------------------------------------------------------------
# thread_id_from_run_dir
# ---------------------------------------------------------------------------


class ThreadIdFromRunDirTests(unittest.TestCase):
    def test_recognises_frontend_layout(self) -> None:
        p = Path("/some/repo/runs/threads/thread_3ae5ec30/grilling")
        self.assertEqual(thread_id_from_run_dir(p), "thread_3ae5ec30")

    def test_returns_none_for_cli_layout(self) -> None:
        p = Path("/some/repo/runs/grilling/sess_abc123")
        self.assertIsNone(thread_id_from_run_dir(p))

    def test_returns_none_for_arbitrary_dir(self) -> None:
        p = Path("/tmp/whatever/anything")
        self.assertIsNone(thread_id_from_run_dir(p))


# ---------------------------------------------------------------------------
# acks.full_auto_mode thread-scope tightening
# ---------------------------------------------------------------------------


class FullAutoModeTighteningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def test_operator_off_thread_cannot_enable(self) -> None:
        # Operator: full_auto OFF. Thread tries to enable → must be ignored
        # (tighten-only rule).
        _write(self.repo / "settings.local.json", {"frontend": {"full_auto_mode": False}})
        tid = "thread_x"
        _write(
            self.repo / "runs" / "threads" / tid / "thread_settings.json",
            {"frontend.full_auto_mode": True},
        )
        self.assertFalse(acks.full_auto_mode(self.repo, tid))

    def test_operator_on_thread_off_disables(self) -> None:
        # Operator: ON. Thread: OFF → effective OFF (tightening allowed).
        _write(self.repo / "settings.local.json", {"frontend": {"full_auto_mode": True}})
        tid = "thread_x"
        _write(
            self.repo / "runs" / "threads" / tid / "thread_settings.json",
            {"frontend.full_auto_mode": False},
        )
        self.assertFalse(acks.full_auto_mode(self.repo, tid))

    def test_operator_on_no_thread_override(self) -> None:
        _write(self.repo / "settings.local.json", {"frontend": {"full_auto_mode": True}})
        self.assertTrue(acks.full_auto_mode(self.repo, "thread_no_override"))

    def test_backward_compatible_without_thread_id(self) -> None:
        _write(self.repo / "settings.local.json", {"frontend": {"full_auto_mode": True}})
        # Old callers pass no thread_id — return is purely operator state.
        self.assertTrue(acks.full_auto_mode(self.repo))
        self.assertFalse(acks.requires_modal(self.repo))


# ---------------------------------------------------------------------------
# Migrated agent functions still import cleanly + don't break their callers
# ---------------------------------------------------------------------------


class AgentMigrationSmokeTests(unittest.TestCase):
    def test_imports_resolve(self) -> None:
        # Just importing should not blow up — the new resolve_for_thread /
        # thread_id_from_run_dir hooks must exist where the migrated files
        # expect them.
        from research_harness.agents import grilling, market_research, research_refiner

        for mod in (grilling, market_research, research_refiner):
            self.assertTrue(hasattr(mod, "resolve_for_thread"))
            self.assertTrue(hasattr(mod, "thread_id_from_run_dir"))

    def test_resolved_settings_compatible_with_resolvers(self) -> None:
        # ResolvedSettings goes through the same code paths as a raw dict —
        # spot-checked here so a future refactor that breaks Mapping support
        # would fail this test rather than only the agent path at runtime.
        from research_harness.config import (
            resolve_agent_budget,
            resolve_agent_max_rounds,
            resolve_agent_model,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(
                repo / "settings.json",
                {
                    "runtime": {
                        "agent_models": {"grilling_agent": "claude-opus-4-7"},
                        "agent_budgets": {"grilling_agent": "0.50"},
                        "agent_max_rounds": {"grilling_agent": 8},
                    }
                },
            )
            resolved = resolve_for_thread(repo, None)
            self.assertEqual(resolve_agent_model(resolved, "grilling_agent"), "claude-opus-4-7")
            self.assertEqual(resolve_agent_budget(resolved, "grilling_agent"), "0.50")
            self.assertEqual(resolve_agent_max_rounds(resolved, "grilling_agent"), 8)


if __name__ == "__main__":
    unittest.main()
