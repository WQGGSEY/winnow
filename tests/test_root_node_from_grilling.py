from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_harness.orchestrator.root_node_from_grilling import (
    PLACEHOLDER_BASELINE_DOSSIER_ID,
    attach_market_research_dossier,
    build_root_node_from_grilling,
    has_placeholder_baseline,
    write_root_node,
)
from research_harness.orchestrator.validation import (
    ValidationError,
    validate_node_invariants,
)
from research_harness.schemas.validator import validate_named_schema


def _sample_grilling_session() -> dict:
    return {
        "session_id": "grill_abc123",
        "status": "done",
        "user_goal": "improve dense retrieval on legal QA",
        "max_rounds": 8,
        "model": "sonnet",
        "created_at": "2026-05-25T00:00:00+00:00",
        "rounds": [],
        "extracted": {
            "root_goal_id": "rg_legal_qa_retrieval",
            "domain": "retrieval",
            "node_type": "capability",
            "claim_under_test": "Method X improves nDCG@10 on legal QA over BGE-large.",
            "mandatory_baselines": [
                "current_best_known: BGE-large",
                "naive: BM25",
                "random_or_null: random ranking",
            ],
            "success_criteria": ["nDCG@10 +5% on LegalBench retrieval"],
            "disproof_conditions": ["nDCG@10 within noise of BM25"],
            "goal_facets": ["performance", "interpretability"],
            "taste_constraints": ["claim_first", "necessity_check"],
            "search_query_seed": "dense retrieval legal QA",
        },
        "usage_estimate": {
            "rounds_used": 3,
            "total_cost_usd": 0.012,
            "total_input_tokens": 1500,
            "total_output_tokens": 220,
        },
    }


class RootNodeFromGrillingTests(unittest.TestCase):
    def test_builds_schema_valid_node_with_placeholder_dossier(self) -> None:
        session = _sample_grilling_session()
        node = build_root_node_from_grilling(session)

        validate_named_schema("node", node)
        validate_node_invariants(node)
        self.assertEqual(node["id"], "n_legal_qa_retrieval_root")
        self.assertEqual(node["type"], "capability")
        self.assertEqual(node["status"], "ready")
        self.assertEqual(node["domain"], "retrieval")
        self.assertEqual(node["parent"], None)
        self.assertEqual(node["lineage"]["root_goal_id"], "rg_legal_qa_retrieval")
        self.assertIn("performance", node["lineage"]["covers_goal_facets"])
        self.assertEqual(
            node["claim_contract"]["claim_under_test"],
            session["extracted"]["claim_under_test"],
        )
        self.assertEqual(
            node["baseline_refs"][0]["baseline_dossier_id"],
            PLACEHOLDER_BASELINE_DOSSIER_ID,
        )
        self.assertEqual(
            sorted(node["baseline_refs"][0]["roles"]),
            ["current_best_known", "naive", "random_or_null"],
        )
        self.assertTrue(has_placeholder_baseline(node))
        self.assertIn("retrieval", node["failure_retrieval"]["query_tags"])

    def test_explicit_baseline_dossier_replaces_placeholder(self) -> None:
        session = _sample_grilling_session()
        node = build_root_node_from_grilling(
            session,
            baseline_dossier_id="bd_real_dossier",
            candidate_ids=["c1", "c2", "c3"],
        )
        self.assertEqual(
            node["baseline_refs"][0]["baseline_dossier_id"],
            "bd_real_dossier",
        )
        self.assertEqual(node["baseline_refs"][0]["candidate_ids"], ["c1", "c2", "c3"])
        self.assertFalse(has_placeholder_baseline(node))

    def test_attach_market_research_dossier_replaces_placeholder(self) -> None:
        session = _sample_grilling_session()
        node = build_root_node_from_grilling(session)

        patched = attach_market_research_dossier(
            node,
            baseline_dossier_id="bd_arxiv_20260525",
            candidate_ids=["c_bge_large", "c_bm25", "c_random"],
        )
        self.assertFalse(has_placeholder_baseline(patched))
        self.assertEqual(
            patched["baseline_refs"][0]["baseline_dossier_id"],
            "bd_arxiv_20260525",
        )
        # original not mutated
        self.assertTrue(has_placeholder_baseline(node))

    def test_attach_fails_when_no_placeholder(self) -> None:
        session = _sample_grilling_session()
        node = build_root_node_from_grilling(
            session, baseline_dossier_id="bd_already_real"
        )
        with self.assertRaises(ValidationError):
            attach_market_research_dossier(
                node,
                baseline_dossier_id="bd_other",
                candidate_ids=[],
            )

    def test_grilling_with_minimal_extracted_still_passes_invariants(self) -> None:
        session = _sample_grilling_session()
        session["extracted"]["goal_facets"] = []
        session["extracted"]["taste_constraints"] = []
        node = build_root_node_from_grilling(session)
        validate_node_invariants(node)

    def test_write_root_node_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "grilling_session.json"
            output_path = Path(tmp) / "root_node.json"
            session_path.write_text(json.dumps(_sample_grilling_session()))

            node = write_root_node(session_path, output_path)
            self.assertTrue(output_path.exists())
            loaded = json.loads(output_path.read_text())
            self.assertEqual(node, loaded)


if __name__ == "__main__":
    unittest.main()
