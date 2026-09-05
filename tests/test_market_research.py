from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_harness.agents.market_research import (
    MarketResearchError,
    run_market_research,
)


from research_harness.config import load_yaml
from research_harness.memory.baseline_dossier import (
    load_baseline_dossier,
    validate_baseline_dossier,
)
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


SAMPLE_ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2305.12345v1</id>
    <updated>2024-05-01T00:00:00Z</updated>
    <published>2024-05-01T00:00:00Z</published>
    <title>Dense Retrieval with Contrastive Pretraining</title>
    <summary>We propose a dense retrieval model that uses contrastive pretraining.</summary>
    <author><name>Alice Researcher</name></author>
    <author><name>Bob Scientist</name></author>
    <link href="http://arxiv.org/abs/2305.12345" rel="alternate" type="text/html"/>
    <link href="http://arxiv.org/pdf/2305.12345" rel="related" type="application/pdf"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <updated>2024-01-15T00:00:00Z</updated>
    <published>2024-01-15T00:00:00Z</published>
    <title>BM25 Baseline Revisited for Legal QA</title>
    <summary>A study on BM25 as a baseline for legal QA.</summary>
    <author><name>Carol Reviewer</name></author>
    <link href="http://arxiv.org/abs/2401.00001" rel="alternate" type="text/html"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2402.99999v1</id>
    <updated>2024-02-10T00:00:00Z</updated>
    <published>2024-02-10T00:00:00Z</published>
    <title>Random Ranking as a Null Hypothesis for Retrieval</title>
    <summary>We explore random ranking baselines.</summary>
    <author><name>Dave Skeptic</name></author>
    <link href="http://arxiv.org/abs/2402.99999" rel="alternate" type="text/html"/>
  </entry>
</feed>
"""


def _grilling_session() -> dict:
    return {
        "session_id": "grill_test_001",
        "status": "done",
        "user_goal": "improve dense retrieval on legal QA",
        "max_rounds": 8,
        "model": "sonnet",
        "created_at": "2026-05-25T00:00:00+00:00",
        "rounds": [],
        "extracted": {
            "root_goal_id": "rg_legal_qa",
            "domain": "retrieval",
            "node_type": "capability",
            "claim_under_test": "Method X improves nDCG@10 on legal QA over BGE-large.",
            "mandatory_baselines": [
                "current_best_known: BGE-large",
                "naive: BM25",
                "random_or_null: random ranking",
            ],
            "success_criteria": ["nDCG@10 +5%"],
            "disproof_conditions": ["within noise of BM25"],
            "goal_facets": ["performance"],
            "taste_constraints": ["claim_first"],
            "search_query_seed": "dense retrieval legal QA",
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
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        for prefix, payload in self.responses.items():
            if url.startswith(prefix):
                return payload
        raise RuntimeError(f"FakeHttp has no response for {url}")


class MarketResearchTests(unittest.TestCase):
    def _run(self, *, tmp: Path, http: _FakeHttp | None = None, pdf=None, **kwargs):
        return run_market_research(
            REPO_ROOT,
            _grilling_session(),
            run_dir=tmp,
            http_fetcher=http or _FakeHttp({}),
            pdf_fetcher=pdf,
            write_dossier_to_memory=False,
            **kwargs,
        )

    def test_arxiv_response_is_parsed_into_papers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            http = _FakeHttp(
                {
                    "http://export.arxiv.org/api/query": SAMPLE_ARXIV_XML.encode("utf-8"),
                }
            )
            pdf = _FakeHttp(
                {
                    "http://arxiv.org/pdf/": b"%PDF-1.4 fake pdf body",
                }
            )
            outcome = self._run(
                tmp=Path(tmp),
                http=http,
                pdf=pdf,
                enable_google_scholar=False,
            )

            self.assertEqual(outcome.brief["status"], "completed")
            self.assertEqual(outcome.brief["usage"]["papers_found"], 3)
            self.assertEqual(outcome.brief["usage"]["papers_downloaded"], 3)
            self.assertEqual(outcome.brief["usage"]["papers_failed"], 0)
            self.assertEqual(outcome.brief["sources_used"], ["arxiv"])
            for paper in outcome.brief["papers"]:
                validate_named_schema("reference_paper", paper)
                self.assertEqual(paper["source"], "arxiv")
                self.assertEqual(paper["download_status"], "downloaded")
                self.assertTrue(Path(paper["pdf_path"]).exists())

    def test_pdf_failure_is_recorded_as_failed_not_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            http = _FakeHttp(
                {
                    "http://export.arxiv.org/api/query": SAMPLE_ARXIV_XML.encode("utf-8"),
                }
            )

            def _broken_pdf(url: str) -> bytes:
                raise RuntimeError("connection reset")

            outcome = self._run(
                tmp=Path(tmp),
                http=http,
                pdf=_broken_pdf,
                enable_google_scholar=False,
            )
            self.assertEqual(outcome.brief["status"], "completed_with_warnings")
            self.assertEqual(outcome.brief["usage"]["papers_downloaded"], 0)
            self.assertEqual(outcome.brief["usage"]["papers_failed"], 3)
            for paper in outcome.brief["papers"]:
                self.assertEqual(paper["download_status"], "failed")
                self.assertEqual(paper["download_error"], "connection reset")

    def test_arxiv_failure_is_recorded_in_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def _broken_http(url: str) -> bytes:
                raise RuntimeError("arxiv 503")

            outcome = self._run(
                tmp=Path(tmp),
                http=_broken_http,
                pdf=_broken_http,
                enable_google_scholar=False,
            )
            self.assertEqual(outcome.brief["status"], "failed")
            self.assertIn("arxiv search failed", " ".join(outcome.brief["warnings"]))

    def test_baseline_dossier_candidate_satisfies_schema_and_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            http = _FakeHttp(
                {
                    "http://export.arxiv.org/api/query": SAMPLE_ARXIV_XML.encode("utf-8"),
                }
            )
            pdf = _FakeHttp({"http://arxiv.org/pdf/": b"%PDF-1.4 fake"})
            outcome = self._run(
                tmp=Path(tmp),
                http=http,
                pdf=pdf,
                enable_google_scholar=False,
            )

            self.assertIsNone(outcome.dossier["selected"])
            decisions = {c["decision"] for c in outcome.dossier["candidates_index"]}
            self.assertEqual(decisions, {"unqualified"})
            self.assertEqual(len(outcome.dossier["candidates_index"]), 3)
            supported = {
                support
                for source in outcome.dossier["source_index"]
                for support in source["supports"]
            }
            retrieved_candidate_ids = {
                candidate["id"]
                for candidate in outcome.dossier["candidates_index"]
                if "placeholder" not in candidate["id"]
            }
            self.assertLessEqual(retrieved_candidate_ids, supported)
            validate_named_schema("baseline_dossier", outcome.dossier)
            self.assertTrue(
                outcome.brief["baseline_dossier_id"].startswith("bd_grill_test_001_")
            )

    def test_discovery_preserves_later_papers_as_selectable_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            http = _FakeHttp(
                {"http://export.arxiv.org/api/query": SAMPLE_ARXIV_XML.encode("utf-8")}
            )
            pdf = _FakeHttp({"http://arxiv.org/pdf/": b"%PDF-1.4 fake"})
            outcome = self._run(
                tmp=Path(tmp), http=http, pdf=pdf, enable_google_scholar=False
            )

            candidates = outcome.dossier["candidates_index"]
            second_paper_candidate = candidates[1]
            self.assertIn("BM25 Baseline Revisited", second_paper_candidate["method"])
            self.assertNotEqual(second_paper_candidate["id"], candidates[0]["id"])
            supporting_sources = [
                source["id"]
                for source in outcome.dossier["source_index"]
                if second_paper_candidate["id"] in source["supports"]
            ]
            self.assertTrue(supporting_sources)
            self.assertEqual(second_paper_candidate["decision"], "unqualified")

    def test_write_to_memory_creates_dossier_files_that_load_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            http = _FakeHttp(
                {
                    "http://export.arxiv.org/api/query": SAMPLE_ARXIV_XML.encode("utf-8"),
                }
            )
            pdf = _FakeHttp({"http://arxiv.org/pdf/": b"%PDF-1.4 fake"})
            outcome = run_market_research(
                repo,
                _grilling_session(),
                run_dir=Path(tmp) / "run",
                http_fetcher=http,
                pdf_fetcher=pdf,
                enable_google_scholar=False,
                write_dossier_to_memory=True,
            )

            dossier_id = outcome.dossier["id"]
            loaded = load_baseline_dossier(repo, dossier_id)
            self.assertEqual(loaded["id"], dossier_id)
            validate_baseline_dossier(repo, loaded)

    def test_google_scholar_captcha_is_warned_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            http = _FakeHttp(
                {
                    "http://export.arxiv.org/api/query": SAMPLE_ARXIV_XML.encode("utf-8"),
                    "https://scholar.google.com/scholar": b"Our systems have detected unusual traffic",
                }
            )
            pdf = _FakeHttp({"http://arxiv.org/pdf/": b"%PDF-1.4 fake"})
            outcome = self._run(
                tmp=Path(tmp),
                http=http,
                pdf=pdf,
                enable_google_scholar=True,
            )
            self.assertEqual(outcome.brief["status"], "completed_with_warnings")
            self.assertIn("google_scholar", outcome.brief["sources_used"])
            warnings_text = " ".join(outcome.brief["warnings"])
            self.assertIn("google scholar search failed", warnings_text)

    def test_brief_path_is_written_and_schema_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            http = _FakeHttp(
                {
                    "http://export.arxiv.org/api/query": SAMPLE_ARXIV_XML.encode("utf-8"),
                }
            )
            pdf = _FakeHttp({"http://arxiv.org/pdf/": b"%PDF-1.4 fake"})
            outcome = self._run(
                tmp=Path(tmp),
                http=http,
                pdf=pdf,
                enable_google_scholar=False,
            )
            brief_path = Path(outcome.brief["brief_path"])
            self.assertTrue(brief_path.exists())
            saved = json.loads(brief_path.read_text())
            validate_named_schema("market_research_brief", saved)


if __name__ == "__main__":
    unittest.main()


def test_crossref_discovery_preserves_metadata_without_inventing_an_abstract(tmp_path):
    urls = []

    def fetch(url):
        urls.append(url)
        return json.dumps({"message": {"items": [{
            "DOI": "10.1234/example", "title": ["A retrieved title"],
            "author": [{"given": "A", "family": "Researcher"}],
            "published": {"date-parts": [[2024, 2]]},
        }]}}).encode()

    result = run_market_research(
        REPO_ROOT, _grilling_session(), run_dir=tmp_path,
        search_provider="crossref", http_fetcher=fetch,
        enable_google_scholar=False, enable_claude_websearch=False,
        write_dossier_to_memory=False,
    )
    assert len(urls) == 1 and urls[0].startswith("https://api.crossref.org/works?")
    paper = result.papers[0]
    assert paper["source"] == "crossref"
    assert paper["url"] == "https://doi.org/10.1234/example"
    assert paper["authors"] == ["A Researcher"]
    assert paper["year"] == 2024 and paper["abstract"] is None
    assert result.brief["sources_used"] == ["crossref"]
