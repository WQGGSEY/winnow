from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_harness.orchestrator.demo import run_demo
from research_harness.publishing.slides_html import render_slides_html


REPO_ROOT = Path(__file__).resolve().parents[1]


class SlidesHTMLTests(unittest.TestCase):
    def test_render_slides_html_writes_file_with_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = run_demo(REPO_ROOT, Path(tmp) / "run")
            state = json.loads((run_dir / "research_state_bundle.json").read_text())
            output_path = run_dir / "slides_summary.html"

            render_slides_html(state, output_path)

            html_text = output_path.read_text(encoding="utf-8")
            self.assertTrue(html_text.startswith("<!doctype html>"))
            self.assertIn("Claim Contract", html_text)
            self.assertIn("AC Decision", html_text)
            self.assertIn("Critic Governance", html_text)
            self.assertIn(state["ac_decision"]["decision"], html_text)
            self.assertIn(state["node"]["id"], html_text)


if __name__ == "__main__":
    unittest.main()
