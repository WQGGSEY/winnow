from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Mapping, Protocol, Sequence

from research_harness.connector.field_sampler import field_permutation
from research_harness.orchestrator.goal_contract import (
    GoalContract,
    parse_goal_contract,
    serialize_goal_contract,
)
from research_harness.schemas.validator import validate_named_schema


FingerprintAxis = Literal[
    "mechanism",
    "intervention",
    "observables_and_data",
    "analysis_unit",
    "timescale",
    "system_boundary",
]
AxisRelation = Literal["changed", "equivalent"]
DataNeedKind = Literal[
    "registered_adapter",
    "public_api",
    "public_page",
    "crawl",
]

FINGERPRINT_AXES: tuple[FingerprintAxis, ...] = (
    "mechanism",
    "intervention",
    "observables_and_data",
    "analysis_unit",
    "timescale",
    "system_boundary",
)
_AXIS_RELATIONS = frozenset({"changed", "equivalent"})
_DATA_NEED_KINDS = frozenset(
    {"registered_adapter", "public_api", "public_page", "crawl"}
)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_FINGERPRINT_ID_RE = re.compile(r"^fingerprint_[a-f0-9]{64}$")
_DIRECTION_ID_RE = re.compile(r"^direction_[a-f0-9]{64}$")


class DirectionGenerationError(ValueError):
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


def _text(value: object, label: str, *, lowercase: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DirectionGenerationError(f"{label} must be a non-empty string")
    normalized = re.sub(r"\s+", " ", value.strip())
    return normalized.lower() if lowercase else normalized


def _canonical_text(value: object, label: str, *, lowercase: bool = False) -> str:
    normalized = _text(value, label, lowercase=lowercase)
    if value != normalized:
        raise DirectionGenerationError(f"{label} is not canonical")
    return normalized


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DirectionGenerationError(f"{label} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    label: str,
) -> None:
    missing = sorted(required - value.keys())
    unexpected = sorted(value.keys() - required)
    if missing:
        raise DirectionGenerationError(f"{label} is missing keys {missing}")
    if unexpected:
        raise DirectionGenerationError(f"{label} has unexpected keys {unexpected}")


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DirectionGenerationError(f"{label} must be a non-negative integer")
    return value


def _fingerprint_payload(
    *,
    mechanism: str,
    intervention: str,
    observables_and_data: str,
    analysis_unit: str,
    timescale: str,
    system_boundary: str,
) -> dict[str, str]:
    return {
        "mechanism": mechanism,
        "intervention": intervention,
        "observables_and_data": observables_and_data,
        "analysis_unit": analysis_unit,
        "timescale": timescale,
        "system_boundary": system_boundary,
    }


@dataclass(frozen=True, slots=True)
class DirectionFingerprint:
    fingerprint_id: str
    mechanism: str
    intervention: str
    observables_and_data: str
    analysis_unit: str
    timescale: str
    system_boundary: str

    def __post_init__(self) -> None:
        if not isinstance(self.fingerprint_id, str) or not _FINGERPRINT_ID_RE.fullmatch(
            self.fingerprint_id
        ):
            raise DirectionGenerationError("fingerprint id is invalid")
        for axis in FINGERPRINT_AXES:
            _canonical_text(getattr(self, axis), axis, lowercase=True)
        expected = _fingerprint_id(_fingerprint_payload_from_value(self))
        if self.fingerprint_id != expected:
            raise DirectionGenerationError(
                "fingerprint identity does not match its content"
            )


def _fingerprint_payload_from_value(
    value: DirectionFingerprint,
) -> dict[str, str]:
    return _fingerprint_payload(
        mechanism=value.mechanism,
        intervention=value.intervention,
        observables_and_data=value.observables_and_data,
        analysis_unit=value.analysis_unit,
        timescale=value.timescale,
        system_boundary=value.system_boundary,
    )


def _fingerprint_id(payload: Mapping[str, str]) -> str:
    return f"fingerprint_{_sha256(dict(payload))}"


def make_direction_fingerprint(
    *,
    mechanism: str,
    intervention: str,
    observables_and_data: str,
    analysis_unit: str,
    timescale: str,
    system_boundary: str,
) -> DirectionFingerprint:
    payload = _fingerprint_payload(
        mechanism=_text(mechanism, "mechanism", lowercase=True),
        intervention=_text(intervention, "intervention", lowercase=True),
        observables_and_data=_text(
            observables_and_data,
            "observables_and_data",
            lowercase=True,
        ),
        analysis_unit=_text(analysis_unit, "analysis_unit", lowercase=True),
        timescale=_text(timescale, "timescale", lowercase=True),
        system_boundary=_text(
            system_boundary,
            "system_boundary",
            lowercase=True,
        ),
    )
    return DirectionFingerprint(
        fingerprint_id=_fingerprint_id(payload),
        **payload,
    )


def serialize_direction_fingerprint(
    fingerprint: DirectionFingerprint,
) -> dict[str, object]:
    if not isinstance(fingerprint, DirectionFingerprint):
        raise DirectionGenerationError("value is not a DirectionFingerprint")
    document: dict[str, object] = {
        "fingerprint_id": fingerprint.fingerprint_id,
        **_fingerprint_payload_from_value(fingerprint),
    }
    validate_named_schema("direction_fingerprint", document)
    return document


def parse_direction_fingerprint(value: object) -> DirectionFingerprint:
    raw = _mapping(value, "direction fingerprint")
    validate_named_schema("direction_fingerprint", dict(raw))
    _exact_keys(
        raw,
        required=frozenset({"fingerprint_id", *FINGERPRINT_AXES}),
        label="direction fingerprint",
    )
    return DirectionFingerprint(
        fingerprint_id=raw["fingerprint_id"],
        mechanism=raw["mechanism"],
        intervention=raw["intervention"],
        observables_and_data=raw["observables_and_data"],
        analysis_unit=raw["analysis_unit"],
        timescale=raw["timescale"],
        system_boundary=raw["system_boundary"],
    )


@dataclass(frozen=True, slots=True)
class RandomPerspective:
    draw_index: int
    epoch: int
    category_code: str
    category_name: str
    archive: str

    def __post_init__(self) -> None:
        _nonnegative_integer(self.draw_index, "draw index")
        _nonnegative_integer(self.epoch, "perspective epoch")
        _canonical_text(self.category_code, "category code")
        _canonical_text(self.category_name, "category name")
        _canonical_text(self.archive, "category archive")


def _epoch_seed(seed: int, epoch: int) -> int:
    return int(_sha256({"seed": seed, "epoch": epoch}), 16)


def sample_random_perspective(
    *,
    seed: int,
    draw_index: int,
) -> RandomPerspective:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise DirectionGenerationError("perspective seed must be an integer")
    draw_index = _nonnegative_integer(draw_index, "draw index")
    first_epoch = field_permutation(seed=_epoch_seed(seed, 0))
    if not first_epoch:
        raise DirectionGenerationError("arXiv category namespace is empty")
    epoch, offset = divmod(draw_index, len(first_epoch))
    categories = (
        first_epoch
        if epoch == 0
        else field_permutation(seed=_epoch_seed(seed, epoch))
    )
    category = categories[offset]
    try:
        code = _text(category["code"], "category code")
        name = _text(category["name"], "category name")
        archive = _text(category["archive"], "category archive")
    except KeyError as exc:
        raise DirectionGenerationError(
            "arXiv category namespace entry is incomplete"
        ) from exc
    return RandomPerspective(
        draw_index=draw_index,
        epoch=epoch,
        category_code=code,
        category_name=name,
        archive=archive,
    )


def _serialize_random_perspective(value: RandomPerspective) -> dict[str, object]:
    if not isinstance(value, RandomPerspective):
        raise DirectionGenerationError("value is not a RandomPerspective")
    return {
        "draw_index": value.draw_index,
        "epoch": value.epoch,
        "category_code": value.category_code,
        "category_name": value.category_name,
        "archive": value.archive,
    }


def _parse_random_perspective(value: object) -> RandomPerspective:
    raw = _mapping(value, "random perspective")
    _exact_keys(
        raw,
        required=frozenset(
            {"draw_index", "epoch", "category_code", "category_name", "archive"}
        ),
        label="random perspective",
    )
    return RandomPerspective(
        draw_index=_nonnegative_integer(raw["draw_index"], "draw index"),
        epoch=_nonnegative_integer(raw["epoch"], "perspective epoch"),
        category_code=_canonical_text(raw["category_code"], "category code"),
        category_name=_canonical_text(raw["category_name"], "category name"),
        archive=_canonical_text(raw["archive"], "category archive"),
    )


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    goal_contract: GoalContract
    random_perspective: RandomPerspective
    development_evidence: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.goal_contract, GoalContract):
            raise DirectionGenerationError("goal contract is invalid")
        if not isinstance(self.random_perspective, RandomPerspective):
            raise DirectionGenerationError("random perspective is invalid")


def serialize_generation_request(request: GenerationRequest) -> dict[str, object]:
    if not isinstance(request, GenerationRequest):
        raise DirectionGenerationError("value is not a GenerationRequest")
    document = {
        "goal_contract": serialize_goal_contract(request.goal_contract),
        "random_perspective": _serialize_random_perspective(
            request.random_perspective
        ),
    }
    if request.development_evidence is not None:
        document['development_evidence'] = dict(request.development_evidence)
    validate_named_schema("generation_request", document)
    return document


def parse_generation_request(value: object) -> GenerationRequest:
    raw = _mapping(value, "generation request")
    validate_named_schema("generation_request", dict(raw))
    _exact_keys(
        raw,
        required=frozenset({"goal_contract", "random_perspective"} | ({'development_evidence'} if 'development_evidence' in raw else set())),
        label="generation request",
    )
    return GenerationRequest(
        goal_contract=parse_goal_contract(raw["goal_contract"]),
        random_perspective=_parse_random_perspective(raw["random_perspective"]),
        development_evidence=raw.get('development_evidence'),
    )


@dataclass(frozen=True, slots=True)
class DataNeed:
    kind: DataNeedKind
    description: str

    def __post_init__(self) -> None:
        if self.kind not in _DATA_NEED_KINDS:
            raise DirectionGenerationError(f"unsupported data need kind {self.kind!r}")
        _canonical_text(self.description, "data need description")


def _serialize_data_need(value: DataNeed) -> dict[str, str]:
    if not isinstance(value, DataNeed):
        raise DirectionGenerationError("data need is invalid")
    return {"kind": value.kind, "description": value.description}


def _parse_data_need(value: object, index: int) -> DataNeed:
    raw = _mapping(value, f"data_needs[{index}]")
    _exact_keys(
        raw,
        required=frozenset({"kind", "description"}),
        label=f"data_needs[{index}]",
    )
    kind = raw["kind"]
    if kind not in _DATA_NEED_KINDS:
        raise DirectionGenerationError(f"unsupported data need kind {kind!r}")
    return DataNeed(
        kind=kind,
        description=_canonical_text(
            raw["description"],
            f"data_needs[{index}].description",
        ),
    )


def _text_tuple(
    value: object,
    label: str,
    *,
    minimum: int,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise DirectionGenerationError(f"{label} must be a string array")
    normalized = tuple(dict.fromkeys(_text(item, f"{label} item") for item in value))
    if len(normalized) < minimum:
        raise DirectionGenerationError(f"{label} requires at least {minimum} items")
    return normalized


def _canonical_text_tuple(
    value: object,
    label: str,
    *,
    minimum: int,
) -> tuple[str, ...]:
    normalized = _text_tuple(value, label, minimum=minimum)
    if not isinstance(value, tuple) or value != normalized:
        raise DirectionGenerationError(f"{label} is not canonical")
    return normalized


def _draft_payload(
    *,
    claim: str,
    fingerprint: DirectionFingerprint,
    experiment_objective: str,
    data_needs: tuple[DataNeed, ...],
    predicted_outcomes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "claim": claim,
        "fingerprint": {
            "fingerprint_id": fingerprint.fingerprint_id,
            **_fingerprint_payload_from_value(fingerprint),
        },
        "experiment_objective": experiment_objective,
        "data_needs": [_serialize_data_need(item) for item in data_needs],
        "predicted_outcomes": list(predicted_outcomes),
    }


@dataclass(frozen=True, slots=True)
class DirectionDraft:
    direction_id: str
    claim: str
    fingerprint: DirectionFingerprint
    experiment_objective: str
    data_needs: tuple[DataNeed, ...]
    predicted_outcomes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.direction_id, str) or not _DIRECTION_ID_RE.fullmatch(
            self.direction_id
        ):
            raise DirectionGenerationError("direction id is invalid")
        _canonical_text(self.claim, "direction claim")
        if not isinstance(self.fingerprint, DirectionFingerprint):
            raise DirectionGenerationError("direction fingerprint is invalid")
        _canonical_text(self.experiment_objective, "experiment objective")
        if not isinstance(self.data_needs, tuple) or any(
            not isinstance(item, DataNeed) for item in self.data_needs
        ):
            raise DirectionGenerationError("data needs must be an immutable tuple")
        if len(self.data_needs) != len(set(self.data_needs)):
            raise DirectionGenerationError("data needs must be unique")
        _canonical_text_tuple(
            self.predicted_outcomes,
            "predicted outcomes",
            minimum=2,
        )
        payload = _draft_payload(
            claim=self.claim,
            fingerprint=self.fingerprint,
            experiment_objective=self.experiment_objective,
            data_needs=self.data_needs,
            predicted_outcomes=self.predicted_outcomes,
        )
        if self.direction_id != f"direction_{_sha256(payload)}":
            raise DirectionGenerationError(
                "direction identity does not match its content"
            )


def make_direction_draft(
    *,
    claim: str,
    fingerprint: DirectionFingerprint,
    experiment_objective: str,
    data_needs: Sequence[DataNeed] = (),
    predicted_outcomes: Sequence[str],
) -> DirectionDraft:
    if not isinstance(fingerprint, DirectionFingerprint):
        raise DirectionGenerationError("direction fingerprint is invalid")
    if isinstance(data_needs, (str, bytes)):
        raise DirectionGenerationError("data needs must be DataNeed values")
    normalized_needs = tuple(dict.fromkeys(data_needs))
    if any(not isinstance(item, DataNeed) for item in normalized_needs):
        raise DirectionGenerationError("data needs must be DataNeed values")
    normalized_claim = _text(claim, "direction claim")
    normalized_objective = _text(experiment_objective, "experiment objective")
    normalized_outcomes = _text_tuple(
        predicted_outcomes,
        "predicted outcomes",
        minimum=2,
    )
    payload = _draft_payload(
        claim=normalized_claim,
        fingerprint=fingerprint,
        experiment_objective=normalized_objective,
        data_needs=normalized_needs,
        predicted_outcomes=normalized_outcomes,
    )
    return DirectionDraft(
        direction_id=f"direction_{_sha256(payload)}",
        claim=normalized_claim,
        fingerprint=fingerprint,
        experiment_objective=normalized_objective,
        data_needs=normalized_needs,
        predicted_outcomes=normalized_outcomes,
    )


def serialize_direction_draft(draft: DirectionDraft) -> dict[str, object]:
    if not isinstance(draft, DirectionDraft):
        raise DirectionGenerationError("value is not a DirectionDraft")
    document = {
        "direction_id": draft.direction_id,
        **_draft_payload(
            claim=draft.claim,
            fingerprint=draft.fingerprint,
            experiment_objective=draft.experiment_objective,
            data_needs=draft.data_needs,
            predicted_outcomes=draft.predicted_outcomes,
        ),
    }
    validate_named_schema("direction_draft", document)
    return document


def parse_direction_draft(value: object) -> DirectionDraft:
    raw = _mapping(value, "direction draft")
    validate_named_schema("direction_draft", dict(raw))
    _exact_keys(
        raw,
        required=frozenset(
            {
                "direction_id",
                "claim",
                "fingerprint",
                "experiment_objective",
                "data_needs",
                "predicted_outcomes",
            }
        ),
        label="direction draft",
    )
    data_needs = raw["data_needs"]
    if not isinstance(data_needs, list):
        raise DirectionGenerationError("data needs must be an array")
    outcomes = raw["predicted_outcomes"]
    if not isinstance(outcomes, list):
        raise DirectionGenerationError("predicted outcomes must be an array")
    return DirectionDraft(
        direction_id=raw["direction_id"],
        claim=_canonical_text(raw["claim"], "direction claim"),
        fingerprint=parse_direction_fingerprint(raw["fingerprint"]),
        experiment_objective=_canonical_text(
            raw["experiment_objective"],
            "experiment objective",
        ),
        data_needs=tuple(
            _parse_data_need(item, index) for index, item in enumerate(data_needs)
        ),
        predicted_outcomes=tuple(
            _canonical_text(item, "predicted outcome") for item in outcomes
        ),
    )


class DirectionGenerationTransport(Protocol):
    def generate(self, request: Mapping[str, object], /) -> Mapping[str, object]: ...


def invoke_direction_generator(
    request: GenerationRequest,
    transport: DirectionGenerationTransport,
) -> DirectionDraft:
    if not isinstance(request, GenerationRequest):
        raise DirectionGenerationError("value is not a GenerationRequest")
    response = transport.generate(serialize_generation_request(request))
    return parse_direction_draft(response)


@dataclass(frozen=True, slots=True)
class StructuralEquivalenceAssessment:
    candidate_fingerprint_id: str
    reference_fingerprint_id: str
    mechanism: AxisRelation
    intervention: AxisRelation
    observables_and_data: AxisRelation
    analysis_unit: AxisRelation
    timescale: AxisRelation
    system_boundary: AxisRelation
    evidence_source_digest: str
    assessment_receipt_digest: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.candidate_fingerprint_id, "candidate fingerprint id"),
            (self.reference_fingerprint_id, "reference fingerprint id"),
        ):
            if not isinstance(value, str) or not _FINGERPRINT_ID_RE.fullmatch(value):
                raise DirectionGenerationError(f"{label} is invalid")
        if any(getattr(self, axis) not in _AXIS_RELATIONS for axis in FINGERPRINT_AXES):
            raise DirectionGenerationError("axis assessment is invalid")
        for value, label in (
            (self.evidence_source_digest, "assessor evidence source digest"),
            (self.assessment_receipt_digest, "assessment receipt digest"),
        ):
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise DirectionGenerationError(f"{label} is invalid")
        expected = f"sha256:{_sha256(_assessment_payload(self))}"
        if self.assessment_receipt_digest != expected:
            raise DirectionGenerationError(
                "assessment receipt digest does not match its content"
            )

    @property
    def changed_axes(self) -> frozenset[FingerprintAxis]:
        return frozenset(
            axis for axis in FINGERPRINT_AXES if getattr(self, axis) == "changed"
        )


def _assessment_payload(
    assessment: StructuralEquivalenceAssessment,
) -> dict[str, object]:
    return {
        "candidate_fingerprint_id": assessment.candidate_fingerprint_id,
        "reference_fingerprint_id": assessment.reference_fingerprint_id,
        "relations": {
            axis: getattr(assessment, axis) for axis in FINGERPRINT_AXES
        },
        "evidence_source_digest": assessment.evidence_source_digest,
    }


def make_structural_equivalence_assessment(
    *,
    candidate: DirectionFingerprint,
    reference: DirectionFingerprint,
    mechanism: AxisRelation,
    intervention: AxisRelation,
    observables_and_data: AxisRelation,
    analysis_unit: AxisRelation,
    timescale: AxisRelation,
    system_boundary: AxisRelation,
    evidence_source_digest: str,
) -> StructuralEquivalenceAssessment:
    if not isinstance(candidate, DirectionFingerprint) or not isinstance(
        reference,
        DirectionFingerprint,
    ):
        raise DirectionGenerationError(
            "assessment fingerprints are invalid"
        )
    fields = {
        "candidate_fingerprint_id": candidate.fingerprint_id,
        "reference_fingerprint_id": reference.fingerprint_id,
        "mechanism": mechanism,
        "intervention": intervention,
        "observables_and_data": observables_and_data,
        "analysis_unit": analysis_unit,
        "timescale": timescale,
        "system_boundary": system_boundary,
        "evidence_source_digest": evidence_source_digest,
    }
    receipt_payload = {
        "candidate_fingerprint_id": candidate.fingerprint_id,
        "reference_fingerprint_id": reference.fingerprint_id,
        "relations": {
            axis: fields[axis] for axis in FINGERPRINT_AXES
        },
        "evidence_source_digest": evidence_source_digest,
    }
    receipt = f"sha256:{_sha256(receipt_payload)}"
    return StructuralEquivalenceAssessment(
        **fields,
        assessment_receipt_digest=receipt,
    )


class StructuralEquivalenceAssessor(Protocol):
    def assess(
        self,
        candidate: DirectionFingerprint,
        reference: DirectionFingerprint,
        /,
    ) -> StructuralEquivalenceAssessment: ...


class NoveltyReason(str, Enum):
    NO_CLOSED_NEGATIVES = "no_closed_negatives"
    ESCALATION_SATISFIED = "escalation_satisfied"
    EXACT_FINGERPRINT_DUPLICATE = "exact_fingerprint_duplicate"
    INSUFFICIENT_AXIS_CHANGE = "insufficient_axis_change"
    CORE_AXES_EQUIVALENT = "core_axes_equivalent"
    SYSTEM_BOUNDARY_EQUIVALENT = "system_boundary_equivalent"


@dataclass(frozen=True, slots=True)
class DirectionNoveltyDecision:
    decision: Literal["accepted", "rejected"]
    reason: NoveltyReason
    assessments: tuple[StructuralEquivalenceAssessment, ...]

    def __post_init__(self) -> None:
        if self.decision not in {"accepted", "rejected"}:
            raise DirectionGenerationError("novelty decision is invalid")
        if not isinstance(self.reason, NoveltyReason):
            raise DirectionGenerationError("novelty reason is invalid")
        if not isinstance(self.assessments, tuple) or any(
            not isinstance(item, StructuralEquivalenceAssessment)
            for item in self.assessments
        ):
            raise DirectionGenerationError("novelty assessments are invalid")


def _assessment(
    assessor: StructuralEquivalenceAssessor,
    candidate: DirectionFingerprint,
    reference: DirectionFingerprint,
) -> StructuralEquivalenceAssessment:
    if assessor is None or not callable(getattr(assessor, "assess", None)):
        raise DirectionGenerationError(
            "a structural equivalence assessor is required"
        )
    result = assessor.assess(candidate, reference)
    if not isinstance(result, StructuralEquivalenceAssessment):
        raise DirectionGenerationError("structural assessor returned an invalid result")
    if (
        result.candidate_fingerprint_id != candidate.fingerprint_id
        or result.reference_fingerprint_id != reference.fingerprint_id
    ):
        raise DirectionGenerationError(
            "structural assessment does not match its fingerprint pair"
        )
    return result


def gate_direction_novelty(
    candidate: DirectionFingerprint,
    closed_negatives: Sequence[DirectionFingerprint],
    assessor: StructuralEquivalenceAssessor,
) -> DirectionNoveltyDecision:
    if not isinstance(candidate, DirectionFingerprint):
        raise DirectionGenerationError("candidate fingerprint is invalid")
    if isinstance(closed_negatives, (str, bytes)) or any(
        not isinstance(item, DirectionFingerprint) for item in closed_negatives
    ):
        raise DirectionGenerationError("closed fingerprints are invalid")
    closed = tuple(closed_negatives)
    if any(item.fingerprint_id == candidate.fingerprint_id for item in closed):
        return DirectionNoveltyDecision(
            decision="rejected",
            reason=NoveltyReason.EXACT_FINGERPRINT_DUPLICATE,
            assessments=(),
        )
    if not closed:
        return DirectionNoveltyDecision(
            decision="accepted",
            reason=NoveltyReason.NO_CLOSED_NEGATIVES,
            assessments=(),
        )
    if len(closed) == 1:
        assessment = _assessment(assessor, candidate, closed[-1])
        changed = assessment.changed_axes
        if len(changed) < 2 or not changed.intersection(
            {"mechanism", "intervention"}
        ):
            return DirectionNoveltyDecision(
                decision="rejected",
                reason=NoveltyReason.INSUFFICIENT_AXIS_CHANGE,
                assessments=(assessment,),
            )
        return DirectionNoveltyDecision(
            decision="accepted",
            reason=NoveltyReason.ESCALATION_SATISFIED,
            assessments=(assessment,),
        )

    assessments = tuple(
        _assessment(assessor, candidate, reference) for reference in closed
    )
    if any(
        assessment.mechanism != "changed"
        or assessment.intervention != "changed"
        for assessment in assessments
    ):
        return DirectionNoveltyDecision(
            decision="rejected",
            reason=NoveltyReason.CORE_AXES_EQUIVALENT,
            assessments=assessments,
        )
    if len(closed) >= 3 and any(
        assessment.system_boundary != "changed" for assessment in assessments
    ):
        return DirectionNoveltyDecision(
            decision="rejected",
            reason=NoveltyReason.SYSTEM_BOUNDARY_EQUIVALENT,
            assessments=assessments,
        )
    return DirectionNoveltyDecision(
        decision="accepted",
        reason=NoveltyReason.ESCALATION_SATISFIED,
        assessments=assessments,
    )
