"""PR11 tests for the `claude -p` Web-Search enrichment of market_research."""

from __future__ import annotations

import json
import subprocess
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
        )
        self.assertIn("alpha discovery for quant", prompt)
        self.assertIn("AlphaForgeBench", prompt)
        self.assertIn("gplearn paper", prompt)
        self.assertIn("websearch", prompt.lower())
        self.assertIn("JSON", prompt)

    def test_prompt_caps_existing_titles(self):
        prompt = mrc._build_prompt(
            query="q",
            user_goal="g",
            existing_papers=[{"title": f"paper_{i}"} for i in range(30)],
        )
        self.assertIn("paper_0", prompt)
        self.assertNotIn("paper_29", prompt)


class EnrichTests(unittest.TestCase):
    def _fake_completed(self, stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
        cp = subprocess.CompletedProcess(args=["claude"], returncode=returncode)
        cp.stdout = stdout
        cp.stderr = stderr
        return cp

    def test_no_claude_on_path_returns_empty(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.object(mrc, "_which", return_value=None):
                out = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=Path(tmp),
                )
            self.assertEqual(out, [])

    def test_subprocess_returns_valid_json_normalized(self):
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
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            with mock.patch.object(mrc, "_which", return_value="/usr/bin/claude"), \
                 mock.patch.object(
                     mrc.subprocess,
                     "run",
                     return_value=self._fake_completed(json.dumps(sample)),
                 ):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=run_dir,
                    timeout_seconds=5.0,
                )
            self.assertEqual(len(papers), 1)
            paper = papers[0]
            self.assertEqual(paper["source"], "websearch")
            self.assertEqual(paper["url"], "https://example.com/paper")
            self.assertEqual(paper["title"], "Sample Industry Paper")
            self.assertEqual(paper["download_status"], "skipped")
            self.assertEqual(paper["arxiv_id"], None)
            # reported_metric is folded into abstract to stay
            # schema-compliant (reference_paper.schema has
            # additionalProperties: false).
            self.assertIn("AUC=0.6", paper["abstract"])
            # schema-only fields are present; non-schema fields removed.
            self.assertNotIn("reported_metric", paper)
            self.assertNotIn("role_hint", paper)
            self.assertNotIn("published", paper)
            # log file written with stdout
            log = (run_dir / "claude_websearch.log").read_text(encoding="utf-8")
            self.assertIn("=== stdout ===", log)

    def test_subprocess_timeout_returns_empty_and_logs(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            timeout_exc = subprocess.TimeoutExpired(
                cmd=["claude"], timeout=1, output=b"partial", stderr=b""
            )
            with mock.patch.object(mrc, "_which", return_value="/usr/bin/claude"), \
                 mock.patch.object(mrc.subprocess, "run", side_effect=timeout_exc):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=run_dir,
                    timeout_seconds=1.0,
                )
            self.assertEqual(papers, [])
            log = (run_dir / "claude_websearch.log").read_text(encoding="utf-8")
            self.assertIn("TIMEOUT", log)

    def test_subprocess_nonzero_exit_returns_empty(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            with mock.patch.object(mrc, "_which", return_value="/usr/bin/claude"), \
                 mock.patch.object(
                     mrc.subprocess, "run",
                     return_value=self._fake_completed("", returncode=2),
                 ):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=run_dir,
                )
            self.assertEqual(papers, [])

    def test_strips_code_fence_in_output(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            sample = [{"id": "w1", "title": "T", "url": "https://a"}]
            fenced = "```json\n" + json.dumps(sample) + "\n```\n"
            with mock.patch.object(mrc, "_which", return_value="/usr/bin/claude"), \
                 mock.patch.object(
                     mrc.subprocess, "run",
                     return_value=self._fake_completed(fenced),
                 ):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=run_dir,
                )
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0]["title"], "T")

    def test_prose_around_json_is_recovered(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            sample = [{"id": "w1", "title": "T", "url": "https://a"}]
            wrapped = (
                "I searched and found the following:\n\n"
                + json.dumps(sample)
                + "\n\nLet me know if you need more."
            )
            with mock.patch.object(mrc, "_which", return_value="/usr/bin/claude"), \
                 mock.patch.object(
                     mrc.subprocess, "run",
                     return_value=self._fake_completed(wrapped),
                 ):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=run_dir,
                )
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0]["title"], "T")

    def test_empty_array_returns_empty(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.object(mrc, "_which", return_value="/usr/bin/claude"), \
                 mock.patch.object(
                     mrc.subprocess, "run",
                     return_value=self._fake_completed("[]"),
                 ):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=Path(tmp),
                )
            self.assertEqual(papers, [])

    def test_normalized_papers_pass_reference_paper_schema(self):
        """Regression: every normalized paper MUST satisfy reference_paper
        schema (additionalProperties: false), otherwise market_research's
        validate_named_schema loop blows up the whole phase."""
        from research_harness.schemas.validator import validate_named_schema

        sample = [
            {
                "id": "web_chain_of_alpha",
                "title": "Chain-of-Alpha",
                "url": "https://arxiv.org/abs/2508.06312",
                "authors": [],
                "abstract": "Dual-chain LLM framework.",
                "arxiv_id": "2508.06312",
                "published": "2025-08",
                "reported_metric": "IC 0.07 / Sharpe 1.5",
                "role_hint": "current_best_known",
            }
        ]
        with TemporaryDirectory() as tmp:
            with mock.patch.object(mrc, "_which", return_value="/usr/bin/claude"), \
                 mock.patch.object(
                     mrc.subprocess, "run",
                     return_value=self._fake_completed(json.dumps(sample)),
                 ):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=Path(tmp),
                )
            self.assertEqual(len(papers), 1)
            # Critical: schema validation must succeed.
            validate_named_schema("reference_paper", papers[0])
            # arxiv_id preserved when claude provided it.
            self.assertEqual(papers[0]["arxiv_id"], "2508.06312")
            # year extracted from "2025-08".
            self.assertEqual(papers[0]["year"], 2025)

    def test_malformed_json_returns_empty(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.object(mrc, "_which", return_value="/usr/bin/claude"), \
                 mock.patch.object(
                     mrc.subprocess, "run",
                     return_value=self._fake_completed("not json at all"),
                 ):
                papers = mrc.enrich_with_claude_websearch(
                    query="q",
                    user_goal="g",
                    existing_papers=[],
                    run_dir=Path(tmp),
                )
            self.assertEqual(papers, [])


if __name__ == "__main__":
    unittest.main()
