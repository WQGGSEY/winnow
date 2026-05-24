from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from research_harness.agents.market_research import run_market_research
from research_harness.orchestrator.root_node_from_grilling import (
    attach_market_research_dossier,
    build_root_node_from_grilling,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


SAMPLE_ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2305.99999v1</id>
    <updated>2024-05-01T00:00:00Z</updated>
    <published>2024-05-01T00:00:00Z</published>
    <title>Paper For Analysis Test</title>
    <summary>Abstract content for analysis testing.</summary>
    <author><name>Test Author</name></author>
    <link href="http://arxiv.org/abs/2305.99999" rel="alternate" type="text/html"/>
  </entry>
</feed>
"""


def _grilling_session() -> dict:
    return {
        "session_id": "grill_analysis_test",
        "status": "done",
        "user_goal": "improve retrieval",
        "max_rounds": 8,
        "model": "sonnet",
        "created_at": "2026-05-25T00:00:00+00:00",
        "rounds": [],
        "extracted": {
            "root_goal_id": "rg_analysis_test",
            "domain": "retrieval",
            "node_type": "capability",
            "claim_under_test": "Method X improves nDCG@10.",
            "mandatory_baselines": ["BGE-large", "BM25", "random"],
            "success_criteria": ["nDCG@10 +5%"],
            "disproof_conditions": ["within noise of BM25"],
            "goal_facets": ["performance"],
            "taste_constraints": [],
            "search_query_seed": "retrieval test",
        },
        "usage_estimate": {
            "rounds_used": 1,
            "total_cost_usd": 0.01,
            "total_input_tokens": 50,
            "total_output_tokens": 20,
        },
    }


class _FakeHttp:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses

    def __call__(self, url: str) -> bytes:
        for prefix, payload in self.responses.items():
            if url.startswith(prefix):
                return payload
        raise RuntimeError(f"FakeHttp has no response for {url}")


def _make_completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")


class BaselineAnalysisMdTests(unittest.TestCase):
    def _http(self):
        return _FakeHttp(
            {"http://export.arxiv.org/api/query": SAMPLE_ARXIV_XML.encode("utf-8")}
        )

    def test_default_writes_deterministic_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_market_research(
                REPO_ROOT,
                _grilling_session(),
                run_dir=Path(tmp),
                http_fetcher=self._http(),
                pdf_fetcher=lambda url: b"%PDF",
                enable_google_scholar=False,
                write_dossier_to_memory=False,
            )
            self.assertEqual(outcome.brief["baseline_analysis_source"], "deterministic_metadata")
            md_path = Path(outcome.brief["baseline_analysis_md_path"])
            self.assertTrue(md_path.exists())
            md = md_path.read_text(encoding="utf-8")
            self.assertIn("Baseline Analysis (deterministic)", md)
            self.assertIn("Paper For Analysis Test", md)

    def test_sonnet_enabled_writes_sonnet_md(self) -> None:
        analyst_md = "## Current-best\nBGE-large (https://arxiv.org/abs/2305.99999): nDCG@10 0.42 on LegalBench.\n\n## Naive\nBM25.\n\n## Random/Null\nRandom ranking.\n"
        cli_payload = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "result": analyst_md,
                "total_cost_usd": 0.005,
                "usage": {"input_tokens": 1000, "output_tokens": 200},
            }
        )

        def _runner(cmd, *, input, capture_output, text, timeout, check, env):
            return _make_completed(cli_payload)

        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_market_research(
                REPO_ROOT,
                _grilling_session(),
                run_dir=Path(tmp),
                http_fetcher=self._http(),
                pdf_fetcher=lambda url: b"%PDF",
                enable_google_scholar=False,
                write_dossier_to_memory=False,
                enable_sonnet_analysis=True,
                billing_ack=True,
                execution_ack=True,
                analysis_command_runner=_runner,
            )
            self.assertEqual(outcome.brief["baseline_analysis_source"], "sonnet_analysis")
            md = Path(outcome.brief["baseline_analysis_md_path"]).read_text(encoding="utf-8")
            self.assertIn("BGE-large", md)
            self.assertIn("nDCG@10 0.42", md)
            usage = outcome.brief["baseline_analysis_usage"]
            self.assertGreater(usage["cost_usd"], 0)
            self.assertEqual(usage["input_tokens"], 1000)

    def test_sonnet_billing_fail_falls_back_to_deterministic(self) -> None:
        def _runner(cmd, **kwargs):
            raise AssertionError("sonnet must not be called when billing fails")

        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_market_research(
                REPO_ROOT,
                _grilling_session(),
                run_dir=Path(tmp),
                http_fetcher=self._http(),
                pdf_fetcher=lambda url: b"%PDF",
                enable_google_scholar=False,
                write_dossier_to_memory=False,
                enable_sonnet_analysis=True,
                billing_ack=False,
                analysis_command_runner=_runner,
            )
            self.assertEqual(
                outcome.brief["baseline_analysis_source"], "sonnet_failed_fallback"
            )
            self.assertTrue(
                any("blocked by billing_ack" in w for w in outcome.brief["warnings"])
            )
            md = Path(outcome.brief["baseline_analysis_md_path"]).read_text()
            self.assertIn("Baseline Analysis (deterministic)", md)

    def test_sonnet_runtime_failure_falls_back_with_warning(self) -> None:
        def _runner(cmd, **kwargs):
            raise RuntimeError("network down")

        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_market_research(
                REPO_ROOT,
                _grilling_session(),
                run_dir=Path(tmp),
                http_fetcher=self._http(),
                pdf_fetcher=lambda url: b"%PDF",
                enable_google_scholar=False,
                write_dossier_to_memory=False,
                enable_sonnet_analysis=True,
                billing_ack=True,
                execution_ack=True,
                analysis_command_runner=_runner,
            )
            self.assertEqual(
                outcome.brief["baseline_analysis_source"], "sonnet_failed_fallback"
            )
            self.assertTrue(
                any("sonnet baseline analysis failed" in w for w in outcome.brief["warnings"])
            )


class RootNodeAttachBaselineAnalysisMdTests(unittest.TestCase):
    def test_attach_records_md_path_in_lineage(self) -> None:
        node = build_root_node_from_grilling(_grilling_session())
        patched = attach_market_research_dossier(
            node,
            baseline_dossier_id="bd_x",
            candidate_ids=["c1"],
            baseline_analysis_md_path="/abs/path/baseline_analysis.md",
        )
        notes = patched["lineage"]["inherited_assumptions"]
        self.assertTrue(
            any(
                "market_research_baseline_analysis_md: /abs/path/baseline_analysis.md" in line
                for line in notes
            )
        )

    def test_attach_without_md_path_does_not_change_lineage(self) -> None:
        node = build_root_node_from_grilling(_grilling_session())
        original_inherited = list(node["lineage"]["inherited_assumptions"])
        patched = attach_market_research_dossier(
            node, baseline_dossier_id="bd_x", candidate_ids=["c1"]
        )
        self.assertEqual(
            patched["lineage"]["inherited_assumptions"], original_inherited
        )


if __name__ == "__main__":
    unittest.main()
