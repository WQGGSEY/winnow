from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from research_harness.memory.baseline_dossier import load_baseline_dossier
from research_harness.schemas.validator import validate_named_schema


TargetScope = Literal["deployment", "feasibility", "directional"]
FalsifierKind = Literal["real_holdout"]
PredicateOperator = Literal[">=", ">", "<=", "<", "=="]
RegisteredBy = Literal["operator", "supervisor_bootstrap"]

_TARGET_SCOPES = frozenset({"deployment", "feasibility", "directional"})
_FALSIFIER_KINDS = frozenset({"real_holdout"})
_PREDICATE_OPERATORS = frozenset({">=", ">", "<=", "<", "=="})
_REGISTERED_BY = frozenset({"operator", "supervisor_bootstrap"})
_BASELINE_ROLE_VALUES = frozenset(
    {"current_best_known", "naive", "random_or_null"}
)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_CONTRACT_ID_RE = re.compile(r"^contract_[a-f0-9]{64}$")
_DOSSIER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_COMPILER_VERSION = 1


class SolutionContractError(ValueError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SolutionContractError(f"{label} must be a non-empty string")
    return re.sub(r"\s+", " ", value.strip())


def _text_tuple(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SolutionContractError(f"{label} must be a string array")
    items = tuple(dict.fromkeys(_text(item, f"{label} item") for item in value))
    if not items and not allow_empty:
        raise SolutionContractError(f"{label} must not be empty")
    return items


def _provenance_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SolutionContractError(f"{label} must be a string array")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SolutionContractError(f"{label} must contain non-empty strings")
        normalized.append(item.strip())
    items = tuple(dict.fromkeys(normalized))
    if not items:
        raise SolutionContractError(f"{label} must not be empty")
    return items


def _require_tuple(value: object, label: str) -> None:
    if not isinstance(value, tuple):
        raise SolutionContractError(f"{label} must be an immutable tuple")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SolutionContractError(f"{label} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    label: str,
) -> None:
    missing = sorted(required - value.keys())
    unexpected = sorted(value.keys() - required - optional)
    if missing:
        raise SolutionContractError(f"{label} is missing keys {missing}")
    if unexpected:
        raise SolutionContractError(f"{label} has unexpected keys {unexpected}")


@dataclass(frozen=True, slots=True)
class FalsifierPredicate:
    metric: str
    operator: PredicateOperator
    threshold: float

    def __post_init__(self) -> None:
        _text(self.metric, "falsifier metric")
        if self.operator not in _PREDICATE_OPERATORS:
            raise SolutionContractError(
                f"unsupported falsifier operator {self.operator!r}"
            )
        if isinstance(self.threshold, bool) or not isinstance(
            self.threshold, (int, float)
        ):
            raise SolutionContractError("falsifier threshold must be numeric")
        if not math.isfinite(float(self.threshold)):
            raise SolutionContractError("falsifier threshold must be finite")


@dataclass(frozen=True, slots=True)
class HoldoutRequirement:
    kind: FalsifierKind
    holdout_source_id: str
    predicate: FalsifierPredicate
    registered_by: RegisteredBy

    def __post_init__(self) -> None:
        if self.kind not in _FALSIFIER_KINDS:
            raise SolutionContractError("holdout kind must be real_holdout")
        _text(self.holdout_source_id, "holdout source id")
        if not isinstance(self.predicate, FalsifierPredicate):
            raise SolutionContractError("holdout predicate is invalid")
        if self.registered_by not in _REGISTERED_BY:
            raise SolutionContractError(
                f"unsupported holdout registrant {self.registered_by!r}"
            )


@dataclass(frozen=True, slots=True)
class BaselineEvidence:
    candidate_id: str
    method: str
    role: str
    provenance: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.candidate_id, "baseline candidate id")
        _text(self.method, "baseline method")
        _text(self.role, "baseline role")
        if self.role not in _BASELINE_ROLE_VALUES:
            raise SolutionContractError(f"unsupported baseline role {self.role!r}")
        _require_tuple(self.provenance, "baseline provenance")
        _provenance_tuple(self.provenance, "baseline provenance")


@dataclass(frozen=True, slots=True)
class SolutionContractCompilerInput:
    question: str
    claim_under_test: str
    mandatory_baselines: tuple[str, ...]
    success_criteria: tuple[str, ...]
    disproof_conditions: tuple[str, ...]
    operator_requirements: tuple[str, ...]
    target_scope: TargetScope
    acceptable_scopes: tuple[TargetScope, ...]
    baseline_evidence: tuple[BaselineEvidence, ...]
    safety_limits: tuple[str, ...]
    holdout_requirement: HoldoutRequirement

    def __post_init__(self) -> None:
        _text(self.question, "question")
        _text(self.claim_under_test, "claim under test")
        for value, label in (
            (self.mandatory_baselines, "mandatory baselines"),
            (self.success_criteria, "success criteria"),
            (self.disproof_conditions, "disproof conditions"),
            (self.operator_requirements, "operator requirements"),
            (self.acceptable_scopes, "acceptable scopes"),
            (self.baseline_evidence, "baseline evidence"),
            (self.safety_limits, "safety limits"),
        ):
            _require_tuple(value, label)
        _text_tuple(self.mandatory_baselines, "mandatory baselines")
        _text_tuple(self.success_criteria, "success criteria")
        _text_tuple(self.disproof_conditions, "disproof conditions")
        _text_tuple(self.operator_requirements, "operator requirements")
        if self.target_scope not in _TARGET_SCOPES:
            raise SolutionContractError(
                f"unsupported target scope {self.target_scope!r}"
            )
        if not self.acceptable_scopes or any(
            scope not in _TARGET_SCOPES for scope in self.acceptable_scopes
        ):
            raise SolutionContractError("acceptable scopes are invalid")
        if not self.baseline_evidence or any(
            not isinstance(item, BaselineEvidence) for item in self.baseline_evidence
        ):
            raise SolutionContractError("baseline evidence must not be empty")
        missing_baseline_roles = _BASELINE_ROLE_VALUES - {
            item.role for item in self.baseline_evidence
        }
        if missing_baseline_roles:
            raise SolutionContractError(
                "baseline evidence is missing required roles "
                f"{sorted(missing_baseline_roles)}"
            )
        _text_tuple(self.safety_limits, "safety limits")
        if not isinstance(self.holdout_requirement, HoldoutRequirement):
            raise SolutionContractError("holdout requirement is invalid")


@dataclass(frozen=True, slots=True)
class SolutionContract:
    version: Literal[1]
    contract_id: str
    digest: str
    question: str
    claim_under_test: str
    mandatory_baselines: tuple[str, ...]
    success_criteria: tuple[str, ...]
    disproof_conditions: tuple[str, ...]
    operator_requirements: tuple[str, ...]
    target_scope: TargetScope
    acceptable_scopes: tuple[TargetScope, ...]
    baseline_evidence: tuple[BaselineEvidence, ...]
    safety_limits: tuple[str, ...]
    holdout_requirement: HoldoutRequirement
    compiler_version: Literal[1]

    def __post_init__(self) -> None:
        if self.version != 1 or self.compiler_version != _COMPILER_VERSION:
            raise SolutionContractError("unsupported solution contract version")
        if _CONTRACT_ID_RE.fullmatch(self.contract_id) is None:
            raise SolutionContractError("solution contract id is invalid")
        if _SHA256_RE.fullmatch(self.digest) is None:
            raise SolutionContractError("solution contract digest is invalid")
        for value, label in (
            (self.mandatory_baselines, "mandatory baselines"),
            (self.success_criteria, "success criteria"),
            (self.disproof_conditions, "disproof conditions"),
            (self.operator_requirements, "operator requirements"),
            (self.acceptable_scopes, "acceptable scopes"),
            (self.baseline_evidence, "baseline evidence"),
            (self.safety_limits, "safety limits"),
        ):
            _require_tuple(value, label)
        source = _compiler_input_from_contract(self)
        canonical_source = _canonicalize_source(source)
        if source != canonical_source:
            raise SolutionContractError("solution contract content is not canonical")
        payload = _contract_payload(canonical_source)
        digest = _sha256(payload)
        if (
            self.digest != f"sha256:{digest}"
            or self.contract_id != f"contract_{digest}"
        ):
            raise SolutionContractError(
                "solution contract identity does not match its content"
            )


def _baseline_to_dict(value: BaselineEvidence) -> dict[str, object]:
    return {
        "candidate_id": value.candidate_id,
        "method": value.method,
        "role": value.role,
        "provenance": list(value.provenance),
    }


def _holdout_to_dict(value: HoldoutRequirement) -> dict[str, object]:
    return {
        "kind": value.kind,
        "holdout_source_id": value.holdout_source_id,
        "predicate": {
            "metric": value.predicate.metric,
            "op": value.predicate.operator,
            "threshold": float(value.predicate.threshold),
        },
        "registered_by": value.registered_by,
    }


def _contract_payload(source: SolutionContractCompilerInput) -> dict[str, object]:
    return {
        "version": 1,
        "question": source.question,
        "bar": {
            "claim_under_test": source.claim_under_test,
            "mandatory_baselines": list(source.mandatory_baselines),
            "success_criteria": list(source.success_criteria),
            "disproof_conditions": list(source.disproof_conditions),
            "operator_requirements": list(source.operator_requirements),
            "target_scope": source.target_scope,
            "acceptable_scopes": list(source.acceptable_scopes),
        },
        "baseline_evidence": [
            _baseline_to_dict(value) for value in source.baseline_evidence
        ],
        "safety_limits": list(source.safety_limits),
        "holdout_requirement": _holdout_to_dict(source.holdout_requirement),
        "compiler_version": _COMPILER_VERSION,
    }


def _compiler_input_from_contract(
    contract: SolutionContract,
) -> SolutionContractCompilerInput:
    return SolutionContractCompilerInput(
        question=contract.question,
        claim_under_test=contract.claim_under_test,
        mandatory_baselines=contract.mandatory_baselines,
        success_criteria=contract.success_criteria,
        disproof_conditions=contract.disproof_conditions,
        operator_requirements=contract.operator_requirements,
        target_scope=contract.target_scope,
        acceptable_scopes=contract.acceptable_scopes,
        baseline_evidence=contract.baseline_evidence,
        safety_limits=contract.safety_limits,
        holdout_requirement=contract.holdout_requirement,
    )


def _canonicalize_baseline(value: BaselineEvidence) -> BaselineEvidence:
    return BaselineEvidence(
        candidate_id=_text(value.candidate_id, "baseline candidate id"),
        method=_text(value.method, "baseline method"),
        role=_text(value.role, "baseline role"),
        provenance=tuple(
            sorted(_provenance_tuple(value.provenance, "baseline provenance"))
        ),
    )


def _canonicalize_holdout(value: HoldoutRequirement) -> HoldoutRequirement:
    return HoldoutRequirement(
        kind=value.kind,
        holdout_source_id=_text(value.holdout_source_id, "holdout source id"),
        predicate=FalsifierPredicate(
            metric=_text(value.predicate.metric, "falsifier metric"),
            operator=value.predicate.operator,
            threshold=float(value.predicate.threshold),
        ),
        registered_by=value.registered_by,
    )


def _canonicalize_source(
    source: SolutionContractCompilerInput,
) -> SolutionContractCompilerInput:
    baseline_by_value = {
        (
            baseline.candidate_id,
            baseline.method,
            baseline.role,
            baseline.provenance,
        ): baseline
        for baseline in (
            _canonicalize_baseline(value) for value in source.baseline_evidence
        )
    }
    baselines = tuple(
        baseline_by_value[key]
        for key in sorted(baseline_by_value)
    )
    return SolutionContractCompilerInput(
        question=_text(source.question, "question"),
        claim_under_test=_text(source.claim_under_test, "claim under test"),
        mandatory_baselines=_text_tuple(
            source.mandatory_baselines, "mandatory baselines"
        ),
        success_criteria=_text_tuple(source.success_criteria, "success criteria"),
        disproof_conditions=_text_tuple(
            source.disproof_conditions, "disproof conditions"
        ),
        operator_requirements=_text_tuple(
            source.operator_requirements, "operator requirements"
        ),
        target_scope=source.target_scope,
        acceptable_scopes=_text_tuple(
            source.acceptable_scopes, "acceptable scopes"
        ),
        baseline_evidence=baselines,
        safety_limits=_text_tuple(source.safety_limits, "safety limits"),
        holdout_requirement=_canonicalize_holdout(source.holdout_requirement),
    )


def compile_solution_contract(
    source: SolutionContractCompilerInput,
) -> SolutionContract:
    if not isinstance(source, SolutionContractCompilerInput):
        raise SolutionContractError(
            "compile_solution_contract requires SolutionContractCompilerInput"
        )
    source = _canonicalize_source(source)
    payload = _contract_payload(source)
    digest = _sha256(payload)
    return SolutionContract(
        version=1,
        contract_id=f"contract_{digest}",
        digest=f"sha256:{digest}",
        question=source.question,
        claim_under_test=source.claim_under_test,
        mandatory_baselines=source.mandatory_baselines,
        success_criteria=source.success_criteria,
        disproof_conditions=source.disproof_conditions,
        operator_requirements=source.operator_requirements,
        target_scope=source.target_scope,
        acceptable_scopes=source.acceptable_scopes,
        baseline_evidence=source.baseline_evidence,
        safety_limits=source.safety_limits,
        holdout_requirement=source.holdout_requirement,
        compiler_version=_COMPILER_VERSION,
    )


def serialize_solution_contract(contract: SolutionContract) -> dict[str, object]:
    if not isinstance(contract, SolutionContract):
        raise SolutionContractError("value is not a SolutionContract")
    document = {
        **_contract_payload(_compiler_input_from_contract(contract)),
        "contract_id": contract.contract_id,
        "digest": contract.digest,
    }
    validate_named_schema("solution_contract", document)
    return document


def _parse_baseline(value: object, index: int) -> BaselineEvidence:
    raw = _mapping(value, f"baseline_evidence[{index}]")
    _exact_keys(
        raw,
        required=frozenset({"candidate_id", "method", "role", "provenance"}),
        label=f"baseline_evidence[{index}]",
    )
    return BaselineEvidence(
        candidate_id=_text(raw["candidate_id"], "baseline candidate id"),
        method=_text(raw["method"], "baseline method"),
        role=_text(raw["role"], "baseline role"),
        provenance=_provenance_tuple(raw["provenance"], "baseline provenance"),
    )


def _parse_holdout(value: object) -> HoldoutRequirement:
    raw = _mapping(value, "holdout_requirement")
    kind = raw.get("kind")
    if kind not in _FALSIFIER_KINDS:
        raise SolutionContractError("holdout kind must be real_holdout")
    _exact_keys(
        raw,
        required=frozenset(
            {"kind", "holdout_source_id", "predicate", "registered_by"}
        ),
        label="holdout_requirement",
    )
    predicate_raw = _mapping(raw["predicate"], "holdout predicate")
    _exact_keys(
        predicate_raw,
        required=frozenset({"metric", "op", "threshold"}),
        label="holdout predicate",
    )
    operator = predicate_raw["op"]
    registered_by = raw["registered_by"]
    if operator not in _PREDICATE_OPERATORS:
        raise SolutionContractError(f"unsupported falsifier operator {operator!r}")
    if registered_by not in _REGISTERED_BY:
        raise SolutionContractError(
            f"unsupported holdout registrant {registered_by!r}"
        )
    threshold = predicate_raw["threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise SolutionContractError("falsifier threshold must be numeric")
    return HoldoutRequirement(
        kind=kind,
        holdout_source_id=_text(raw["holdout_source_id"], "holdout source id"),
        predicate=FalsifierPredicate(
            metric=_text(predicate_raw["metric"], "falsifier metric"),
            operator=operator,
            threshold=float(threshold),
        ),
        registered_by=registered_by,
    )


def parse_solution_contract(value: object) -> SolutionContract:
    raw = _mapping(value, "solution contract")
    validate_named_schema("solution_contract", dict(raw))
    _exact_keys(
        raw,
        required=frozenset(
            {
                "version",
                "contract_id",
                "digest",
                "question",
                "bar",
                "baseline_evidence",
                "safety_limits",
                "holdout_requirement",
                "compiler_version",
            }
        ),
        label="solution contract",
    )
    bar = _mapping(raw["bar"], "solution contract bar")
    _exact_keys(
        bar,
        required=frozenset(
            {
                "claim_under_test",
                "mandatory_baselines",
                "success_criteria",
                "disproof_conditions",
                "operator_requirements",
                "target_scope",
                "acceptable_scopes",
            }
        ),
        label="solution contract bar",
    )
    target_scope = bar["target_scope"]
    if target_scope not in _TARGET_SCOPES:
        raise SolutionContractError(f"unsupported target scope {target_scope!r}")
    acceptable_scopes = _text_tuple(
        bar["acceptable_scopes"], "acceptable scopes"
    )
    if any(scope not in _TARGET_SCOPES for scope in acceptable_scopes):
        raise SolutionContractError("acceptable scopes are invalid")
    baseline_raw = raw["baseline_evidence"]
    if not isinstance(baseline_raw, list):
        raise SolutionContractError("baseline evidence must be an array")
    contract = SolutionContract(
        version=raw["version"],
        contract_id=raw["contract_id"],
        digest=raw["digest"],
        question=_text(raw["question"], "question"),
        claim_under_test=_text(bar["claim_under_test"], "claim under test"),
        mandatory_baselines=_text_tuple(
            bar["mandatory_baselines"], "mandatory baselines"
        ),
        success_criteria=_text_tuple(bar["success_criteria"], "success criteria"),
        disproof_conditions=_text_tuple(
            bar["disproof_conditions"], "disproof conditions"
        ),
        operator_requirements=_text_tuple(
            bar["operator_requirements"], "operator requirements"
        ),
        target_scope=target_scope,
        acceptable_scopes=acceptable_scopes,
        baseline_evidence=tuple(
            _parse_baseline(item, index) for index, item in enumerate(baseline_raw)
        ),
        safety_limits=_text_tuple(raw["safety_limits"], "safety limits"),
        holdout_requirement=_parse_holdout(raw["holdout_requirement"]),
        compiler_version=raw["compiler_version"],
    )
    return contract


_BASELINE_ROLES = {
    "selected": "current_best_known",
    "selected_as_best_known": "current_best_known",
    "selected_as_naive": "naive",
    "selected_as_random_or_null": "random_or_null",
}


def _baseline_evidence_from_dossier(
    baseline_dossier: Mapping[str, Any],
    *,
    dossier_id: str,
) -> tuple[BaselineEvidence, ...]:
    candidates = baseline_dossier.get("candidates_index")
    sources = baseline_dossier.get("source_index")
    if not isinstance(candidates, list) or not isinstance(sources, list):
        raise SolutionContractError(
            "baseline dossier requires candidates_index and source_index arrays"
        )
    provenance_by_candidate: dict[str, set[str]] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        url = source.get("url")
        supports = source.get("supports")
        if not isinstance(url, str) or not isinstance(supports, list):
            continue
        for supported in supports:
            if isinstance(supported, str):
                provenance_by_candidate.setdefault(supported, set()).add(url)
    evidence: list[BaselineEvidence] = []
    present_roles: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        role = _BASELINE_ROLES.get(candidate.get("decision"))
        candidate_id = candidate.get("id")
        method = candidate.get("method")
        detail_file = candidate.get("detail_file")
        if (
            role is None
            or not isinstance(candidate_id, str)
            or not isinstance(method, str)
        ):
            continue
        provenance = tuple(
            sorted(
                {
                    *provenance_by_candidate.get(candidate_id, set()),
                    f"baseline_dossier:{dossier_id}#{detail_file}",
                }
            )
        )
        evidence.append(
            BaselineEvidence(
                candidate_id=_text(candidate_id, "baseline candidate id"),
                method=_text(method, "baseline method"),
                role=role,
                provenance=provenance,
            )
        )
        present_roles.add(role)
    missing_roles = sorted(_BASELINE_ROLE_VALUES - present_roles)
    if missing_roles:
        raise SolutionContractError(
            f"baseline dossier is missing required roles {missing_roles}"
        )
    return tuple(sorted(evidence, key=lambda item: (item.role, item.candidate_id)))


def solution_contract_input_from_artifacts(
    *,
    repo_root: Path,
    baseline_dossier_id: str,
    operator_problem: str,
    grilling_record: Mapping[str, Any],
    feasibility_envelope: Mapping[str, Any],
    safety_limits: Iterable[str] = (),
) -> SolutionContractCompilerInput:
    if not isinstance(repo_root, Path):
        raise SolutionContractError("repo root must be a Path")
    dossier_id = _text(baseline_dossier_id, "baseline dossier id")
    if _DOSSIER_ID_RE.fullmatch(dossier_id) is None:
        raise SolutionContractError("baseline dossier id is invalid")
    baseline_dossier = load_baseline_dossier(repo_root, dossier_id)
    if baseline_dossier.get("id") != dossier_id:
        raise SolutionContractError(
            "loaded baseline dossier id does not match the requested id"
        )
    question = _text(operator_problem, "operator problem")
    extracted = _mapping(grilling_record.get("extracted"), "grilling extracted")
    intent = _mapping(
        feasibility_envelope.get("operator_intent"), "feasibility scope"
    )
    target_scope = intent.get("target_deploy_grade_scope")
    if target_scope not in _TARGET_SCOPES:
        raise SolutionContractError(f"unsupported target scope {target_scope!r}")
    acceptable_raw = intent.get("acceptable_alternative_scopes") or [target_scope]
    acceptable_scopes = _text_tuple(acceptable_raw, "acceptable scopes")
    if any(scope not in _TARGET_SCOPES for scope in acceptable_scopes):
        raise SolutionContractError("acceptable scopes are invalid")

    round_requirements = []
    for round_record in grilling_record.get("rounds") or []:
        if isinstance(round_record, Mapping):
            response = round_record.get("user_response")
            if isinstance(response, str) and response.strip():
                round_requirements.append(_text(response, "operator requirement"))
    taste_constraints = _text_tuple(
        extracted.get("taste_constraints") or (),
        "taste constraints",
        allow_empty=True,
    )
    operator_requirements = tuple(
        dict.fromkeys((question, *round_requirements, *taste_constraints))
    )
    if isinstance(safety_limits, str):
        raise SolutionContractError("safety limits must be an iterable of strings")
    normalized_safety = tuple(
        dict.fromkeys(
            (
                *(_text(item, "safety limit") for item in safety_limits),
                *taste_constraints,
            )
        )
    )
    if not normalized_safety:
        raise SolutionContractError("safety limits must not be empty")

    return SolutionContractCompilerInput(
        question=question,
        claim_under_test=_text(
            extracted.get("claim_under_test") or question,
            "claim under test",
        ),
        mandatory_baselines=_text_tuple(
            extracted.get("mandatory_baselines"), "mandatory baselines"
        ),
        success_criteria=_text_tuple(
            extracted.get("success_criteria"), "success criteria"
        ),
        disproof_conditions=_text_tuple(
            extracted.get("disproof_conditions"), "disproof conditions"
        ),
        operator_requirements=operator_requirements,
        target_scope=target_scope,
        acceptable_scopes=acceptable_scopes,
        baseline_evidence=_baseline_evidence_from_dossier(
            baseline_dossier,
            dossier_id=dossier_id,
        ),
        safety_limits=normalized_safety,
        holdout_requirement=_parse_holdout(
            feasibility_envelope.get("external_falsifier")
        ),
    )


def project_research_goal(contract: SolutionContract) -> dict[str, object]:
    if not isinstance(contract, SolutionContract):
        raise SolutionContractError("value is not a SolutionContract")
    identity = {"question": contract.question, "bar_digest": contract.digest}
    document = {
        "id": f"goal_{_sha256(identity)}",
        "question": contract.question,
        "bar": {
            "claim_under_test": contract.claim_under_test,
            "mandatory_baselines": list(contract.mandatory_baselines),
            "success_criteria": list(contract.success_criteria),
            "disproof_conditions": list(contract.disproof_conditions),
            "operator_requirements": list(contract.operator_requirements),
            "target_scope": contract.target_scope,
            "external_falsifier": _holdout_to_dict(contract.holdout_requirement),
        },
        "bar_digest": contract.digest,
        "source": "pre_generation",
        "strong_completion_blocked": False,
    }
    validate_named_schema("research_goal", document)
    return document
