from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from research_harness.acquisition import (
    AcquisitionBlocked,
    AcquisitionCheckpoint,
    AcquisitionCommand,
    AcquisitionComplete,
    AcquisitionCursor,
    AcquisitionOutcome,
    parse_command,
    parse_cursor,
    serialize_command,
    serialize_cursor,
)
from research_harness.orchestrator.adaptive_search import (
    initialize_adaptive_state,
    strategy_fingerprint,
)
from research_harness.orchestrator.attempt_evidence import (
    ConclusiveFailure,
    NeedsData,
    NeedsMoreEvidence,
    StrongCandidate,
    derive_attempt_evidence,
    derive_falsifier_failure,
    serialize_failure_evidence_receipt,
)
from research_harness.orchestrator.blind_reorientation import (
    AcquisitionReserved,
    AwaitingEvidence,
    Checkpointed,
    ClosedDirectionAttempt,
    DirectionAttemptRef,
    GenerationReserved,
    GoalAchieved,
    HardExternalBlock,
    HardExternalBlockCode,
    ReorientationState,
    Seeking,
    initialize_reorientation_state,
    make_acquisition_reservation,
    make_checkpoint,
    make_generation_reservation,
    parse_reorientation_state,
    serialize_reorientation_state,
)
from research_harness.orchestrator.direction_generation import (
    DirectionDraft,
    DirectionFingerprint,
    DirectionGenerationTransport,
    DirectionNoveltyDecision,
    GenerationRequest,
    StructuralEquivalenceAssessor,
    gate_direction_novelty,
    invoke_direction_generator,
    parse_direction_draft,
    parse_generation_request,
    sample_random_perspective,
    serialize_direction_draft,
    serialize_generation_request,
)
from research_harness.orchestrator.search_state import (
    initialize_search_state,
    search_policy_from_config,
    validate_search_state,
)
from research_harness.orchestrator.goal_contract import (
    GoalContract,
    EvaluationProtocolRequired,
    compile_goal_contract,
    goal_contract_input_from_artifacts,
    parse_goal_contract,
    project_research_goal,
    serialize_goal_contract,
)


class BlindSequentialResearchError(RuntimeError):
    pass


class AcquisitionAdapter(Protocol):
    def acquire(
        self,
        command: AcquisitionCommand,
        *,
        cursor: AcquisitionCursor | None = None,
    ) -> AcquisitionOutcome: ...

    def verify_manifest(self, manifest_id: str, *, node_id: str): ...


WriterLock = Callable[[], AbstractContextManager[None]]


@dataclass(frozen=True, slots=True)
class StrongResultBinding:
    contract_id: str
    attempt_id: str
    direction_id: str
    node_id: str
    manifest_id: str


@dataclass(frozen=True, slots=True)
class VerifiedStrongResult:
    binding: StrongResultBinding
    receipt_sha256: str


class StrongResultVerifier(Protocol):
    def verify(
        self,
        binding: StrongResultBinding,
        /,
    ) -> VerifiedStrongResult | None: ...


def resolve_attempt_node_binding(
    node_attempts: Mapping[str, Any],
    *,
    contract_id: str,
    attempt_id: str,
    direction_id: str,
    node_id: str | None = None,
) -> StrongResultBinding:
    if not all(
        isinstance(value, str) and value
        for value in (contract_id, attempt_id, direction_id)
    ) or (node_id is not None and (not isinstance(node_id, str) or not node_id)):
        raise BlindSequentialResearchError(
            "strong result binding identity is malformed"
        )
    if not isinstance(node_attempts, Mapping):
        raise BlindSequentialResearchError("node attempt index is malformed")
    nodes = node_attempts.get("nodes")
    if not isinstance(nodes, Mapping):
        raise BlindSequentialResearchError("node attempt index is malformed")
    if any(
        not isinstance(candidate_id, str) or not isinstance(binding, Mapping)
        for candidate_id, binding in nodes.items()
    ):
        raise BlindSequentialResearchError("node attempt index is malformed")
    matches = [
        (candidate_id, binding)
        for candidate_id, binding in nodes.items()
        if binding.get("attempt_id") == attempt_id
    ]
    if len(matches) != 1:
        raise BlindSequentialResearchError(
            "active attempt must have exactly one materialized node"
        )
    resolved_node_id, raw_binding = matches[0]
    if node_id is not None and resolved_node_id != node_id:
        raise BlindSequentialResearchError(
            "requested node is not the active attempt materialized node"
        )
    manifest_id = raw_binding.get("manifest_id")
    if (
        raw_binding.get("legacy_audit_only") is not False
        or raw_binding.get("direction_id") != direction_id
        or not isinstance(manifest_id, str)
        or not manifest_id
    ):
        raise BlindSequentialResearchError(
            "strong result is not bound to the active blind direction"
        )
    return StrongResultBinding(
        contract_id=contract_id,
        attempt_id=attempt_id,
        direction_id=direction_id,
        node_id=resolved_node_id,
        manifest_id=manifest_id,
    )


def resolve_strong_result_binding(
    state: ReorientationState,
    node_attempts: Mapping[str, Any],
    *,
    node_id: str | None = None,
) -> StrongResultBinding:
    phase = state.phase
    if isinstance(phase, Checkpointed):
        phase = phase.continuation
    if not isinstance(phase, AwaitingEvidence):
        raise BlindSequentialResearchError(
            "strong result requires an active evidence attempt"
        )
    return resolve_attempt_node_binding(
        node_attempts,
        contract_id=state.contract_id,
        attempt_id=phase.active_attempt.attempt_id,
        direction_id=phase.active_attempt.direction_id,
        node_id=node_id,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def strong_result_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    if not isinstance(receipt, Mapping):
        raise BlindSequentialResearchError("strong result receipt must be an object")
    return _digest(receipt)


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise BlindSequentialResearchError(f"cannot read durable artifact {path}") from exc


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True, slots=True)
class ReorientationPaths:
    root: Path

    @classmethod
    def for_thread(cls, thread_dir: Path) -> ReorientationPaths:
        return cls(root=thread_dir / "production" / "reorientation")

    @property
    def goal_contract(self) -> Path:
        return self.root / "goal_contract.json"

    @property
    def confirmation_use(self) -> Path:
        return self.root / "confirmation_use.json"

    @property
    def legacy_solution_contract(self) -> Path:
        return self.root / "solution_contract.json"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def migration_block(self) -> Path:
        return self.root / "migration_block.json"

    @property
    def node_attempts(self) -> Path:
        return self.root / "node_attempts.json"

    def reservation(self, reservation_id: str) -> Path:
        return self.root / "reservations" / f"{reservation_id}.json"

    def direction(self, direction_id: str) -> Path:
        return self.root / "directions" / f"{direction_id}.json"

    def acquisition_command(self, command_id: str) -> Path:
        return self.root / "acquisition_commands" / f"{command_id}.json"

    def command_receipt(self, command_id: str) -> Path:
        name = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
        return self.root / "commands" / f"{name}.json"

    def checkpoint_payload(self, payload_digest: str) -> Path:
        return self.root / "checkpoint_payloads" / f"{payload_digest[7:]}.json"

    def generation_decision(self, reservation_id: str) -> Path:
        return self.root / "generation_decisions" / f"{reservation_id}.json"

    def failure_receipt(self, attempt_id: str) -> Path:
        return self.root / "failure_receipts" / f"{attempt_id}.json"


@dataclass(frozen=True, slots=True)
class _GenerationPlan:
    revision: int
    reservation_id: str
    request: GenerationRequest
    closed_fingerprints: tuple[DirectionFingerprint, ...]


@dataclass(frozen=True, slots=True)
class _AcquisitionPlan:
    revision: int
    phase_digest: str
    reservation_id: str
    command: AcquisitionCommand
    cursor: AcquisitionCursor | None


@dataclass(frozen=True, slots=True)
class _Immediate:
    result: dict[str, object]


@dataclass(frozen=True, slots=True)
class _Transition:
    before: ReorientationState
    after: ReorientationState
    result: dict[str, object]


_Plan = _GenerationPlan | _AcquisitionPlan | _Immediate | _Transition


class _MigrationBlocked(Exception):
    def __init__(self, result: dict[str, object]) -> None:
        super().__init__(str(result.get("required_external_action") or "migration blocked"))
        self.result = result


class BlindSequentialResearch:
    def __init__(
        self,
        *,
        repo_root: Path,
        thread_dir: Path,
        writer_lock: WriterLock,
        direction_generator: DirectionGenerationTransport,
        structural_assessor: StructuralEquivalenceAssessor,
        acquisition: AcquisitionAdapter,
        strong_result_verifier: StrongResultVerifier | None = None,
        perspective_seed: int = 0,
    ) -> None:
        self._repo_root = repo_root
        self._thread_dir = thread_dir
        self._paths = ReorientationPaths.for_thread(thread_dir)
        self._writer_lock = writer_lock
        self._direction_generator = direction_generator
        self._structural_assessor = structural_assessor
        self._acquisition = acquisition
        self._strong_result_verifier = strong_result_verifier
        self._perspective_seed = perspective_seed

    @property
    def paths(self) -> ReorientationPaths:
        return self._paths

    def read_goal_contract(self) -> GoalContract | None:
        if not self._paths.goal_contract.exists():
            return None
        return parse_goal_contract(_read_json(self._paths.goal_contract))

    def read_state(self) -> ReorientationState | None:
        if not self._paths.state.exists():
            return None
        return parse_reorientation_state(_read_json(self._paths.state))

    def advance_research(
        self,
        *,
        command_id: str,
        acquisition_command: AcquisitionCommand | None = None,
        expected_revision: int | None = None,
        expected_checkpoint_id: str | None = None,
        expected_strong_result_receipt_sha256: str | None = None,
    ) -> dict[str, object]:
        command_id = command_id.strip()
        if not command_id:
            raise BlindSequentialResearchError("command_id must be non-empty")
        input_digest = _digest(
            {
                "acquisition_command": (
                    serialize_command(acquisition_command)
                    if acquisition_command is not None
                    else None
                ),
                "expected_revision": expected_revision,
                "expected_checkpoint_id": expected_checkpoint_id,
                "expected_strong_result_receipt_sha256": (
                    expected_strong_result_receipt_sha256
                ),
            }
        )
        for _ in range(3):
            with self._writer_lock():
                self._reconcile_prepared_receipts_locked()
                previous = self._read_command_receipt(command_id, input_digest)
                if previous is not None:
                    return previous
                try:
                    contract, state = self._load_or_migrate_locked()
                except _MigrationBlocked as exc:
                    self._write_command_receipt(
                        command_id,
                        input_digest,
                        exc.result,
                    )
                    return exc.result
                if expected_revision is not None and state.revision != expected_revision:
                    result = {
                        "status": "state_changed",
                        "expected_revision": expected_revision,
                        "actual_revision": state.revision,
                    }
                    self._write_command_receipt(command_id, input_digest, result)
                    return result
                from research_harness.orchestrator.confirmation_use import initialize_confirmation_ledger_locked

                initialize_confirmation_ledger_locked(self._paths.confirmation_use, contract)
                if expected_checkpoint_id is not None and (
                    not isinstance(state.phase, Checkpointed)
                    or state.phase.checkpoint.checkpoint_id
                    != expected_checkpoint_id
                ):
                    result = {
                        "status": "state_changed",
                        "expected_checkpoint_id": expected_checkpoint_id,
                    }
                    self._write_command_receipt(command_id, input_digest, result)
                    return result
                plan = self._plan_locked(
                    contract,
                    state,
                    acquisition_command=acquisition_command,
                    expected_strong_result_receipt_sha256=(
                        expected_strong_result_receipt_sha256
                    ),
                )
                if isinstance(plan, _Immediate):
                    self._write_command_receipt(command_id, input_digest, plan.result)
                    return plan.result
                if isinstance(plan, _Transition):
                    self._commit_transition(
                        command_id,
                        input_digest,
                        plan,
                    )
                    return plan.result
            if isinstance(plan, _GenerationPlan):
                generation_error = None
                try:
                    draft = invoke_direction_generator(
                        plan.request,
                        self._direction_generator,
                    )
                    decision = gate_direction_novelty(
                        draft.fingerprint,
                        plan.closed_fingerprints,
                        self._structural_assessor,
                    )
                except Exception as exc:
                    generation_error = {"type": type(exc).__name__, "message": str(exc)[:4000]}
                    external_result: DirectionDraft | None = None
                    external_decision: DirectionNoveltyDecision | None = None
                else:
                    external_result = draft
                    external_decision = decision
                with self._writer_lock():
                    self._reconcile_prepared_receipts_locked()
                    previous = self._read_command_receipt(command_id, input_digest)
                    if previous is not None:
                        return previous
                    committed = self._commit_generation_locked(
                        plan,
                        external_result,
                        external_decision,
                    )
                    if committed is None:
                        continue
                    if generation_error is not None:
                        committed.result["generation_error"] = generation_error
                        committed.result["next_tool_to_call"] = "advance_research"
                        committed.result["required_work"] = (
                            "Retry with the returned revision/checkpoint. The direction generator reads only the frozen "
                            "goal contract and a fresh perspective; it does not read the preparation hypotheses catalog. "
                            "Regenerating that catalog cannot repair this generation failure."
                        )
                    self._commit_transition(
                        command_id,
                        input_digest,
                        committed,
                    )
                    return committed.result

            if isinstance(plan, _AcquisitionPlan):
                outcome = self._acquisition.acquire(plan.command, cursor=plan.cursor)
                if isinstance(outcome, AcquisitionComplete):
                    verified = self._acquisition.verify_manifest(
                        outcome.manifest.manifest_id,
                        node_id=plan.command.node_id,
                    )
                    if verified != outcome.manifest:
                        raise BlindSequentialResearchError(
                            "acquisition verification changed the returned manifest"
                        )
                with self._writer_lock():
                    self._reconcile_prepared_receipts_locked()
                    previous = self._read_command_receipt(command_id, input_digest)
                    if previous is not None:
                        return previous
                    committed = self._commit_acquisition_locked(plan, outcome)
                    if committed is None:
                        continue
                    if expected_checkpoint_id is not None:
                        committed.result["resumed_from_checkpoint_id"] = (
                            expected_checkpoint_id
                        )
                    self._commit_transition(
                        command_id,
                        input_digest,
                        committed,
                    )
                    return committed.result
        raise BlindSequentialResearchError(
            "advance could not commit after concurrent state changes"
        )

    def _load_or_migrate_locked(self) -> tuple[GoalContract, ReorientationState]:
        if self._paths.legacy_solution_contract.exists():
            raise self._migration_block(
                HardExternalBlockCode.OPERATOR_SCOPE_CONFLICT,
                "Migrate the legacy solution_contract.json into a strategy-free "
                "GoalContract explicitly, then retry.",
            )
        contract = self.read_goal_contract()
        state = self.read_state()
        if contract is not None and state is not None:
            if state.contract_id != contract.contract_id:
                raise BlindSequentialResearchError(
                    "reorientation state does not match the goal contract"
                )
            self._ensure_node_attempt_index_locked()
            return contract, state
        if contract is not None:
            self._ensure_node_attempt_index_locked()
            state = initialize_reorientation_state(contract)
            self._write_state(state)
            return contract, state
        return self._migrate_legacy_locked()

    def _migrate_legacy_locked(self) -> tuple[GoalContract, ReorientationState]:
        search_state_path = (
            self._thread_dir / "production" / "tree" / "search_state.json"
        )
        search_state = (
            self._read_search_state() if search_state_path.exists() else {"nodes": []}
        )
        handoff_path = (
            self._thread_dir / "production" / "intake_to_claim_dialog.json"
        )
        handoff = _read_json(handoff_path) if handoff_path.exists() else None
        accepted_contract = (
            handoff.get("new_contract") if isinstance(handoff, Mapping) else None
        )
        roots = [
            node
            for node in search_state.get("nodes") or []
            if isinstance(node, Mapping) and node.get("parent") is None
        ]
        bars = (
            {
                _canonical_json(
                    {
                        key: (node.get("claim_contract") or {}).get(key)
                        for key in (
                            "mandatory_baselines",
                            "success_criteria",
                            "disproof_conditions",
                            "decision_rule",
                            "deploy_grade_scope",
                        )
                    }
                )
                for node in roots
            }
            if not isinstance(accepted_contract, Mapping)
            else set()
        )
        if len(bars) > 1:
            raise self._migration_block(
                HardExternalBlockCode.OPERATOR_SCOPE_CONFLICT,
                "Resolve the disagreeing legacy root success bars, then retry.",
            )
        try:
            thread = self._required_mapping(self._thread_dir / "thread.json")
            grilling = self._required_mapping(
                self._thread_dir / "grilling" / "grilling_session.json"
            )
            if isinstance(accepted_contract, Mapping):
                grilling = deepcopy(dict(grilling))
                extracted = dict(grilling.get("extracted") or {})
                extracted.update(
                    {
                        key: accepted_contract[key]
                        for key in (
                            "mandatory_baselines",
                            "success_criteria",
                            "disproof_conditions",
                        )
                        if key in accepted_contract
                    }
                )
                grilling["extracted"] = extracted
            market = self._required_mapping(
                self._thread_dir / "market" / "market_research_brief.json"
            )
            envelope = self._required_mapping(
                self._thread_dir / "production" / "feasibility_envelope.json"
            )
            extracted = grilling.get("extracted") or {}
            if not isinstance(extracted, Mapping):
                raise ValueError("grilling extraction is missing")
            safety_limits = tuple(extracted.get("taste_constraints") or ()) or (
                "Do not exceed the operator-registered feasibility and access boundaries.",
            )
            qualification_path = (
                self._thread_dir / "market" / "baseline_qualification.json"
            )
            baseline_qualification = (
                self._required_mapping(qualification_path)
                if qualification_path.exists()
                else None
            )
            source = goal_contract_input_from_artifacts(
                repo_root=self._repo_root,
                baseline_dossier_id=str(market.get("baseline_dossier_id") or ""),
                operator_problem=str(
                    thread.get("user_goal") or grilling.get("user_goal") or ""
                ),
                grilling_record=grilling,
                feasibility_envelope=envelope,
                safety_limits=safety_limits,
                baseline_qualification=baseline_qualification,
                baseline_artifact_root=(
                    self._thread_dir / "production" / "tree"
                    if baseline_qualification is not None
                    else None
                ),
            )
            contract = compile_goal_contract(source)
        except EvaluationProtocolRequired:
            result = {
                "status": "protocol_required",
                "next_tool_to_call": "submit_feasibility_envelope",
                "required_work": (
                    "Read the registered envelope and propose a prospective real_holdout protocol with an untouched "
                    "holdout_source_id and a meaningful metric predicate. Submit the full envelope for independent review "
                    "without changing the registered resources, scope, or increasing the budget. Do not inspect the holdout. "
                    "After approval, call advance_research to create the claim before baseline qualification. "
                    "This is autonomous research design work, not an external blocker or a request for human approval."
                ),
            }
            _write_json_atomic(self._paths.migration_block, result)
            raise _MigrationBlocked(result)
        except FileNotFoundError as exc:
            raise self._migration_block(
                HardExternalBlockCode.OPERATOR_SCOPE_CONFLICT,
                f"Provide the missing pre-generation artifact, then retry: {exc.filename}",
            )
        except OSError as exc:
            raise self._migration_block(
                HardExternalBlockCode.STORAGE_UNAVAILABLE,
                f"Restore writable durable storage, then retry migration: {exc}",
            )
        except (BlindSequentialResearchError, KeyError, TypeError, ValueError) as exc:
            raise self._migration_block(
                HardExternalBlockCode.OPERATOR_SCOPE_CONFLICT,
                f"Resolve the pre-generation contract conflict, then retry: {exc}",
            )

        state = initialize_reorientation_state(contract)
        node_attempts = {
            "version": 1,
            "nodes": self._legacy_attempt_bindings(search_state),
        }
        _write_json_atomic(self._paths.node_attempts, node_attempts)
        _write_json_atomic(
            self._paths.goal_contract,
            serialize_goal_contract(contract),
        )
        self._write_state(state)
        try:
            self._paths.migration_block.unlink()
        except FileNotFoundError:
            pass
        return contract, state

    def _migration_block(
        self,
        code: HardExternalBlockCode,
        action: str,
    ) -> _MigrationBlocked:
        result: dict[str, object] = {
            "status": "hard_external_block",
            "code": code.value,
            "required_external_action": action,
        }
        _write_json_atomic(self._paths.migration_block, result)
        return _MigrationBlocked(result)

    def _plan_locked(
        self,
        contract: GoalContract,
        state: ReorientationState,
        *,
        acquisition_command: AcquisitionCommand | None,
        expected_strong_result_receipt_sha256: str | None = None,
    ) -> _Plan:
        phase = state.phase
        if isinstance(phase, GoalAchieved):
            if (
                expected_strong_result_receipt_sha256 is not None
                and phase.strong_result_receipt_sha256
                != expected_strong_result_receipt_sha256
            ):
                return _Immediate({"status": "strong_result_changed"})
            return _Immediate(
                {
                    "status": "goal_achieved",
                    "strong_result_receipt_sha256": phase.strong_result_receipt_sha256,
                }
            )
        verified_goal = self._verified_goal_transition_locked(state)
        if expected_strong_result_receipt_sha256 is not None:
            if (
                verified_goal is None
                or verified_goal.result["strong_result_receipt_sha256"]
                != expected_strong_result_receipt_sha256
            ):
                return _Immediate({"status": "strong_result_changed"})
            return verified_goal
        if verified_goal is not None:
            return verified_goal
        if isinstance(phase, HardExternalBlock):
            if isinstance(phase.continuation, AcquisitionReserved):
                return self._plan_acquisition_locked(
                    state,
                    phase.continuation,
                    acquisition_command=acquisition_command,
                    checkpointed=True,
                )
            return _Immediate(
                {
                    "status": "hard_external_block",
                    "code": phase.code.value,
                    "required_external_action": phase.required_external_action,
                }
            )
        if isinstance(phase, Checkpointed):
            continuation = phase.continuation
            if isinstance(continuation, Seeking):
                return self._reserve_generation_locked(contract, state)
            if isinstance(continuation, AcquisitionReserved):
                return self._plan_acquisition_locked(
                    state,
                    continuation,
                    acquisition_command=acquisition_command,
                    checkpointed=True,
                )
            if isinstance(continuation, AwaitingEvidence):
                return self._evaluate_evidence_locked(state, continuation)
            if isinstance(continuation, GenerationReserved):
                return self._generation_plan_from_reservation(state, continuation)
        if isinstance(phase, Seeking):
            return self._reserve_generation_locked(contract, state)
        if isinstance(phase, GenerationReserved):
            return self._generation_plan_from_reservation(state, phase)
        if isinstance(phase, AcquisitionReserved):
            return self._plan_acquisition_locked(
                state,
                phase,
                acquisition_command=acquisition_command,
                checkpointed=False,
            )
        if isinstance(phase, AwaitingEvidence):
            return self._evaluate_evidence_locked(state, phase)
        raise BlindSequentialResearchError("unsupported reorientation phase")

    def _verified_goal_transition_locked(
        self,
        state: ReorientationState,
    ) -> _Transition | None:
        if self._strong_result_verifier is None:
            return None
        phase = state.phase
        if isinstance(phase, Checkpointed):
            phase = phase.continuation
        if not isinstance(phase, AwaitingEvidence):
            return None
        binding = resolve_strong_result_binding(
            state,
            self._read_node_attempts(),
        )
        verified = self._strong_result_verifier.verify(binding)
        if verified is None:
            return None
        if verified.binding != binding:
            raise BlindSequentialResearchError(
                "strong result verifier returned a different active binding"
            )
        next_state = ReorientationState(
            version=2,
            contract_id=state.contract_id,
            revision=state.revision + 1,
            closed_attempts=state.closed_attempts,
            phase=GoalAchieved(
                strong_result_receipt_sha256=verified.receipt_sha256,
            ),
        )
        return _Transition(
            before=state,
            after=next_state,
            result={
                "status": "goal_achieved",
                "attempt_id": binding.attempt_id,
                "node_id": binding.node_id,
                "strong_result_receipt_sha256": verified.receipt_sha256,
            },
        )

    def _reserve_generation_locked(
        self,
        contract: GoalContract,
        state: ReorientationState,
    ) -> _GenerationPlan:
        draw_index = self._next_draw_index()
        from research_harness.orchestrator.research_control import development_evidence
        from research_harness.orchestrator.research_knowledge import research_brief, brief_context

        observations = development_evidence(self._thread_dir)
        if observations:
            observations['research_brief'] = brief_context(self._thread_dir, research_brief(self._thread_dir))
        request = GenerationRequest(
            goal_contract=contract,
            random_perspective=sample_random_perspective(
                seed=self._perspective_seed,
                draw_index=draw_index,
            ),
            development_evidence=observations if observations and draw_index % 4 != 3 else None,
        )
        request_document = serialize_generation_request(request)
        reservation = make_generation_reservation(
            expected_revision=state.revision,
            request_digest=_digest(request_document),
        )
        payload = {
            "version": 1,
            "kind": "generation",
            "reservation": {
                "reservation_id": reservation.reservation_id,
                "expected_revision": reservation.expected_revision,
                "request_digest": reservation.request_digest,
            },
            "request": request_document,
        }
        _write_json_atomic(self._paths.reservation(reservation.reservation_id), payload)
        reserved_state = ReorientationState(
            version=2,
            contract_id=state.contract_id,
            revision=state.revision + 1,
            closed_attempts=state.closed_attempts,
            phase=GenerationReserved(reservation=reservation),
        )
        self._write_state(reserved_state)
        return _GenerationPlan(
            revision=reserved_state.revision,
            reservation_id=reservation.reservation_id,
            request=request,
            closed_fingerprints=tuple(
                item.attempt.fingerprint for item in reserved_state.closed_attempts
            ),
        )

    def _generation_plan_from_reservation(
        self,
        state: ReorientationState,
        phase: GenerationReserved,
    ) -> _GenerationPlan:
        raw = _read_json(self._paths.reservation(phase.reservation.reservation_id))
        if not isinstance(raw, Mapping) or raw.get("kind") != "generation":
            raise BlindSequentialResearchError("generation reservation payload is invalid")
        request = parse_generation_request(raw.get("request"))
        if _digest(serialize_generation_request(request)) != phase.reservation.request_digest:
            raise BlindSequentialResearchError("generation reservation request digest differs")
        return _GenerationPlan(
            revision=state.revision,
            reservation_id=phase.reservation.reservation_id,
            request=request,
            closed_fingerprints=tuple(
                item.attempt.fingerprint for item in state.closed_attempts
            ),
        )

    def _plan_acquisition_locked(
        self,
        state: ReorientationState,
        phase: AcquisitionReserved,
        *,
        acquisition_command: AcquisitionCommand | None,
        checkpointed: bool,
    ) -> _Plan:
        stored_command, stored_cursor = self._stored_acquisition(state, phase)
        command = acquisition_command or stored_command
        if command is None:
            draft = self._load_direction(phase.active_attempt.direction_id)
            return _Immediate(
                {
                    "status": "direction_ready",
                    "attempt_id": phase.active_attempt.attempt_id,
                    "direction": serialize_direction_draft(draft),
                    "reservation_id": phase.reservation.reservation_id,
                    "node_id": self._node_id(draft.direction_id),
                    "next_tool_to_call": "advance_research",
                }
            )
        self._validate_acquisition_command(command, phase)
        if stored_command is not None and stored_command.command_id != command.command_id:
            raise BlindSequentialResearchError(
                "checkpoint is bound to a different acquisition command"
            )
        if not checkpointed:
            _write_json_atomic(
                self._paths.acquisition_command(command.command_id),
                serialize_command(command),
            )
        return _AcquisitionPlan(
            revision=state.revision,
            phase_digest=_digest(serialize_reorientation_state(state)["phase"]),
            reservation_id=phase.reservation.reservation_id,
            command=command,
            cursor=stored_cursor,
        )

    def _commit_generation_locked(
        self,
        plan: _GenerationPlan,
        draft: DirectionDraft | None,
        decision: DirectionNoveltyDecision | None,
    ) -> _Transition | None:
        state = self.read_state()
        if state is None or not self._generation_plan_matches(state, plan):
            return None
        if draft is None:
            checkpoint = make_checkpoint(Seeking(), reason="generation_retry")
            next_state = ReorientationState(
                version=2,
                contract_id=state.contract_id,
                revision=state.revision + 1,
                closed_attempts=state.closed_attempts,
                phase=checkpoint,
            )
            return _Transition(
                before=state,
                after=next_state,
                result=self._checkpoint_result(next_state),
            )
        if decision is None:
            raise BlindSequentialResearchError(
                "generated direction is missing its novelty decision"
            )
        _write_json_atomic(
            self._paths.generation_decision(plan.reservation_id),
            self._serialize_novelty_decision(draft, decision),
        )
        if decision.decision == "rejected":
            checkpoint = make_checkpoint(Seeking(), reason="generation_retry")
            next_state = ReorientationState(
                version=2,
                contract_id=state.contract_id,
                revision=state.revision + 1,
                closed_attempts=state.closed_attempts,
                phase=checkpoint,
            )
            return _Transition(
                before=state,
                after=next_state,
                result=self._checkpoint_result(next_state),
            )

        _write_json_atomic(
            self._paths.direction(draft.direction_id),
            serialize_direction_draft(draft),
        )
        ordinal = len(state.closed_attempts)
        attempt = DirectionAttemptRef(
            attempt_id=f"attempt_{ordinal}_{draft.direction_id[10:22]}",
            direction_id=draft.direction_id,
            fingerprint=draft.fingerprint,
            ordinal=ordinal,
        )
        acquisition_request = {
            "attempt_id": attempt.attempt_id,
            "direction": serialize_direction_draft(draft),
        }
        reservation = make_acquisition_reservation(
            expected_revision=state.revision,
            request_digest=_digest(acquisition_request),
        )
        _write_json_atomic(
            self._paths.reservation(reservation.reservation_id),
            {
                "version": 1,
                "kind": "acquisition",
                "reservation": {
                    "reservation_id": reservation.reservation_id,
                    "expected_revision": reservation.expected_revision,
                    "request_digest": reservation.request_digest,
                },
                "request": acquisition_request,
            },
        )
        next_state = ReorientationState(
            version=2,
            contract_id=state.contract_id,
            revision=state.revision + 1,
            closed_attempts=state.closed_attempts,
            phase=AcquisitionReserved(
                active_attempt=attempt,
                reservation=reservation,
            ),
        )
        return _Transition(
            before=state,
            after=next_state,
            result={
                "status": "direction_ready",
                "attempt_id": attempt.attempt_id,
                "direction": serialize_direction_draft(draft),
                "reservation_id": reservation.reservation_id,
                "node_id": self._node_id(draft.direction_id),
                "next_tool_to_call": "advance_research",
            },
        )

    def _commit_acquisition_locked(
        self,
        plan: _AcquisitionPlan,
        outcome: AcquisitionOutcome,
    ) -> _Transition | None:
        state = self.read_state()
        if state is None or not self._acquisition_plan_matches(state, plan):
            return None
        phase = self._acquisition_continuation(state.phase)
        if phase is None:
            return None
        if isinstance(outcome, AcquisitionCheckpoint):
            self._validate_outcome_cursor(plan.command, outcome.cursor)
            payload = {
                "command": serialize_command(plan.command),
                "cursor": serialize_cursor(outcome.cursor),
            }
            payload_digest = _digest(payload)
            checkpoint = make_checkpoint(
                phase,
                reason=outcome.reason,
                payload_digest=payload_digest,
            )
            next_state = ReorientationState(
                version=2,
                contract_id=state.contract_id,
                revision=state.revision + 1,
                closed_attempts=state.closed_attempts,
                phase=checkpoint,
            )
            _write_json_atomic(
                self._paths.checkpoint_payload(payload_digest),
                payload,
            )
            return _Transition(
                before=state,
                after=next_state,
                result=self._checkpoint_result(next_state),
            )
        if isinstance(outcome, AcquisitionBlocked):
            self._validate_outcome_cursor(plan.command, outcome.cursor)
            payload = {
                "command": serialize_command(plan.command),
                "cursor": serialize_cursor(outcome.cursor),
            }
            payload_digest = _digest(payload)
            blocked = HardExternalBlock(
                code=outcome.code,
                required_external_action=outcome.required_external_action,
                continuation=phase,
                payload_digest=payload_digest,
            )
            next_state = ReorientationState(
                version=2,
                contract_id=state.contract_id,
                revision=state.revision + 1,
                closed_attempts=state.closed_attempts,
                phase=blocked,
            )
            _write_json_atomic(
                self._paths.checkpoint_payload(payload_digest),
                payload,
            )
            return _Transition(
                before=state,
                after=next_state,
                result={
                    "status": "hard_external_block",
                    "code": outcome.code.value,
                    "required_external_action": outcome.required_external_action,
                },
            )
        if not isinstance(outcome, AcquisitionComplete):
            raise BlindSequentialResearchError("acquisition returned an invalid outcome")
        manifest = outcome.manifest
        if (
            manifest.command_id != plan.command.command_id
            or manifest.node_id != plan.command.node_id
            or manifest.attempt_id != phase.active_attempt.attempt_id
            or manifest.direction_id != phase.active_attempt.direction_id
            or len(manifest.acquired_needs) != len(plan.command.needs)
        ):
            raise BlindSequentialResearchError(
                "acquisition manifest is not bound to the active attempt"
            )
        for acquired, need in zip(
            manifest.acquired_needs,
            plan.command.needs,
            strict=True,
        ):
            if (
                acquired.need_index != need.need_index
                or acquired.description != need.need.description
            ):
                raise BlindSequentialResearchError(
                    "acquisition manifest completed different data needs"
                )
        self._materialize_direction(plan.command, manifest.manifest_id)
        next_state = ReorientationState(
            version=2,
            contract_id=state.contract_id,
            revision=state.revision + 1,
            closed_attempts=state.closed_attempts,
            phase=AwaitingEvidence(active_attempt=phase.active_attempt),
        )
        return _Transition(
            before=state,
            after=next_state,
            result={
                "status": "acquisition_running",
                "acquisition_status": "complete",
                "attempt_id": phase.active_attempt.attempt_id,
                "node_id": manifest.node_id,
                "manifest_id": manifest.manifest_id,
                "next_tool_to_call": "get_next_admissible_node",
            },
        )

    def _evaluate_evidence_locked(
        self,
        state: ReorientationState,
        phase: AwaitingEvidence,
    ) -> _Immediate | _Transition:
        mapping = self._read_node_attempts()
        node_id = next(
            (
                node_id
                for node_id, binding in mapping["nodes"].items()
                if isinstance(binding, Mapping)
                and binding.get("attempt_id") == phase.active_attempt.attempt_id
            ),
            None,
        )
        if node_id is None:
            raise BlindSequentialResearchError("active attempt has no materialized node")
        worker_path = (
            self._thread_dir
            / "production"
            / "tree"
            / "nodes"
            / node_id
            / "worker_report.json"
        )
        if not worker_path.exists():
            return _Immediate(
                {
                    "status": "acquisition_running",
                    "acquisition_status": "complete",
                    "attempt_id": phase.active_attempt.attempt_id,
                    "node_id": node_id,
                    "next_tool_to_call": "get_next_admissible_node",
                }
            )
        evidence = derive_attempt_evidence(
            _read_json(worker_path),
            data_status="satisfied",
        )
        if isinstance(evidence, (StrongCandidate, ConclusiveFailure)):
            from research_harness.memory.baseline_review import require_goal_baseline_approval
            try:
                require_goal_baseline_approval(self._repo_root, self._thread_dir,
                                               serialize_goal_contract(self.read_goal_contract()))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _Immediate({"status": "baseline_qualification_required", "node_id": node_id,
                                   "next_tool_to_call": "submit_baseline_qualification", "reason": str(exc)})
        if isinstance(evidence, StrongCandidate):
            falsifier_path = (
                self._thread_dir
                / "production"
                / "rebuttal"
                / "falsifier_result.json"
            )
            binding = resolve_strong_result_binding(
                state,
                mapping,
                node_id=node_id,
            )
            falsifier_failure = derive_falsifier_failure(
                _read_json(worker_path),
                _read_json(falsifier_path) if falsifier_path.exists() else None,
                binding={
                    "contract_id": binding.contract_id,
                    "attempt_id": binding.attempt_id,
                    "direction_id": binding.direction_id,
                    "node_id": binding.node_id,
                    "manifest_id": binding.manifest_id,
                },
            )
            if falsifier_failure is not None:
                evidence = falsifier_failure
            else:
                return _Immediate(
                    {
                        "status": "acquisition_running",
                        "acquisition_status": "strong_candidate",
                        "attempt_id": phase.active_attempt.attempt_id,
                        "node_id": node_id,
                        "evidence_digest": evidence.evidence_digest,
                        "next_tool_to_call": "get_next_admissible_node",
                    }
                )
        if isinstance(evidence, ConclusiveFailure):
            _write_json_atomic(
                self._paths.failure_receipt(phase.active_attempt.attempt_id),
                serialize_failure_evidence_receipt(evidence.receipt),
            )
            closed = ClosedDirectionAttempt(
                attempt=phase.active_attempt,
                failure_receipt_sha256=evidence.receipt.receipt_sha256,
                private_lesson_sha256=evidence.private_lesson_sha256,
            )
            checkpoint = make_checkpoint(Seeking(), reason="generation_retry")
            next_state = ReorientationState(
                version=2,
                contract_id=state.contract_id,
                revision=state.revision + 1,
                closed_attempts=(*state.closed_attempts, closed),
                phase=checkpoint,
            )
            self._mark_strategy_refuted(node_id)
            return _Transition(
                before=state,
                after=next_state,
                result=self._checkpoint_result(next_state),
            )
        reason = "inconclusive_evidence"
        if isinstance(evidence, NeedsData):
            detail = evidence.data_status
        elif isinstance(evidence, NeedsMoreEvidence):
            detail = evidence.reason.value
        else:
            raise BlindSequentialResearchError("unsupported attempt evidence")
        checkpoint = make_checkpoint(phase, reason=reason)
        next_state = ReorientationState(
            version=2,
            contract_id=state.contract_id,
            revision=state.revision + 1,
            closed_attempts=state.closed_attempts,
            phase=checkpoint,
        )
        result = self._checkpoint_result(next_state)
        result["evidence_reason"] = detail
        return _Transition(before=state, after=next_state, result=result)

    def _materialize_direction(
        self,
        command: AcquisitionCommand,
        manifest_id: str,
    ) -> None:
        state_path = self._thread_dir / "production" / "tree" / "search_state.json"
        state = self._read_search_state() if state_path.exists() else None
        existing_node = next(
            (
                node
                for node in (state or {}).get("nodes") or []
                if node.get("id") == command.node_id
            ),
            None,
        )
        if existing_node is not None:
            attempts = self._read_node_attempts()
            binding = attempts["nodes"].get(command.node_id)
            if isinstance(binding, Mapping):
                if (
                    binding.get("attempt_id") != command.attempt_id
                    or binding.get("direction_id") != command.direction.direction_id
                    or binding.get("manifest_id") != manifest_id
                ):
                    raise BlindSequentialResearchError(
                        "existing node is bound to a different direction attempt"
                    )
                return
            strategy = existing_node.get("strategy") or {}
            artifacts = (existing_node.get("outputs") or {}).get("artifacts") or []
            if (
                strategy.get("derived_from_direction_id")
                != command.direction.direction_id
                or f"acquisition_manifest:{manifest_id}" not in artifacts
            ):
                raise BlindSequentialResearchError(
                    "existing node is bound to a different direction attempt"
                )
            attempts["nodes"][command.node_id] = {
                "attempt_id": command.attempt_id,
                "direction_id": command.direction.direction_id,
                "manifest_id": manifest_id,
                "legacy_audit_only": False,
            }
            _write_json_atomic(self._paths.node_attempts, attempts)
            return
        contract = self.read_goal_contract()
        if contract is None:
            raise BlindSequentialResearchError("goal contract is missing")
        goal = project_research_goal(contract)
        strategy_id = strategy_fingerprint(
            mechanism=command.direction.fingerprint.mechanism,
            intervention=command.direction.fingerprint.intervention,
        )
        strategy = {
            "id": strategy_id,
            "goal_id": goal["id"],
            "family": f"blind direction in {command.direction.fingerprint.system_boundary}",
            "mechanism": command.direction.fingerprint.mechanism,
            "intervention": command.direction.fingerprint.intervention,
            "information_target": command.direction.experiment_objective,
            "predicted_outcomes": list(command.direction.predicted_outcomes),
            "tests_bar_gaps": list(contract.success_criteria),
            "required_capabilities": [],
            "estimated_cost": 0.5,
            "derived_from_direction_id": command.direction.direction_id,
            "status": "candidate",
            "priority": {
                "bar_gap_closure": 1.0,
                "information_gain": 1.0,
                "mechanism_novelty": 1.0,
                "capability_fit": 1.0,
                "normalized_cost": 0.5,
                "evidence_basis": [command.direction.direction_id],
                "score": 9.0,
            },
        }
        dossier_ids = sorted(
            {
                value.split("#", 1)[0].split(":", 1)[1]
                for baseline in contract.baseline_evidence
                for value in baseline.provenance
                if value.startswith("baseline_dossier:") and "#" in value
            }
        )
        node = {
            "id": command.node_id,
            "type": "mechanism",
            "status": "ready",
            "domain": command.direction.fingerprint.system_boundary,
            "stage": "experimentation",
            "parent": None,
            "lineage": {
                "root_goal_id": goal["id"],
                "covers_goal_facets": list(contract.success_criteria),
                "inherited_assumptions": list(contract.operator_requirements),
                "introduced_assumptions": [command.direction.experiment_objective],
                "taste_constraints_applied": list(contract.safety_limits),
            },
            "claim_contract": {
                "claim_under_test": command.direction.claim,
                "mandatory_baselines": list(contract.mandatory_baselines),
                "success_criteria": list(contract.success_criteria),
                "disproof_conditions": list(contract.disproof_conditions),
                "deploy_grade_scope": contract.target_scope,
                "data_source_anchor": f"acquisition_manifest:{manifest_id}",
                "data_source_snapshot_id": (
                    "as_" + manifest_id.removeprefix("acqmanifest_")
                ),
            },
            "baseline_refs": [
                {
                    "baseline_dossier_id": dossier_ids[0] if dossier_ids else "contract",
                    "candidate_ids": [item.candidate_id for item in contract.baseline_evidence],
                    "roles": ["current_best_known", "naive", "random_or_null"],
                }
            ] if contract.baseline_evidence else [],
            "runtime_profile": {
                "worker_type": "experiment_worker",
                "timeout_policy": "task_class_dependent",
                "turn_budget": 6,
            },
            "failure_retrieval": {"query_tags": [], "selected_fail_files": []},
            "outputs": {
                "artifacts": [f"acquisition_manifest:{manifest_id}"],
                "verdict": None,
            },
            "strategy": strategy,
        }
        qualification_path = self._thread_dir / "market/baseline_qualification.json"
        if not contract.baseline_evidence and qualification_path.exists():
            from research_harness.memory.baseline_review import require_goal_baseline_approval
            require_goal_baseline_approval(self._repo_root, self._thread_dir, serialize_goal_contract(contract))
            qualification = _read_json(qualification_path)
            node["baseline_refs"] = [{
                "baseline_dossier_id": qualification["dossier_id"],
                "candidate_ids": [row["candidate_id"] for row in qualification["assignments"]],
                "roles": [row["role"] for row in qualification["assignments"]],
            }]
        if state is None:
            state = initialize_search_state(
                search_id=f"blind_{contract.contract_id.removeprefix('contract_')[:16]}",
                root_node=node,
                policy=search_policy_from_config(self._repo_root),
            )
            state["frontier"][0].update(
                {
                    "priority": 9.0,
                    "reason": "blind sequential reorientation",
                    "priority_components": strategy["priority"],
                }
            )
        else:
            state["nodes"].append(node)
            state["frontier"].append(
                {
                    "node_id": command.node_id,
                    "parent": None,
                    "depth": 0,
                    "priority": 9.0,
                    "stage": "experimentation",
                    "status": "queued",
                    "reason": "blind sequential reorientation",
                    "priority_components": strategy["priority"],
                }
            )
        adaptive = state.get("adaptive")
        if not isinstance(adaptive, dict):
            adaptive = initialize_adaptive_state(goal)
            state["adaptive"] = adaptive
        adaptive["goal"] = goal
        if not any(item.get("id") == strategy_id for item in adaptive["strategies"]):
            adaptive["strategies"].append(strategy)
        adaptive["pause"] = None
        adaptive["disposition"] = "continue"
        adaptive["revision"] = int(adaptive["revision"]) + 1
        state["status"] = "running"
        validate_search_state(state)
        _write_json_atomic(
            self._thread_dir / "production" / "tree" / "search_state.json",
            state,
        )
        attempts = self._read_node_attempts()
        attempts["nodes"][command.node_id] = {
            "attempt_id": command.attempt_id,
            "direction_id": command.direction.direction_id,
            "manifest_id": manifest_id,
            "legacy_audit_only": False,
        }
        _write_json_atomic(self._paths.node_attempts, attempts)

    def _mark_strategy_refuted(self, node_id: str) -> None:
        state = self._read_search_state()
        node = next(
            (item for item in state.get("nodes") or [] if item.get("id") == node_id),
            None,
        )
        if not isinstance(node, Mapping):
            return
        strategy_id = (node.get("strategy") or {}).get("id")
        adaptive = state.get("adaptive") or {}
        for strategy in adaptive.get("strategies") or []:
            if isinstance(strategy, dict) and strategy.get("id") == strategy_id:
                strategy["status"] = "refuted"
        validate_search_state(state)
        _write_json_atomic(
            self._thread_dir / "production" / "tree" / "search_state.json",
            state,
        )

    def _stored_acquisition(
        self,
        state: ReorientationState,
        phase: AcquisitionReserved,
    ) -> tuple[AcquisitionCommand | None, AcquisitionCursor | None]:
        if isinstance(state.phase, (Checkpointed, HardExternalBlock)):
            payload_digest = (
                state.phase.checkpoint.payload_digest
                if isinstance(state.phase, Checkpointed)
                else state.phase.payload_digest
            )
            if payload_digest is None:
                raise BlindSequentialResearchError(
                    "resumable acquisition state is missing its payload digest"
                )
            raw = _read_json(self._paths.checkpoint_payload(payload_digest))
            if not isinstance(raw, Mapping):
                raise BlindSequentialResearchError("checkpoint payload is invalid")
            if _digest(raw) != payload_digest:
                raise BlindSequentialResearchError(
                    "checkpoint payload digest does not match durable state"
                )
            command = parse_command(raw.get("command"))
            cursor = parse_cursor(raw.get("cursor"))
            self._validate_outcome_cursor(command, cursor)
            return command, cursor
        command_dir = self._paths.root / "acquisition_commands"
        commands = sorted(command_dir.glob("*.json")) if command_dir.exists() else []
        matches: list[AcquisitionCommand] = []
        for path in commands:
            command = parse_command(_read_json(path))
            if command.reservation_id == phase.reservation.reservation_id:
                matches.append(command)
        if len(matches) > 1:
            raise BlindSequentialResearchError(
                "acquisition reservation has multiple durable commands"
            )
        return (matches[0], None) if matches else (None, None)

    def _validate_acquisition_command(
        self,
        command: AcquisitionCommand,
        phase: AcquisitionReserved,
    ) -> None:
        expected_node_id = self._node_id(phase.active_attempt.direction_id)
        if (
            command.reservation_id != phase.reservation.reservation_id
            or command.attempt_id != phase.active_attempt.attempt_id
            or command.direction.direction_id != phase.active_attempt.direction_id
            or command.node_id != expected_node_id
        ):
            raise BlindSequentialResearchError(
                "acquisition command is not bound to the active direction reservation"
            )

    @staticmethod
    def _validate_outcome_cursor(
        command: AcquisitionCommand,
        cursor: AcquisitionCursor,
    ) -> None:
        if cursor.command_id != command.command_id:
            raise BlindSequentialResearchError(
                "acquisition cursor is bound to a different command"
            )

    def _generation_plan_matches(
        self,
        state: ReorientationState,
        plan: _GenerationPlan,
    ) -> bool:
        return (
            state.revision == plan.revision
            and isinstance(state.phase, GenerationReserved)
            and state.phase.reservation.reservation_id == plan.reservation_id
        )

    def _acquisition_plan_matches(
        self,
        state: ReorientationState,
        plan: _AcquisitionPlan,
    ) -> bool:
        phase = self._acquisition_continuation(state.phase)
        return (
            state.revision == plan.revision
            and _digest(serialize_reorientation_state(state)["phase"])
            == plan.phase_digest
            and phase is not None
            and phase.reservation.reservation_id == plan.reservation_id
        )

    @staticmethod
    def _acquisition_continuation(
        phase: object,
    ) -> AcquisitionReserved | None:
        if isinstance(phase, AcquisitionReserved):
            return phase
        if isinstance(phase, Checkpointed) and isinstance(
            phase.continuation, AcquisitionReserved
        ):
            return phase.continuation
        if isinstance(phase, HardExternalBlock) and isinstance(
            phase.continuation, AcquisitionReserved
        ):
            return phase.continuation
        return None

    def _next_draw_index(self) -> int:
        directory = self._paths.root / "reservations"
        highest = -1
        if directory.exists():
            for path in directory.glob("reservation_*.json"):
                raw = _read_json(path)
                if not isinstance(raw, Mapping) or raw.get("kind") != "generation":
                    continue
                request = parse_generation_request(raw.get("request"))
                highest = max(highest, request.random_perspective.draw_index)
        return highest + 1

    def _read_command_receipt(
        self,
        command_id: str,
        input_digest: str,
    ) -> dict[str, object] | None:
        path = self._paths.command_receipt(command_id)
        if not path.exists():
            return None
        raw = _read_json(path)
        if not isinstance(raw, Mapping):
            raise BlindSequentialResearchError("command receipt is malformed")
        if raw.get("command_id") != command_id or raw.get("input_digest") != input_digest:
            raise BlindSequentialResearchError(
                "command_id was already used with different input"
            )
        result = raw.get("result")
        if not isinstance(result, dict):
            raise BlindSequentialResearchError("command receipt result is malformed")
        if raw.get("status") != "committed":
            raise BlindSequentialResearchError("command receipt status is invalid")
        return result

    def _reconcile_prepared_receipts_locked(self) -> None:
        directory = self._paths.root / "commands"
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.json")):
            raw = _read_json(path)
            if not isinstance(raw, Mapping) or raw.get("status") != "prepared":
                continue
            command_id = raw.get("command_id")
            input_digest = raw.get("input_digest")
            result = raw.get("result")
            after_document = raw.get("after_state")
            if (
                not isinstance(command_id, str)
                or not isinstance(input_digest, str)
                or not isinstance(result, dict)
                or not isinstance(after_document, Mapping)
            ):
                raise BlindSequentialResearchError(
                    "prepared command receipt is malformed"
                )
            after = parse_reorientation_state(after_document)
            current = self.read_state()
            if current is None:
                raise BlindSequentialResearchError(
                    "prepared command receipt has no durable state"
                )
            current_document = serialize_reorientation_state(current)
            current_digest = _digest(current_document)
            before_digest = raw.get("before_state_digest")
            after_digest = _digest(after_document)
            if current_digest == before_digest:
                self._write_state(after)
            elif current_digest != after_digest and current.revision <= after.revision:
                raise BlindSequentialResearchError(
                    "prepared command receipt conflicts with durable state"
                )
            self._write_command_receipt(command_id, input_digest, result)

    def _write_command_receipt(
        self,
        command_id: str,
        input_digest: str,
        result: Mapping[str, object],
    ) -> None:
        _write_json_atomic(
            self._paths.command_receipt(command_id),
            {
                "version": 1,
                "status": "committed",
                "command_id": command_id,
                "input_digest": input_digest,
                "result": dict(result),
            },
        )

    def _commit_transition(
        self,
        command_id: str,
        input_digest: str,
        transition: _Transition,
    ) -> None:
        current = self.read_state()
        if current != transition.before:
            raise BlindSequentialResearchError(
                "transition no longer matches durable state"
            )
        after_document = serialize_reorientation_state(transition.after)
        _write_json_atomic(
            self._paths.command_receipt(command_id),
            {
                "version": 1,
                "status": "prepared",
                "command_id": command_id,
                "input_digest": input_digest,
                "before_state_digest": _digest(
                    serialize_reorientation_state(transition.before)
                ),
                "after_state": after_document,
                "result": transition.result,
            },
        )
        self._write_state(transition.after)
        self._write_command_receipt(
            command_id,
            input_digest,
            transition.result,
        )

    def _write_state(self, state: ReorientationState) -> None:
        _write_json_atomic(self._paths.state, serialize_reorientation_state(state))

    def _load_direction(self, direction_id: str) -> DirectionDraft:
        return parse_direction_draft(_read_json(self._paths.direction(direction_id)))

    def _read_search_state(self) -> dict[str, Any]:
        raw = _read_json(
            self._thread_dir / "production" / "tree" / "search_state.json"
        )
        if not isinstance(raw, dict):
            raise BlindSequentialResearchError("legacy search state is malformed")
        validate_search_state(raw)
        return raw

    def _read_node_attempts(self) -> dict[str, Any]:
        if not self._paths.node_attempts.exists():
            return {"version": 1, "nodes": {}}
        raw = _read_json(self._paths.node_attempts)
        if (
            not isinstance(raw, dict)
            or raw.get("version") != 1
            or not isinstance(raw.get("nodes"), dict)
        ):
            raise BlindSequentialResearchError("node attempt index is malformed")
        return raw

    def _ensure_node_attempt_index_locked(self) -> None:
        if self._paths.node_attempts.exists():
            self._read_node_attempts()
            return
        search_state_path = (
            self._thread_dir / "production" / "tree" / "search_state.json"
        )
        search_state = (
            self._read_search_state() if search_state_path.exists() else {"nodes": []}
        )
        bindings = self._legacy_attempt_bindings(search_state)
        command_dir = self._paths.root / "acquisition_commands"
        commands = (
            [parse_command(_read_json(path)) for path in sorted(command_dir.glob("*.json"))]
            if command_dir.exists()
            else []
        )
        for node in search_state.get("nodes") or []:
            if not isinstance(node, Mapping):
                continue
            node_id = node.get("id")
            direction_id = (node.get("strategy") or {}).get(
                "derived_from_direction_id"
            )
            if not isinstance(node_id, str) or not isinstance(direction_id, str):
                continue
            command = next(
                (
                    item
                    for item in commands
                    if item.node_id == node_id
                    and item.direction.direction_id == direction_id
                ),
                None,
            )
            if command is None:
                raise BlindSequentialResearchError(
                    "blind direction node has no durable acquisition command"
                )
            artifacts = (node.get("outputs") or {}).get("artifacts") or []
            manifest_ids = [
                value.split(":", 1)[1]
                for value in artifacts
                if isinstance(value, str)
                and value.startswith("acquisition_manifest:")
            ]
            if len(manifest_ids) != 1:
                raise BlindSequentialResearchError(
                    "blind direction node has no unique acquisition manifest"
                )
            bindings[node_id] = {
                "attempt_id": command.attempt_id,
                "direction_id": direction_id,
                "manifest_id": manifest_ids[0],
                "legacy_audit_only": False,
            }
        _write_json_atomic(
            self._paths.node_attempts,
            {"version": 1, "nodes": bindings},
        )

    @staticmethod
    def _required_mapping(path: Path) -> Mapping[str, Any]:
        raw = _read_json(path)
        if not isinstance(raw, Mapping):
            raise BlindSequentialResearchError(f"required artifact is malformed: {path}")
        return raw

    @staticmethod
    def _node_id(direction_id: str) -> str:
        return f"n_blind_{direction_id[10:]}"

    @staticmethod
    def _legacy_attempt_id(node_id: str) -> str:
        digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:16]
        return f"attempt_legacy_{digest}"

    @classmethod
    def _legacy_attempt_bindings(
        cls,
        search_state: Mapping[str, Any],
    ) -> dict[str, dict[str, object]]:
        nodes = {
            str(node["id"]): node
            for node in search_state.get("nodes") or []
            if isinstance(node, Mapping) and node.get("id")
        }
        bindings: dict[str, dict[str, object]] = {}
        for node_id in sorted(nodes):
            current_id = node_id
            visited: set[str] = set()
            while True:
                if current_id in visited:
                    raise BlindSequentialResearchError(
                        "legacy node lineage contains a cycle"
                    )
                visited.add(current_id)
                current = nodes.get(current_id)
                if current is None:
                    raise BlindSequentialResearchError(
                        "legacy node lineage references a missing parent"
                    )
                parent = current.get("parent")
                if parent is None:
                    root_id = current_id
                    break
                current_id = str(parent)
            bindings[node_id] = {
                "attempt_id": cls._legacy_attempt_id(root_id),
                "legacy_audit_only": True,
            }
        return bindings

    @staticmethod
    def _checkpoint_result(state: ReorientationState) -> dict[str, object]:
        if not isinstance(state.phase, Checkpointed):
            raise BlindSequentialResearchError("state is not checkpointed")
        return {
            "status": "checkpointed",
            "checkpoint_id": state.phase.checkpoint.checkpoint_id,
            "reason": state.phase.checkpoint.reason,
            "revision": state.revision,
            "payload_digest": state.phase.checkpoint.payload_digest,
        }

    @staticmethod
    def _serialize_novelty_decision(
        draft: DirectionDraft,
        decision: DirectionNoveltyDecision,
    ) -> dict[str, object]:
        return {
            "direction_id": draft.direction_id,
            "decision": decision.decision,
            "reason": decision.reason.value,
            "assessments": [
                {
                    "candidate_fingerprint_id": item.candidate_fingerprint_id,
                    "reference_fingerprint_id": item.reference_fingerprint_id,
                    "relations": {
                        axis: getattr(item, axis)
                        for axis in (
                            "mechanism",
                            "intervention",
                            "observables_and_data",
                            "analysis_unit",
                            "timescale",
                            "system_boundary",
                        )
                    },
                    "evidence_source_digest": item.evidence_source_digest,
                    "assessment_receipt_digest": item.assessment_receipt_digest,
                }
                for item in decision.assessments
            ],
        }
