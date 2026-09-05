"""MCP server smoke + integration tests.

These exercise the JSON-RPC stdio surface with an in-memory thread directory.
They cover the tool catalog, persona rejection, authoritative-node selection,
and durable Professor decisions.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import research_harness.mcp_server as srv


def test_confirmation_reuse_is_rejected_before_predicate_execution(tmp_path):
    import shutil
    from types import SimpleNamespace
    from tests.test_thread_supervisor import _write_verified_terminal_state
    from research_harness.falsifier import compute_falsifier_result
    from research_harness.orchestrator.confirmation_use import confirmation_evidence_from_result

    tid = "thread_confirmation"
    tdir = tmp_path / "runs" / "threads" / tid
    (tdir / "production").mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "settings.json", tmp_path / "settings.json")
    _write_verified_terminal_state(tdir)
    result = json.loads((tdir / "production" / "rebuttal" / "falsifier_result.json").read_text())
    binding = {key: result[key] for key in ("contract_id", "attempt_id", "direction_id", "node_id", "manifest_id")}
    evidence = confirmation_evidence_from_result(result)
    with srv._repo_root_scope(tmp_path), mock.patch.object(
        srv, "_authoritative_strong_binding", return_value=SimpleNamespace(**binding)
    ) as active, mock.patch("research_harness.falsifier.compute_falsifier_result", wraps=compute_falsifier_result) as evaluate:
        assert srv.handle_compute_falsifier_result({"thread_id": tid, "evidence": evidence})["status"] == "ok"
        evaluate.reset_mock()
        active.return_value = SimpleNamespace(**{**binding, "attempt_id": "another_attempt"})
        rejected = srv.handle_compute_falsifier_result({"thread_id": tid, "evidence": evidence})
        assert rejected["status"] == "rejected"
        assert "consumed" in rejected["reason"]
        evaluate.assert_not_called()


def test_paper_render_rechecks_evidence_after_attestation(tmp_path):
    from tests.test_sakana_paper import _build_inputs
    from tests.test_thread_supervisor import _write_verified_terminal_state

    tid = "thread_publication"
    tdir = tmp_path / "runs" / "threads" / tid
    (tdir / "production").mkdir(parents=True)
    _write_verified_terminal_state(tdir)
    publication = tdir / "production" / "publication"
    publication.mkdir()
    _, _, _, figures, outline, sections = _build_inputs(publication)
    from research_harness.publishing.figures import render_figure, figure_source_projection
    import hashlib

    report_path = next((tdir / "production" / "tree" / "nodes").glob("*/worker_report.json"))
    report = json.loads(report_path.read_text())
    data_spec = {"main_metric_key": "worker_report.metrics.score"}
    artifact = render_figure("f_baseline", "baseline_bars", "Baselines", data_spec, report, {}, publication / "figures")
    projection = figure_source_projection("baseline_bars", data_spec, report, {})
    figures["f_baseline"].update({"data_spec": data_spec, "source_digest": projection.digest,
                                "source_paths": list(projection.source_paths),
                                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()})
    for section in sections.values():
        section["evidence_anchors"] = ["worker_report.metrics.score=1.1"]
    sections["introduction"]["citation_source_ids"] = ["fixture_source"]
    sections["introduction"]["prose_html"] += '<a href="#ref_fixture_source">Fixture citation</a>'
    market = tdir / "market"
    market.mkdir()
    (market / "market_research_brief.json").write_text(json.dumps({"papers": [{
        "id": "fixture_source", "title": "Test-only bibliographic record",
        "authors": ["Fixture Author"], "year": 2026,
        "url": "https://example.org/fixture", "source": "test_fixture",
    }]}))
    drafts = publication / "_drafts"
    (drafts / "sections").mkdir(parents=True)
    (drafts / "outline.json").write_text(json.dumps(outline))
    (drafts / "figures.json").write_text(json.dumps(figures))
    for sid, section in sections.items():
        (drafts / "sections" / f"{sid}.json").write_text(json.dumps(section))
    with srv._repo_root_scope(tmp_path):
        missing_references = srv.handle_render_final_paper({"thread_id": tid})
        assert missing_references["status"] == "rejected"
        assert "references" in missing_references["reason"]
        for sid in ("abstract", "references"):
            outline["section_outline"].append({"section_id": sid, "title": sid.title()})
            (drafts / "sections" / f"{sid}.json").write_text(
                json.dumps({"prose_html": f"<p>Fixture {sid}.</p>"})
            )
        (drafts / "outline.json").write_text(json.dumps(outline))
        method_file = drafts / "sections" / "method.json"
        valid_method = method_file.read_text()
        for bad_anchor in ("worker_report.metrics.missing", "worker_report.metrics.score=999"):
            bad_section = json.loads(valid_method)
            bad_section["evidence_anchors"] = [bad_anchor]
            method_file.write_text(json.dumps(bad_section))
            rejection = srv.handle_render_final_paper({"thread_id": tid})
            assert rejection["status"] == "rejected"
            assert "anchor" in rejection["reason"]
        method_file.write_text(valid_method)
        for extra in ("<p>/home/author/private/run.json</p>", '<a href="#ref_missing">Missing source</a>'):
            bad_section = json.loads(valid_method)
            bad_section["prose_html"] += extra
            method_file.write_text(json.dumps(bad_section))
            assert srv.handle_render_final_paper({"thread_id": tid})["status"] == "rejected"
        method_file.write_text(valid_method)
        assert srv.handle_render_final_paper({"thread_id": tid})["status"] == "ok"
        from research_harness.thread_supervisor import is_terminal

        assert is_terminal(tmp_path, tid)[0]
        for artifact in (publication / "paper.html", drafts / "outline.json",
                         publication / "figures" / "f_baseline.png",
                         tdir / "production" / "reorientation" / "confirmation_use.json",
                         publication / "publication_receipt.json"):
            original = artifact.read_bytes()
            artifact.unlink()
            assert not is_terminal(tmp_path, tid)[0]
            artifact.write_bytes(original + b"tampered")
            assert not is_terminal(tmp_path, tid)[0]
            artifact.write_bytes(original)
            assert is_terminal(tmp_path, tid)[0]
        report_path = next((tdir / "production" / "tree" / "nodes").glob("*/worker_report.json"))
        report = json.loads(report_path.read_text())
        report["metrics"]["unverified_change"] = 999.0
        report_path.write_text(json.dumps(report))
        paper = publication / "paper.html"
        paper.write_text("previous verified manuscript")
        result = srv.handle_render_final_paper({"thread_id": tid})
    assert result["status"] == "rejected"
    assert "strong" in result["reason"].lower()
    assert paper.read_text() == "previous verified manuscript"


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
        core_expected = {
            "get_research_state",
            "get_next_admissible_node",
            "advance_research",
            "resume_production_state",
            "design_initial_claim_contract",
            "design_experiment_template",
            "execute_node_experiment",
            "run_critic_reviews",
            "submit_grad_student_review",
            "submit_professor_decision",
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
        dual_gate_expected = {
            "submit_professor_user_goal_attestation",
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
        retired = {
            "propose_alternative_root_directions",
            "render_honest_failure_paper",
            "revise_root_after_reject",
            "seed_alternative_root_formulation",
            "seed_forest_from_connector",
            "select_alternative_root",
            "select_strongest_survivor",
            "snapshot_root_terminal",
        }
        operator_prompt_expected = {
            "enqueue_operator_prompt",
            "get_pending_operator_response",
        }
        self.assertEqual(
            core_expected | practitioner_expected | dual_gate_expected
            | envelope_expected | operator_prompt_expected,
            names,
        )
        self.assertTrue(retired.isdisjoint(names))

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
                with mock.patch.object(
                    srv,
                    "_authoritative_active_node_id",
                    return_value=state["nodes"][0]["id"],
                ):
                    r = srv.handle_get_next_admissible_node({"thread_id": tid})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "retry_evidence")
            self.assertEqual(r["node_id"], state["nodes"][0]["id"])
            self.assertEqual(r["next_tool_to_call"], "design_experiment_template")

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

    def test_get_next_admissible_node_picks_only_authoritative_node(self) -> None:
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
                with mock.patch.object(
                    srv,
                    "_authoritative_active_node_id",
                    return_value="n_capability_2",
                ):
                    r = srv.handle_get_next_admissible_node({"thread_id": tid})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["node_type"], "capability")

    def test_get_next_admissible_node_bootstraps_blind_engine_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orig = _patch_thread_dir(Path(tmp))
            try:
                r = srv.handle_get_next_admissible_node({"thread_id": "missing"})
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "hard_external_block")
            self.assertEqual(r["code"], "operator_scope_conflict")

    # --- cycle-1 skill-isolation (bar-sanity) gate -----------------------
    _PRED = {"metric": "excess_return", "op": ">=", "threshold": 0.10}

    def test_bar_sanity_gate_inactive_when_flag_off(self) -> None:
        # require_skill_isolation off → gate is a no-op, selector picks normally.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _setup_skill_iso_thread(tmp_path, "t_bs_off", flag=False, predicate=self._PRED)
            orig = _patch_thread_dir(tmp_path)
            try:
                with mock.patch.object(
                    srv,
                    "_authoritative_active_node_id",
                    return_value="n_validity_0",
                ):
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
                with mock.patch.object(
                    srv,
                    "_authoritative_active_node_id",
                    return_value="n_validity_0",
                ):
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
                with mock.patch.object(
                    srv,
                    "_authoritative_active_node_id",
                    return_value="n_validity_0",
                ):
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
                with mock.patch.object(
                    srv,
                    "_authoritative_active_node_id",
                    return_value="n_validity_0",
                ):
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
                with mock.patch.object(
                    srv,
                    "_authoritative_active_node_id",
                    return_value="n_validity_0",
                ):
                    r = srv.handle_submit_professor_decision(
                        {
                            "thread_id": tid,
                            "node_id": "n_capability_1",
                            "next_transition": "promoted",
                        },
                        settings={},
                    )
            finally:
                srv._thread_dir = orig
            self.assertEqual(r["status"], "rejected")
            self.assertIn("authoritative blind node", r["reason"])


if __name__ == "__main__":
    unittest.main()
