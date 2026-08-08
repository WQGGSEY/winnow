from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_harness.memory.failure_retrieval import (
    FailureRetrievalError,
    format_failure_summaries,
    load_failure_index,
    retrieve_failure_summaries,
)


PACKAGED_INVALID_EXPERIMENT = (
    "invalid_experiment/n_demo_001__invalid_experiment__01330e757f.md"
)


class FailureRetrievalTests(unittest.TestCase):
    def _temp_repo(self, tmp: str) -> Path:
        return Path(tmp)

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

    def test_packaged_failures_are_retrievable_without_operator_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summaries = retrieve_failure_summaries(
                Path(tmp),
                query_tags=["agent_harness", "subscription_oauth"],
                selected_fail_files=[],
                top_k=2,
            )

        self.assertEqual(len(summaries), 2)
        self.assertTrue(all(summary.category == "invalid_experiment" for summary in summaries))

    def test_operator_record_shadows_packaged_record_without_duplicate_index_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            failures_dir = repo / "memory" / "failures"
            record_path = failures_dir / PACKAGED_INVALID_EXPERIMENT
            record_path.parent.mkdir(parents=True)
            (failures_dir / "index.yaml").write_text(
                """categories:
  invalid_experiment:
    description: "operator-local failures"
    files:
      - "invalid_experiment/n_demo_001__invalid_experiment__01330e757f.md"
      - "invalid_experiment/n_demo_001__invalid_experiment__01330e757f.md"
""",
                encoding="utf-8",
            )
            record_path.write_text(
                """---
id: local-shadow
category: invalid_experiment
tags: [operator_local]
lesson: "Operator-local lesson wins."
---
# Local Failure

## Reason

Operator-local reason wins.
""",
                encoding="utf-8",
            )

            index = load_failure_index(repo)
            files = index["categories"]["invalid_experiment"]["files"]
            summaries = retrieve_failure_summaries(
                repo,
                query_tags=[],
                selected_fail_files=[PACKAGED_INVALID_EXPERIMENT],
                top_k=1,
            )

        self.assertEqual(files.count(PACKAGED_INVALID_EXPERIMENT), 1)
        self.assertEqual(summaries[0].lesson, "Operator-local lesson wins.")
        self.assertEqual(summaries[0].reason, "Operator-local reason wins.")

    def test_explicit_selected_file_is_included_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._temp_repo(tmp)

            summaries = retrieve_failure_summaries(
                repo,
                query_tags=[],
                selected_fail_files=[PACKAGED_INVALID_EXPERIMENT],
                top_k=1,
            )

            self.assertEqual(summaries[0].file, PACKAGED_INVALID_EXPERIMENT)
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
