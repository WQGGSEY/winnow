from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from research_harness.config import load_settings
from research_harness.orchestrator.demo import run_demo
from research_harness.publishing.publish import PublishError, publish_state_bundle


REPO_ROOT = Path(__file__).resolve().parents[1]


def _bundle_for_tests(tmp: Path) -> dict:
    run_dir = run_demo(REPO_ROOT, tmp / "run")
    return json.loads((run_dir / "research_state_bundle.json").read_text())


class PublishDispatchTests(unittest.TestCase):
    def test_accept_renders_default_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _bundle_for_tests(Path(tmp))
            state["ac_decision"]["decision"] = "accept"
            settings = load_settings(REPO_ROOT)
            output_dir = Path(tmp) / "pub"

            dispatch = publish_state_bundle(state, settings, output_dir)

            self.assertEqual(dispatch["decision"], "accept")
            self.assertIsNone(dispatch["blocked_reason"])
            rendered_names = sorted(item["output"] for item in dispatch["rendered_artifacts"])
            self.assertEqual(rendered_names, sorted(settings["publishing"]["default_outputs"]))
            for item in dispatch["rendered_artifacts"]:
                self.assertTrue(Path(item["artifact_path"]).exists())
            self.assertTrue((output_dir / "publication_dispatch.json").exists())

    def test_revise_does_not_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _bundle_for_tests(Path(tmp))
            state["ac_decision"]["decision"] = "revise"
            settings = load_settings(REPO_ROOT)
            output_dir = Path(tmp) / "pub"

            dispatch = publish_state_bundle(state, settings, output_dir)

            self.assertEqual(dispatch["decision"], "revise")
            self.assertEqual(dispatch["rendered_artifacts"], [])
            self.assertIn("only run on accept", dispatch["blocked_reason"])
            self.assertFalse((output_dir / "interactive_summary.html").exists())
            self.assertFalse((output_dir / "slides_summary.html").exists())
            self.assertTrue((output_dir / "publication_dispatch.json").exists())

    def test_unknown_renderer_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _bundle_for_tests(Path(tmp))
            state["ac_decision"]["decision"] = "accept"
            settings = copy.deepcopy(load_settings(REPO_ROOT))
            settings["publishing"]["default_outputs"] = ["interactive_html", "nonexistent_renderer"]
            output_dir = Path(tmp) / "pub"

            dispatch = publish_state_bundle(state, settings, output_dir)

            rendered = [item["output"] for item in dispatch["rendered_artifacts"]]
            skipped = [item["output"] for item in dispatch["skipped_outputs"]]
            self.assertEqual(rendered, ["interactive_html"])
            self.assertEqual(skipped, ["nonexistent_renderer"])

    def test_final_tex_is_filtered_when_allow_tex_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _bundle_for_tests(Path(tmp))
            state["ac_decision"]["decision"] = "accept"
            settings = copy.deepcopy(load_settings(REPO_ROOT))
            settings["publishing"]["default_outputs"] = ["interactive_html", "final_tex"]
            settings["publishing"]["allow_tex"] = False
            output_dir = Path(tmp) / "pub"

            dispatch = publish_state_bundle(state, settings, output_dir)

            rendered = [item["output"] for item in dispatch["rendered_artifacts"]]
            self.assertEqual(rendered, ["interactive_html"])
            self.assertNotIn("final_tex", dispatch["requested_outputs"])

    def test_missing_ac_decision_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _bundle_for_tests(Path(tmp))
            state.pop("ac_decision")
            settings = load_settings(REPO_ROOT)

            with self.assertRaises(PublishError):
                publish_state_bundle(state, settings, Path(tmp) / "pub")

    def test_evidence_is_fake_blocks_render_even_on_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _bundle_for_tests(Path(tmp))
            state["ac_decision"]["decision"] = "accept"
            state["evidence_is_fake"] = True
            settings = load_settings(REPO_ROOT)
            output_dir = Path(tmp) / "pub"

            dispatch = publish_state_bundle(state, settings, output_dir)

            self.assertTrue(dispatch["evidence_is_fake"])
            self.assertEqual(dispatch["rendered_artifacts"], [])
            self.assertIn("evidence_is_fake=true", dispatch["blocked_reason"])
            self.assertIn("experiment_plan_templates", dispatch["blocked_reason"])
            self.assertFalse((output_dir / "interactive_summary.html").exists())
            self.assertFalse((output_dir / "slides_summary.html").exists())

    def test_evidence_is_fake_false_still_renders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _bundle_for_tests(Path(tmp))
            state["ac_decision"]["decision"] = "accept"
            state["evidence_is_fake"] = False
            settings = load_settings(REPO_ROOT)
            output_dir = Path(tmp) / "pub"

            dispatch = publish_state_bundle(state, settings, output_dir)

            self.assertFalse(dispatch["evidence_is_fake"])
            self.assertIsNone(dispatch["blocked_reason"])
            self.assertGreater(len(dispatch["rendered_artifacts"]), 0)

    def test_missing_evidence_flag_defaults_to_not_fake_backward_compat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _bundle_for_tests(Path(tmp))
            state["ac_decision"]["decision"] = "accept"
            # no evidence_is_fake key at all — legacy state
            self.assertNotIn("evidence_is_fake", state)
            settings = load_settings(REPO_ROOT)
            output_dir = Path(tmp) / "pub"

            dispatch = publish_state_bundle(state, settings, output_dir)

            self.assertFalse(dispatch["evidence_is_fake"])
            self.assertGreater(len(dispatch["rendered_artifacts"]), 0)


if __name__ == "__main__":
    unittest.main()
