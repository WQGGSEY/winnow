"""PR11 tests for the Claude-driven web-search enrichment of market_research."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from research_harness.agents import market_research_claude as mrc


class WhichTests(unittest.TestCase):
    def test_which_returns_path_for_python(self):
        self.assertIsNotNone(mrc._which("python3") or mrc._which("python"))

    def test_which_returns_none_for_missing(self):
        self.assertIsNone(mrc._which("definitely-not-a-real-bin-xyz123"))


class PromptTests(unittest.TestCase):
    def test_prompt_includes_query_and_existing_titles(self):
        prompt = mrc._build_prompt(
            query="alpha discovery for quant",
            user_goal="help quant team",
            existing_papers=[
                {"title": "AlphaForgeBench"},
                {"title": "gplearn paper"},
            ],
            output_path=Path("/tmp/out.json"),
        )
        self.assertIn("alpha discovery for quant", prompt)
        self.assertIn("AlphaForgeBench", prompt)
        self.assertIn("gplearn paper", prompt)
        self.assertIn("/tmp/out.json", prompt)
        self.assertIn("websearch", prompt.lower())

    def test_prompt_caps_existing_titles(self):
        # 30 titles in -> only first 10 shown.
        prompt = mrc._build_prompt(
            query="q",
            user_goal="g",
            existing_papers=[{"title": f"paper_{i}"} for i in range(30)],
            output_path=Path("/tmp/x.json"),
        )
        self.assertIn("paper_0", prompt)
        self.assertNotIn("paper_29", prompt)


class EnrichTests(unittest.TestCase):
    def test_no_claude_on_path_returns_empty(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            with mock.patch.object(mrc, "_which", return_value=None):
                out = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=run_dir,
                )
            self.assertEqual(out, [])

    def test_subprocess_writes_valid_json_returns_normalized_papers(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            output_path = run_dir / "web_search_papers.json"
            sample = [
                {
                    "id": "web_xyz",
                    "title": "Sample Industry Paper",
                    "url": "https://example.com/paper",
                    "authors": ["Author A"],
                    "abstract": "We propose X with reported metric Y=0.6.",
                    "reported_metric": "AUC=0.6",
                    "role_hint": "current_best_known",
                }
            ]

            # The function deletes any stale output file at start; we
            # simulate claude completing by having os.write (the
            # prompt-injection moment) create the file.
            def fake_write(fd, data):
                output_path.write_text(json.dumps(sample), encoding="utf-8")
                return len(data)

            with mock.patch.object(mrc, "_which", return_value="/usr/bin/true"), \
                 mock.patch.object(mrc.pty, "fork", return_value=(12345, 99)), \
                 mock.patch.object(mrc.os, "write", side_effect=fake_write), \
                 mock.patch.object(mrc.select, "select", return_value=([], [], [])), \
                 mock.patch.object(mrc.os, "read", return_value=b""), \
                 mock.patch.object(mrc.os, "waitpid", return_value=(12345, 0)), \
                 mock.patch.object(mrc.os, "kill"), \
                 mock.patch.object(mrc.os, "close"), \
                 mock.patch.object(mrc.time, "sleep"):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=run_dir,
                    timeout_seconds=5.0,
                    boot_delay=0.0,
                )

            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0]["source"], "websearch")
            self.assertEqual(papers[0]["url"], "https://example.com/paper")
            self.assertEqual(papers[0]["title"], "Sample Industry Paper")
            self.assertEqual(papers[0]["download_status"], "skipped")
            self.assertEqual(papers[0]["arxiv_id"], None)
            self.assertEqual(papers[0]["reported_metric"], "AUC=0.6")

    def test_subprocess_no_file_returns_empty(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            # NO output file ever created.
            def fake_fork():
                return (12345, 99)

            with mock.patch.object(mrc, "_which", return_value="/usr/bin/true"), \
                 mock.patch.object(mrc.pty, "fork", side_effect=fake_fork), \
                 mock.patch.object(mrc.os, "write", return_value=10), \
                 mock.patch.object(mrc.select, "select", return_value=([], [], [])), \
                 mock.patch.object(mrc.os, "read", return_value=b""), \
                 mock.patch.object(mrc.os, "waitpid", return_value=(12345, 0)), \
                 mock.patch.object(mrc.os, "kill"), \
                 mock.patch.object(mrc.os, "close"), \
                 mock.patch.object(mrc.time, "sleep"):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=run_dir,
                    timeout_seconds=0.5,
                    boot_delay=0.0,
                )
            self.assertEqual(papers, [])

    def test_strips_code_fence_in_output(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            output_path = run_dir / "web_search_papers.json"
            fenced = (
                "```json\n"
                + json.dumps([{"id": "w1", "title": "T", "url": "https://a"}])
                + "\n```\n"
            )

            def fake_write(fd, data):
                output_path.write_text(fenced, encoding="utf-8")
                return len(data)

            with mock.patch.object(mrc, "_which", return_value="/usr/bin/true"), \
                 mock.patch.object(mrc.pty, "fork", return_value=(12345, 99)), \
                 mock.patch.object(mrc.os, "write", side_effect=fake_write), \
                 mock.patch.object(mrc.select, "select", return_value=([], [], [])), \
                 mock.patch.object(mrc.os, "read", return_value=b""), \
                 mock.patch.object(mrc.os, "waitpid", return_value=(12345, 0)), \
                 mock.patch.object(mrc.os, "kill"), \
                 mock.patch.object(mrc.os, "close"), \
                 mock.patch.object(mrc.time, "sleep"):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=run_dir,
                    timeout_seconds=5.0,
                    boot_delay=0.0,
                )
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0]["title"], "T")


if __name__ == "__main__":
    unittest.main()
