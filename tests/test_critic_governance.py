from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from research_harness.critics.governance import CriticGovernanceError, select_critics
from research_harness.orchestrator.demo import _demo_node


REPO_ROOT = Path(__file__).resolve().parents[1]


class CriticGovernanceTests(unittest.TestCase):
    def test_promotion_routing_applies_always_node_domain_and_stage_critics(self) -> None:
        node = _demo_node()
        bundle = select_critics(REPO_ROOT, node)
        critic_ids = {critic["critic_id"] for critic in bundle["applied_critics"]}

        self.assertIn("invariants_v1", critic_ids)
        self.assertIn("runtime_safety_v1", critic_ids)
        self.assertIn("capability_strict_v1", critic_ids)
        self.assertIn("nested_agent_risk_v1", critic_ids)
        self.assertIn("paper_story_v1", critic_ids)

    def test_rebuttal_stage_applies_rebuttal_critics(self) -> None:
        node = _demo_node()
        node["stage"] = "rebuttal"
        bundle = select_critics(REPO_ROOT, node)
        critic_ids = {critic["critic_id"] for critic in bundle["applied_critics"]}

        self.assertIn("whole_case_validity_v1", critic_ids)
        self.assertIn("overclaim_detector_v1", critic_ids)
        self.assertIn("publication_readiness_v1", critic_ids)

    def test_node_cannot_request_skipping_critics(self) -> None:
        node = copy.deepcopy(_demo_node())
        node["skip_critics"] = ["invariants_v1"]

        with self.assertRaisesRegex(CriticGovernanceError, "skip_critics"):
            select_critics(REPO_ROOT, node)

    def test_duplicate_critic_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(REPO_ROOT / "critics", tmp_root / "critics")
            duplicate = tmp_root / "critics" / "by_stage" / "promotion" / "duplicate.md"
            duplicate.write_text(
                "---\n"
                "critic_profile_id: invariants_v1\n"
                "override_policy: taste_only\n"
                "---\n"
                "# Duplicate\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CriticGovernanceError, "duplicate"):
                select_critics(tmp_root, _demo_node())


if __name__ == "__main__":
    unittest.main()

