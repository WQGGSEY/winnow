from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_harness.config import (
    ConfigError,
    load_critic_profile,
    load_harness_config,
    load_lessons,
    load_research_profile,
    load_settings,
    load_yaml,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigLoaderTests(unittest.TestCase):
    def test_loads_main_configs(self) -> None:
        settings = load_settings(REPO_ROOT)
        harness = load_harness_config(REPO_ROOT)
        profile = load_research_profile(REPO_ROOT)
        lessons = load_lessons(REPO_ROOT)

        self.assertEqual(settings["runtime"]["default_backend"], "mock")
        self.assertIn("harness_semantics", harness)
        self.assertEqual(profile["profile_id"], "seongje_research_v1")
        self.assertGreaterEqual(len(lessons["active_lessons"]), 1)

    def test_lessons_are_one_line(self) -> None:
        lessons = load_lessons(REPO_ROOT)

        for lesson in lessons["active_lessons"]:
            self.assertNotIn("\n", lesson["text"])

    def test_critic_override_policy_is_limited_to_taste_only(self) -> None:
        profile = load_critic_profile(REPO_ROOT / "critics" / "always" / "invariants.md")

        self.assertEqual(profile["override_policy"], "taste_only")

    def test_bad_critic_override_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.md"
            path.write_text(
                "---\n"
                "critic_profile_id: bad\n"
                "override_policy: full_override\n"
                "---\n"
                "# Bad\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "taste_only"):
                load_critic_profile(path)

    def test_yaml_loader_parses_list_of_maps(self) -> None:
        parsed = load_yaml(REPO_ROOT / "memory" / "baseline_dossiers" / "index.yaml")

        self.assertEqual(parsed["dossiers"][0]["id"], "bd_agent_harness_20260523")
        self.assertEqual(
            parsed["dossiers"][0]["selected_roles"]["current_best_known"],
            "c1_sakana_ai_scientist_v2",
        )


if __name__ == "__main__":
    unittest.main()

