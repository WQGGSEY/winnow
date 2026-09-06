"""Thread supervisor for hands-free production sessions.

Watches a single thread, spawns Codex JSONL subprocesses when the
MCP server has been idle past a threshold, feeds the resume prompt
deterministically, and only terminates when the thread reaches a verified
strong-result publication.

This is the missing piece between the MCP server (which exposes tools but
needs an agent session to drive them) and operator hands-free
operation. The operator runs:

    python -m research_harness.thread_supervisor watch <thread_id>

once. The supervisor handles every subsequent session respawn until the
thread terminates. The spawned Codex subprocess uses the operator's ChatGPT login.

Design constraints:
- ChatGPT login only. No direct API path.
- Single supervisor per thread (lock file enforced).
- Survives subprocess crashes, Codex session limits, MCP daemon
  restarts. The only fatal state is operator SIGINT or thread terminal.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from research_harness.agent_runtime import AgentPrompt, ResearchHarnessMcp
from research_harness.adapters.codex_cli import CodexCliAdapter

from research_harness.config import load_settings
from research_harness.orchestrator.blind_sequential_research import (
    BlindSequentialResearchError,
    StrongResultBinding,
    VerifiedStrongResult,
    resolve_attempt_node_binding,
    resolve_strong_result_binding,
    strong_result_receipt_sha256,
)

LOG = sys.stderr
WORK_UNIT_EXIT_CODE = 75


# --- thresholds (configurable via CLI flags) ----------------------------- #

DEFAULT_MAX_IDLE_SECONDS = 600.0   # 10 min — an agent session is typically idle
                                   #          between MCP calls during heavy
                                   #          experiments; we want to detect
                                   #          true death, not a long step.
DEFAULT_POLL_SECONDS = 30.0        # check every 30s
DEFAULT_MILESTONE_CYCLE = 10       # informational logging milestone (PR8:
                                   #          no longer a termination cause —
                                   #          supervisor never quits on count)
DEFAULT_CODEX_BOOT_DELAY = 0.0
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_STALL_TIMEOUT = 600.0  # kill a Codex cycle that emits NO output
                                      #   for this long (a hang — e.g. model/API
                                      #   stall after a tool call). Distinct from
                                      #   the fast-fail EXIT detector, which never
                                      #   fires on a hang because the process
                                      #   never exits. A healthy cycle streams
                                      #   tool_use/tool_result lines continuously.

# PR8: rate-limit backoff. When the Codex subprocess fails fast (exit < 30s)
# we treat it as a likely rate-limit and back off exponentially. Backoff
# resets when a cycle runs healthily for >= RATE_LIMIT_HEALTHY_CYCLE_SECONDS.
DEFAULT_RATE_LIMIT_BACKOFF_INITIAL = 60.0
DEFAULT_RATE_LIMIT_BACKOFF_MAX = 1800.0
RATE_LIMIT_FAST_FAIL_SECONDS = 30.0
RATE_LIMIT_HEALTHY_CYCLE_SECONDS = 300.0


# --- terminal-state detection ------------------------------------------- #


def _thread_dir(repo: Path, tid: str) -> Path:
    return repo / "runs" / "threads" / tid


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _terminal_evidence_is_strong(
    *,
    repo: Path,
    tid: str,
    state: dict[str, object],
    adaptive: dict[str, object],
    receipt: dict[str, object],
    promoted_node: dict[str, object],
    artifacts: dict[str, dict[str, object]],
    experiment_plan: dict[str, object],
    worker_report: dict[str, object],
) -> bool:
    """Re-derive strong completion from typed evidence, not receipt labels."""

    try:
        from research_harness.construct_adversary import (
            evaluate_construct_adversary_report,
        )
        from research_harness.falsifier import (
            PRODUCED_BY as FALSIFIER_PRODUCED_BY,
            evaluate_predicate,
            transfer_evidence_admissible,
        )
        from research_harness.mcp_server import _construct_adversary_thresholds
        from research_harness.orchestrator.adaptive_search import (
            experiment_fingerprint,
            strategy_fingerprint,
        )
        from research_harness.orchestrator.strong_result import (
            StrongExecutionEvidenceError,
            verify_strong_execution_evidence,
        )
        from research_harness.schemas.validator import validate_named_schema

        validate_named_schema("node", promoted_node)
        validate_named_schema("experiment_plan", experiment_plan)
        validate_named_schema("worker_report", worker_report)
        validate_named_schema("falsifier_result", artifacts["falsifier_result"])
        validate_named_schema("ac_decision", artifacts["ac_decision"])
        validate_named_schema("user_goal_attestation", artifacts["attestation"])

        frozen_question = json.loads(
            (
                _thread_dir(repo, tid)
                / "production"
                / "frozen_question.json"
            ).read_text(encoding="utf-8")
        )
        validate_named_schema("frozen_question", frozen_question)
        stamped_construct = artifacts["construct_adversary"]
        raw_construct = {
            key: value
            for key, value in stamped_construct.items()
            if key not in {"harness_verdict", "harness_reasons"}
        }
        validate_named_schema("construct_adversary_report", raw_construct)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False

    strategy = promoted_node.get("strategy") or {}
    if not isinstance(strategy, dict):
        return False
    strategy_id = receipt.get("strategy_id")
    if strategy_fingerprint(
        mechanism=str(strategy.get("mechanism") or ""),
        intervention=str(strategy.get("intervention") or ""),
    ) != strategy_id:
        return False
    goal = adaptive.get("goal") or {}
    if not isinstance(goal, dict) or strategy.get("goal_id") != goal.get("id"):
        return False
    registered_strategy = next(
        (
            candidate
            for candidate in adaptive.get("strategies") or []
            if isinstance(candidate, dict) and candidate.get("id") == strategy_id
        ),
        None,
    )
    if registered_strategy != strategy:
        return False
    promoted_node_id = receipt.get("promoted_node_id")
    if (
        promoted_node.get("id") != promoted_node_id
        or promoted_node.get("status") != "promoted"
        or promoted_node_id not in (state.get("promoted_node_ids") or [])
    ):
        return False

    experiment_id = experiment_fingerprint(experiment_plan)
    if (
        receipt.get("experiment_id") != experiment_id
        or experiment_plan.get("node_id") != promoted_node_id
        or worker_report.get("node_id") != promoted_node_id
    ):
        return False
    executed = next(
        (
            experiment
            for experiment in adaptive.get("experiments") or []
            if isinstance(experiment, dict)
            and experiment.get("id") == experiment_id
            and experiment.get("node_id") == promoted_node_id
            and experiment.get("strategy_id") == strategy_id
            and experiment.get("status") == "executed"
        ),
        None,
    )
    if executed is None:
        return False
    if (
        worker_report.get("status") != "completed"
        or worker_report.get("claim_verdict_candidate") != "supported"
        or (worker_report.get("baseline_evidence_status") or {}).get("overall")
        != "passed"
        or worker_report.get("disproof_conditions_hit")
    ):
        return False
    node_dir = (
        _thread_dir(repo, tid)
        / "production"
        / "tree"
        / "nodes"
        / str(promoted_node_id)
    )
    try:
        execution_evidence = verify_strong_execution_evidence(
            node=promoted_node,
            experiment_plan=experiment_plan,
            worker_report=worker_report,
            node_dir=node_dir,
            tree_dir=node_dir.parent.parent,
            settings=load_settings(repo) if (repo / "settings.json").exists() else {},
        )
    except StrongExecutionEvidenceError:
        return False
    if any(
        receipt.get(key) != execution_evidence[key]
        for key in (
            "job_manifest_sha256",
            "runner_result_sha256",
            "metrics_evidence_sha256",
        )
    ):
        return False

    falsifier = artifacts["falsifier_result"]
    frozen_falsifier = (goal.get("bar") or {}).get("external_falsifier") or {}
    predicate = falsifier.get("predicate") or {}
    if (
        falsifier.get("thread_id") != tid
        or falsifier.get("contract_id") != receipt.get("contract_id")
        or falsifier.get("attempt_id") != receipt.get("attempt_id")
        or falsifier.get("direction_id") != receipt.get("direction_id")
        or falsifier.get("node_id") != receipt.get("promoted_node_id")
        or falsifier.get("manifest_id")
        != receipt.get("acquisition_manifest_id")
        or falsifier.get("produced_by") != FALSIFIER_PRODUCED_BY
        or falsifier.get("kind") != "real_holdout"
        or falsifier.get("kind") != frozen_falsifier.get("kind")
        or falsifier.get("holdout_source_id")
        != frozen_falsifier.get("holdout_source_id")
        or predicate != (frozen_falsifier.get("predicate") or {})
        or falsifier.get("passed") is not True
        or falsifier.get("verdict") != "passed"
        or not transfer_evidence_admissible(falsifier)
        or not evaluate_predicate(
            float(falsifier.get("observed")),
            str(predicate.get("op")),
            float(predicate.get("threshold")),
        )
    ):
        return False

    construct = artifacts["construct_adversary"]
    settings = load_settings(repo) if (repo / "settings.json").exists() else {}
    min_budget, min_worlds = _construct_adversary_thresholds(settings)
    construct_verdict = evaluate_construct_adversary_report(
        raw_construct,
        min_budget=min_budget,
        min_distinct_worlds=min_worlds,
    )
    if (
        construct.get("harness_verdict") != "survived"
        or construct_verdict.get("verdict") != "survived"
        or construct.get("thread_id") != tid
        or construct.get("question_id") != frozen_question.get("question_id")
        or construct.get("construction_ref") != promoted_node_id
    ):
        return False

    ac = artifacts["ac_decision"]
    methodology = (ac.get("rebuttal_synthesis") or {}).get(
        "methodology_assessment", {}
    )
    if (
        ac.get("decision") not in {"accept", "revise"}
        or ac.get("blocking_reasons")
        or ac.get("required_next_search_nodes")
        or methodology.get("aggregate_verdict") != "provides"
    ):
        return False
    attestation = artifacts["attestation"]
    ledger = attestation.get("referent_ledger") or {}
    if (
        attestation.get("thread_id") != tid
        or attestation.get("promoted_node_id") != promoted_node_id
        or attestation.get("achieved") is not True
        or attestation.get("attested_status") != "goal_achieved"
        or attestation.get("verdict_strength") != "transfer_valid"
        or attestation.get("required_additional_research")
        or ledger.get("has_real_referent") is not True
        or ledger.get("max_reachable_verdict") != "transfer_valid"
        or (attestation.get("scope_attainment") or {}).get("narrowed") is True
        or receipt.get("verdict_strength") != "transfer_valid"
    ):
        return False
    return True


def resolve_supervisor_model(repo: Path, tid: str) -> str:
    """Model the production supervisor drives Codex with.

    Implements the documented contract (settings.json _model_comment):
    'thread.json.mcp_model overrides default_model'. Resolution order:
      1. thread.json.mcp_model        — per-thread frontend selection
      2. runtime.llm_orchestrator.mcp.default_model  — operator/project default
      3. DEFAULT_CODEX_MODEL          — last-resort fallback

    Previously the supervisor always spawned a fixed model, so the
    production phase ignored every model setting the operator configured.
    """
    # 1. per-thread override.
    try:
        tj = json.loads((_thread_dir(repo, tid) / "thread.json").read_text(encoding="utf-8"))
        m = tj.get("mcp_model")
        if isinstance(m, str) and m.strip():
            return m.strip()
    except (OSError, json.JSONDecodeError):
        pass
    # 2. operator/project default from settings.json.
    try:
        s = json.loads((repo / "settings.json").read_text(encoding="utf-8"))
        m = (
            s.get("runtime", {})
            .get("llm_orchestrator", {})
            .get("mcp", {})
            .get("default_model")
        )
        if isinstance(m, str) and m.strip():
            return m.strip()
    except (OSError, json.JSONDecodeError):
        pass
    # 3. fallback.
    return DEFAULT_CODEX_MODEL


def is_terminal(
    repo: Path,
    tid: str,
    *,
    require_rendered: bool = True,
    expected_binding: StrongResultBinding | None = None,
    expected_search_state_sha256: str | None = None,
) -> tuple[bool, str | None]:
    """Return true only for a rendered, verified strong result.

    Negative papers, bounded results, construct-valid screens, and unverified
    screens remain useful artifacts. They do not satisfy the research goal.
    Adaptive runs additionally require the typed strong-result receipt in the
    canonical search state.
    """
    pdir = _thread_dir(repo, tid) / "production"
    if require_rendered:
        summary_path = pdir / "production_run_summary.json"
        if not summary_path.exists():
            return False, None
        try:
            s = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, None

        rebuttal = s.get("rebuttal_summary") or {}
        rendered = (s.get("publication_dispatch") or {}).get("rendered_artifacts")
        attestation = rebuttal.get("user_goal_attestation")
        if not attestation:
            attestation_path = pdir / "rebuttal" / "user_goal_attestation.json"
            if attestation_path.exists():
                try:
                    attestation = json.loads(
                        attestation_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    attestation = None
        attestation = attestation or {}
        if s.get("outcome") in {"honest_failure", "bounded_result"}:
            return False, None
        ac_decision = (rebuttal.get("ac_decision") or {}).get("decision")
        if ac_decision not in {"accept", "revise"} or not rendered:
            return False, None
        if not attestation.get("achieved"):
            return False, None

    state_path = pdir / "tree" / "search_state.json"
    if not state_path.exists():
        return False, None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        adaptive = state.get("adaptive") or {}
    except (OSError, json.JSONDecodeError):
        return False, None
    if (
        expected_search_state_sha256 is not None
        and _canonical_json_sha256(state) != expected_search_state_sha256
    ):
        return False, None
    if not adaptive:
        return False, None
    try:
        from research_harness.orchestrator.goal_contract import (
            parse_goal_contract,
            project_research_goal,
        )

        contract = parse_goal_contract(
            json.loads(
                (
                    pdir / "reorientation" / "goal_contract.json"
                ).read_text(encoding="utf-8")
            )
        )
        from research_harness.memory.baseline_review import require_goal_baseline_approval
        from research_harness.orchestrator.goal_contract import serialize_goal_contract
        require_goal_baseline_approval(repo, pdir.parent, serialize_goal_contract(contract))
        rebuilt_goal = project_research_goal(contract)
    except (OSError, json.JSONDecodeError, ValueError):
        return False, None
    frozen_goal = adaptive.get("goal") or {}
    if (
        frozen_goal.get("source") != "pre_generation"
        or frozen_goal.get("strong_completion_blocked") is True
        or rebuilt_goal != frozen_goal
    ):
        return False, None
    receipt = adaptive.get("strong_result_receipt") or {}
    strategy_id = receipt.get("strategy_id")
    promoted_node_id = receipt.get("promoted_node_id")
    known_strategy_ids = {
        strategy.get("id")
        for strategy in adaptive.get("strategies") or []
        if isinstance(strategy, dict) and strategy.get("id")
    }
    nodes_by_id = {
        node.get("id"): node
        for node in state.get("nodes") or []
        if isinstance(node, dict) and node.get("id")
    }
    promoted_node = nodes_by_id.get(promoted_node_id) or {}
    executed_strategy = any(
        isinstance(experiment, dict)
        and experiment.get("node_id") == promoted_node_id
        and experiment.get("strategy_id") == strategy_id
        and experiment.get("status") == "executed"
        for experiment in adaptive.get("experiments") or []
    )
    rebuttal_dir = pdir / "rebuttal"
    node_dir = pdir / "tree" / "nodes" / str(promoted_node_id)
    artifact_hashes = {
        "falsifier_result_sha256": (
            rebuttal_dir / "falsifier_result.json",
            "falsifier_result",
        ),
        "construct_adversary_sha256": (
            rebuttal_dir / "construct_adversary_report.json",
            "construct_adversary",
        ),
        "ac_decision_sha256": (
            rebuttal_dir / "ac_decision.json",
            "ac_decision",
        ),
        "critic_resolution_sha256": (
            rebuttal_dir / "orchestrator_reduction.json",
            "critic_resolution",
        ),
        "attestation_sha256": (
            rebuttal_dir / "user_goal_attestation.json",
            "attestation",
        ),
        "experiment_plan_sha256": (
            node_dir / "experiment_plan.json",
            "experiment_plan",
        ),
        "worker_report_sha256": (
            node_dir / "worker_report.json",
            "worker_report",
        ),
    }
    hashes_match = True
    loaded_artifacts: dict[str, dict[str, object]] = {}
    for receipt_key, (artifact_path, artifact_name) in artifact_hashes.items():
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            hashes_match = False
            break
        if not isinstance(artifact, dict):
            hashes_match = False
            break
        loaded_artifacts[artifact_name] = artifact
        if receipt.get(receipt_key) != _canonical_json_sha256(artifact):
            hashes_match = False
            break
    if (
        adaptive.get("disposition") != "goal_achieved"
        or receipt.get("verified") is not True
        or receipt.get("goal_id") != (adaptive.get("goal") or {}).get("id")
        or receipt.get("bar_digest")
        != (adaptive.get("goal") or {}).get("bar_digest")
        or not strategy_id
        or strategy_id not in known_strategy_ids
        or (promoted_node.get("strategy") or {}).get("id") != strategy_id
        or not executed_strategy
        or not hashes_match
    ):
        return False, None
    if receipt.get("contract_id") != contract.contract_id:
        return False, None
    try:
        node_attempts = json.loads(
            (
                pdir / "reorientation" / "node_attempts.json"
            ).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False, None
    if not isinstance(node_attempts, Mapping):
        return False, None
    promoted_node_id = receipt.get("promoted_node_id")
    if not isinstance(promoted_node_id, str):
        return False, None
    try:
        resolved_binding = resolve_attempt_node_binding(
            node_attempts,
            contract_id=contract.contract_id,
            attempt_id=receipt.get("attempt_id"),
            direction_id=receipt.get("direction_id"),
            node_id=promoted_node_id,
        )
    except BlindSequentialResearchError:
        return False, None
    if (
        resolved_binding.manifest_id != receipt.get("acquisition_manifest_id")
        or (expected_binding is not None and resolved_binding != expected_binding)
    ):
        return False, None
    promoted_strategy = promoted_node.get("strategy") or {}
    node_artifacts = (promoted_node.get("outputs") or {}).get("artifacts") or []
    manifest_refs = [
        value.split(":", 1)[1]
        for value in node_artifacts
        if isinstance(value, str) and value.startswith("acquisition_manifest:")
    ]
    expected_manifest_id = receipt.get("acquisition_manifest_id")
    if (
        promoted_strategy.get("derived_from_direction_id")
        != receipt.get("direction_id")
        or manifest_refs != [expected_manifest_id]
        or (promoted_node.get("claim_contract") or {}).get("data_source_anchor")
        != f"acquisition_manifest:{expected_manifest_id}"
    ):
        return False, None
    try:
        from research_harness.acquisition import PublicAcquisition

        pinned_manifest = PublicAcquisition(
            pdir / "reorientation" / "acquisition_cache"
        ).verify_manifest(
            str(expected_manifest_id),
            node_id=str(receipt.get("promoted_node_id")),
        )
    except (OSError, ValueError):
        return False, None
    if (
        pinned_manifest.attempt_id != receipt.get("attempt_id")
        or pinned_manifest.direction_id != receipt.get("direction_id")
        or pinned_manifest.manifest_id != expected_manifest_id
    ):
        return False, None
    if not _terminal_evidence_is_strong(
        repo=repo,
        tid=tid,
        state=state,
        adaptive=adaptive,
        receipt=receipt,
        promoted_node=promoted_node,
        artifacts=loaded_artifacts,
        experiment_plan=loaded_artifacts["experiment_plan"],
        worker_report=loaded_artifacts["worker_report"],
    ):
        return False, None

    from research_harness.orchestrator.confirmation_use import (
        confirmation_evidence_from_result, digest_confirmation_evidence,
        verify_terminal_confirmation,
    )
    try:
        from research_harness.confirmation_sampling import active_sampling_registration
        if active_sampling_registration(pdir.parent):
            from dataclasses import asdict
            from research_harness.orchestrator.confirmation_execution import verified_confirmation_receipt
            measured = verified_confirmation_receipt(pdir.parent, asdict(resolved_binding))
            if confirmation_evidence_from_result(loaded_artifacts['falsifier_result']).get('observed') != measured['observed']:
                return False, None
        verify_terminal_confirmation(
            pdir / "reorientation" / "confirmation_use.json", contract,
            binding={"contract_id": resolved_binding.contract_id,
                     "attempt_id": resolved_binding.attempt_id,
                     "direction_id": resolved_binding.direction_id,
                     "node_id": resolved_binding.node_id,
                     "manifest_id": resolved_binding.manifest_id},
            evidence_digest=digest_confirmation_evidence(
                contract, confirmation_evidence_from_result(loaded_artifacts["falsifier_result"])),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False, None

    receipt_digest = strong_result_receipt_sha256(receipt)
    if require_rendered:
        from research_harness.publishing.integrity import verify_publication_receipt

        if not verify_publication_receipt(
            pdir, s.get("publication_dispatch") or {},
            s.get("publication_receipt_sha256"), receipt_digest,
        ):
            return False, None
        from research_harness.publishing.submission import verify_submission
        if not verify_submission(pdir / 'publication'):
            return False, None
        try:
            from research_harness.orchestrator.blind_reorientation import (
                GoalAchieved,
                parse_reorientation_state,
            )

            reorientation = parse_reorientation_state(
                json.loads(
                    (
                        pdir / "reorientation" / "state.json"
                    ).read_text(encoding="utf-8")
                )
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return False, None
        if (
            not isinstance(reorientation.phase, GoalAchieved)
            or reorientation.contract_id != contract.contract_id
            or reorientation.phase.strong_result_receipt_sha256 != receipt_digest
        ):
            return False, None

    return True, "accept_with_goal_achieved"


def verify_strong_result_binding(
    repo: Path,
    tid: str,
    binding: StrongResultBinding,
) -> VerifiedStrongResult | None:
    try:
        state = json.loads(
            (
                _thread_dir(repo, tid)
                / "production"
                / "tree"
                / "search_state.json"
            ).read_text(encoding="utf-8")
        )
        receipt = (state.get("adaptive") or {})["strong_result_receipt"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    verified, _ = is_terminal(
        repo,
        tid,
        require_rendered=False,
        expected_binding=binding,
        expected_search_state_sha256=_canonical_json_sha256(state),
    )
    if not verified:
        return None
    return VerifiedStrongResult(
        binding=binding,
        receipt_sha256=strong_result_receipt_sha256(receipt),
    )


def advance_resumable_reorientation(
    repo: Path,
    tid: str,
) -> dict[str, object] | None:
    from research_harness.mcp_server import recover_prepared_blind_terminal
    from research_harness.orchestrator.blind_mcp_adapter import (
        adaptive_writer_lock,
        build_blind_research_engine,
    )
    from research_harness.orchestrator.blind_reorientation import (
        AcquisitionReserved,
        Checkpointed,
        GoalAchieved,
    )
    from research_harness.settings_scoped import resolve_for_thread

    thread_dir = _thread_dir(repo, tid)
    recovered_terminal = recover_prepared_blind_terminal(
        tid,
        repo_root=repo,
    )
    if recovered_terminal is not None:
        resume_kind = (
            "strong_terminal_recovery"
            if recovered_terminal.get("status") == "ok"
            else "strong_terminal_recovery_failed"
        )
        return {
            **recovered_terminal,
            "supervisor_resume_kind": resume_kind,
        }
    state_path = thread_dir / "production" / "reorientation" / "state.json"
    if not state_path.exists():
        return None
    engine = build_blind_research_engine(
        repo_root=repo,
        thread_dir=thread_dir,
        writer_lock=lambda: adaptive_writer_lock(thread_dir),
        settings=resolve_for_thread(repo, tid),
    )
    state = engine.read_state()
    if state is None or isinstance(state.phase, GoalAchieved):
        return None
    if isinstance(state.phase, Checkpointed):
        if (
            state.phase.checkpoint.reason not in {
                "time_budget",
                "cost_budget",
                "download_budget",
                "request_budget",
            }
            or not isinstance(state.phase.continuation, AcquisitionReserved)
        ):
            return None
        trigger = (
            f"checkpoint:{state.phase.checkpoint.checkpoint_id}:"
            f"revision:{state.revision}"
        )
        resume_kind = "physical_checkpoint"
        expected_checkpoint_id = state.phase.checkpoint.checkpoint_id
        expected_strong_result_receipt_sha256 = None
    else:
        try:
            search_state = json.loads(
                (
                    thread_dir
                    / "production"
                    / "tree"
                    / "search_state.json"
                ).read_text(encoding="utf-8")
            )
            strong_receipt = (search_state.get("adaptive") or {}).get(
                "strong_result_receipt"
            )
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(strong_receipt, dict):
            return None
        try:
            binding = resolve_strong_result_binding(
                state,
                json.loads(engine.paths.node_attempts.read_text(encoding="utf-8")),
                node_id=str(strong_receipt.get("promoted_node_id")),
            )
        except (
            BlindSequentialResearchError,
            OSError,
            json.JSONDecodeError,
            ValueError,
        ):
            return None
        verified_strong_result = verify_strong_result_binding(repo, tid, binding)
        if verified_strong_result is None:
            return None
        trigger = "strong:" + verified_strong_result.receipt_sha256
        resume_kind = "strong_terminal"
        expected_checkpoint_id = None
        expected_strong_result_receipt_sha256 = (
            verified_strong_result.receipt_sha256
        )
    command_id = "supervisor_reorientation_" + hashlib.sha256(
        f"{tid}:{trigger}".encode("utf-8")
    ).hexdigest()
    result = engine.advance_research(
        command_id=command_id,
        expected_revision=state.revision,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_strong_result_receipt_sha256=(
            expected_strong_result_receipt_sha256
        ),
    )
    if (
        resume_kind == "physical_checkpoint"
        and result.get("status") == "checkpointed"
        and result.get("checkpoint_id") == expected_checkpoint_id
        and result.get("resumed_from_checkpoint_id") == expected_checkpoint_id
    ):
        resume_kind = "physical_checkpoint_no_progress"
    return {**result, "supervisor_resume_kind": resume_kind}


def collect_needed_resources(repo: Path, tid: str) -> list[dict[str, object]]:
    """Scan the thread state for unmet resources that block publication.

    Sources are the latest user-goal attestation and feasibility-envelope gaps.
    The supervisor logs this to needed_resources.yaml so the operator can
    see what's blocking and add the missing pieces.
    """
    pdir = _thread_dir(repo, tid) / "production"
    needs: list[dict[str, object]] = []

    # Source 1: latest attestation's required_additional_research.
    att_path = pdir / "rebuttal" / "user_goal_attestation.json"
    if att_path.exists():
        try:
            a = json.loads(att_path.read_text(encoding="utf-8"))
            if not a.get("achieved"):
                for r in a.get("required_additional_research") or []:
                    needs.append({
                        "type": "additional_research",
                        "axis": r.get("axis"),
                        "experiment": r.get("experiment", "")[:300],
                        "rationale": r.get("rationale", "")[:200],
                        "source": "user_goal_attestation",
                    })
        except (OSError, json.JSONDecodeError):
            pass

    # Source 2: feasibility envelope gaps. If target_scope=deployment and
    # no real_adapter is declared, that's a structural blocker the operator
    # must address by registering an adapter.
    env_path = pdir / "feasibility_envelope.json"
    if env_path.exists():
        try:
            env = json.loads(env_path.read_text(encoding="utf-8"))
            target = (env.get("operator_intent") or {}).get("target_deploy_grade_scope")
            real = [s for s in env.get("data_sources_available", []) if s.get("kind") == "real_adapter"]
            if target == "deployment" and not real:
                needs.append({
                    "type": "data_adapter",
                    "spec": "register a real_adapter under settings.json.data_adapters.registered",
                    "rationale": (
                        "operator_intent.target_deploy_grade_scope='deployment' but "
                        "no real_adapter is in the envelope. Without one, deployment-"
                        "scope claims are blocked."
                    ),
                    "source": "feasibility_envelope",
                })
        except (OSError, json.JSONDecodeError):
            pass

    return needs


def _format_needed_resources_yaml(needs: list[dict[str, object]]) -> str:
    """Hand-rolled YAML emitter compatible with the harness's
    parse_simple_yaml. Mirrors mcp_server._format_failure_index style."""
    if not needs:
        return "needs: []\n"
    lines = ["needs:"]
    for n in needs:
        lines.append(f"  - type: \"{_q(n.get('type', ''))}\"")
        for k in ("axis", "spec", "experiment", "rationale", "source"):
            v = n.get(k)
            if v:
                lines.append(f"    {k}: \"{_q(str(v))}\"")
    return "\n".join(lines) + "\n"


def _q(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ")


def update_needed_resources_file(repo: Path, tid: str) -> list[dict[str, object]]:
    """Write the current needed_resources snapshot to disk. Returns the
    list so the supervisor can include it in the next resume prompt."""
    needs = collect_needed_resources(repo, tid)
    path = _thread_dir(repo, tid) / "needed_resources.yaml"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_format_needed_resources_yaml(needs), encoding="utf-8")
    except OSError:
        pass
    return needs


# --- MCP idle detection ------------------------------------------------- #


def mcp_idle_seconds(repo: Path, tid: str) -> float:
    """Return seconds since the most recent file mtime under the thread's
    production directory. Used as a proxy for MCP activity because research
    control and evidence submission calls write at least one production file.

    Returns inf when the production dir is empty or missing.
    """
    pdir = _thread_dir(repo, tid) / "production"
    if not pdir.exists():
        return float("inf")
    most_recent = 0.0
    for sub in pdir.rglob("*"):
        if sub.is_file():
            try:
                most_recent = max(most_recent, sub.stat().st_mtime)
            except OSError:
                continue
    if most_recent == 0.0:
        return float("inf")
    return max(0.0, time.time() - most_recent)


# --- envelope auto-bootstrap (supervisor side) -------------------------- #


def ensure_existing_envelope_selection_matches(
    repo: Path,
    tid: str,
    *,
    target_scope: str,
    data_source_anchor: str | None,
) -> None:
    """Fail closed when a resume request conflicts with frozen operator intent."""
    from research_harness.data_adapters import AdapterError

    env_path = _thread_dir(repo, tid) / "production" / "feasibility_envelope.json"
    if not env_path.exists():
        return
    try:
        envelope = json.loads(env_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"existing feasibility envelope is unreadable: {exc}") from exc
    intent = envelope.get("operator_intent") or {}
    existing_scope = intent.get("target_deploy_grade_scope")
    existing_anchor = intent.get("data_source_anchor")
    mismatches = []
    if existing_scope != target_scope:
        mismatches.append(f"scope is {existing_scope!r}, requested {target_scope!r}")
    if existing_anchor != data_source_anchor:
        mismatches.append(
            f"data_source_anchor is {existing_anchor!r}, requested {data_source_anchor!r}"
        )
    if mismatches:
        raise AdapterError(
            "existing feasibility envelope conflicts with this supervisor start: "
            + "; ".join(mismatches)
        )


def bootstrap_envelope_if_missing(
    repo: Path,
    tid: str,
    *,
    target_scope: str = "directional",
    data_source_anchor: str | None = None,
) -> dict[str, object] | None:
    """Auto-construct a FeasibilityEnvelope from settings.json + thread
    market dossier and write it to the thread's production dir, if no
    envelope exists yet.

    The operator should NOT have to hand-craft this on every new thread.
    The supervisor is the operator's stand-in: it reads what the harness
    actually has (registered data adapters, available oracle = the
    Codex ChatGPT session, compute envelope from settings) and
    builds the envelope. Returns the envelope dict that was written (or
    None if already present / failed).
    """
    env_path = _thread_dir(repo, tid) / "production" / "feasibility_envelope.json"
    if env_path.exists():
        ensure_existing_envelope_selection_matches(
            repo,
            tid,
            target_scope=target_scope,
            data_source_anchor=data_source_anchor,
        )
        return None

    # Read settings.
    settings_path = repo / "settings.json"
    settings: dict[str, object] = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    prod_term = settings.get("production_termination") or {}
    # Conservative default compute budget — operator can override by
    # adjusting settings.production_termination or by writing the
    # envelope themselves before launching supervisor.
    compute = {
        "max_runner_seconds_per_node": 900,
        "max_concurrent_nodes": 2,
        "max_total_node_hours": 8.0,
    }

    from research_harness.data_adapters import (
        AdapterError,
        ensure_thread_adapter_snapshots,
        select_snapshot,
    )

    snapshot_document = ensure_thread_adapter_snapshots(
        repo, _thread_dir(repo, tid)
    )
    data_sources: list[dict[str, object]] = []
    for snapshot in snapshot_document["snapshots"]:
        data_sources.append({
            "kind": "real_adapter",
            "id": snapshot["adapter_id"],
            "snapshot_id": snapshot["snapshot_id"],
            "scope_note": snapshot["provenance"],
        })
    data_sources.append({"kind": "synthetic", "id": "synthetic_generator_default"})

    try:
        selected_snapshot = select_snapshot(snapshot_document, data_source_anchor)
    except AdapterError:
        raise
    if target_scope == "deployment" and selected_snapshot is None:
        raise AdapterError("deployment requires one ready registered adapter")

    # Pull baseline provenance from the thread's market dossier candidate
    # (if present) so the envelope's baseline_provenance_available is real.
    baseline_prov: list[dict[str, object]] = []
    market_brief_path = _thread_dir(repo, tid) / "market" / "market_research_brief.json"
    if market_brief_path.exists():
        try:
            brief = json.loads(market_brief_path.read_text(encoding="utf-8"))
            for paper in brief.get("papers", []) or []:
                if not isinstance(paper, dict) or not paper.get("id"):
                    continue
                prov = (
                    paper.get("url")
                    or paper.get("arxiv_id")
                    or paper.get("doi")
                    or paper.get("repo_url")
                    or paper.get("pdf_path")
                    or paper.get("title")
                    or ""
                )
                if prov:
                    baseline_prov.append({
                        "candidate_id": paper["id"],
                        "provenance": str(prov),
                    })
        except (OSError, json.JSONDecodeError):
            pass
    if not baseline_prov:
        # Schema requires minItems=1; supply a transparent placeholder so
        # the envelope is schema-valid AND the operator can see that no
        # real baselines were grounded by market_research.
        baseline_prov.append({
            "candidate_id": "no_market_baselines_found",
            "provenance": "market_research did not produce paper-cited baseline candidates for this thread",
        })

    if target_scope not in {"deployment", "feasibility", "directional"}:
        target_scope = "directional"
    # acceptable_alternative_scopes always includes scopes weaker or equal
    # to the target — the system never silently upgrades.
    alts_order = ["deployment", "feasibility", "directional"]
    target_idx = alts_order.index(target_scope)
    acceptable = alts_order[target_idx:]

    # ADR 0006: the bootstrap NEVER invents a falsifier — it cannot author a
    # meaningful predicate or a held-out source the worker doesn't see. It
    # honestly declares kind="none", which caps the thread at
    # 'unverified_screen' (achieved=true structurally impossible). To reach
    # 'goal_achieved' the operator hand-writes a real_holdout falsifier (with
    # a predicate) before launch, or the Professor registers a
    # cross_generator_transfer falsifier via submit_feasibility_envelope.
    has_real = any(s.get("kind") == "real_adapter" for s in data_sources)
    falsifier = {"kind": "none", "registered_by": "supervisor_bootstrap"}
    max_attestable = "unverified_screen"
    bootstrap_note = (
        "Auto-bootstrapped by thread_supervisor from settings.json + "
        "market dossier. Operator can override by writing the envelope "
        "manually before launching supervisor. ADR 0006: external_falsifier "
        "defaults to kind='none' so max_attestable_status='unverified_screen' "
        "(achieved=true is refused). "
    )
    if has_real:
        bootstrap_note += (
            "A real_adapter IS registered — to reach goal_achieved, set "
            "external_falsifier.kind='real_holdout' with holdout_source_id "
            "= that adapter id and a predicate, then re-submit the envelope. "
        )
    else:
        bootstrap_note += (
            "Synthetic-only: the only air-gapped falsifier is "
            "cross_generator_transfer (held-out generator B), registered by "
            "the Professor before attestation. "
        )
    envelope = {
        "thread_id": tid,
        "data_sources_available": data_sources,
        "llm_oracles_available": [{
            "kind": "subscription_codex",
            "model": resolve_supervisor_model(repo, tid),
        }],
        "compute_budget": compute,
        "runtime_capabilities": list(
            dict.fromkeys(
                str(value)
                for value in (
                    ((settings.get("runtime") or {}).get("runner_capabilities") or [])
                    if isinstance(settings.get("runtime") or {}, dict)
                    else []
                )
            )
        ),
        "baseline_provenance_available": baseline_prov,
        "operator_intent": {
            "target_deploy_grade_scope": target_scope,
            "acceptable_alternative_scopes": acceptable,
        },
        "external_falsifier": falsifier,
        "max_attestable_status": max_attestable,
        "notes": bootstrap_note,
    }
    if selected_snapshot is not None:
        envelope["operator_intent"].update({
            "data_source_anchor": selected_snapshot["adapter_id"],
            "data_source_snapshot_id": selected_snapshot["snapshot_id"],
        })

    # Validate against the schema before writing. If validation fails, do
    # NOT write a bad envelope. Codex can then submit one itself.
    try:
        from research_harness.schemas.validator import validate_named_schema
        validate_named_schema("feasibility_envelope", envelope)
    except Exception as exc:  # noqa: BLE001
        # Surface the failure so the operator sees why the auto-bootstrap
        # didn't take.
        print(
            f"[supervisor] envelope auto-bootstrap failed schema check: {exc}",
            file=LOG,
        )
        return None

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        json.dumps(envelope, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return envelope


# --- resume prompt assembly --------------------------------------------- #


def _last_known_state_summary(repo: Path, tid: str) -> dict[str, object]:
    """Pull a few cheap facts from the thread state for the resume prompt.
    Best-effort — missing files return defaults."""
    from research_harness.runner.baseline_preflight import baseline_preparation_state

    pdir = _thread_dir(repo, tid) / "production"
    summary: dict[str, object] = {"thread_id": tid}
    state_path = pdir / "tree" / "search_state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            summary["promoted_node_ids"] = state.get("promoted_node_ids") or []
            summary["pruned_node_ids"] = state.get("pruned_node_ids") or []
            ready_or_running = [
                n["id"] for n in state.get("nodes", [])
                if n.get("status") in {
                    "ready", "running", "completed_worker_report",
                    "critic_reviewed", "orchestrator_reduced",
                }
            ]
            summary["mid_state_nodes"] = ready_or_running
            summary["search_state_status"] = state.get("status")
        except (OSError, json.JSONDecodeError):
            pass
    ac_path = pdir / "rebuttal" / "ac_decision.json"
    if ac_path.exists():
        try:
            ac = json.loads(ac_path.read_text(encoding="utf-8"))
            summary["last_ac_decision"] = ac.get("decision")
        except (OSError, json.JSONDecodeError):
            pass
    archived = sorted(p.name for p in _thread_dir(repo, tid).glob("production.attempt_*"))
    summary["archived_attempts"] = len(archived)
    summary["baseline_preparation"] = baseline_preparation_state(pdir.parent)
    work_path = pdir / "research_control" / "current.json"
    if work_path.exists():
        try:
            work = json.loads(work_path.read_text(encoding="utf-8"))
            summary["research_work"] = {
                "path": str(work_path),
                "work_id": work.get("work_id"),
                "status": work.get("status"),
                "decision": work.get("decision"),
                "outcome": work.get("outcome"),
                "prepared_implementation": work.get("prepared_implementation"),
                "binding": work.get("binding"),
            }
        except (OSError, json.JSONDecodeError):
            pass
    return summary


def build_resume_prompt(repo: Path, tid: str, cycle: int) -> str:
    from research_harness.orchestrator.operator_prompts import list_responses

    state = _last_known_state_summary(repo, tid)
    mid = state.get("mid_state_nodes") or []
    promoted = state.get("promoted_node_ids") or []
    archived = state.get("archived_attempts", 0)
    last_ac = state.get("last_ac_decision")
    needs = update_needed_resources_file(repo, tid)
    responses = [
        {"event_id": item["event_id"], "prompt": item["prompt"], "response": item["response"], "status": item["status"]}
        for item in list_responses(repo / "runs" / "threads" / tid)
    ]

    executable_overrides: dict[str, object] = {}
    environment_override_keys: list[str] = []
    settings_path = repo / "settings.json"
    if settings_path.exists():
        try:
            runtime = (
                json.loads(settings_path.read_text(encoding="utf-8")).get("runtime")
                or {}
            )
            if isinstance(runtime, dict):
                configured_executables = runtime.get("runner_executable_overrides") or {}
                if isinstance(configured_executables, dict):
                    executable_overrides = configured_executables
                configured_environment = runtime.get("runner_environment_overrides") or {}
                if isinstance(configured_environment, dict):
                    environment_override_keys = sorted(
                        str(key) for key in configured_environment
                    )
        except (OSError, json.JSONDecodeError):
            pass

    needs_block = ["needed_resources (불만족 상태 / 다음 cycle에 해결 시도):"]
    if needs:
        for n in needs:
            tag = n.get("type", "?")
            spec = n.get("spec") or n.get("experiment") or n.get("axis") or "-"
            needs_block.append(f"  - [{tag}] {str(spec)[:200]}")
    else:
        needs_block.append("  (없음 또는 첫 cycle)")

    lines = [
        f"research_harness MCP에 붙어있어. Thread {tid} 를 이어서 진행할 거야.",
        "",
        f"[supervisor cycle #{cycle}] 이전 세션이 한도로 종료됐어. 새 세션이야.",
        "디스크 상태를 읽고 어디서 멈췄는지 파악해서 이어가.",
        "",
        "**절대 종료 조건 (ADR 0013):** 이 연구는 강한 결과 하나로만 끝나:",
        "  GOAL ACHIEVED: submit_ac_decision ∈ {accept, revise} AND",
        "      submit_professor_user_goal_attestation achieved=true.",
        "      ※ ADR 0006: achieved=true는 external falsifier 통과 없이는",
        "        구조적으로 거부됨. 필요조건: envelope.max_attestable_status=",
        "        goal_achieved (= falsifier 등록됨) + 통과한 falsifier_result.",
        "  construct_valid_screen, unverified_screen, bounded_result는",
        "  진행 증거일 뿐 연구 완료가 아니다. 실행할 수 있는 새 전략이 없으면",
        "  advance_research가 이전 실패를 보지 않는 새 방향을 생성하게 해.",
        "",
        "현재 상태 (디스크 스냅샷):",
        f"  - promoted nodes: {promoted}",
        f"  - mid-state nodes (ready/running/completed_worker_report/...): {mid}",
        f"  - archived previous attempts: {archived}",
        f"  - last AC decision: {last_ac!r}",
        f"  - search state status: {state.get('search_state_status')!r}",
        "  - baseline preparation (not claim evidence): " + json.dumps(state['baseline_preparation'], ensure_ascii=False),
        "  - current research work: " + json.dumps(state.get('research_work'), ensure_ascii=False),
        "현재 작업의 outcome과 연결된 실행 기록부터 이어가. claim graph의 오래된 worker_report를 전체 연구의 마지막 실행으로 간주하지 마. 현재 작업의 완료는 논문이나 claim의 검증 완료를 뜻하지 않아.",
        "",
        *needs_block,
        "",
        "과거 연구자 피드백 (기록으로 참고하고 새 승인이나 답변을 기다리지 마):",
        json.dumps(responses, ensure_ascii=False),
        "",
        "Operator-owned experiment runtime:",
        "  - LocalRunner executable overrides: "
        + json.dumps(executable_overrides, ensure_ascii=False, sort_keys=True),
        "  - LocalRunner environment override keys: "
        + json.dumps(environment_override_keys, ensure_ascii=False),
        "  - ambient shell Python은 실험 Python이 아니다. 자원 가능 여부는 위",
        "    LocalRunner 설정이나 실제 execute_node_experiment 결과로 판정해.",
        "",
        "HTML 렌더 뒤 finalize_submission_package로 독립 논문 검토와 공식 양식 PDF/source archive까지 생성해야 종료할 수 있어. get_research_state의 publication_target을 따르고, 지정이 없으면 원래 사용자가 허용한 대상 중 적합한 지원 형식을 선택해.",
        "방향:",
        f"  1. get_research_state(thread_id=\"{tid}\") 로 정확한 현재 상태 확인.",
        "     새로운 실행 전 plan_research_work로 다음 작업의 근거·경쟁 예측·예산을 기록해.",
        "     반환된 work_id와 test를 실행 코드에 적용하고 그 작업의 예산을 넘기지 마.",
        "     kind=analysis는 resolve_research_work로 기존 근거에서 답해. 의미 해석을 위한 실험 코드를 만들지 마.",
        "     미래 sampler가 제공되면 replace_holdout=true, defer_holdout_generation=true로 과거 bank를 전부 폐기하고 생성 규칙만 먼저 등록해. kind=protocol_revision은 revise_evaluation_protocol로 notes 변경안과 근거를 독립 검토에 제출해. 원래 목표·평가 기준·미사용 holdout을 보존하고 변경 이력을 공개해.",
        "     한 실행이 끝나면 supervisor가 새 세션에서 결과를 해석하고 다음 작업을 계획한다.",
        "     실행 오류를 과학적 반박으로 해석하지 말고, 동일 관측이면 판별 검사로 원인을 좁혀.",
        "     planned 작업에 거절 사유와 dispatch_request_path가 있으면 저장된 요청의 입력 오류를 고쳐 같은 검사를 재시도해. 실행 전 거절은 새 연구 관측이 아니야.",
        "     구현 또는 프로토콜 검토가 필요한 기록의 부재나 절차의 한계를 드러내면 plan_research_work(reconsider_reason=...)로 절차를 재검토해. 목표와 기존 근거를 보존하고 불가능한 검사 구현을 반복하지 마.",
        "  2. needed_resources가 있으면 advance_research의 획득 경계로 해결해.",
        "     frozen bar를 좁히지 말고 checkpoint 또는 hard_external_block을 보존해.",
        "     기준선 승인은 claim 생성의 선행 조건이 아니다. 정식 노드가 없으면",
        "     advance_research로 주장과 실행 계획부터 만들고 기준선은 증거 요건으로 해결해.",
        "     protocol_required이면 미사용 holdout과 평가 판정식을 설계하여",
        "     submit_feasibility_envelope의 독립 검토에 제출하고 승인 후 advance_research로 돌아가.",
        "     baseline_evidence가 비어 있으면 기준선 미승인 상태이며 목표가 고정돼도",
        "     문헌 갱신·preflight·자격 검토를 진행할 수 있다. 비교 주장 채택 전에는 승인이 필요해.",
        "     문헌 원문을 찾아 방법을",
        "     검토하기 전에 hypotheses가 없으면 develop_research_hypotheses를 호출해.",
        "     선택된 가설의 작은 판별 실험으로 경쟁 설명을 구별하고 구현 전제를 확인해.",
        "     그 근거를 바탕으로 기준선을 구현하고 execute_baseline_preflight로 측정해.",
        "     가설 후보는 미검증 상태다. 비평의 ready_for_test를 검증된 주장으로 해석하지 마.",
        "     로컬 셸은 읽기 전용이다. 문헌 후보는 update_baseline_sources로,",
        "     실험 코드는 execute_baseline_preflight 또는 design_experiment_template로 제출해.",
        "     하네스 검증기나 운영자 설정 오류는 근거와 함께 보고하고 직접 수정하지 마.",
        "     논문 메타데이터만 있는 것은 추가 검색이 필요한 상태다. 접근 실패를",
        "     실제로 확인한 자원만 external block으로 기록해. 사람의 답변을 기다리지 마.",
        "  3. get_next_admissible_node를 호출해. 권위 있는 blind active node가 있으면",
        "     그 노드만 재개하고, 없으면 advance_research로 새 방향을 준비한다.",
        "  4. checkpointed이면 응답의 expected_revision과 checkpoint_id를 유지한",
        "     채 다음 물리 사이클에서 재개해.",
        "  5. publish 직전 단계 도달하면: (a) submit_ac_decision ∈",
        "       {accept, revise}; (b) compute_falsifier_result로 held-out 검증",
        "       등록된 real holdout과 고정 predicate를 사용);",
        "       (c) 통과하면 submit_professor_user_goal_attestation achieved=true.",
        "       falsifier가 fail하면 achieved=true 불가 — 파이프라인 개선 후 재측정.",
        "  6. 음성 결정은 submit_professor_decision에 pruned로 제출하고",
        "     follow-up claim을 생성하지 마. 하네스가 실패 lesson을 비공개로 저장한 뒤",
        "     GoalContract + random perspective만으로 다음 방향을 독립 생성한다.",
        "",
        "Anti-laziness 룰 작동 중 (PR1):",
        "  - claim narrowing-without-breadth → reject",
        "  - 인용만으로 구현이 그 논문 방법의 재현임을 인정하지 마.",
        "    실행 영수증은 실행 사실만 증명한다. 원문의 방법과 구현을 대조하고",
        "    차이와 역할 적합성을 검토해. 임의 정책에 논문 이름을 붙이지 마.",
        "    naive/random 대조군은 실제 구현 그대로 기술하고 관련 없는 인용을 붙이지 마.",
        "  - synthetic data without bridging argument → reject",
        "  - disclaimer-only camera_ready_directives (revise일 때) → reject",
        "  - capability claim without decision_rule → reject",
        "Anti-laziness 통과 못 하면 retry 메시지가 explicit reason과 함께 와.",
        "",
        "Feasibility envelope (PR7, supervisor 자동 작성):",
        "  - feasibility_envelope.json이 production/ 에 이미 supervisor가 자동 작성한 상태.",
        "    평가 프로토콜 등록이 필요하면 submit_feasibility_envelope로 독립 리뷰를 받아.",
        "    초기 data_sources / llm_oracles / compute_budget / operator_intent 안에서 claim 설계.",
        "    envelope 못 맞추면 validate_claim_fits_envelope이 reject.",
        "  - envelope에 적힌 target_deploy_grade_scope을 claim_contract.deploy_grade_scope에",
        "    그대로 박고, data_source_anchor도 envelope에 적힌 real_adapter id 또는",
        "    'synthetic:<label>' 중 하나로.",
        "",
        "Memory (PR4):",
        "  - prior failures + active lessons는 prepare_rebuttal_packet /",
        "    prepare_paper_writing_context의 응답 payload에 inject됨.",
        "  - 너의 prune/contradicted 결정은 memory/failures/ 에 자동 기록됨.",
        "",
        "세션 한도 가까워지면 self-judge로 멈춰. 한 줄 status 남기고 종료해.",
        "supervisor가 곧 새 cycle spawn 해서 이어받을 거야. 약한 screen이나",
        "historical honest_failure를 완료로 취급하지 마. 새 작업이 불가능하면",
        "advance_research가 반환한 checkpoint 또는 hard_external_block을 남겨.",
        "",
        "Production은 human hands-off다. 연구 판단은 직접 수행하고 독립 리뷰의 거절은 수정과 재실험으로 해결해.",
        "과거 미응답 operator prompt는 대기 조건이 아니다. 필요한 가정과 근거를 기록하고 계속해.",
        "submit_baseline_qualification은 독립 리뷰를 실행한다. 승인되면 계속하고 거절이면 구체적 결함을 수정해.",
        "평가 프로토콜은 submit_feasibility_envelope로 독립 심사 후 등록해. 계산 한도와 고정 목표는 바꾸지 마.",
        "지금 시작:",
    ]
    return "\n".join(lines)


# --- pty subprocess spawn ----------------------------------------------- #


def _which(name: str) -> str | None:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        full = os.path.join(p, name)
        if os.access(full, os.X_OK):
            return full
    return None


def _experiment_running(state_path: Path) -> "bool | None":
    """Is a node experiment currently running? Reads the production search_state.
    Returns True (a node has status 'running'), False (readable, none running),
    or None (state unreadable — caller treats as 'can't tell' and defers)."""
    try:
        if not state_path.exists():
            return False
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return any(n.get("status") == "running" for n in (data.get("nodes") or []))


def _should_terminate_stall(
    silent: float,
    stall_timeout: float,
    hard_cap: "float | None",
    experiment_running: "bool | None",
) -> bool:
    """Stall-watchdog decision. A synchronous execute_node_experiment blocks
    agent output for the experiment's whole duration, so a legit long experiment
    looks identical to a hang. Kill only a TRUE hang (silent past stall_timeout
    AND no node experiment running), or ANY silence past the hard cap."""
    if silent <= stall_timeout:
        return False
    if hard_cap is not None and silent >= hard_cap:
        return True
    if experiment_running is False:   # readable state, no running node = true hang
        return True
    return False  # running (True) or unknown (None) -> defer up to the hard cap


def spawn_codex_session(
    prompt: str,
    *,
    repo_root: Path | None = None,
    thread_id: str | None = None,
    model: str = DEFAULT_CODEX_MODEL,
    boot_delay: float = DEFAULT_CODEX_BOOT_DELAY,
    log_path: Path | None = None,
    active_child_ref: dict | None = None,
    stall_timeout: float = DEFAULT_CODEX_STALL_TIMEOUT,
    state_path: Path | None = None,
    experiment_hard_cap: float | None = None,
) -> int:
    """Run one ephemeral Codex JSONL session with per-invocation MCP config."""
    import subprocess as _subprocess

    codex_bin = _which("codex")
    if not codex_bin:
        raise RuntimeError(
            "`codex` CLI not found in PATH. Install Codex and run `codex login`."
        )
    del boot_delay
    repo_root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    settings = load_settings(repo_root)
    mcp = ResearchHarnessMcp.from_settings(repo_root, settings)
    if thread_id is not None:
        mcp = replace(mcp, environment={**mcp.environment, "RESEARCH_HARNESS_THREAD_ID": thread_id})
    log_fh = log_path.open("a", encoding="utf-8") if log_path else None

    # Match the Professor's shell environment to the EXPERIMENT RUNNER's, so its
    # own hand-checks (`python -c "import backtester"`, `echo $COIN_DATA_DIR`, …)
    # report what the runner will actually see. Otherwise the Professor tests in a
    # DIFFERENT environment (system python, no experiment env), mis-diagnoses the
    # real-data path as unreachable, and escapes to synthetic data.
    #   (1) Harness venv first on PATH: the runner executes experiments with
    #       sys.executable (the venv python — see professor._coerce_experiment_plan),
    #       so the Professor's `python`/`python3` must resolve to that interpreter.
    #   (2) Merge the research_harness MCP server's declared env (COIN_DATA_DIR, …)
    #       — the SAME values the runner inherits, read from one source so the two
    #       environments cannot drift.
    child_env = os.environ.copy()
    venv_bin = Path(__file__).resolve().parents[1] / "venv" / "bin"
    if venv_bin.is_dir():
        child_env["PATH"] = f"{venv_bin}{os.pathsep}{child_env.get('PATH', '')}"
    child_env.update(mcp.environment)
    session = CodexCliAdapter(
        codex_path=codex_bin,
        popen=_subprocess.Popen,
    ).start_session(
        prompt=AgentPrompt(instructions="", input=prompt),
        model=model,
        cwd=repo_root,
        mcp=mcp,
        env=child_env,
    )
    if active_child_ref is not None:
        active_child_ref["pid"] = session.pid

    if log_fh:
        log_fh.write(f"\n=== codex spawn pid={session.pid} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log_fh.flush()
    events_fh = log_path.with_suffix(".events.jsonl").open("a", encoding="utf-8") if log_path else None
    if events_fh:
        events_fh.write(json.dumps({"event": "session_start", "pid": session.pid, "model": model, "prompt": prompt}, ensure_ascii=False) + "\n")
        events_fh.flush()

    # Stall watchdog: a healthy cycle streams events continuously. If the
    # subprocess emits NO output for stall_timeout (a hang — e.g. a model/API
    # stall after a tool call, which the fast-fail EXIT detector cannot catch
    # because the process never exits), terminate it so the supervisor recovers
    # instead of blocking forever in the readline loop below.
    last_activity = [time.time()]
    stop_watchdog = threading.Event()
    completed_calls = 0
    execution_completed = False
    pending_calls: set[str] = set()

    def _watchdog() -> None:
        check = max(1.0, min(15.0, stall_timeout / 4.0))
        deferred_logged = False
        while not stop_watchdog.wait(timeout=check):
            silent = time.time() - last_activity[0]
            if silent <= stall_timeout:
                deferred_logged = False
                continue
            running = _experiment_running(state_path) if state_path is not None else False
            if not _should_terminate_stall(silent, stall_timeout, experiment_hard_cap, running):
                # A node experiment is running (execute_node_experiment blocks
                # agent output for its whole duration, so defer the watchdog.
                if not deferred_logged:
                    dmsg = (f"\n=== watchdog: codex silent {silent:.0f}s but a node "
                            f"experiment is running — deferring kill (hard cap "
                            f"{experiment_hard_cap:.0f}s) ===\n")
                    for fh in (log_fh, LOG):
                        try:
                            if fh:
                                fh.write(dmsg)
                                fh.flush()
                        except Exception:  # noqa: BLE001
                            pass
                    deferred_logged = True
                continue
            reason = (
                "silence exceeded hard cap"
                if (experiment_hard_cap is not None and silent >= experiment_hard_cap)
                else f"no output for {stall_timeout:.0f}s and no running experiment"
            )
            msg = (f"\n=== watchdog: {reason} ({silent:.0f}s) — "
                   f"terminating codex (pid={session.pid}) ===\n")
            for fh in (log_fh, LOG):
                try:
                    if fh:
                        fh.write(msg)
                        fh.flush()
                except Exception:  # noqa: BLE001
                    pass
            session.terminate()
            try:
                session.wait(timeout=5)
            except _subprocess.TimeoutExpired:
                session.terminate(force=True)
            return

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()

    try:
        for event in session.events():
            last_activity[0] = time.time()
            if events_fh:
                events_fh.write(json.dumps({"pid": session.pid, "at": time.time(), "kind": event.kind, "raw": dict(event.raw)}, ensure_ascii=False) + "\n")
                events_fh.flush()
            formatted = f"{event.kind}> {event.summary[:400]}"
            stamped = f"[{time.strftime('%H:%M:%S')}] {formatted}"
            if log_fh:
                try:
                    log_fh.write(stamped + "\n")
                    log_fh.flush()
                except OSError:
                    pass
            try:
                LOG.write(stamped + "\n")
                LOG.flush()
            except Exception:  # noqa: BLE001
                pass
            item = event.raw.get('item', {})
            if item.get('type') == 'mcp_tool_call':
                if event.raw.get('type') == 'item.started':
                    pending_calls.add(item['id'])
                elif event.raw.get('type') == 'item.completed':
                    pending_calls.discard(item['id'])
                    completed_calls += 1
                    if item.get('tool') in {'execute_baseline_preflight', 'execute_node_experiment', 'resolve_research_work', 'revise_evaluation_protocol', 'execute_confirmation_experiment', 'design_experiment_template', 'finalize_submission_package'}:
                        for content in (item.get('result') or {}).get('content', []):
                            if content.get('type') == 'text':
                                try:
                                    payload = json.loads(content['text'])
                                except (ValueError, KeyError):
                                    continue
                                if isinstance(payload, dict) and (payload.get('research_work_checkpoint') or payload.get('publication_checkpoint')):
                                    execution_completed = True
                    if not pending_calls and (completed_calls >= 16 or execution_completed):
                        # Results are already durable and logged. Never interrupt an
                        # in-flight tool or mistake this intentional yield for failure.
                        session.terminate()
                        try:
                            session.wait(timeout=5)
                        except _subprocess.TimeoutExpired:
                            session.terminate(force=True)
                        return WORK_UNIT_EXIT_CODE
        return session.wait()
    except KeyboardInterrupt:
        session.terminate()
        try:
            session.wait(timeout=3)
        except _subprocess.TimeoutExpired:
            session.terminate(force=True)
        raise
    finally:
        stop_watchdog.set()
        watchdog.join(timeout=1.0)
        if active_child_ref is not None:
            active_child_ref["pid"] = None
        if log_fh:
            log_fh.close()
        if events_fh:
            events_fh.close()


# --- supervisor loop ---------------------------------------------------- #


class SupervisorLock:
    """Single-supervisor-per-thread enforcement. Lock file holds the
    supervisor PID; stale locks (PID dead) are auto-cleared."""

    def __init__(self, thread_dir: Path):
        self.path = thread_dir / ".supervisor.lock"

    def acquire(self) -> None:
        if self.path.exists():
            try:
                pid = int(self.path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = -1
            if pid > 0 and _pid_alive(pid):
                raise RuntimeError(
                    f"another supervisor (pid={pid}) is already running for "
                    f"this thread (lock: {self.path}). kill it first or wait."
                )
            # stale lock — clear it.
            try:
                self.path.unlink()
            except OSError:
                pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(self.release)

    def release(self) -> None:
        try:
            if self.path.exists() and self.path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else — treat as alive.


def _log(supervisor_log_path: Path, msg: str) -> None:
    """Append a timestamped line to the supervisor log AND mirror to stderr."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        supervisor_log_path.parent.mkdir(parents=True, exist_ok=True)
        with supervisor_log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    print(line, end="", file=LOG, flush=True)


def _sync_terminal_thread_index(repo: Path, tid: str, outcome: str | None) -> None:
    index_path = _thread_dir(repo, tid) / "thread.json"
    if not index_path.exists():
        return
    from research_harness.frontend.threads import update_thread

    sidebar_outcome = (
        "accept" if outcome == "accept_with_goal_achieved" else "inconclusive"
    )
    update_thread(
        repo,
        tid,
        current_phase="production",
        phase_status="complete",
        outcome=sidebar_outcome,
    )


def watch_thread(
    repo: Path,
    tid: str,
    *,
    max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    milestone_cycle: int = DEFAULT_MILESTONE_CYCLE,
    model: str = DEFAULT_CODEX_MODEL,
    boot_delay: float = DEFAULT_CODEX_BOOT_DELAY,
    rate_limit_backoff_initial: float = DEFAULT_RATE_LIMIT_BACKOFF_INITIAL,
    rate_limit_backoff_max: float = DEFAULT_RATE_LIMIT_BACKOFF_MAX,
    target_scope: str = "directional",  # bootstrap envelope target
    data_source_anchor: str | None = None,
    max_cycles: int | None = None,  # PR8: only honored when explicitly set;
                                    #      default behavior never quits on count.
) -> dict[str, object]:
    """Run until the verified dual-gate publication or operator interruption.

    Historical honest_failure artifacts are nonterminal. The supervisor starts
    Codex with instructions to continue through the blind engine. Rate limits trigger
    exponential backoff (1m → 2m → 4m → ... cap).

    Exit conditions:
      - dual-gate publish (AC accept + attestation.achieved=true) → status='terminal'
      - SIGINT/SIGTERM → status='interrupted'
      - explicit max_cycles override hit → status='max_cycles_exceeded'
        (left available for tests + emergency operator stop; default = unlimited)
    """
    tdir = _thread_dir(repo, tid)
    if not tdir.exists():
        raise RuntimeError(f"thread directory not found: {tdir}")
    log_path = tdir / "supervisor.log"
    lock = SupervisorLock(tdir)
    lock.acquire()

    interrupted = {"flag": False}
    active_child: dict[str, int | None] = {"pid": None}

    def _sigint(_signum, _frame):
        interrupted["flag"] = True
        _log(
            log_path,
            "signal received, terminating active Codex subprocess then exiting.",
        )
        # Cascade the signal to the active Codex subprocess so the
        # current cycle ends in seconds, not minutes. Without this, the
        # supervisor's loop can only check the interrupted flag between
        # cycles, after the running Codex subprocess exits on
        # its own — and the operator sees the supervisor 'still running'
        # in the UI for the whole subprocess lifetime.
        child_pid = active_child.get("pid")
        if child_pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(child_pid, sig)
                except ProcessLookupError:
                    break
                # Brief pause to let SIGTERM take effect before SIGKILL.
                time.sleep(0.5)
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    _log(log_path, f"supervisor starting for thread {tid} (pid={os.getpid()})")
    _log(
        log_path,
        f"max_idle={max_idle_seconds}s poll={poll_seconds}s milestone_cycle={milestone_cycle} "
        f"model={model} max_cycles={max_cycles!r} (None=unlimited) target_scope={target_scope!r}",
    )

    # PR7+PR8: auto-bootstrap the feasibility envelope from settings.json +
    # market dossier. Operator no longer needs to hand-craft the envelope
    # nor instruct Codex to submit it. The supervisor is the operator's
    # stand-in.
    bootstrapped = bootstrap_envelope_if_missing(
        repo,
        tid,
        target_scope=target_scope,
        data_source_anchor=data_source_anchor,
    )
    if bootstrapped is not None:
        adapter_ids = [
            s.get("id") for s in bootstrapped.get("data_sources_available", [])
            if s.get("kind") == "real_adapter"
        ]
        _log(
            log_path,
            f"feasibility envelope auto-bootstrapped: target_scope={target_scope!r}, "
            f"real_adapters={adapter_ids}, "
            f"baseline_provenance_count={len(bootstrapped.get('baseline_provenance_available') or [])}",
        )

    # Stall-watchdog inputs: a synchronous execute_node_experiment blocks Codex
    # output for the experiment's whole duration, so the watchdog must not mistake
    # a legit long experiment for a hang. It defers killing while a node is
    # 'running', up to a hard cap = longest configured experiment + a margin.
    state_path = tdir / "production" / "tree" / "search_state.json"
    try:
        from research_harness.config import load_settings as _ls
        _rt = (_ls(repo).get("runtime", {}).get("runner_timeouts", {}) or {})
        _max_rt = max((float(v) for v in _rt.values()), default=DEFAULT_CODEX_STALL_TIMEOUT)
    except Exception:  # noqa: BLE001
        _max_rt = DEFAULT_CODEX_STALL_TIMEOUT
    experiment_hard_cap = _max_rt + DEFAULT_CODEX_STALL_TIMEOUT
    _log(log_path, f"stall watchdog: stall_timeout={DEFAULT_CODEX_STALL_TIMEOUT:.0f}s "
                   f"experiment_hard_cap={experiment_hard_cap:.0f}s (longest runner_timeout + margin)")

    from research_harness.orchestrator.research_control import current_work

    cycle = 0
    rate_limit_backoff = rate_limit_backoff_initial
    rate_limit_armed = False  # toggled after a fast-fail cycle
    resume_without_idle = False
    while True:
        terminal, outcome = is_terminal(repo, tid)
        if terminal:
            _sync_terminal_thread_index(repo, tid, outcome)
            _log(log_path, f"DUAL-GATE PASS: outcome={outcome!r} after {cycle} cycle(s). exiting cleanly.")
            return {"status": "terminal", "outcome": outcome, "cycles": cycle}

        if interrupted["flag"]:
            _log(log_path, "interrupted by signal — exiting.")
            return {"status": "interrupted", "cycles": cycle}

        if max_cycles is not None and cycle >= max_cycles:
            _log(log_path, f"explicit max_cycles override ({max_cycles}) hit. exiting.")
            return {"status": "max_cycles_exceeded", "cycles": cycle}

        try:
            resumed = advance_resumable_reorientation(repo, tid)
        except (BlindSequentialResearchError, OSError, ValueError) as exc:
            _log(
                log_path,
                "deterministic reorientation resume failed; "
                f"falling back to the next Codex cycle: {type(exc).__name__}: {exc}",
            )
        else:
            if resumed is not None:
                resume_kind = resumed.get("supervisor_resume_kind")
                if resume_kind == "physical_checkpoint":
                    cycle += 1
                _log(
                    log_path,
                    "deterministic reorientation advance: "
                    f"status={resumed.get('status')!r}",
                )
                if resume_kind not in {
                    "physical_checkpoint_no_progress",
                    "strong_terminal_recovery_failed",
                }:
                    continue

        # PR8 milestone-not-termination logging.
        if cycle > 0 and cycle % milestone_cycle == 0:
            needs = update_needed_resources_file(repo, tid)
            _log(
                log_path,
                f"[milestone] cycle {cycle} reached without dual-gate. "
                f"blocking on {len(needs)} resource(s); see needed_resources.yaml. "
                "supervisor continues indefinitely per never-quit policy.",
            )

        idle = mcp_idle_seconds(repo, tid)
        # Cold start: on the first cycle there is no prior Codex session to
        # be "idle" relative to — the only recent write is the envelope
        # auto-bootstrap, which would otherwise force a full max_idle wait before
        # the first spawn (the ~10-min cold-start delay). Spawn cycle #1
        # immediately; the idle gate governs only subsequent (resume) cycles.
        if cycle > 0 and idle <= max_idle_seconds and not resume_without_idle:
            # Recent activity — MCP still being driven. Wait.
            time.sleep(poll_seconds)
            continue

        # Start a new Codex cycle. Update needed_resources before generating
        # the resume prompt so the LLM sees the latest gaps.
        update_needed_resources_file(repo, tid)
        cycle += 1
        prompt = build_resume_prompt(repo, tid, cycle)
        if cycle == 1:
            _log(log_path, "cycle #1: cold start, starting Codex immediately (idle gate applies from cycle #2)")
        else:
            _log(log_path, f"cycle #{cycle}: starting Codex to continue unfinished work" if resume_without_idle
                 else f"cycle #{cycle}: idle={idle:.0f}s > {max_idle_seconds:.0f}s, starting Codex")
        work_before = current_work(tdir)
        spawn_started = time.time()
        try:
            exit_code = spawn_codex_session(
                prompt,
                repo_root=repo,
                thread_id=tid,
                model=model,
                boot_delay=boot_delay,
                log_path=tdir / "codex_subprocess.log",
                active_child_ref=active_child,
                state_path=state_path,
                experiment_hard_cap=experiment_hard_cap,
            )
            spawn_elapsed = time.time() - spawn_started
            work_unit_yielded = exit_code == WORK_UNIT_EXIT_CODE
            work_after = current_work(tdir)
            pending_work_written = exit_code == 0 and work_after != work_before and work_after.get('status') in {'planned', 'completed'}
            resume_without_idle = work_unit_yielded or pending_work_written
            _log(
                log_path,
                f"cycle #{cycle}: Codex subprocess exited code={exit_code} after {spawn_elapsed:.1f}s",
            )
            # PR8 rate-limit detection. A real Codex session normally runs
            # at least several minutes (MCP tool calls + reasoning). A
            # sub-30s exit usually means auth/rate-limit/binary failure.
            if interrupted['flag']:
                continue
            if work_unit_yielded:
                _log(log_path, 'work unit checkpoint: interpreting durable evidence in the next cycle.')
            elif pending_work_written:
                _log(log_path, 'unfinished research work was updated; continuing without an idle wait.')
            elif spawn_elapsed < RATE_LIMIT_FAST_FAIL_SECONDS:
                rate_limit_armed = True
                _log(
                    log_path,
                    f"cycle #{cycle}: fast-fail ({spawn_elapsed:.1f}s) — likely rate limit / "
                    f"auth issue. backing off {rate_limit_backoff:.0f}s.",
                )
                time.sleep(rate_limit_backoff)
                rate_limit_backoff = min(
                    rate_limit_backoff_max, rate_limit_backoff * 2.0
                )
            elif spawn_elapsed >= RATE_LIMIT_HEALTHY_CYCLE_SECONDS:
                if rate_limit_armed:
                    _log(
                        log_path,
                        "cycle ran healthy — resetting rate-limit backoff.",
                    )
                rate_limit_armed = False
                rate_limit_backoff = rate_limit_backoff_initial
        except Exception as exc:  # noqa: BLE001
            resume_without_idle = False
            _log(log_path, f"cycle #{cycle}: spawn raised {type(exc).__name__}: {exc}")
            time.sleep(min(60.0, poll_seconds * 2))


# --- CLI ----------------------------------------------------------------- #


def _repo_root_default() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="research_harness.thread_supervisor",
        description=(
            "True hands-free thread executor. Start Codex JSONL subprocesses "
            "under ChatGPT login until the thread reaches a "
            "terminal publication outcome."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_watch = sub.add_parser("watch", help="watch a thread to terminal outcome")
    p_watch.add_argument("thread_id", help="thread id (under runs/threads/)")
    p_watch.add_argument("--repo-root", type=Path, default=None)
    p_watch.add_argument("--max-idle-seconds", type=float, default=DEFAULT_MAX_IDLE_SECONDS)
    p_watch.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    p_watch.add_argument("--milestone-cycle", type=int, default=DEFAULT_MILESTONE_CYCLE,
                          help="informational logging interval (PR8: not a termination)")
    p_watch.add_argument("--max-cycles", type=int, default=None,
                          help="emergency operator override; default = unlimited (PR8 never-quit policy)")
    p_watch.add_argument(
        "--target-scope",
        choices=["deployment", "feasibility", "directional"],
        default="directional",
        help=(
            "PR7 target deploy_grade_scope. Default 'directional' (safest — "
            "any registered data adapter suffices). Use 'deployment' only "
            "when settings.json.data_adapters.registered has at least one "
            "real adapter for this thread's domain."
        ),
    )
    p_watch.add_argument(
        "--data-source-anchor",
        default=None,
        help="Registered local adapter ID selected by the operator.",
    )
    p_watch.add_argument(
        "--model", default=None,
        help=(
            "Override the production model. When omitted (the frontend path), "
            "the supervisor resolves it via thread.json.mcp_model -> "
            "settings.runtime.llm_orchestrator.mcp.default_model -> default."
        ),
    )
    p_watch.add_argument("--boot-delay", type=float, default=DEFAULT_CODEX_BOOT_DELAY)

    args = parser.parse_args(argv)
    if args.cmd == "watch":
        repo = (args.repo_root or _repo_root_default()).resolve()
        # Resolve the model from settings when not explicitly overridden, so
        # the production phase honours the operator's configured model instead
        # of the hard-coded default.
        model = args.model or resolve_supervisor_model(repo, args.thread_id)
        result = watch_thread(
            repo,
            args.thread_id,
            max_idle_seconds=args.max_idle_seconds,
            poll_seconds=args.poll_seconds,
            milestone_cycle=args.milestone_cycle,
            max_cycles=args.max_cycles,
            model=model,
            boot_delay=args.boot_delay,
            target_scope=args.target_scope,
            data_source_anchor=args.data_source_anchor,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("status") in {
            "terminal",
            "interrupted",
        } else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
