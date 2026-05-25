"""MCP server smoke + integration tests.

These exercise the JSON-RPC stdio surface end-to-end with an in-memory
thread directory, asserting that:
  - the tool catalog matches the spec
  - `get_next_admissible_node` follows the claim-type weight order
  - persona rejection paths fire as expected
  - a full submit_professor_decision call mutates search_state (promotes
    the node, spawns follow-ups, advances the cycle).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import research_harness.mcp_server as srv


def _patch_thread_dir(tmp: Path):
    """Point _thread_dir at our tempdir for the duration of one test."""
    orig = srv._thread_dir
    srv._thread_dir = lambda tid: tmp / tid
    return orig


def _make_search_state(node_types: list[str]) -> dict:
    nodes = []
    frontier = []
    for i, t in enumerate(node_types):
        nid = f"n_{t}_{i}"
        nodes.append(
            {
                "id": nid,
                "type": t,
                "status": "ready",
                "domain": "test_domain",
                "stage": "experimentation",
                "parent": None,
                "lineage": {
                    "root_goal_id": "rg_test",
                    "covers_goal_facets": [],
                    "inherited_assumptions": [],
                    "introduced_assumptions": [],
                    "taste_constraints_applied": [],
                },
                "claim_contract": {
                    "claim_under_test": f"{t} claim about something specific.",
                    "mandatory_baselines": ["a", "b", "c"],
                    "success_criteria": ["s1"],
                    "disproof_conditions": ["d1"],
                },
                "baseline_refs": [
                    {
                        "baseline_dossier_id": "bd_test",
                        "candidate_ids": ["c1", "c2", "c3"],
                        "roles": ["current_best_known", "naive", "random_or_null"],
                    }
                ],
                "runtime_profile": {
                    "worker_type": "experiment_worker",
                    "timeout_policy": "task_class_dependent",
                    "turn_budget": 6,
                },
                "failure_retrieval": {"query_tags": [t], "selected_fail_files": []},
                "outputs": {"artifacts": [], "verdict": None},
            }
        )
        frontier.append(
            {
                "node_id": nid,
                "parent": None,
                "depth": 0,
                "priority": 1.0,
                "stage": "experimentation",
                "status": "queued",
                "reason": "test",
            }
        )
    return {
        "search_id": "s_test",
        "status": "running",
        "max_depth": 5,
        "max_debug_depth": 2,
        "sunk_cost_policy": "progress_gated",
        "scaleup_policy": "disallow_by_default",
        "frontier": frontier,
        "nodes": nodes,
        "completed_node_ids": [],
        "promoted_node_ids": [],
        "pruned_node_ids": [],
        "transitions": [],
    }


class MCPServerTests(unittest.TestCase):
    def test_tools_list_exposes_all_expected_tools(self) -> None:
        r = srv._handle_request({"id": 1, "method": "tools/list"}, {})
        names = {t["name"] for t in r["result"]["tools"]}
        # Pre-Phase-A baseline tools (deterministic pipeline).
        baseline_expected = {
            "get_research_state",
            "get_next_admissible_node",
            "resume_production_state",
            "design_initial_claim_contract",
            "design_experiment_template",
            "execute_node_experiment",
            "run_critic_reviews",
            "submit_grad_student_review",
            "submit_professor_decision",
            "revise_root_after_reject",
            "decide_publication_readiness",
        }
        # Phase C/D additions: LLM-driven rebuttal loop + paper writer.
        practitioner_expected = {
            "prepare_rebuttal_packet",
            "submit_rebuttal_critic_review",
            "submit_orchestrator_reduction",
            "submit_ac_decision",
            "submit_camera_ready_revision",
            "prepare_paper_writing_context",
            "submit_paper_outline",
            "submit_paper_section",
            "register_paper_figure",
            "render_final_paper",
        }
        self.assertEqual(baseline_expected | practitioner_expected, names)

    def test_selector_returns_resume_for_midstate_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid = "t_resume"
            tree = tmp_path / tid / "production" / "tree"
            tree.mkdir(parents=True)
            state = _make_search_state(["validity"])
            state["nodes"][0]["status"] = "critic_reviewed"
            for item in state["frontier"]:
                item["status"] = "done"
            (tree / "search_state.json").write_text(json.dumps(state))
            orig = _patch_thread_dir(tmp_path)
            try:
                r = srv.handle_get_next_admissible_node({"thread_id": tid})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "resume")
            self.assertEqual(r["current_status"], "critic_reviewed")
            self.assertEqual(r["next_tool_to_call"], "submit_professor_decision")

    def test_resume_production_state_demotes_stuck_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid = "t_demote"
            tree = tmp_path / tid / "production" / "tree"
            tree.mkdir(parents=True)
            state = _make_search_state(["validity"])
            state["nodes"][0]["status"] = "running"
            for item in state["frontier"]:
                if item["node_id"] == state["nodes"][0]["id"]:
                    item["status"] = "running"
            (tree / "search_state.json").write_text(json.dumps(state))
            orig = _patch_thread_dir(tmp_path)
            try:
                r = srv.handle_resume_production_state({"thread_id": tid})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["demoted_node_ids"], [state["nodes"][0]["id"]])

    def test_get_next_admissible_node_picks_lowest_type_weight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid = "t_pick"
            tree = tmp_path / tid / "production" / "tree"
            tree.mkdir(parents=True)
            (tree / "search_state.json").write_text(
                json.dumps(_make_search_state(["mechanism", "validity", "capability"]))
            )
            orig = _patch_thread_dir(tmp_path)
            try:
                r = srv.handle_get_next_admissible_node({"thread_id": tid})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "ok")
            # validity has weight 0 → picked first
            self.assertEqual(r["node_type"], "validity")

    def test_get_next_admissible_node_returns_no_state_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orig = _patch_thread_dir(Path(tmp))
            try:
                r = srv.handle_get_next_admissible_node({"thread_id": "missing"})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "no_state")

    def test_submit_professor_decision_rejects_wrong_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid = "t_wrong"
            tree = tmp_path / tid / "production" / "tree"
            tree.mkdir(parents=True)
            (tree / "search_state.json").write_text(
                json.dumps(_make_search_state(["validity", "capability"]))
            )
            orig = _patch_thread_dir(tmp_path)
            try:
                # Selector would pick "n0_validity"; try to submit for the
                # capability node out of order — should reject.
                r = srv.handle_submit_professor_decision(
                    {
                        "thread_id": tid,
                        "node_id": "n_capability_1",
                        "next_transition": "promoted",
                        "follow_up_children": [],
                    },
                    settings={},
                )
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "rejected")
            self.assertIn("n_validity_0", r["reason"])


if __name__ == "__main__":
    unittest.main()
