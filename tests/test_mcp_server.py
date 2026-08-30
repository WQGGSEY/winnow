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
                    "claim_under_test": "A shared problem-level test claim.",
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


def _setup_skill_iso_thread(
    tmp_path: Path,
    tid: str,
    *,
    flag: bool,
    predicate: dict | None,
    bar_sanity: dict | None = None,
    node_types: list[str] | None = None,
) -> None:
    """Lay down a thread dir for the cycle-1 bar-sanity gate: a search_state
    (so the selector would otherwise return a node) plus a feasibility envelope
    that optionally sets require_skill_isolation + the deployment predicate, and
    optionally a recorded bar_sanity.json."""
    prod = tmp_path / tid / "production"
    tree = prod / "tree"
    tree.mkdir(parents=True)
    (tree / "search_state.json").write_text(
        json.dumps(_make_search_state(node_types or ["validity"]))
    )
    env: dict = {"external_falsifier": {}}
    if predicate is not None:
        env["external_falsifier"]["predicate"] = predicate
    if flag:
        env["execution_constraints"] = {"require_skill_isolation": True}
    (prod / "feasibility_envelope.json").write_text(json.dumps(env))
    if bar_sanity is not None:
        (prod / "bar_sanity.json").write_text(json.dumps(bar_sanity))


class MCPServerTests(unittest.TestCase):
    def test_tools_list_exposes_all_expected_tools(self) -> None:
        r = srv._handle_request({"id": 1, "method": "tools/list"}, {})
        names = {t["name"] for t in r["result"]["tools"]}
        design_tool = next(
            tool
            for tool in r["result"]["tools"]
            if tool["name"] == "design_experiment_template"
        )
        self.assertIn("EVIDENCE OUTPUT CONTRACT", design_tool["description"])
        self.assertIn("top-level `baselines`", design_tool["description"])
        self.assertIn("primary_dataset.relative_path", design_tool["description"])
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
        # Phase F additions: dual-gate + fan-out + honest-failure exit.
        dual_gate_expected = {
            "submit_professor_user_goal_attestation",
            "propose_alternative_root_directions",
            "select_alternative_root",
            "render_honest_failure_paper",
            "submit_bar_sanity_result",
        }
        # PR7: feasibility envelope tool.
        # ADR 0006: external-falsifier gate adds compute_falsifier_result.
        # ADR 0008: construct-adversary (Axis 1) adds pin_frozen_question +
        # submit_construct_adversary_report.
        envelope_expected = {
            "submit_feasibility_envelope",
            "compute_falsifier_result",
            "pin_frozen_question",
            "submit_construct_adversary_report",
        }
        # Multi-root tournament: alternative root formulations + the ADR 0012
        # connector->production forest handoff + forest select-strongest +
        # snapshot-and-reset per-root terminal storage.
        multi_root_expected = {
            "seed_alternative_root_formulation",
            "seed_forest_from_connector",
            "select_strongest_survivor",
            "snapshot_root_terminal",
        }
        # Hands-free: operator-prompt channel for auto-resolver escalations.
        operator_prompt_expected = {
            "enqueue_operator_prompt",
            "get_pending_operator_response",
        }
        self.assertEqual(
            baseline_expected | practitioner_expected | dual_gate_expected
            | envelope_expected | multi_root_expected | operator_prompt_expected,
            names,
        )

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

    # --- cycle-1 skill-isolation (bar-sanity) gate -----------------------
    _PRED = {"metric": "excess_return", "op": ">=", "threshold": 0.10}

    def test_bar_sanity_gate_inactive_when_flag_off(self) -> None:
        # require_skill_isolation off → gate is a no-op, selector picks normally.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _setup_skill_iso_thread(tmp_path, "t_bs_off", flag=False, predicate=self._PRED)
            orig = _patch_thread_dir(tmp_path)
            try:
                r = srv.handle_get_next_admissible_node({"thread_id": "t_bs_off"})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["node_type"], "validity")

    def test_bar_sanity_gate_required_when_no_submission(self) -> None:
        # flag on, no bar_sanity yet → block the search, demand the baseline.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _setup_skill_iso_thread(tmp_path, "t_bs_req", flag=True, predicate=self._PRED)
            orig = _patch_thread_dir(tmp_path)
            try:
                r = srv.handle_get_next_admissible_node({"thread_id": "t_bs_req"})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "bar_sanity_required")
            self.assertEqual(r["next_tool_to_call"], "submit_bar_sanity_result")

    def test_bar_sanity_gate_blocks_when_exposure_clears_bar(self) -> None:
        # no-skill exposure +0.12 clears bar (>= +0.10) → bar is gameable → block.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _setup_skill_iso_thread(
                tmp_path, "t_bs_broken", flag=True, predicate=self._PRED,
                bar_sanity={"no_skill_exposure_metric": 0.12, "detail": "2x lev hold"},
            )
            orig = _patch_thread_dir(tmp_path)
            try:
                r = srv.handle_get_next_admissible_node({"thread_id": "t_bs_broken"})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "bar_broken")

    def test_bar_sanity_gate_passes_when_exposure_below_bar(self) -> None:
        # no-skill exposure +0.03 does NOT clear bar → bar isolates skill → proceed.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _setup_skill_iso_thread(
                tmp_path, "t_bs_ok", flag=True, predicate=self._PRED,
                bar_sanity={"no_skill_exposure_metric": 0.03, "detail": "2x lev hold"},
            )
            orig = _patch_thread_dir(tmp_path)
            try:
                r = srv.handle_get_next_admissible_node({"thread_id": "t_bs_ok"})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["node_type"], "validity")

    def test_submit_bar_sanity_result_persists_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _setup_skill_iso_thread(tmp_path, "t_bs_submit", flag=True, predicate=self._PRED)
            orig = _patch_thread_dir(tmp_path)
            try:
                r = srv.handle_submit_bar_sanity_result(
                    {
                        "thread_id": "t_bs_submit",
                        "no_skill_exposure_metric": 0.12,
                        "detail": "2x leverage buy-and-hold sweep",
                    }
                )
                bs = json.loads(
                    (tmp_path / "t_bs_submit" / "production" / "bar_sanity.json").read_text()
                )
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "accepted")
            self.assertEqual(bs["no_skill_exposure_metric"], 0.12)
            self.assertEqual(r["bar_sanity"]["state"], "broken")

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
