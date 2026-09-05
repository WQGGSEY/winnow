"""Tests for thread_supervisor — hands-free e2e daemon.

We don't start real Codex subprocesses in tests. We exercise:
  - is_terminal() against various production_run_summary.json shapes
  - mcp_idle_seconds() against various mtime patterns
  - build_resume_prompt() output shape
  - SupervisorLock contention + stale lock recovery
  - watch_thread() loop with mocked spawn (verify terminate, idle-spawn,
    max-cycles, SIGINT)
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from research_harness import thread_supervisor as ts


def _make_thread(repo: Path, tid: str) -> Path:
    tdir = repo / "runs" / "threads" / tid
    (tdir / "production").mkdir(parents=True, exist_ok=True)
    return tdir


def _write_verified_terminal_state(tdir: Path, *, pending_baselines: bool = False) -> None:
    from research_harness.acquisition import (
        AcquisitionBudget,
        AcquisitionComplete,
        PublicAcquisition,
        make_acquisition_command,
    )
    from research_harness.orchestrator.blind_reorientation import (
        GoalAchieved,
        ReorientationState,
        serialize_reorientation_state,
    )
    from research_harness.orchestrator.goal_contract import (
        BaselineEvidence,
        FalsifierPredicate,
        GoalContractCompilerInput,
        HoldoutRequirement,
        compile_goal_contract,
        project_research_goal,
        serialize_goal_contract,
    )
    from research_harness.orchestrator.blind_sequential_research import (
        strong_result_receipt_sha256,
    )
    from research_harness.orchestrator.direction_generation import (
        make_direction_draft,
        make_direction_fingerprint,
    )

    thread = {
        "thread_id": f"thread_{tdir.name}",
        "title": "Strong strategy test",
        "created_at": "2026-08-31T00:00:00+00:00",
        "updated_at": "2026-08-31T00:00:00+00:00",
        "current_phase": "production",
        "phase_status": "running",
        "outcome": None,
        "user_goal": "Find a strategy that beats baseline B.",
    }
    grilling = {
        "user_goal": thread["user_goal"],
        "extracted": {
            "claim_under_test": thread["user_goal"],
            "mandatory_baselines": ["baseline B"],
            "success_criteria": ["beat baseline B"],
            "disproof_conditions": ["does not beat baseline B"],
        },
    }
    envelope = {
        "operator_intent": {"target_deploy_grade_scope": "directional"},
        "external_falsifier": {
            "kind": "real_holdout",
            "holdout_source_id": "holdout",
            "predicate": {"metric": "score", "op": ">=", "threshold": 1.0},
            "registered_by": "operator",
        },
    }
    (tdir / "thread.json").write_text(json.dumps(thread), encoding="utf-8")
    grilling_dir = tdir / "grilling"
    grilling_dir.mkdir(exist_ok=True)
    (grilling_dir / "grilling_session.json").write_text(
        json.dumps(grilling), encoding="utf-8"
    )
    (tdir / "production" / "feasibility_envelope.json").write_text(
        json.dumps(envelope), encoding="utf-8"
    )
    contract = compile_goal_contract(
        GoalContractCompilerInput(
            question=thread["user_goal"],
            mandatory_baselines=("baseline B",),
            success_criteria=("beat baseline B",),
            disproof_conditions=("does not beat baseline B",),
            operator_requirements=("provide an actionable intervention",),
            target_scope="directional",
            acceptable_scopes=("directional",),
            baseline_evidence=() if pending_baselines else (
                BaselineEvidence(
                    candidate_id="baseline_current",
                    method="baseline B",
                    role="current_best_known",
                    provenance=("test:baseline-current",),
                ),
                BaselineEvidence(
                    candidate_id="baseline_naive",
                    method="naive baseline",
                    role="naive",
                    provenance=("test:baseline-naive",),
                ),
                BaselineEvidence(
                    candidate_id="baseline_null",
                    method="random baseline",
                    role="random_or_null",
                    provenance=("test:baseline-null",),
                ),
            ),
            safety_limits=("do not bypass access controls",),
            holdout_requirement=HoldoutRequirement(
                kind="real_holdout",
                holdout_source_id="holdout",
                predicate=FalsifierPredicate(
                    metric="score",
                    operator=">=",
                    threshold=1.0,
                ),
                registered_by="operator",
            ),
        )
    )
    goal = project_research_goal(contract)
    reorientation = tdir / "production" / "reorientation"
    reorientation.mkdir(parents=True, exist_ok=True)
    (reorientation / "goal_contract.json").write_text(
        json.dumps(serialize_goal_contract(contract)),
        encoding="utf-8",
    )
    from research_harness.orchestrator.adaptive_search import (
        experiment_fingerprint,
        strategy_fingerprint,
    )

    node_id = "n_strong"
    attempt_id = "attempt_strong"
    direction = make_direction_draft(
        claim="Adding interaction-preserving features improves held-out score.",
        fingerprint=make_direction_fingerprint(
            mechanism="baseline misses causal interactions",
            intervention="add interaction-preserving features",
            observables_and_data="held-out score and baseline score",
            analysis_unit="held-out evaluation unit",
            timescale="one evaluation cycle",
            system_boundary="operator decision pipeline",
        ),
        experiment_objective="Measure held-out score against baseline B.",
        predicted_outcomes=(
            "The intervention improves held-out score.",
            "The intervention does not improve held-out score.",
        ),
    )
    direction_id = direction.direction_id
    acquisition_command = make_acquisition_command(
        reservation_id="reservation_" + "a" * 64,
        node_id=node_id,
        attempt_id=attempt_id,
        direction=direction,
        needs=(),
        budget=AcquisitionBudget(
            max_requests=1,
            max_download_bytes=1024,
            max_wall_seconds=10,
        ),
    )
    acquisition = PublicAcquisition(
        tdir / "production" / "reorientation" / "acquisition_cache"
    ).acquire(acquisition_command)
    if not isinstance(acquisition, AcquisitionComplete):
        raise AssertionError("terminal fixture acquisition did not complete")
    manifest_id = acquisition.manifest.manifest_id
    tree = tdir / "production" / "tree"
    node_dir = tree / "nodes" / node_id
    workspace = node_dir / "workspace"
    strategy = {
        "id": strategy_fingerprint(
            mechanism="baseline misses causal interactions",
            intervention="add interaction-preserving features",
        ),
        "goal_id": goal["id"],
        "family": "interaction recovery",
        "mechanism": "baseline misses causal interactions",
        "intervention": "add interaction-preserving features",
        "information_target": "whether interactions close the frozen bar gap",
        "predicted_outcomes": ["score improves", "score does not improve"],
        "tests_bar_gaps": ["beat baseline B"],
        "required_capabilities": ["local_runner"],
        "estimated_cost": 0.2,
        "derived_from_direction_id": direction_id,
        "status": "cleared_bar",
        "priority": {"score": 1.0},
    }
    strategy_id = strategy["id"]
    node = {
        "id": node_id,
        "type": "mechanism",
        "status": "promoted",
        "domain": "test",
        "stage": "promotion",
        "parent": "n_parent",
        "lineage": {
            "root_goal_id": "rg_test",
            "covers_goal_facets": [],
            "inherited_assumptions": [],
            "introduced_assumptions": [],
            "taste_constraints_applied": [],
        },
        "claim_contract": {
            "claim_under_test": thread["user_goal"],
            "mandatory_baselines": ["baseline B"],
            "success_criteria": ["beat baseline B"],
            "disproof_conditions": ["does not beat baseline B"],
            "deploy_grade_scope": "directional",
            "data_source_anchor": f"acquisition_manifest:{manifest_id}",
            "data_source_snapshot_id": "as_" + manifest_id.removeprefix("acqmanifest_"),
        },
        "baseline_refs": [
            {
                "baseline_dossier_id": "bd_test",
                "candidate_ids": ["baseline_b"],
                "roles": ["current_best_known"],
            }
        ],
        "runtime_profile": {
            "worker_type": "experiment_worker",
            "timeout_policy": "test",
            "turn_budget": 2,
        },
        "failure_retrieval": {"query_tags": [], "selected_fail_files": []},
        "outputs": {
            "artifacts": [f"acquisition_manifest:{manifest_id}"],
            "verdict": "supported",
        },
        "strategy": strategy,
    }
    experiment_plan = {
        "plan_id": "plan_strong",
        "node_id": node_id,
        "claim_under_test": thread["user_goal"],
        "objective": "Test the interaction-preserving strategy.",
        "task_class": "eval",
        "workspace": str(workspace.resolve()),
        "source_files": [
            {
                "path": "src/run.py",
                "purpose": "test",
                "content": (
                    "import json\n"
                    "from pathlib import Path\n"
                    "Path('artifacts').mkdir(exist_ok=True)\n"
                    "Path('artifacts/metrics.json').write_text(json.dumps({"
                    "'metrics': {'score': 1.1}, "
                    "'baselines': {'baseline_score': 1.0}, "
                    "'claim_verdict_candidate': 'supported', "
                    "'disproof_conditions_hit': [], "
                    "'unexpected_observations': []}))\n"
                ),
            }
        ],
        "entrypoint": {"command": ["python3"], "args": ["src/run.py"]},
        "resources": {"timeout_sec": 10},
        "inputs": {},
        "expected_outputs": {
            "metrics_files": ["artifacts/metrics.json"],
            "logs": ["stdout.log", "stderr.log"],
            "artifact_dirs": ["artifacts"],
        },
        "baseline_evidence_requirements": [
            {
                "role": "current_best_known",
                "metric_key": "score",
                "baseline_key": "baseline_score",
                "operator": "greater_than",
                "margin": 0,
                "required": True,
            }
        ],
        "mandatory_baselines": ["baseline B"],
        "success_criteria": ["beat baseline B"],
        "disproof_conditions": ["does not beat baseline B"],
        "guardrails": {
            "allowed_write_roots": ["workspace"],
            "forbidden_actions": ["network"],
            "scope_policy": "test",
        },
        "failure_index_hints": {},
        "reproducibility": {
            "seed": 7,
            "code_snapshot": "code-v1",
            "data_snapshot": "data-v1",
        },
    }
    experiment_id = experiment_fingerprint(experiment_plan)
    frozen_question = {
        "thread_id": tdir.name,
        "question_id": "q_test",
        "formal_statement": "Whether the strategy beats baseline B in the operator decision context.",
        "true_iff": "The held-out score is strictly above baseline B.",
        "source_artifact": "operator_pinned",
        "source_provenance": "operator:test",
        "frozen_at_phase": "production_entry",
    }
    (tdir / "production" / "frozen_question.json").write_text(
        json.dumps(frozen_question), encoding="utf-8"
    )
    construct = {
        "thread_id": tdir.name,
        "question_id": frozen_question["question_id"],
        "construction_ref": node_id,
        "budget_total": 8,
        "pass_but_wrong_region": ["worlds where the score passes but utility fails"],
        "worlds_tested": [
            {
                "world_id": f"w{index}",
                "world_description": f"Distinct adversarial world number {index}",
                "measurement_passes": False,
                "frozen_answer": "no",
            }
            for index in range(3)
        ],
        "breaking_instance": None,
        "produced_by": "construct_adversary",
        "harness_verdict": "survived",
        "harness_reasons": [],
    }
    falsifier = {
        "thread_id": tdir.name,
        "contract_id": contract.contract_id,
        "attempt_id": attempt_id,
        "direction_id": direction_id,
        "node_id": node_id,
        "manifest_id": manifest_id,
        "kind": "real_holdout",
        "holdout_source_id": "holdout",
        "predicate": {"metric": "score", "op": ">=", "threshold": 1.0},
        "observed": 1.1,
        "passed": True,
        "verdict": "passed",
        "transfer_evidence_admissible": True,
        "produced_by": "harness_falsifier_module",
    }
    ac_decision = {
        "decision": "accept",
        "confidence": "high",
        "score_summary": {
            "novelty": 8,
            "validity": 9,
            "necessity": 8,
            "clarity": 8,
            "reproducibility": 9,
            "taste_alignment": 8,
        },
        "blocking_reasons": [],
        "required_next_search_nodes": [],
        "camera_ready_conditions": [],
        "camera_ready_directives": [
            {
                "directive": "State the held-out decision threshold explicitly.",
                "origin_critic_ids": ["critic_1"],
                "must_appear_in_section": "method",
                "rationale": "The operator needs the threshold to reproduce the decision.",
            }
        ],
        "advisor_message_to_professor": (
            "The evidence is strong; preserve the exact held-out threshold and "
            "strategy mechanism in the camera-ready paper."
        ),
        "rebuttal_synthesis": {
            "strongest_supporting_evidence": ["held-out score 1.1"],
            "load_bearing_objections": [],
            "minority_dissent": [],
            "methodology_assessment": {
                "aggregate_verdict": "provides",
                "methodology_for_user": "Apply interaction features and the held-out threshold.",
                "remaining_gap": "",
            },
        },
    }
    attestation = {
        "thread_id": tdir.name,
        "promoted_node_id": node_id,
        "user_intake_recap": (
            "The operator needs a deployable decision strategy that beats baseline B."
        ),
        "achieved": True,
        "what_user_can_do_with_this_paper": (
            "The operator can add interaction-preserving features, run the fixed "
            "held-out protocol, and deploy only when score is at least 1.0."
        ),
        "evidence_anchors_back_to_intake": ["held-out score", "baseline comparison"],
        "required_additional_research": [],
        "attested_status": "goal_achieved",
        "verdict_strength": "transfer_valid",
        "referent_ledger": {
            "has_real_referent": True,
            "has_construct_referent": True,
            "max_reachable_verdict": "transfer_valid",
        },
        "scope_attainment": {
            "seed_target_scope": "directional",
            "attested_scope": "directional",
            "narrowed": False,
        },
    }
    artifacts = {
        "falsifier_result.json": falsifier,
        "construct_adversary_report.json": construct,
        "ac_decision.json": ac_decision,
        "orchestrator_reduction.json": {"blocking_objections": []},
        "user_goal_attestation.json": attestation,
    }
    rebuttal = tdir / "production" / "rebuttal"
    rebuttal.mkdir(parents=True, exist_ok=True)
    for name, value in artifacts.items():
        (rebuttal / name).write_text(json.dumps(value), encoding="utf-8")
    workspace.mkdir(parents=True, exist_ok=True)
    (node_dir / "experiment_plan.json").write_text(
        json.dumps(experiment_plan), encoding="utf-8"
    )
    source_path = workspace / "src" / "run.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(
        experiment_plan["source_files"][0]["content"],
        encoding="utf-8",
    )
    metrics_path = workspace / "artifacts" / "metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_payload = {
        "metrics": {"score": 1.1},
        "baselines": {"baseline_score": 1.0},
        "claim_verdict_candidate": "supported",
        "disproof_conditions_hit": [],
        "unexpected_observations": [],
    }
    metrics_path.write_text(json.dumps(metrics_payload), encoding="utf-8")
    stdout_path = workspace / "stdout.log"
    stderr_path = workspace / "stderr.log"
    stdout_path.write_text("experiment completed\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    from research_harness.orchestrator.experiment_plan import (
        derive_job_manifest_from_experiment_plan,
    )
    from research_harness.runner.evidence import (
        build_worker_report_from_runner_evidence,
    )

    job_manifest = derive_job_manifest_from_experiment_plan(node, experiment_plan)
    (node_dir / "job_manifest.json").write_text(
        json.dumps(job_manifest), encoding="utf-8"
    )
    runner_result = {
        "job_id": job_manifest["job_id"],
        "experiment_plan_id": experiment_plan["plan_id"],
        "node_id": node_id,
        "status": "completed",
        "exit_code": 0,
        "elapsed_sec": 0.1,
        "timeout_sec": 10,
        "workspace": str(workspace.resolve()),
        "source_files": [str(source_path.resolve())],
        "command": ["python3", "src/run.py"],
        "stdout_path": str(stdout_path.resolve()),
        "stderr_path": str(stderr_path.resolve()),
        "failure_record_candidate": None,
    }
    (workspace / "runner_result.json").write_text(
        json.dumps(runner_result), encoding="utf-8"
    )
    worker_report = build_worker_report_from_runner_evidence(
        node,
        job_manifest,
        runner_result,
        tree,
    ).worker_report
    (node_dir / "worker_report.json").write_text(
        json.dumps(worker_report), encoding="utf-8"
    )
    from research_harness.orchestrator.strong_result import (
        verify_strong_execution_evidence,
    )

    execution_evidence = verify_strong_execution_evidence(
        node=node,
        experiment_plan=experiment_plan,
        worker_report=worker_report,
        node_dir=node_dir,
        tree_dir=tree,
    )
    receipt = {
        "verified": True,
        "contract_id": contract.contract_id,
        "attempt_id": attempt_id,
        "direction_id": direction_id,
        "acquisition_manifest_id": manifest_id,
        "goal_id": goal["id"],
        "bar_digest": goal["bar_digest"],
        "strategy_id": strategy_id,
        "promoted_node_id": node_id,
        "experiment_id": experiment_id,
        "verdict_strength": "transfer_valid",
        "falsifier_result_sha256": ts._canonical_json_sha256(
            artifacts["falsifier_result.json"]
        ),
        "construct_adversary_sha256": ts._canonical_json_sha256(
            artifacts["construct_adversary_report.json"]
        ),
        "ac_decision_sha256": ts._canonical_json_sha256(
            artifacts["ac_decision.json"]
        ),
        "critic_resolution_sha256": ts._canonical_json_sha256(
            artifacts["orchestrator_reduction.json"]
        ),
        "attestation_sha256": ts._canonical_json_sha256(
            artifacts["user_goal_attestation.json"]
        ),
        "experiment_plan_sha256": ts._canonical_json_sha256(experiment_plan),
        "worker_report_sha256": ts._canonical_json_sha256(worker_report),
        "job_manifest_sha256": execution_evidence["job_manifest_sha256"],
        "runner_result_sha256": execution_evidence["runner_result_sha256"],
        "metrics_evidence_sha256": execution_evidence[
            "metrics_evidence_sha256"
        ],
    }
    receipt_digest = strong_result_receipt_sha256(receipt)
    (reorientation / "node_attempts.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": {
                    node_id: {
                        "attempt_id": attempt_id,
                        "direction_id": direction_id,
                        "manifest_id": manifest_id,
                        "legacy_audit_only": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (reorientation / "state.json").write_text(
        json.dumps(
            serialize_reorientation_state(
                ReorientationState(
                    version=2,
                    contract_id=contract.contract_id,
                    revision=4,
                    closed_attempts=(),
                    phase=GoalAchieved(
                        strong_result_receipt_sha256=receipt_digest,
                    ),
                )
            )
        ),
        encoding="utf-8",
    )
    tree.mkdir(exist_ok=True)
    state = {
        "nodes": [node],
        "promoted_node_ids": [node_id],
        "adaptive": {
            "disposition": "goal_achieved",
            "goal": goal,
            "strategies": [strategy],
            "experiments": [
                {
                    "id": experiment_id,
                    "node_id": node_id,
                    "strategy_id": strategy_id,
                    "status": "executed",
                }
            ],
            "strong_result_receipt": receipt,
        },
    }
    (tree / "search_state.json").write_text(json.dumps(state), encoding="utf-8")

    from contextlib import nullcontext
    from research_harness.orchestrator.confirmation_use import (
        confirmation_evidence_from_result, digest_confirmation_evidence,
        initialize_confirmation_ledger_locked, consume_confirmation,
    )
    confirmation_path = tdir / "production" / "reorientation" / "confirmation_use.json"
    confirmation_path.unlink(missing_ok=True)
    initialize_confirmation_ledger_locked(confirmation_path, contract)
    consume_confirmation(
        confirmation_path, contract,
        binding={key: falsifier[key] for key in ("contract_id", "attempt_id", "direction_id", "node_id", "manifest_id")},
        evidence_digest=digest_confirmation_evidence(contract, confirmation_evidence_from_result(falsifier)),
        writer_lock=nullcontext,
    )

    summary_path = tdir / "production" / "production_run_summary.json"
    if summary_path.exists():
        from research_harness.publishing.integrity import issue_publication_receipt
        from research_harness.orchestrator.blind_sequential_research import strong_result_receipt_sha256

        publication = tdir / "production" / "publication"
        drafts = publication / "_drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        (drafts / "outline.json").write_text(json.dumps({"title": "Verified fixture"}))
        paper = publication / "paper.html"
        paper.write_text("<html><body>Verified fixture manuscript</body></html>")
        summary = json.loads(summary_path.read_text())
        dispatch = {"rendered_artifacts": [{"output": "paper_html", "artifact_path": str(paper)}]}
        summary["publication_dispatch"] = dispatch
        summary["publication_receipt_sha256"] = issue_publication_receipt(
            tdir / "production", dispatch,
            strong_result_receipt_sha256(state["adaptive"]["strong_result_receipt"]),
        )
        summary_path.write_text(json.dumps(summary))


class TerminalDetectionTests(unittest.TestCase):
    def test_no_summary_file_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)
            self.assertIsNone(outcome)

    def test_terminal_fails_closed_on_malformed_node_attempt_shapes(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            _write_verified_terminal_state(tdir)
            attempts_path = (
                tdir / "production" / "reorientation" / "node_attempts.json"
            )
            for malformed in (
                [],
                {"version": 1, "nodes": []},
                {"version": 1, "nodes": {"n_strong": []}},
            ):
                with self.subTest(malformed=malformed):
                    attempts_path.write_text(
                        json.dumps(malformed),
                        encoding="utf-8",
                    )
                    self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

    def test_terminal_rejects_duplicate_nodes_for_receipt_attempt(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            _write_verified_terminal_state(tdir)
            attempts_path = (
                tdir / "production" / "reorientation" / "node_attempts.json"
            )
            attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
            attempts["nodes"]["n_duplicate"] = dict(
                attempts["nodes"]["n_strong"]
            )
            attempts_path.write_text(json.dumps(attempts), encoding="utf-8")

            self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

    def test_terminal_verification_is_bound_to_one_search_snapshot(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            _write_verified_terminal_state(tdir)
            state_path = tdir / "production" / "tree" / "search_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            expected_digest = ts._canonical_json_sha256(state)
            self.assertTrue(
                ts.is_terminal(
                    repo,
                    "t1",
                    require_rendered=False,
                    expected_search_state_sha256=expected_digest,
                )[0]
            )
            state["adaptive"]["revision"] = 1
            state_path.write_text(json.dumps(state), encoding="utf-8")

            self.assertEqual(
                ts.is_terminal(
                    repo,
                    "t1",
                    require_rendered=False,
                    expected_search_state_sha256=expected_digest,
                ),
                (False, None),
            )

    def test_honest_failure_outcome_is_NOT_terminal_under_pr8(self):
        # PR8: honest_failure is a retreat state, not a terminal one.
        # supervisor must keep iterating until dual-gate passes.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({"outcome": "honest_failure"}), encoding="utf-8"
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)

    def test_accept_without_attestation_is_NOT_terminal_under_pr8(self):
        # PR8: AC accept alone is no longer enough. Dual-gate requires
        # Professor user_goal_attestation.achieved=true.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {
                        "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                    },
                }),
                encoding="utf-8",
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)

    def test_dual_gate_without_canonical_receipt_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({"achieved": True, "what_user_can_do_with_this_paper": "x"}),
                encoding="utf-8",
            )
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {
                        "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                    },
                }),
                encoding="utf-8",
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)
            self.assertIsNone(outcome)

    def test_adaptive_goal_achievement_requires_matching_strong_receipt(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({"achieved": True}), encoding="utf-8"
            )
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps(
                    {
                        "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                        "publication_dispatch": {
                            "rendered_artifacts": [{"output": "paper_html"}]
                        },
                    }
                ),
                encoding="utf-8",
            )
            tree = tdir / "production" / "tree"
            tree.mkdir()
            adaptive = {
                "disposition": "continue",
                "goal": {"id": "goal_id", "bar_digest": "sha256:bar"},
                "strong_result_receipt": None,
            }
            (tree / "search_state.json").write_text(
                json.dumps({"adaptive": adaptive}), encoding="utf-8"
            )

            self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

            _write_verified_terminal_state(tdir)
            self.assertEqual(
                ts.is_terminal(repo, "t1"),
                (True, "accept_with_goal_achieved"),
            )

    def test_terminal_rejects_broken_attempt_and_contract_bindings(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            summary_path = tdir / "production" / "production_run_summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                        "publication_dispatch": {
                            "rendered_artifacts": [{"output": "paper_html"}]
                        },
                    }
                ),
                encoding="utf-8",
            )

            _write_verified_terminal_state(tdir)
            state_path = tdir / "production" / "tree" / "search_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            replacement_direction = "direction_" + "9" * 64
            state["nodes"][0]["strategy"]["derived_from_direction_id"] = (
                replacement_direction
            )
            state["adaptive"]["strategies"][0]["derived_from_direction_id"] = (
                replacement_direction
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

            _write_verified_terminal_state(tdir)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["nodes"][0]["outputs"]["artifacts"] = [
                "acquisition_manifest:acqmanifest_" + "8" * 64
            ]
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

            _write_verified_terminal_state(tdir)
            state = json.loads(state_path.read_text())
            falsifier_path = (
                tdir / "production" / "rebuttal" / "falsifier_result.json"
            )
            falsifier = json.loads(falsifier_path.read_text())
            falsifier["attempt_id"] = "attempt_from_previous_direction"
            falsifier_path.write_text(json.dumps(falsifier), encoding="utf-8")
            state["adaptive"]["strong_result_receipt"][
                "falsifier_result_sha256"
            ] = ts._canonical_json_sha256(falsifier)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

            _write_verified_terminal_state(tdir)
            reorientation_path = (
                tdir / "production" / "reorientation" / "state.json"
            )
            reorientation = json.loads(
                reorientation_path.read_text(encoding="utf-8")
            )
            reorientation["contract_id"] = "contract_" + "7" * 64
            reorientation_path.write_text(
                json.dumps(reorientation),
                encoding="utf-8",
            )
            self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

    def test_terminal_requires_later_baseline_review_for_early_goal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            _write_verified_terminal_state(tdir, pending_baselines=True)
            self.assertEqual(ts.is_terminal(repo, "t1", require_rendered=False), (False, None))
            market = tdir / "market"
            market.mkdir(exist_ok=True)
            (market / "baseline_qualification.json").write_text('{}')
            with mock.patch("research_harness.memory.baseline_review.require_baseline_approval") as review:
                self.assertEqual(ts.is_terminal(repo, "t1", require_rendered=False),
                                 (True, "accept_with_goal_achieved"))
                review.assert_called_once_with(repo, tdir, {})
            with mock.patch("research_harness.memory.baseline_review.require_baseline_approval",
                            side_effect=ValueError("stale baseline review")):
                self.assertEqual(ts.is_terminal(repo, "t1", require_rendered=False), (False, None))

    def test_terminal_rederives_worker_and_falsifier_semantics(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps(
                    {
                        "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                        "publication_dispatch": {
                            "rendered_artifacts": [{"output": "paper_html"}]
                        },
                    }
                ),
                encoding="utf-8",
            )
            _write_verified_terminal_state(tdir)
            self.assertEqual(
                ts.is_terminal(repo, "t1"),
                (True, "accept_with_goal_achieved"),
            )

            state_path = tdir / "production" / "tree" / "search_state.json"
            state = json.loads(state_path.read_text())
            node_id = state["adaptive"]["strong_result_receipt"]["promoted_node_id"]
            worker_path = (
                tdir
                / "production"
                / "tree"
                / "nodes"
                / node_id
                / "worker_report.json"
            )
            worker = json.loads(worker_path.read_text())
            worker["claim_verdict_candidate"] = "contradicted"
            worker["baseline_evidence_status"]["overall"] = "failed"
            worker_path.write_text(json.dumps(worker), encoding="utf-8")
            state["adaptive"]["strong_result_receipt"]["worker_report_sha256"] = (
                ts._canonical_json_sha256(worker)
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

            _write_verified_terminal_state(tdir)
            state = json.loads(state_path.read_text())
            node_id = state["adaptive"]["strong_result_receipt"][
                "promoted_node_id"
            ]
            node_dir = tdir / "production" / "tree" / "nodes" / node_id
            metrics_path = node_dir / "workspace" / "artifacts" / "metrics.json"
            metrics = json.loads(metrics_path.read_text())
            metrics["metrics"]["score"] = 0.5
            metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
            worker_path = node_dir / "worker_report.json"
            worker = json.loads(worker_path.read_text())
            worker["metrics"]["score"] = 0.5
            worker["claim_verdict_candidate"] = "supported"
            worker["baseline_evidence_status"]["overall"] = "passed"
            worker_path.write_text(json.dumps(worker), encoding="utf-8")
            receipt = state["adaptive"]["strong_result_receipt"]
            receipt["worker_report_sha256"] = ts._canonical_json_sha256(worker)
            receipt["metrics_evidence_sha256"] = ts._canonical_json_sha256(
                [
                    {
                        "path": (
                            f"nodes/{node_id}/workspace/artifacts/metrics.json"
                        ),
                        "payload": metrics,
                    }
                ]
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

            _write_verified_terminal_state(tdir)
            (node_dir / "workspace" / "runner_result.json").unlink()
            self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

            _write_verified_terminal_state(tdir)
            state = json.loads(state_path.read_text())
            falsifier_path = (
                tdir / "production" / "rebuttal" / "falsifier_result.json"
            )
            falsifier = json.loads(falsifier_path.read_text())
            falsifier["observed"] = 0.1
            falsifier_path.write_text(json.dumps(falsifier), encoding="utf-8")
            state["adaptive"]["strong_result_receipt"][
                "falsifier_result_sha256"
            ] = ts._canonical_json_sha256(falsifier)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(ts.is_terminal(repo, "t1"), (False, None))

    def test_accept_without_artifacts_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {"rendered_artifacts": []},
                }),
                encoding="utf-8",
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)

    def test_attestation_achieved_false_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({"achieved": False}), encoding="utf-8"
            )
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {
                        "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                    },
                }),
                encoding="utf-8",
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)

    def test_corrupt_summary_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                "not json", encoding="utf-8"
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)

    def test_legacy_pause_does_not_stop_blind_reorientation(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tid = "thread_terminal"
            tdir = _make_thread(repo, tid)
            (repo / "settings.json").write_text(
                json.dumps({"data_adapters": {"registered": []}}),
                encoding="utf-8",
            )
            (tdir / "thread.json").write_text(
                json.dumps(
                    {
                        "thread_id": tid,
                        "title": "terminal test",
                        "created_at": "2026-08-30T00:00:00Z",
                        "updated_at": "2026-08-30T00:00:00Z",
                        "current_phase": "connector",
                        "phase_status": "complete",
                        "outcome": None,
                        "user_goal": "test terminal index synchronization",
                    }
                ),
                encoding="utf-8",
            )
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({"achieved": False, "attested_status": "unverified_screen"}),
                encoding="utf-8",
            )
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps(
                    {
                        "outcome": "honest_failure",
                        "publication_dispatch": {
                            "rendered_artifacts": [
                                {"output": "honest_failure_html", "artifact_path": "/x"}
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            tree = tdir / "production" / "tree"
            tree.mkdir(parents=True)
            (tree / "search_state.json").write_text(
                json.dumps(
                    {
                        "adaptive": {
                            "disposition": "paused_needs_expansion",
                            "pause": {
                                "reason": "missing_capability",
                                "missing_capabilities": ["real_holdout"],
                                "resume_condition": "register the real holdout",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(
                ts,
                "advance_resumable_reorientation",
                return_value=None,
            ), mock.patch.object(
                ts,
                "mcp_idle_seconds",
                return_value=float("inf"),
            ), mock.patch.object(
                ts,
                "spawn_codex_session",
                return_value=0,
            ) as spawn:
                result = ts.watch_thread(
                    repo,
                    tid,
                    max_cycles=1,
                    rate_limit_backoff_initial=0.001,
                )

            self.assertEqual(result["status"], "max_cycles_exceeded")
            spawn.assert_called_once()
            index = json.loads((tdir / "thread.json").read_text(encoding="utf-8"))
            self.assertNotEqual(index["phase_status"], "awaiting_input")
            self.assertIsNone(index["outcome"])


class ReorientationStrongCommitTests(unittest.TestCase):
    def test_verifier_rejects_duplicate_nodes_for_active_attempt(self):
        from research_harness.orchestrator.blind_reorientation import (
            AwaitingEvidence,
            DirectionAttemptRef,
            ReorientationState,
            parse_reorientation_state,
            serialize_reorientation_state,
        )
        from research_harness.orchestrator.blind_sequential_research import (
            StrongResultBinding,
        )
        from research_harness.orchestrator.direction_generation import (
            make_direction_fingerprint,
        )

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            _write_verified_terminal_state(tdir)
            reorientation = tdir / "production" / "reorientation"
            search_state = json.loads(
                (
                    tdir / "production" / "tree" / "search_state.json"
                ).read_text(encoding="utf-8")
            )
            receipt = search_state["adaptive"]["strong_result_receipt"]
            previous = parse_reorientation_state(
                json.loads(
                    (reorientation / "state.json").read_text(encoding="utf-8")
                )
            )
            attempt = DirectionAttemptRef(
                attempt_id=receipt["attempt_id"],
                direction_id=receipt["direction_id"],
                fingerprint=make_direction_fingerprint(
                    mechanism="baseline misses causal interactions",
                    intervention="add interaction-preserving features",
                    observables_and_data="held-out score and baseline score",
                    analysis_unit="held-out evaluation unit",
                    timescale="one evaluation cycle",
                    system_boundary="operator decision pipeline",
                ),
                ordinal=0,
            )
            (reorientation / "state.json").write_text(
                json.dumps(
                    serialize_reorientation_state(
                        ReorientationState(
                            version=2,
                            contract_id=previous.contract_id,
                            revision=3,
                            closed_attempts=(),
                            phase=AwaitingEvidence(active_attempt=attempt),
                        )
                    )
                ),
                encoding="utf-8",
            )
            attempts_path = reorientation / "node_attempts.json"
            attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
            attempts["nodes"]["n_duplicate"] = dict(
                attempts["nodes"][receipt["promoted_node_id"]]
            )
            attempts_path.write_text(json.dumps(attempts), encoding="utf-8")
            binding = StrongResultBinding(
                contract_id=receipt["contract_id"],
                attempt_id=receipt["attempt_id"],
                direction_id=receipt["direction_id"],
                node_id=receipt["promoted_node_id"],
                manifest_id=receipt["acquisition_manifest_id"],
            )

            self.assertIsNone(ts.verify_strong_result_binding(repo, "t1", binding))

    def test_supervisor_recovers_attestation_crash_and_replays_goal(self):
        import research_harness.mcp_server as mcp
        from research_harness.orchestrator.adaptive_search import (
            initialize_adaptive_state,
        )
        from research_harness.orchestrator.blind_reorientation import (
            AwaitingEvidence,
            DirectionAttemptRef,
            GoalAchieved,
            ReorientationState,
            parse_reorientation_state,
            serialize_reorientation_state,
        )
        from research_harness.orchestrator.direction_generation import (
            make_direction_fingerprint,
        )
        from research_harness.orchestrator.search_state import validate_search_state

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (repo / "settings.json").write_text("{}", encoding="utf-8")
            _write_verified_terminal_state(tdir)
            tree = tdir / "production" / "tree"
            search_state = json.loads(
                (tree / "search_state.json").read_text(encoding="utf-8")
            )
            node = search_state["nodes"][0]
            existing_receipt = search_state["adaptive"]["strong_result_receipt"]
            goal = search_state["adaptive"]["goal"]
            adaptive = initialize_adaptive_state(goal)
            adaptive["strategies"] = search_state["adaptive"]["strategies"]
            adaptive["experiments"] = search_state["adaptive"]["experiments"]
            state = {
                "search_id": "s_strong",
                "status": "running",
                "max_depth": 5,
                "max_debug_depth": 2,
                "sunk_cost_policy": "progress_gated",
                "scaleup_policy": "disallow_by_default",
                "frontier": [],
                "nodes": [node],
                "completed_node_ids": [],
                "promoted_node_ids": [node["id"]],
                "pruned_node_ids": [],
                "transitions": [],
                "adaptive": adaptive,
            }
            validate_search_state(state)
            (tree / "search_state.json").write_text(
                json.dumps(state),
                encoding="utf-8",
            )
            reorientation_dir = tdir / "production" / "reorientation"
            previous_reorientation = parse_reorientation_state(
                json.loads(
                    (reorientation_dir / "state.json").read_text(encoding="utf-8")
                )
            )
            attempt = DirectionAttemptRef(
                attempt_id=existing_receipt["attempt_id"],
                direction_id=existing_receipt["direction_id"],
                fingerprint=make_direction_fingerprint(
                    mechanism="baseline misses causal interactions",
                    intervention="add interaction-preserving features",
                    observables_and_data="held-out score and baseline score",
                    analysis_unit="held-out evaluation unit",
                    timescale="one evaluation cycle",
                    system_boundary="operator decision pipeline",
                ),
                ordinal=0,
            )
            (reorientation_dir / "state.json").write_text(
                json.dumps(
                    serialize_reorientation_state(
                        ReorientationState(
                            version=2,
                            contract_id=previous_reorientation.contract_id,
                            revision=3,
                            closed_attempts=(),
                            phase=AwaitingEvidence(active_attempt=attempt),
                        )
                    )
                ),
                encoding="utf-8",
            )
            attestation_path = (
                tdir
                / "production"
                / "rebuttal"
                / "user_goal_attestation.json"
            )
            attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
            for key in (
                "attested_status",
                "verdict_strength",
                "referent_ledger",
                "scope_attainment",
            ):
                attestation.pop(key)
            import research_harness.orchestrator.strong_result as strong_result

            actual_verify = strong_result.verify_strong_execution_evidence
            verification_calls = 0

            def race_execution_evidence(*args, **kwargs):
                nonlocal verification_calls
                verification_calls += 1
                verified = actual_verify(*args, **kwargs)
                if verification_calls == 2:
                    return {
                        **verified,
                        "runner_result_sha256": "sha256:" + "0" * 64,
                    }
                return verified

            with mock.patch.object(mcp, "_repo_root", return_value=repo), mock.patch.object(
                mcp,
                "_thread_dir",
                side_effect=lambda thread_id: repo / "runs" / "threads" / thread_id,
            ), mock.patch.object(
                strong_result,
                "verify_strong_execution_evidence",
                side_effect=race_execution_evidence,
            ):
                raced = mcp.handle_submit_professor_user_goal_attestation(
                    {"thread_id": "t1", "attestation": dict(attestation)}
                )

            self.assertEqual(raced["status"], "rejected")
            self.assertIn("active strong execution evidence", raced["reason"])
            self.assertEqual(verification_calls, 2)
            self.assertFalse(
                (reorientation_dir / "terminal_commands").exists()
            )
            with mock.patch.object(mcp, "_repo_root", return_value=repo), mock.patch.object(
                mcp,
                "_thread_dir",
                side_effect=lambda thread_id: repo / "runs" / "threads" / thread_id,
            ):
                with mock.patch.object(
                    mcp,
                    "_write_search_state_atomic",
                    side_effect=OSError("simulated process exit before adaptive commit"),
                ):
                    with self.assertRaisesRegex(OSError, "simulated process exit"):
                        mcp.handle_submit_professor_user_goal_attestation(
                            {"thread_id": "t1", "attestation": dict(attestation)}
                        )

            terminal_receipts = list(
                (reorientation_dir / "terminal_commands").glob("*.json")
            )
            self.assertEqual(len(terminal_receipts), 1)
            prepared = json.loads(
                terminal_receipts[0].read_text(encoding="utf-8")
            )
            self.assertEqual(prepared["status"], "prepared")
            self.assertIsNone(
                json.loads((tree / "search_state.json").read_text())["adaptive"]
                ["strong_result_receipt"],
            )

            recovered = ts.advance_resumable_reorientation(repo, "t1")
            committed = parse_reorientation_state(
                json.loads(
                    (reorientation_dir / "state.json").read_text(encoding="utf-8")
                )
            )
            with mock.patch.object(mcp, "_repo_root", return_value=repo), mock.patch.object(
                mcp,
                "_thread_dir",
                side_effect=lambda thread_id: repo / "runs" / "threads" / thread_id,
            ):
                committed_receipt = json.loads(
                    terminal_receipts[0].read_text(encoding="utf-8")
                )
                tampered_receipt = dict(committed_receipt)
                tampered_receipt["status"] = "prepared"
                tampered_receipt["attestation"] = {"achieved": False}
                terminal_receipts[0].write_text(
                    json.dumps(tampered_receipt),
                    encoding="utf-8",
                )
                tampered = mcp.handle_submit_professor_user_goal_attestation(
                    {"thread_id": "t1", "attestation": dict(attestation)}
                )
                terminal_receipts[0].write_text(
                    json.dumps(committed_receipt),
                    encoding="utf-8",
                )
                second = mcp.handle_submit_professor_user_goal_attestation(
                    {"thread_id": "t1", "attestation": dict(attestation)}
                )

            self.assertEqual(recovered["status"], "ok", recovered)
            self.assertEqual(
                recovered["supervisor_resume_kind"],
                "strong_terminal_recovery",
            )
            self.assertEqual(
                recovered["blind_reorientation"]["status"],
                "goal_achieved",
            )
            self.assertEqual(tampered["status"], "rejected")
            self.assertIsInstance(committed.phase, GoalAchieved)
            terminal_receipt = json.loads(terminal_receipts[0].read_text(encoding="utf-8"))
            self.assertEqual(terminal_receipt["status"], "committed")
            self.assertEqual(
                terminal_receipt["strong_result_receipt_sha256"],
                committed.phase.strong_result_receipt_sha256,
            )
            self.assertEqual(second["status"], "ok")
            self.assertEqual(second["blind_reorientation"]["status"], "goal_achieved")


class SupervisorReorientationTests(unittest.TestCase):
    @staticmethod
    def _checkpoint_state():
        from research_harness.orchestrator.blind_reorientation import (
            AcquisitionReserved,
            DirectionAttemptRef,
            ReorientationState,
            make_acquisition_reservation,
            make_checkpoint,
        )
        from research_harness.orchestrator.direction_generation import (
            make_direction_fingerprint,
        )

        attempt = DirectionAttemptRef(
            attempt_id="attempt_resume",
            direction_id="direction_" + "b" * 64,
            fingerprint=make_direction_fingerprint(
                mechanism="mechanism",
                intervention="apply intervention",
                observables_and_data="public observations",
                analysis_unit="record",
                timescale="one cycle",
                system_boundary="public source",
            ),
            ordinal=0,
        )
        continuation = AcquisitionReserved(
            active_attempt=attempt,
            reservation=make_acquisition_reservation(
                expected_revision=1,
                request_digest="sha256:" + "c" * 64,
            ),
        )
        return ReorientationState(
            version=2,
            contract_id="contract_" + "d" * 64,
            revision=4,
            closed_attempts=(),
            phase=make_checkpoint(
                continuation,
                reason="request_budget",
                payload_digest="sha256:" + "e" * 64,
            ),
        )

    def test_physical_checkpoint_resumes_after_progressing_prior_slice(self):
        from types import SimpleNamespace

        state = self._checkpoint_state()
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (repo / "settings.json").write_text("{}", encoding="utf-8")
            reorientation = tdir / "production" / "reorientation"
            commands = reorientation / "commands"
            commands.mkdir(parents=True)
            (reorientation / "state.json").write_text("{}", encoding="utf-8")
            (commands / "prior.json").write_text(
                json.dumps(
                    {
                        "command_id": "supervisor_reorientation_prior",
                        "result": {
                            "status": "checkpointed",
                            "resumed_from_checkpoint_id": "checkpoint_" + "1" * 64,
                            "checkpoint_id": state.phase.checkpoint.checkpoint_id,
                            "revision": state.revision,
                        },
                    }
                ),
                encoding="utf-8",
            )

            class Engine:
                paths = SimpleNamespace(root=reorientation)

                def read_state(self):
                    return state

                def advance_research(self, **kwargs):
                    self.call = kwargs
                    return {"status": "acquisition_running"}

            engine = Engine()
            with mock.patch(
                "research_harness.orchestrator.blind_mcp_adapter.build_blind_research_engine",
                return_value=engine,
            ):
                result = ts.advance_resumable_reorientation(repo, "t1")

            self.assertEqual(result["supervisor_resume_kind"], "physical_checkpoint")
            self.assertEqual(engine.call["expected_revision"], state.revision)
            self.assertEqual(
                engine.call["expected_checkpoint_id"],
                state.phase.checkpoint.checkpoint_id,
            )

    def test_no_progress_physical_checkpoint_returns_explicit_outcome(self):
        from types import SimpleNamespace

        state = self._checkpoint_state()
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (repo / "settings.json").write_text("{}", encoding="utf-8")
            reorientation = tdir / "production" / "reorientation"
            commands = reorientation / "commands"
            commands.mkdir(parents=True)
            (reorientation / "state.json").write_text("{}", encoding="utf-8")
            checkpoint_id = state.phase.checkpoint.checkpoint_id
            (commands / "prior.json").write_text(
                json.dumps(
                    {
                        "command_id": "supervisor_reorientation_prior",
                        "result": {
                            "status": "checkpointed",
                            "resumed_from_checkpoint_id": checkpoint_id,
                            "checkpoint_id": checkpoint_id,
                            "revision": state.revision,
                        },
                    }
                ),
                encoding="utf-8",
            )

            class Engine:
                paths = SimpleNamespace(root=reorientation)

                def read_state(self):
                    return state

                def advance_research(self, **kwargs):
                    self.call = kwargs
                    return {
                        "status": "checkpointed",
                        "resumed_from_checkpoint_id": checkpoint_id,
                        "checkpoint_id": checkpoint_id,
                        "revision": state.revision + 1,
                    }

            engine = Engine()
            with mock.patch(
                "research_harness.orchestrator.blind_mcp_adapter.build_blind_research_engine",
                return_value=engine,
            ):
                result = ts.advance_resumable_reorientation(repo, "t1")

            self.assertEqual(
                result["supervisor_resume_kind"],
                "physical_checkpoint_no_progress",
            )
            self.assertEqual(
                engine.call["expected_checkpoint_id"],
                checkpoint_id,
            )


class IdleDetectionTests(unittest.TestCase):
    def test_empty_production_dir_idle_is_inf(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            self.assertEqual(ts.mcp_idle_seconds(repo, "t1"), float("inf"))

    def test_missing_production_dir_idle_is_inf(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(ts.mcp_idle_seconds(Path(tmp), "no_thread"), float("inf"))

    def test_recent_file_yields_small_idle(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            f = tdir / "production" / "tree" / "search_state.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("{}", encoding="utf-8")
            idle = ts.mcp_idle_seconds(repo, "t1")
            self.assertLess(idle, 5.0)

    def test_old_file_yields_large_idle(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            f = tdir / "production" / "tree" / "search_state.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("{}", encoding="utf-8")
            past = time.time() - 7200
            os.utime(f, (past, past))
            idle = ts.mcp_idle_seconds(repo, "t1")
            self.assertGreater(idle, 3600)


class ResumePromptTests(unittest.TestCase):
    def test_resume_delivers_unconsumed_researcher_response(self):
        from research_harness.orchestrator.operator_prompts import (
            enqueue_prompt, list_pending, submit_response, take_pending_response,
        )

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            item = enqueue_prompt(tdir, kind="context_request", prompt="Review baseline")
            submit_response(tdir, event_id=item["event_id"], response="Reject unsupported attribution")
            prompt = ts.build_resume_prompt(repo, "t1", cycle=2)
            self.assertIn("Reject unsupported attribution", prompt)
            self.assertEqual(list_pending(tdir)[0]["status"], "responded")
            take_pending_response(tdir, event_id=item["event_id"])
            self.assertIn("Reject unsupported attribution", ts.build_resume_prompt(repo, "t1", cycle=3))

    def test_resume_prompt_carries_thread_id_and_anti_lazy_brief(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            prompt = ts.build_resume_prompt(repo, "t1", cycle=3)
            self.assertIn("t1", prompt)
            self.assertIn("cycle #3", prompt)
            self.assertIn("goal achieved", prompt.lower())
            self.assertIn("advance_research", prompt)
            self.assertIn("hard_external_block", prompt)
            self.assertIn("honest_failure", prompt)
            self.assertIn("anti-laziness", prompt.lower())

    def test_resume_prompt_reads_state_when_present(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            state = {
                "promoted_node_ids": ["n_root"],
                "pruned_node_ids": [],
                "status": "running",
                "nodes": [
                    {"id": "n_root", "status": "promoted"},
                    {"id": "n_a", "status": "ready"},
                ],
            }
            (tdir / "production" / "tree").mkdir(parents=True, exist_ok=True)
            (tdir / "production" / "tree" / "search_state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            prompt = ts.build_resume_prompt(repo, "t1", cycle=1)
            self.assertIn("n_root", prompt)


class LockTests(unittest.TestCase):
    def test_lock_acquire_and_release(self):
        with TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            lock = ts.SupervisorLock(tdir)
            lock.acquire()
            self.assertTrue((tdir / ".supervisor.lock").exists())
            lock.release()
            self.assertFalse((tdir / ".supervisor.lock").exists())

    def test_stale_lock_is_cleared(self):
        with TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            # 99999999 is virtually guaranteed to be a dead PID.
            (tdir / ".supervisor.lock").write_text("99999999", encoding="utf-8")
            lock = ts.SupervisorLock(tdir)
            lock.acquire()
            # After acquire, the lock should hold THIS process's pid.
            self.assertEqual(
                (tdir / ".supervisor.lock").read_text().strip(), str(os.getpid())
            )
            lock.release()

    def test_live_lock_blocks(self):
        with TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            # current process's pid is alive -> should block.
            (tdir / ".supervisor.lock").write_text(str(os.getpid()), encoding="utf-8")
            lock = ts.SupervisorLock(tdir)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                lock.acquire()
            # cleanup
            (tdir / ".supervisor.lock").unlink()


class WatchLoopTests(unittest.TestCase):
    def test_historical_pending_decision_does_not_pause_research(self):
        from research_harness.orchestrator.operator_prompts import enqueue_prompt

        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            enqueue_prompt(tdir, kind="decision_request", prompt="Choose evaluation")
            with mock.patch.object(ts, "spawn_codex_session", return_value=0) as spawn, \
                 mock.patch.object(ts, "advance_resumable_reorientation", return_value=None), \
                 mock.patch.object(ts.time, "time", side_effect=iter(range(0, 100000, 100))), \
                 mock.patch.object(ts.time, "sleep") as sleep:
                result = ts.watch_thread(repo, "t1", max_cycles=1)
            self.assertEqual(spawn.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(result["status"], "max_cycles_exceeded")

    def test_dual_gate_pass_exits_terminal(self):
        # PR8: only dual-gate pass terminates.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({"achieved": True}), encoding="utf-8"
            )
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {
                        "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                    },
                }),
                encoding="utf-8",
            )
            _write_verified_terminal_state(tdir)
            with mock.patch.object(ts, "spawn_codex_session") as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01, max_cycles=3
                )
            spawn.assert_not_called()
            self.assertEqual(result["status"], "terminal")
            self.assertEqual(result["outcome"], "accept_with_goal_achieved")

    def test_cold_start_spawns_first_cycle_despite_low_idle(self):
        # Fix 2: on cold start the only recent production/ write is the envelope
        # auto-bootstrap, so idle ~= 0 << max_idle. The first cycle must spawn
        # immediately instead of waiting the full max_idle window (the ~10-min
        # cold-start delay). Pre-fix, this loop would sleep forever and never
        # spawn (idle 5s never exceeds 9999s).
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            with mock.patch.object(ts, "spawn_codex_session", return_value=0) as spawn, \
                 mock.patch.object(ts, "mcp_idle_seconds", return_value=5.0):
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=9999.0, poll_seconds=0.01,
                    max_cycles=1, rate_limit_backoff_initial=0.001,
                )
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(result["status"], "max_cycles_exceeded")

    def test_physical_checkpoint_no_progress_ignores_stale_legacy_pause(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            no_progress = {
                "status": "checkpointed",
                "checkpoint_id": "checkpoint_" + "1" * 64,
                "supervisor_resume_kind": "physical_checkpoint_no_progress",
            }
            with mock.patch.object(
                ts,
                "advance_resumable_reorientation",
                return_value=no_progress,
            ), mock.patch.object(
                ts,
                "spawn_codex_session",
                return_value=0,
            ) as spawn:
                result = ts.watch_thread(
                    repo,
                    "t1",
                    max_idle_seconds=0.0,
                    poll_seconds=0.01,
                    max_cycles=1,
                    rate_limit_backoff_initial=0.001,
                )

            spawn.assert_called_once()
            self.assertEqual(result["status"], "max_cycles_exceeded")

    def test_completed_execution_yields_without_waiting_for_stall(self):
        with TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_codex.py"
            event = {"type": "item.completed", "item": {"id": "run1", "type": "mcp_tool_call",
                     "server": "research_harness", "tool": "execute_baseline_preflight",
                     "arguments": {}, "result": {}, "status": "completed"}}
            pending = {"type": "item.started", "item": {"id": "read2", "type": "mcp_tool_call",
                       "server": "research_harness", "tool": "get_research_state", "arguments": {}}}
            completed = {"type": "item.completed", "item": {**pending['item'], "result": {}, "status": "completed"}}
            marker = Path(tmp) / "pending_finished"
            fake.write_text("#!/usr/bin/env python3\nimport time\nfrom pathlib import Path\n"
                            + "print(" + repr(json.dumps(pending)) + ", flush=True)\n"
                            + "print(" + repr(json.dumps(event)) + ", flush=True)\ntime.sleep(0.2)\n"
                            + "Path(" + repr(str(marker)) + ").touch()\n"
                            + "print(" + repr(json.dumps(completed)) + ", flush=True)\ntime.sleep(30)\n")
            fake.chmod(0o755)
            with mock.patch.object(ts, "_which", return_value=str(fake)):
                started = time.time()
                rc = ts.spawn_codex_session("prompt", stall_timeout=20)
            self.assertEqual(rc, ts.WORK_UNIT_EXIT_CODE)
            self.assertTrue(marker.exists())
            self.assertLess(time.time() - started, 8)

    def test_spawn_watchdog_kills_hung_cycle(self):
        # Fix 3: a cycle that produces NO output and never exits (a hang — the
        # observed ToolSearch freeze) must be killed by the stall watchdog so
        # the supervisor recovers, instead of blocking forever on readline.
        with TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_codex.sh"
            fake.write_text("#!/bin/sh\nsleep 30\n")  # ignores args, emits nothing
            fake.chmod(0o755)
            with mock.patch.object(ts, "_which", return_value=str(fake)):
                t0 = time.time()
                rc = ts.spawn_codex_session("prompt", stall_timeout=0.5)
                elapsed = time.time() - t0
            self.assertLess(elapsed, 8.0, "watchdog should kill the hung cycle, not wait 30s")
            self.assertNotEqual(rc, 0, "killed cycle returns a non-zero exit code")

    def test_honest_failure_alone_does_NOT_terminate_under_pr8(self):
        # PR8: honest_failure is a retreat state. Supervisor keeps trying.
        # We use max_cycles=2 as emergency override to bound the test.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({"outcome": "honest_failure"}), encoding="utf-8"
            )
            with mock.patch.object(ts, "spawn_codex_session", return_value=0) as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01,
                    max_cycles=2, rate_limit_backoff_initial=0.001,
                )
            # spawn was called twice — supervisor kept trying despite honest_failure.
            self.assertEqual(spawn.call_count, 2)
            self.assertEqual(result["status"], "max_cycles_exceeded")

    def test_idle_thread_spawns_until_dual_gate(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            summary_path = tdir / "production" / "production_run_summary.json"
            attestation_path = tdir / "production" / "rebuttal" / "user_goal_attestation.json"

            def fake_spawn(prompt, **kw):  # noqa: ARG001
                attestation_path.parent.mkdir(parents=True, exist_ok=True)
                attestation_path.write_text(
                    json.dumps({"achieved": True}), encoding="utf-8"
                )
                summary_path.write_text(
                    json.dumps({
                        "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                        "publication_dispatch": {
                            "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                        },
                    }),
                    encoding="utf-8",
                )
                _write_verified_terminal_state(tdir)
                return 0

            with mock.patch.object(ts, "spawn_codex_session", side_effect=fake_spawn) as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01,
                    max_cycles=3, rate_limit_backoff_initial=0.001,
                )
            spawn.assert_called_once()
            self.assertEqual(result["status"], "terminal")

    def test_max_cycles_override_can_force_exit(self):
        # Emergency operator override still works for safety.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            with mock.patch.object(ts, "spawn_codex_session", return_value=0) as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01,
                    max_cycles=2, rate_limit_backoff_initial=0.001,
                )
            self.assertEqual(spawn.call_count, 2)
            self.assertEqual(result["status"], "max_cycles_exceeded")

    def test_unlimited_cycles_by_default(self):
        # Default (no max_cycles) = supervisor never quits on count.
        # We assert via lots of spawns + manual interrupt via mock.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            count = {"n": 0}
            attestation_path = tdir / "production" / "rebuttal" / "user_goal_attestation.json"

            def fake_spawn(prompt, **kw):  # noqa: ARG001
                count["n"] += 1
                # only complete on the 20th cycle.
                if count["n"] >= 20:
                    attestation_path.parent.mkdir(parents=True, exist_ok=True)
                    attestation_path.write_text(json.dumps({"achieved": True}), encoding="utf-8")
                    (tdir / "production" / "production_run_summary.json").write_text(
                        json.dumps({
                            "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                            "publication_dispatch": {
                                "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                            },
                        }),
                        encoding="utf-8",
                    )
                    _write_verified_terminal_state(tdir)
                return 0

            with mock.patch.object(ts, "spawn_codex_session", side_effect=fake_spawn):
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.001,
                    rate_limit_backoff_initial=0.001,
                    rate_limit_backoff_max=0.01,
                )
            self.assertEqual(result["status"], "terminal")
            self.assertGreaterEqual(count["n"], 20)

    def test_thread_dir_missing_raises(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "not found"):
                ts.watch_thread(Path(tmp), "no_such_thread")


class EnvelopeAutoBootstrapTests(unittest.TestCase):
    def _setup_repo(self, tmp: str, *, with_market: bool = True, registered_adapters=None):
        repo = Path(tmp)
        (repo / "runs" / "threads" / "t1" / "production").mkdir(parents=True)
        if with_market:
            mdir = repo / "runs" / "threads" / "t1" / "market"
            mdir.mkdir(parents=True, exist_ok=True)
            (mdir / "market_research_brief.json").write_text(json.dumps({
                "papers": [
                    {"id": "c1", "arxiv_id": "2401.12345"},
                    {"id": "c2", "pdf_path": "paper.pdf"},
                ]
            }), encoding="utf-8")
        settings = {
            "data_adapters": {
                "registered": registered_adapters or []
            }
        }
        (repo / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        return repo

    def test_bootstrap_writes_schema_valid_envelope(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "dataset.json"
            source.write_text("{}", encoding="utf-8")
            repo = self._setup_repo(tmp, registered_adapters=[
                {
                    "id": "wq_snap",
                    "materializer_type": "benchmark",
                    "role": "evaluation",
                    "source": str(source),
                    "provenance": "operator-curated",
                }
            ])
            env = ts.bootstrap_envelope_if_missing(repo, "t1", target_scope="directional")
            self.assertIsNotNone(env)
            self.assertEqual(env["thread_id"], "t1")
            # real adapter from settings shows up
            real_ids = [s["id"] for s in env["data_sources_available"] if s["kind"] == "real_adapter"]
            self.assertIn("wq_snap", real_ids)
            self.assertEqual(env["operator_intent"]["data_source_anchor"], "wq_snap")
            self.assertTrue(
                env["operator_intent"]["data_source_snapshot_id"].startswith("as_")
            )
            # synthetic always present as fallback
            synth_ids = [s["id"] for s in env["data_sources_available"] if s["kind"] == "synthetic"]
            self.assertTrue(synth_ids)
            # baseline provenance pulled from market dossier
            cand_ids = [b["candidate_id"] for b in env["baseline_provenance_available"]]
            self.assertIn("c1", cand_ids)
            # file actually written
            env_path = repo / "runs" / "threads" / "t1" / "production" / "feasibility_envelope.json"
            self.assertTrue(env_path.exists())

    def test_bootstrap_idempotent(self):
        with TemporaryDirectory() as tmp:
            repo = self._setup_repo(tmp)
            ts.bootstrap_envelope_if_missing(repo, "t1")
            second = ts.bootstrap_envelope_if_missing(repo, "t1")
            self.assertIsNone(second)  # respects existing envelope

    def test_existing_envelope_mismatched_selection_fails_closed(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "dataset.json"
            source.write_text("{}", encoding="utf-8")
            repo = self._setup_repo(tmp, registered_adapters=[
                {
                    "id": "wq_snap",
                    "materializer_type": "benchmark",
                    "role": "evaluation",
                    "source": str(source),
                    "provenance": "operator-curated",
                }
            ])
            ts.bootstrap_envelope_if_missing(
                repo,
                "t1",
                target_scope="directional",
                data_source_anchor="wq_snap",
            )

            with self.assertRaisesRegex(ValueError, "existing feasibility envelope"):
                ts.bootstrap_envelope_if_missing(
                    repo,
                    "t1",
                    target_scope="deployment",
                    data_source_anchor="wq_snap",
                )

    def test_bootstrap_without_market_dossier_uses_placeholder(self):
        with TemporaryDirectory() as tmp:
            repo = self._setup_repo(tmp, with_market=False)
            env = ts.bootstrap_envelope_if_missing(repo, "t1")
            self.assertIsNotNone(env)
            self.assertTrue(env["baseline_provenance_available"])
            self.assertEqual(
                env["baseline_provenance_available"][0]["candidate_id"],
                "no_market_baselines_found",
            )

    def test_bootstrap_target_scope_deployment_with_no_real_adapter_fails_closed(self):
        with TemporaryDirectory() as tmp:
            repo = self._setup_repo(tmp)
            with self.assertRaisesRegex(ValueError, "deployment requires"):
                ts.bootstrap_envelope_if_missing(repo, "t1", target_scope="deployment")
            self.assertFalse(
                (
                    repo
                    / "runs"
                    / "threads"
                    / "t1"
                    / "production"
                    / "feasibility_envelope.json"
                ).exists()
            )


class NeededResourcesTests(unittest.TestCase):
    def test_needs_collected_from_attestation_when_achieved_false(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({
                    "achieved": False,
                    "required_additional_research": [
                        {"axis": "boundary", "experiment": "SNR sweep", "rationale": "needed"},
                        {"axis": "validity", "experiment": "real data", "rationale": "deploy"},
                    ],
                }),
                encoding="utf-8",
            )
            needs = ts.collect_needed_resources(repo, "t1")
            self.assertEqual(len(needs), 2)
            self.assertEqual(needs[0]["axis"], "boundary")

    def test_envelope_deployment_without_real_adapter_logged_as_blocker(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "feasibility_envelope.json").write_text(
                json.dumps({
                    "operator_intent": {"target_deploy_grade_scope": "deployment"},
                    "data_sources_available": [{"kind": "synthetic", "id": "s1"}],
                }),
                encoding="utf-8",
            )
            needs = ts.collect_needed_resources(repo, "t1")
            self.assertTrue(any(n.get("type") == "data_adapter" for n in needs))

    def test_update_needed_resources_writes_yaml(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({"achieved": False, "required_additional_research": [
                    {"axis": "boundary", "experiment": "x", "rationale": "y"},
                ]}),
                encoding="utf-8",
            )
            needs = ts.update_needed_resources_file(repo, "t1")
            self.assertEqual(len(needs), 1)
            written = (tdir / "needed_resources.yaml").read_text(encoding="utf-8")
            self.assertIn("axis:", written)
            self.assertIn("boundary", written)


class WhichTests(unittest.TestCase):
    def test_which_finds_python(self):
        # Python is always on PATH in CI; sanity-check _which.
        self.assertIsNotNone(ts._which("python3") or ts._which("python"))

    def test_which_returns_none_for_missing_binary(self):
        self.assertIsNone(ts._which("definitely-not-a-real-binary-xyz123"))


class ResolveSupervisorModelTests(unittest.TestCase):
    """The production supervisor must honour the operator's configured model
    (the bug: it always spawned a hard-coded model)."""

    def _repo(self, tmp, *, default_model=None, mcp_model=None):
        tid = "thread_x"
        tdir = tmp / "runs" / "threads" / tid
        tdir.mkdir(parents=True, exist_ok=True)
        thread_json = {"thread_id": tid}
        if mcp_model is not None:
            thread_json["mcp_model"] = mcp_model
        (tdir / "thread.json").write_text(json.dumps(thread_json), encoding="utf-8")
        settings = {"runtime": {"llm_orchestrator": {"mcp": {}}}}
        if default_model is not None:
            settings["runtime"]["llm_orchestrator"]["mcp"]["default_model"] = default_model
        (tmp / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        return tid

    def test_uses_project_default_model(self):
        with TemporaryDirectory() as d:
            tmp = Path(d)
            tid = self._repo(tmp, default_model="gpt-5.6-sol")
            self.assertEqual(
                ts.resolve_supervisor_model(tmp, tid), "gpt-5.6-sol"
            )

    def test_thread_mcp_model_overrides_default(self):
        with TemporaryDirectory() as d:
            tmp = Path(d)
            tid = self._repo(tmp, default_model="gpt-5.6-sol",
                             mcp_model="gpt-5.6-terra")
            self.assertEqual(
                ts.resolve_supervisor_model(tmp, tid), "gpt-5.6-terra"
            )

    def test_falls_back_when_unset(self):
        with TemporaryDirectory() as d:
            tmp = Path(d)
            tid = self._repo(tmp)  # no default_model, no mcp_model
            self.assertEqual(
                ts.resolve_supervisor_model(tmp, tid), ts.DEFAULT_CODEX_MODEL
            )

    def test_cli_watch_resolves_model_when_not_passed(self):
        """`watch` with no --model resolves from settings, not the hard default."""
        with TemporaryDirectory() as d:
            tmp = Path(d)
            tid = self._repo(tmp, default_model="gpt-5.6-sol")
            captured = {}

            def fake_watch_thread(repo, thread_id, **kw):
                captured["model"] = kw.get("model")
                return {"status": "terminal", "outcome": "x"}

            with mock.patch.object(ts, "watch_thread", fake_watch_thread):
                ts.main(["watch", tid, "--repo-root", str(tmp)])
            self.assertEqual(captured["model"], "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()
