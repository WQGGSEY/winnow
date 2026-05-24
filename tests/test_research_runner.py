from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from research_harness.agents.market_research import run_market_research
from research_harness.research_runner import cmd_research
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


SAMPLE_ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2306.11111v1</id>
    <updated>2024-06-01T00:00:00Z</updated>
    <published>2024-06-01T00:00:00Z</published>
    <title>Sample Method For Pipeline Test</title>
    <summary>A method for testing the pipeline integration.</summary>
    <author><name>Tester One</name></author>
    <link href="http://arxiv.org/abs/2306.11111" rel="alternate" type="text/html"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2305.22222v1</id>
    <updated>2024-05-01T00:00:00Z</updated>
    <published>2024-05-01T00:00:00Z</published>
    <title>BM25 Baseline Pipeline</title>
    <summary>BM25 baseline.</summary>
    <author><name>Tester Two</name></author>
    <link href="http://arxiv.org/abs/2305.22222" rel="alternate" type="text/html"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2305.33333v1</id>
    <updated>2024-05-01T00:00:00Z</updated>
    <published>2024-05-01T00:00:00Z</published>
    <title>Random Null Baseline Pipeline</title>
    <summary>Random baseline.</summary>
    <author><name>Tester Three</name></author>
    <link href="http://arxiv.org/abs/2305.33333" rel="alternate" type="text/html"/>
  </entry>
</feed>
"""


def _grilling_session() -> dict:
    return {
        "session_id": "grill_pipeline_001",
        "status": "done",
        "user_goal": "deep retrieval calibration",
        "max_rounds": 8,
        "model": "sonnet",
        "created_at": "2026-05-25T00:00:00+00:00",
        "rounds": [],
        "extracted": {
            "root_goal_id": "rg_pipeline",
            "domain": "retrieval",
            "node_type": "capability",
            "claim_under_test": "Calibrated dense retrieval improves answer accuracy on QA.",
            "mandatory_baselines": [
                "current_best_known: BGE-large",
                "naive: BM25",
                "random_or_null: random ranking",
            ],
            "success_criteria": ["nDCG@10 +3% over BGE-large"],
            "disproof_conditions": ["No improvement over BM25"],
            "goal_facets": ["performance"],
            "taste_constraints": ["claim_first"],
            "search_query_seed": "retrieval calibration QA",
        },
        "usage_estimate": {
            "rounds_used": 1,
            "total_cost_usd": 0.01,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
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


class ResearchRunnerCmdTests(unittest.TestCase):
    def test_research_chains_grilling_market_research_and_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            session_path = tmp_path / "grilling_session.json"
            session_path.write_text(json.dumps(_grilling_session()), encoding="utf-8")

            # Pre-build a market research dossier using the same agent path so the
            # dossier file exists in memory/ before cmd_research runs. This sidesteps
            # the live HTTP path while exercising the rest of the chain end-to-end.
            http = _FakeHttp(
                {"http://export.arxiv.org/api/query": SAMPLE_ARXIV_XML.encode("utf-8")}
            )
            outcome = run_market_research(
                REPO_ROOT,
                _grilling_session(),
                run_dir=tmp_path / "premarket",
                http_fetcher=http,
                pdf_fetcher=lambda url: b"%PDF-1.4 fake",
                enable_google_scholar=False,
                write_dossier_to_memory=True,
            )

            try:
                # cmd_research will run market research a second time; provide
                # injection via monkey-patch is heavier — instead, we just
                # exercise the path by skipping cmd_research's market research
                # and calling the inner build directly. But to honor the test
                # contract we run cmd_research and accept that it will use the
                # default http fetcher; if outbound network fails, the bundle
                # status becomes failed but the chain still produces artifacts.
                # To make the test deterministic, monkey-patch the agent module
                # functions used by cmd_research.
                import research_harness.research_runner as runner_module
                import research_harness.agents.market_research as mr_module

                def _stub_run_market_research(
                    repo_root, grilling_session, *, run_dir=None, **kwargs
                ):
                    return run_market_research(
                        repo_root,
                        grilling_session,
                        run_dir=run_dir,
                        http_fetcher=http,
                        pdf_fetcher=lambda url: b"%PDF-1.4 fake",
                        enable_google_scholar=False,
                        write_dossier_to_memory=True,
                        max_papers=kwargs.get("max_papers", 10),
                    )

                original = runner_module.run_market_research
                runner_module.run_market_research = _stub_run_market_research
                try:
                    bundle = cmd_research(
                        REPO_ROOT,
                        grilling_session_path=session_path,
                        run_dir=tmp_path / "research_run",
                        max_papers=5,
                        enable_google_scholar=False,
                        publish=True,
                    )
                finally:
                    runner_module.run_market_research = original

                self.assertEqual(bundle["type"], "research_run_bundle")
                self.assertEqual(bundle["grilling_session_id"], "grill_pipeline_001")
                self.assertTrue(Path(bundle["market_research_brief_path"]).exists())
                self.assertTrue(Path(bundle["root_node_path"]).exists())
                root_node = json.loads(Path(bundle["root_node_path"]).read_text())
                validate_named_schema("node", root_node)
                # placeholder must have been replaced
                self.assertNotEqual(
                    root_node["baseline_refs"][0]["baseline_dossier_id"],
                    "bd_pending_market_research",
                )

                production_summary = bundle["production_summary"]
                self.assertEqual(production_summary["preflight_status"], "passed")
                self.assertGreater(len(production_summary["promoted_node_ids"]), 0)
                self.assertIsNotNone(production_summary["publication_dispatch"])
            finally:
                # cleanup dossier from memory/
                for dossier in (outcome.dossier, bundle.get("baseline_dossier_id") if isinstance(bundle, dict) else None):
                    pass
                self._cleanup_memory_dossiers(REPO_ROOT, [outcome.dossier["id"]])
                if isinstance(bundle, dict) and bundle.get("baseline_dossier_id"):
                    self._cleanup_memory_dossiers(REPO_ROOT, [bundle["baseline_dossier_id"]])

    def _cleanup_memory_dossiers(self, repo_root: Path, ids: list[str]) -> None:
        dossier_dir = repo_root / "memory" / "baseline_dossiers"
        candidates_dir = dossier_dir / "candidates"
        for dossier_id in ids:
            p = dossier_dir / f"{dossier_id}.yaml"
            if p.exists():
                p.unlink()
        # remove candidate detail files generated by the agent (best-effort)
        if candidates_dir.exists():
            for f in list(candidates_dir.iterdir()):
                if f.name.startswith("c_arxiv_") or f.name.startswith("c_naive_placeholder") \
                        or f.name.startswith("c_null_placeholder") or f.name.startswith("c_current_best_placeholder"):
                    f.unlink()


if __name__ == "__main__":
    unittest.main()
