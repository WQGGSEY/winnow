"""Phase 2 wiring tests (ADR 0005).

Verifies:
  - The three resolvers in ``config.py`` accept a ``ResolvedSettings``
    instance and return the same answers they would from a raw dict
    (the Mapping-loosening change is a no-op on existing semantics).
  - Thread-scope overrides flow through the resolvers when called via
    the new ``resolve_agent_*_for_thread`` wrappers.
  - ``validate_resolved`` catches the ``mcp.default_model`` /
    ``mcp.allowed_models`` cross-field invariant when active.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_harness.config import (
    resolve_agent_budget,
    resolve_agent_budget_for_thread,
    resolve_agent_max_rounds,
    resolve_agent_max_rounds_for_thread,
    resolve_agent_model,
    resolve_agent_model_for_thread,
)
from research_harness.settings_scoped import (
    ResolvedSettings,
    resolve_for_thread,
    validate_resolved,
)


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Resolver compatibility with ResolvedSettings
# ---------------------------------------------------------------------------


class ResolverCompatTests(unittest.TestCase):
    """The three resolvers must work identically against a raw dict and a
    ``ResolvedSettings`` Mapping containing the same data."""

    def _both(self, project_dict: dict, role: str) -> tuple:
        resolved = ResolvedSettings(project=project_dict, operator={}, thread={})
        return (
            resolve_agent_model(project_dict, role),
            resolve_agent_model(resolved, role),
            resolve_agent_budget(project_dict, role),
            resolve_agent_budget(resolved, role),
            resolve_agent_max_rounds(project_dict, role, fallback=8),
            resolve_agent_max_rounds(resolved, role, fallback=8),
        )

    def test_role_specific_values_match(self) -> None:
        proj = {
            "runtime": {
                "agent_models": {"grilling_agent": "claude-opus-4-7"},
                "agent_budgets": {"grilling_agent": "0.50"},
                "agent_max_rounds": {"grilling_agent": 8},
            }
        }
        m_dict, m_res, b_dict, b_res, r_dict, r_res = self._both(proj, "grilling_agent")
        self.assertEqual(m_dict, "claude-opus-4-7")
        self.assertEqual(m_res, "claude-opus-4-7")
        self.assertEqual(b_dict, "0.50")
        self.assertEqual(b_res, "0.50")
        self.assertEqual(r_dict, 8)
        self.assertEqual(r_res, 8)

    def test_default_fallback_matches(self) -> None:
        proj = {
            "runtime": {
                "agent_models": {"default": "claude-sonnet-4-6"},
                "agent_budgets": {"default": "0.25"},
                "agent_max_rounds": {"default": "unlimited"},
            }
        }
        m_dict, m_res, b_dict, b_res, r_dict, r_res = self._both(proj, "unspecified_role")
        self.assertEqual(m_dict, m_res)
        self.assertEqual(b_dict, b_res)
        # "unlimited" → None
        self.assertIsNone(r_dict)
        self.assertIsNone(r_res)

    def test_empty_settings_hits_literal_fallback(self) -> None:
        # No runtime.agent_models at all — should fall through to the
        # hard-coded fallback. Same on both inputs.
        m_dict = resolve_agent_model({}, "anything")
        m_res = resolve_agent_model(ResolvedSettings(project={}, operator={}, thread={}), "anything")
        self.assertEqual(m_dict, "claude-sonnet-4-6")
        self.assertEqual(m_res, "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Thread-aware wrappers — overrides flow through
# ---------------------------------------------------------------------------


class ThreadAwareResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        _write(
            self.repo / "settings.json",
            {
                "runtime": {
                    "agent_models": {
                        "default": "claude-sonnet-4-6",
                        "grilling_agent": "claude-opus-4-7",
                    },
                    "agent_budgets": {
                        "default": "0.25",
                        "grilling_agent": "0.50",
                    },
                    "agent_max_rounds": {
                        "grilling_agent": 8,
                    },
                }
            },
        )

    def test_no_thread_returns_project_value(self) -> None:
        m = resolve_agent_model_for_thread(self.repo, None, "grilling_agent")
        b = resolve_agent_budget_for_thread(self.repo, None, "grilling_agent")
        r = resolve_agent_max_rounds_for_thread(self.repo, None, "grilling_agent", fallback=99)
        self.assertEqual(m, "claude-opus-4-7")
        self.assertEqual(b, "0.50")
        self.assertEqual(r, 8)

    def test_thread_override_observed_by_model_resolver(self) -> None:
        tid = "thread_xyz"
        _write(
            self.repo / "runs" / "threads" / tid / "thread_settings.json",
            {"runtime.agent_models.grilling_agent": "claude-haiku-4-5"},
        )
        self.assertEqual(
            resolve_agent_model_for_thread(self.repo, tid, "grilling_agent"),
            "claude-haiku-4-5",
        )
        # Other thread without override still gets project value.
        self.assertEqual(
            resolve_agent_model_for_thread(self.repo, "thread_other", "grilling_agent"),
            "claude-opus-4-7",
        )

    def test_thread_override_observed_by_budget_resolver(self) -> None:
        tid = "thread_b"
        _write(
            self.repo / "runs" / "threads" / tid / "thread_settings.json",
            {"runtime.agent_budgets.grilling_agent": "1.00"},
        )
        self.assertEqual(
            resolve_agent_budget_for_thread(self.repo, tid, "grilling_agent"),
            "1.00",
        )

    def test_thread_override_observed_by_max_rounds_resolver(self) -> None:
        tid = "thread_r"
        _write(
            self.repo / "runs" / "threads" / tid / "thread_settings.json",
            {"runtime.agent_max_rounds.grilling_agent": 20},
        )
        self.assertEqual(
            resolve_agent_max_rounds_for_thread(self.repo, tid, "grilling_agent", fallback=8),
            20,
        )

    def test_thread_override_with_unlimited_string(self) -> None:
        tid = "thread_u"
        _write(
            self.repo / "runs" / "threads" / tid / "thread_settings.json",
            {"runtime.agent_max_rounds.grilling_agent": "unlimited"},
        )
        self.assertIsNone(
            resolve_agent_max_rounds_for_thread(self.repo, tid, "grilling_agent", fallback=8),
        )


# ---------------------------------------------------------------------------
# validate_resolved cross-field invariants
# ---------------------------------------------------------------------------


class ValidateResolvedTests(unittest.TestCase):
    def test_clean_resolved_yields_no_violations(self) -> None:
        resolved = ResolvedSettings(
            project={
                "runtime": {
                    "llm_orchestrator": {
                        "backend": "mcp",
                        "mcp": {
                            "default_model": "claude-opus-4-7",
                            "allowed_models": ["claude-opus-4-7", "claude-sonnet-4-6"],
                        },
                    }
                }
            },
            operator={},
            thread={},
        )
        self.assertEqual(validate_resolved(resolved), [])

    def test_mcp_default_model_not_in_allowed_flagged(self) -> None:
        resolved = ResolvedSettings(
            project={
                "runtime": {
                    "llm_orchestrator": {
                        "backend": "mcp",
                        "mcp": {
                            "default_model": "claude-opus-4-7",
                            "allowed_models": ["claude-opus-4-7", "claude-sonnet-4-6"],
                        },
                    }
                }
            },
            operator={},
            # thread picks a model not in allowed_models
            thread={"runtime.llm_orchestrator.mcp.default_model": "claude-bogus-9-9"},
        )
        violations = validate_resolved(resolved)
        self.assertEqual(len(violations), 1)
        msg = violations[0]
        self.assertIn("claude-bogus-9-9", msg)
        self.assertIn("from thread", msg)
        self.assertIn("allowed_models", msg)

    def test_non_mcp_backend_skips_the_check(self) -> None:
        # When backend is claude_cli or anthropic, mcp.default_model is
        # irrelevant — the resolver picks from runtime.agent_models instead.
        resolved = ResolvedSettings(
            project={
                "runtime": {
                    "llm_orchestrator": {
                        "backend": "claude_cli",
                        "mcp": {
                            "default_model": "anything",
                            "allowed_models": ["x", "y"],
                        },
                    }
                }
            },
            operator={},
            thread={},
        )
        self.assertEqual(validate_resolved(resolved), [])

    def test_missing_allowed_models_skips_the_check(self) -> None:
        # When the allowlist is empty / absent, no enforcement (operator
        # hasn't told the harness which models exist yet).
        resolved = ResolvedSettings(
            project={
                "runtime": {
                    "llm_orchestrator": {
                        "backend": "mcp",
                        "mcp": {"default_model": "claude-opus-4-7", "allowed_models": []},
                    }
                }
            },
            operator={},
            thread={},
        )
        self.assertEqual(validate_resolved(resolved), [])


if __name__ == "__main__":
    unittest.main()
