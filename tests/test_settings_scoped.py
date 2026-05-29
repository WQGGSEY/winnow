"""Tests for the three-scope settings system (ADR 0005, Phase 1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_harness.settings_scoped import (
    FIELD_REGISTRY,
    FieldSpec,
    ResolvedSettings,
    find_spec,
    resolve_for_thread,
    validate_write,
    write_snapshot,
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class ResolvedSettingsTests(unittest.TestCase):
    def test_project_only_resolution(self) -> None:
        resolved = ResolvedSettings(
            project={"runtime": {"default_backend": "mock"}},
            operator={},
            thread={},
        )
        self.assertEqual(resolved.get_dotted("runtime.default_backend"), "mock")
        self.assertEqual(resolved.source_of("runtime.default_backend"), "project")

    def test_operator_overrides_project(self) -> None:
        resolved = ResolvedSettings(
            project={"runtime": {"default_backend": "mock"}},
            operator={"runtime": {"default_backend": "claude_code_live"}},
            thread={},
        )
        self.assertEqual(
            resolved.get_dotted("runtime.default_backend"), "claude_code_live"
        )
        self.assertEqual(resolved.source_of("runtime.default_backend"), "operator")

    def test_thread_overrides_operator_overrides_project(self) -> None:
        resolved = ResolvedSettings(
            project={"runtime": {"llm_orchestrator": {"enabled": True}}},
            operator={"runtime": {"llm_orchestrator": {"enabled": False}}},
            thread={"runtime.llm_orchestrator.enabled": True},
        )
        self.assertTrue(resolved.get_dotted("runtime.llm_orchestrator.enabled"))
        self.assertEqual(
            resolved.source_of("runtime.llm_orchestrator.enabled"), "thread"
        )

    def test_dicts_recurse_arrays_replace(self) -> None:
        """Sibling keys at different scopes coexist; arrays whole-replace."""
        resolved = ResolvedSettings(
            project={
                "runtime": {
                    "agent_max_rounds": {
                        "grilling_agent": 8,
                        "research_refiner_agent": 12,
                    }
                },
                "data_adapters": {"registered": [{"id": "a"}, {"id": "b"}]},
            },
            operator={
                "runtime": {"agent_max_rounds": {"grilling_agent": 5}},
                "data_adapters": {"registered": [{"id": "c"}]},
            },
            thread={},
        )
        # dict recurse: grilling_agent from operator, research_refiner_agent
        # still visible from project.
        self.assertEqual(resolved.get_dotted("runtime.agent_max_rounds.grilling_agent"), 5)
        self.assertEqual(
            resolved.get_dotted("runtime.agent_max_rounds.research_refiner_agent"), 12
        )
        self.assertEqual(
            resolved.source_of("runtime.agent_max_rounds.grilling_agent"), "operator"
        )
        self.assertEqual(
            resolved.source_of("runtime.agent_max_rounds.research_refiner_agent"),
            "project",
        )
        # array replace: operator's [{"id": "c"}] wins entirely.
        self.assertEqual(resolved.get_dotted("data_adapters.registered"), [{"id": "c"}])

    def test_mapping_compat_existing_call_sites(self) -> None:
        """Existing settings['runtime']['agent_max_rounds']['grilling_agent'] still works."""
        resolved = ResolvedSettings(
            project={"runtime": {"agent_max_rounds": {"grilling_agent": 8}}},
            operator={},
            thread={"runtime.agent_max_rounds.grilling_agent": 12},
        )
        # __getitem__ chain
        self.assertEqual(resolved["runtime"]["agent_max_rounds"]["grilling_agent"], 12)
        # .get() chain
        self.assertEqual(
            resolved.get("runtime", {})
            .get("agent_max_rounds", {})
            .get("grilling_agent"),
            12,
        )

    def test_to_dict_deep_copy(self) -> None:
        resolved = ResolvedSettings(
            project={"runtime": {"agent_max_rounds": {"grilling_agent": 8}}},
            operator={},
            thread={},
        )
        d = resolved.to_dict()
        d["runtime"]["agent_max_rounds"]["grilling_agent"] = 999
        self.assertEqual(
            resolved.get_dotted("runtime.agent_max_rounds.grilling_agent"), 8
        )

    def test_resolve_for_thread_reads_three_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(
                root / "settings.json",
                {"runtime": {"llm_orchestrator": {"mcp": {"default_model": "claude-opus-4-7"}}}},
            )
            _write_json(
                root / "settings.local.json",
                {"frontend": {"full_auto_mode": True}},
            )
            tid = "thread_abc"
            _write_json(
                root / "runs" / "threads" / tid / "thread_settings.json",
                {"runtime.llm_orchestrator.mcp.default_model": "claude-sonnet-4-6"},
            )

            resolved = resolve_for_thread(root, tid, phase="grilling")
            self.assertEqual(
                resolved.get_dotted("runtime.llm_orchestrator.mcp.default_model"),
                "claude-sonnet-4-6",
            )
            self.assertEqual(
                resolved.source_of("runtime.llm_orchestrator.mcp.default_model"),
                "thread",
            )
            self.assertTrue(resolved.get_dotted("frontend.full_auto_mode"))
            self.assertEqual(resolved.source_of("frontend.full_auto_mode"), "operator")
            self.assertEqual(resolved.thread_id, tid)
            self.assertEqual(resolved.phase, "grilling")

    def test_resolve_for_thread_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = resolve_for_thread(Path(tmp), thread_id=None)
            self.assertEqual(len(resolved), 0)


class FieldRegistryTests(unittest.TestCase):
    def test_starter_set_well_formed(self) -> None:
        seen_paths: set[str] = set()
        for spec in FIELD_REGISTRY:
            self.assertNotIn(spec.path, seen_paths, f"duplicate registry path: {spec.path}")
            seen_paths.add(spec.path)
            self.assertTrue(spec.writable_at, f"{spec.path} has empty writable_at")
            self.assertTrue(
                set(spec.writable_at) <= {"project", "operator", "thread"},
                f"{spec.path} has invalid scope",
            )
            self.assertTrue(
                set(spec.effective_ui_editable_in) <= set(spec.writable_at),
                f"{spec.path}: ui_editable_in must be subset of writable_at",
            )

    def test_glob_matching(self) -> None:
        spec = find_spec("runtime.agent_max_rounds.grilling_agent")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.path, "runtime.agent_max_rounds.*")
        self.assertTrue(spec.matches("runtime.agent_max_rounds.research_refiner_agent"))
        # glob matches one segment only, not two
        self.assertFalse(spec.matches("runtime.agent_max_rounds.foo.bar"))

    def test_unregistered_path_returns_none(self) -> None:
        self.assertIsNone(find_spec("totally.made.up.path"))


class ValidateWriteTests(unittest.TestCase):
    def test_scope_enforcement(self) -> None:
        # data_adapters.registered is operator-only
        with self.assertRaises(ValueError) as ctx:
            validate_write("data_adapters.registered", [], scope="thread")
        self.assertIn("not writable at scope 'thread'", str(ctx.exception))

    def test_type_integer(self) -> None:
        validate_write("publication_gate.ac_agent.accept_thresholds.validity", 7, "project")
        with self.assertRaises(ValueError):
            validate_write("publication_gate.ac_agent.accept_thresholds.validity", "7", "project")
        with self.assertRaises(ValueError):
            # bool rejected as integer
            validate_write("publication_gate.ac_agent.accept_thresholds.validity", True, "project")

    def test_range(self) -> None:
        validate_write("publication_gate.ac_agent.accept_thresholds.validity", 0, "thread")
        validate_write("publication_gate.ac_agent.accept_thresholds.validity", 10, "thread")
        with self.assertRaises(ValueError):
            validate_write("publication_gate.ac_agent.accept_thresholds.validity", 11, "thread")
        with self.assertRaises(ValueError):
            validate_write("publication_gate.ac_agent.accept_thresholds.validity", -1, "thread")

    def test_enum_static(self) -> None:
        validate_write("runtime.default_backend", "mock", "project")
        with self.assertRaises(ValueError):
            validate_write("runtime.default_backend", "bogus_backend", "project")

    def test_enum_with_enum_source(self) -> None:
        # mcp.default_model's allowed values are pulled from mcp.allowed_models at runtime.
        allowed = ["claude-opus-4-7", "claude-sonnet-4-6"]
        validate_write(
            "runtime.llm_orchestrator.mcp.default_model",
            "claude-opus-4-7",
            "thread",
            enum_resolver=lambda p: allowed,
        )
        with self.assertRaises(ValueError):
            validate_write(
                "runtime.llm_orchestrator.mcp.default_model",
                "claude-bogus-9-9",
                "thread",
                enum_resolver=lambda p: allowed,
            )

    def test_boolean(self) -> None:
        validate_write("publication_gate.enabled", False, "thread")
        with self.assertRaises(ValueError):
            validate_write("publication_gate.enabled", "no", "thread")

    def test_int_or_unlimited(self) -> None:
        validate_write("runtime.agent_max_rounds.grilling_agent", 8, "thread")
        validate_write("runtime.agent_max_rounds.grilling_agent", "unlimited", "thread")
        with self.assertRaises(ValueError):
            validate_write("runtime.agent_max_rounds.grilling_agent", "infinity", "thread")
        with self.assertRaises(ValueError):
            validate_write("runtime.agent_max_rounds.grilling_agent", 0, "thread")  # below min

    def test_regex(self) -> None:
        validate_write("runtime.agent_budgets.grilling_agent", "0.50", "thread")
        with self.assertRaises(ValueError):
            validate_write("runtime.agent_budgets.grilling_agent", "fifty cents", "thread")

    def test_nullable_when_default_is_none(self) -> None:
        # subscription_ack_at has default=None
        validate_write("frontend.subscription_ack_at", None, "operator")
        # publication_gate.enabled has non-null default; rejects None
        with self.assertRaises(ValueError):
            validate_write("publication_gate.enabled", None, "thread")

    def test_list(self) -> None:
        validate_write("data_adapters.registered", [], "operator")
        validate_write(
            "data_adapters.registered",
            [{"id": "x", "provenance": "test"}],
            "operator",
        )
        with self.assertRaises(ValueError):
            validate_write("data_adapters.registered", {"not": "a list"}, "operator")

    def test_unregistered_path_passes_silently(self) -> None:
        # Phase 1 policy: unregistered paths are permitted. Phase 4 tightens.
        validate_write("totally.made.up.path", 12345, "project")


class WriteSnapshotTests(unittest.TestCase):
    def test_snapshot_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(
                root / "settings.json",
                {
                    "runtime": {
                        "default_backend": "mock",
                        "agent_max_rounds": {
                            "grilling_agent": 8,
                            "research_refiner_agent": 12,
                            "_comment": "should be skipped by glob",
                        },
                        "llm_orchestrator": {
                            "mcp": {"default_model": "claude-opus-4-7"},
                        },
                    },
                    "publication_gate": {
                        "enabled": True,
                        "ac_agent": {
                            "accept_thresholds": {
                                "validity": 7,
                                "reproducibility": 6,
                                "necessity": 6,
                                "taste_alignment": 7,
                            }
                        },
                    },
                },
            )
            tid = "thread_snap"
            _write_json(
                root / "runs" / "threads" / tid / "thread_settings.json",
                {"runtime.agent_max_rounds.grilling_agent": 12},
            )
            resolved = resolve_for_thread(root, tid, phase="grilling")
            thread_dir = root / "runs" / "threads" / tid
            out = write_snapshot(resolved, thread_dir, "grilling")

            self.assertTrue(out.exists())
            self.assertEqual(out.name, "resolved_settings_snapshot.json")
            self.assertEqual(out.parent.name, "grilling")

            data = json.loads(out.read_text())
            # registered fields with values present:
            self.assertEqual(
                data["runtime.agent_max_rounds.grilling_agent"],
                {"value": 12, "source": "thread"},
            )
            self.assertEqual(
                data["runtime.agent_max_rounds.research_refiner_agent"],
                {"value": 12, "source": "project"},
            )
            # glob skipped `_comment` metadata key
            self.assertNotIn("runtime.agent_max_rounds._comment", data)
            # default-backed schema_default for an absent field
            # (frontend.full_auto_mode not present in any scope -> default False, schema_default)
            self.assertEqual(
                data["frontend.full_auto_mode"],
                {"value": False, "source": "schema_default"},
            )

    def test_snapshot_atomic_write_leaves_no_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolved = ResolvedSettings(project={}, operator={}, thread={})
            thread_dir = root / "runs" / "threads" / "thread_x"
            out = write_snapshot(resolved, thread_dir, "grilling")
            self.assertTrue(out.exists())
            tmp_files = list(out.parent.glob("*.tmp"))
            self.assertEqual(tmp_files, [], "atomic rename should leave no .tmp behind")


if __name__ == "__main__":
    unittest.main()
