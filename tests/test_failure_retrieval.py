from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from research_harness.memory.failure_retrieval import (
    FailureRetrievalError,
    format_failure_summaries,
    retrieve_failure_summaries,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class FailureRetrievalTests(unittest.TestCase):
    def _temp_repo(self, tmp: str) -> Path:
        repo = Path(tmp)
        (repo / "memory").mkdir()
        shutil.copytree(REPO_ROOT / "memory" / "failures", repo / "memory" / "failures")
        return repo

    def test_retrieves_relevant_failures_by_query_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._temp_repo(tmp)

            summaries = retrieve_failure_summaries(
                repo,
                query_tags=["agent_harness", "subscription_oauth"],
                selected_fail_files=[],
                top_k=2,
            )

            self.assertEqual(len(summaries), 2)
            self.assertTrue(all(summary.score > 0 for summary in summaries))
            formatted = format_failure_summaries(summaries)
            self.assertIn("relevant_failures", formatted)
            self.assertIn("invalid_experiment", formatted)

    def test_explicit_selected_file_is_included_first(self) -> None:
        selected = "invalid_experiment/n_demo_001__invalid_experiment__509089f891.md"
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._temp_repo(tmp)

            summaries = retrieve_failure_summaries(
                repo,
                query_tags=[],
                selected_fail_files=[selected],
                top_k=1,
            )

            self.assertEqual(summaries[0].file, selected)
            self.assertTrue(summaries[0].explicit)

    def test_unindexed_selected_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._temp_repo(tmp)

            with self.assertRaisesRegex(FailureRetrievalError, "listed in failure index"):
                retrieve_failure_summaries(
                    repo,
                    query_tags=[],
                    selected_fail_files=["invalid_experiment/not_listed.md"],
                    top_k=1,
                )

    def test_no_match_returns_compact_none_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._temp_repo(tmp)

            summaries = retrieve_failure_summaries(
                repo,
                query_tags=["unrelated_tag"],
                selected_fail_files=[],
                top_k=5,
            )

            self.assertEqual(summaries, [])
            self.assertIn("relevant_failures: none", format_failure_summaries(summaries))


if __name__ == "__main__":
    unittest.main()
