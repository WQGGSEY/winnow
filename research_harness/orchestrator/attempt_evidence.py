from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Mapping, TypeAlias

from research_harness.schemas.validator import validate_named_schema


DataStatus = Literal["satisfied", "missing", "invalid"]
_DATA_STATUSES = frozenset({"satisfied", "missing", "invalid"})
_RUNNER_FAILURE_STATUSES = frozenset(
    {
        "failed",
        "blocked_preflight",
        "blocked_permission",
        "timeout_or_turn_exhausted",
        "invalid_worker_output",
    }
)
_BASELINE_OPERATORS = frozenset(
    {"greater_than", "greater_equal", "less_than", "less_equal"}
)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_RECEIPT_ID_RE = re.compile(r"^failure_receipt_[a-f0-9]{64}$")


class AttemptEvidenceError(ValueError):
    pass


class NeedsMoreEvidenceReason(str, Enum):
    MALFORMED_EVIDENCE = "malformed_evidence"
    RUNNER_DID_NOT_COMPLETE = "runner_did_not_complete"
    NOT_EVALUABLE = "not_evaluable"
    INCONCLUSIVE = "inconclusive"
    LABEL_ONLY_CONTRADICTION = "label_only_contradiction"
    UNSAFE_POSITIVE = "unsafe_positive"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AttemptEvidenceError(f"{label} must be an object")
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
        raise AttemptEvidenceError(f"{label} is missing keys {missing}")
    if unexpected:
        raise AttemptEvidenceError(f"{label} has unexpected keys {unexpected}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttemptEvidenceError(f"{label} must be a non-empty string")
    return value.strip()


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AttemptEvidenceError(f"{label} must be a non-negative integer")
    return value


def _finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _measurements_are_finite(report: Mapping[str, Any]) -> bool:
    for field in ("metrics", "baselines"):
        values = report.get(field)
        if not isinstance(values, Mapping):
            return False
        for value in values.values():
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and not _finite_number(value)
            ):
                return False
    return True


@dataclass(frozen=True, slots=True)
class FailureEvidenceReceipt:
    version: Literal[1]
    receipt_id: str
    receipt_sha256: str
    node_id: str
    evidence_sha256: str
    baseline_failure_count: int
    strong_gate_failure_count: int = 0

    def __post_init__(self) -> None:
        if self.version != 1:
            raise AttemptEvidenceError("unsupported evidence receipt version")
        if not isinstance(self.receipt_id, str) or not _RECEIPT_ID_RE.fullmatch(
            self.receipt_id
        ):
            raise AttemptEvidenceError("failure receipt id is invalid")
        for value, label in (
            (self.receipt_sha256, "failure receipt digest"),
            (self.evidence_sha256, "evidence digest"),
        ):
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise AttemptEvidenceError(f"{label} is invalid")
        if self.node_id != _text(self.node_id, "worker report node id"):
            raise AttemptEvidenceError("worker report node id must be canonical")
        _nonnegative_integer(
            self.baseline_failure_count,
            "baseline failure count",
        )
        _nonnegative_integer(
            self.strong_gate_failure_count,
            "strong gate failure count",
        )
        if self.baseline_failure_count + self.strong_gate_failure_count < 1:
            raise AttemptEvidenceError(
                "evidence receipt requires a measured gate failure"
            )
        digest = _sha256(_receipt_payload(self))
        if (
            self.receipt_id != f"failure_receipt_{digest}"
            or self.receipt_sha256 != f"sha256:{digest}"
        ):
            raise AttemptEvidenceError(
                "failure receipt identity does not match its content"
            )


def _receipt_payload(receipt: FailureEvidenceReceipt) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": receipt.version,
        "node_id": receipt.node_id,
        "evidence_sha256": receipt.evidence_sha256,
        "baseline_failure_count": receipt.baseline_failure_count,
    }
    if receipt.strong_gate_failure_count:
        payload["strong_gate_failure_count"] = receipt.strong_gate_failure_count
    return payload


def serialize_failure_evidence_receipt(
    receipt: FailureEvidenceReceipt,
) -> dict[str, object]:
    if not isinstance(receipt, FailureEvidenceReceipt):
        raise AttemptEvidenceError("value is not a FailureEvidenceReceipt")
    document = {
        **_receipt_payload(receipt),
        "receipt_id": receipt.receipt_id,
        "receipt_sha256": receipt.receipt_sha256,
    }
    validate_named_schema("failure_evidence_receipt", document)
    return document


def parse_failure_evidence_receipt(value: object) -> FailureEvidenceReceipt:
    raw = _mapping(value, "failure evidence receipt")
    validate_named_schema("failure_evidence_receipt", dict(raw))
    required = frozenset(
        {
            "version",
            "receipt_id",
            "receipt_sha256",
            "node_id",
            "evidence_sha256",
            "baseline_failure_count",
        }
    )
    missing = sorted(required - raw.keys())
    unexpected = sorted(raw.keys() - required - {"strong_gate_failure_count"})
    if missing:
        raise AttemptEvidenceError(
            f"failure evidence receipt is missing keys {missing}"
        )
    if unexpected:
        raise AttemptEvidenceError(
            f"failure evidence receipt has unexpected keys {unexpected}"
        )
    return FailureEvidenceReceipt(
        version=raw["version"],
        receipt_id=raw["receipt_id"],
        receipt_sha256=raw["receipt_sha256"],
        node_id=_text(raw["node_id"], "worker report node id"),
        evidence_sha256=raw["evidence_sha256"],
        baseline_failure_count=_nonnegative_integer(
            raw["baseline_failure_count"],
            "baseline failure count",
        ),
        strong_gate_failure_count=_nonnegative_integer(
            raw.get("strong_gate_failure_count", 0),
            "strong gate failure count",
        ),
    )


@dataclass(frozen=True, slots=True)
class ConclusiveFailure:
    receipt: FailureEvidenceReceipt
    private_lesson_sha256: str
    kind: Literal["conclusive_failure"] = "conclusive_failure"

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, FailureEvidenceReceipt):
            raise AttemptEvidenceError("failure evidence receipt is invalid")
        if not isinstance(
            self.private_lesson_sha256,
            str,
        ) or not _SHA256_RE.fullmatch(self.private_lesson_sha256):
            raise AttemptEvidenceError("private lesson digest is invalid")


@dataclass(frozen=True, slots=True)
class NeedsMoreEvidence:
    reason: NeedsMoreEvidenceReason
    kind: Literal["needs_more_evidence"] = "needs_more_evidence"

    def __post_init__(self) -> None:
        if not isinstance(self.reason, NeedsMoreEvidenceReason):
            raise AttemptEvidenceError("needs-more-evidence reason is invalid")


@dataclass(frozen=True, slots=True)
class NeedsData:
    data_status: Literal["missing", "invalid"]
    kind: Literal["needs_data"] = "needs_data"

    def __post_init__(self) -> None:
        if self.data_status not in {"missing", "invalid"}:
            raise AttemptEvidenceError("needs-data status is invalid")


@dataclass(frozen=True, slots=True)
class StrongCandidate:
    evidence_digest: str
    kind: Literal["strong_candidate"] = "strong_candidate"

    def __post_init__(self) -> None:
        if not isinstance(
            self.evidence_digest,
            str,
        ) or not _SHA256_RE.fullmatch(self.evidence_digest):
            raise AttemptEvidenceError("strong candidate evidence digest is invalid")


AttemptEvidence: TypeAlias = (
    ConclusiveFailure | NeedsMoreEvidence | NeedsData | StrongCandidate
)


def _passes_requirement(
    metric_value: float,
    baseline_value: float,
    operator: str,
    margin: float,
) -> bool:
    if operator == "greater_than":
        return metric_value > baseline_value + margin
    if operator == "greater_equal":
        return metric_value >= baseline_value + margin
    if operator == "less_than":
        return metric_value < baseline_value - margin
    if operator == "less_equal":
        return metric_value <= baseline_value - margin
    raise AttemptEvidenceError(f"unsupported baseline operator {operator!r}")


def _failed_measurements(
    report: Mapping[str, Any],
) -> tuple[dict[str, object], ...]:
    metrics = _mapping(report["metrics"], "worker report metrics")
    baselines = _mapping(report["baselines"], "worker report baselines")
    status = _mapping(
        report.get("baseline_evidence_status"),
        "baseline evidence status",
    )
    _exact_keys(
        status,
        required=frozenset({"overall", "results"}),
        label="baseline evidence status",
    )
    if status["overall"] != "failed" or not isinstance(status["results"], list):
        raise AttemptEvidenceError("baseline failure details are malformed")
    failures: list[dict[str, object]] = []
    required = frozenset(
        {
            "role",
            "metric_key",
            "baseline_key",
            "operator",
            "margin",
            "required",
            "metric_value",
            "baseline_value",
            "status",
            "reason",
        }
    )
    for index, value in enumerate(status["results"]):
        result = _mapping(value, f"baseline result {index}")
        _exact_keys(result, required=required, label=f"baseline result {index}")
        if not isinstance(result["required"], bool):
            raise AttemptEvidenceError("baseline required flag is malformed")
        if result["required"] is not True:
            continue
        operator = result["operator"]
        role = _text(result["role"], "baseline role")
        metric_key = result["metric_key"]
        baseline_key = result["baseline_key"]
        metric_value = result["metric_value"]
        baseline_value = result["baseline_value"]
        margin = result["margin"]
        if (
            operator not in _BASELINE_OPERATORS
            or not isinstance(metric_key, str)
            or not isinstance(baseline_key, str)
            or not _finite_number(metric_value)
            or not _finite_number(baseline_value)
            or not _finite_number(margin)
            or metrics.get(metric_key) != metric_value
            or baselines.get(baseline_key) != baseline_value
        ):
            raise AttemptEvidenceError("baseline measurement details are malformed")
        passed = _passes_requirement(
            float(metric_value),
            float(baseline_value),
            operator,
            float(margin),
        )
        expected_status = "passed" if passed else "failed"
        if result["status"] != expected_status:
            raise AttemptEvidenceError("baseline result status is inconsistent")
        if passed:
            continue
        failures.append(
            {
                "role": role,
                "metric_key": metric_key,
                "baseline_key": baseline_key,
                "operator": operator,
                "margin": float(margin),
                "metric_value": float(metric_value),
                "baseline_value": float(baseline_value),
            }
        )
    normalized = tuple(
        sorted(
            failures,
            key=lambda item: (
                item["role"],
                item["metric_key"],
                item["baseline_key"],
            ),
        )
    )
    if not normalized:
        raise AttemptEvidenceError("baseline failure has no failed requirement")
    return normalized


def _baseline_overall(report: Mapping[str, Any]) -> str | None:
    status = report.get("baseline_evidence_status")
    if not isinstance(status, Mapping):
        return None
    overall = status.get("overall")
    return overall if overall in {"passed", "failed", "not_evaluable"} else None


def _has_measured_metrics(report: Mapping[str, Any]) -> bool:
    metrics = report.get("metrics")
    return isinstance(metrics, Mapping) and any(
        _finite_number(value) for value in metrics.values()
    )


def _failure_outcome(
    report: Mapping[str, Any],
    failures: tuple[dict[str, object], ...],
) -> ConclusiveFailure:
    measurements = {
        "baseline_failures": list(failures),
    }
    evidence_sha256 = f"sha256:{_sha256(measurements)}"
    receipt_payload = {
        "version": 1,
        "node_id": report["node_id"],
        "evidence_sha256": evidence_sha256,
        "baseline_failure_count": len(failures),
    }
    digest = _sha256(receipt_payload)
    receipt = FailureEvidenceReceipt(
        version=1,
        receipt_id=f"failure_receipt_{digest}",
        receipt_sha256=f"sha256:{digest}",
        node_id=report["node_id"],
        evidence_sha256=evidence_sha256,
        baseline_failure_count=len(failures),
    )
    lesson = {
        "kind": "mandatory_baseline_failure",
        "baseline_failures": list(failures),
    }
    return ConclusiveFailure(
        receipt=receipt,
        private_lesson_sha256=f"sha256:{_sha256(lesson)}",
    )


def derive_attempt_evidence(
    worker_report: object,
    *,
    data_status: DataStatus,
) -> AttemptEvidence:
    if data_status not in _DATA_STATUSES:
        raise AttemptEvidenceError(f"unsupported data status {data_status!r}")
    if data_status in {"missing", "invalid"}:
        return NeedsData(data_status=data_status)
    if not isinstance(worker_report, Mapping):
        return NeedsMoreEvidence(NeedsMoreEvidenceReason.MALFORMED_EVIDENCE)
    try:
        validate_named_schema("worker_report", dict(worker_report))
        node_id = _text(worker_report["node_id"], "worker report node id")
        if worker_report["node_id"] != node_id or not _measurements_are_finite(
            worker_report
        ):
            raise AttemptEvidenceError("worker report content is not canonical")
    except (AttemptEvidenceError, KeyError, TypeError, ValueError):
        return NeedsMoreEvidence(NeedsMoreEvidenceReason.MALFORMED_EVIDENCE)
    status = worker_report["status"]
    if status in _RUNNER_FAILURE_STATUSES:
        return NeedsMoreEvidence(
            NeedsMoreEvidenceReason.RUNNER_DID_NOT_COMPLETE
        )
    if status != "completed":
        return NeedsMoreEvidence(NeedsMoreEvidenceReason.MALFORMED_EVIDENCE)

    overall = _baseline_overall(worker_report)
    if overall is None or overall == "not_evaluable":
        return NeedsMoreEvidence(NeedsMoreEvidenceReason.NOT_EVALUABLE)
    if overall == "failed":
        try:
            failures = _failed_measurements(worker_report)
            return _failure_outcome(worker_report, failures)
        except (AttemptEvidenceError, KeyError, TypeError, ValueError):
            return NeedsMoreEvidence(
                NeedsMoreEvidenceReason.MALFORMED_EVIDENCE
            )

    verdict = worker_report["claim_verdict_candidate"]
    if verdict == "supported":
        if (
            _has_measured_metrics(worker_report)
            and not worker_report["disproof_conditions_hit"]
        ):
            evidence = {
                "node_id": worker_report["node_id"],
                "metrics": worker_report["metrics"],
                "baselines": worker_report["baselines"],
                "baseline_evidence_status": worker_report.get(
                    "baseline_evidence_status"
                ),
            }
            try:
                digest = _sha256(evidence)
            except (OverflowError, TypeError, ValueError):
                return NeedsMoreEvidence(
                    NeedsMoreEvidenceReason.MALFORMED_EVIDENCE
                )
            return StrongCandidate(evidence_digest=f"sha256:{digest}")
        return NeedsMoreEvidence(NeedsMoreEvidenceReason.UNSAFE_POSITIVE)
    if verdict == "contradicted":
        return NeedsMoreEvidence(
            NeedsMoreEvidenceReason.LABEL_ONLY_CONTRADICTION
        )
    if verdict == "not_evaluable":
        return NeedsMoreEvidence(NeedsMoreEvidenceReason.NOT_EVALUABLE)
    return NeedsMoreEvidence(NeedsMoreEvidenceReason.INCONCLUSIVE)


def derive_falsifier_failure(
    worker_report: object,
    falsifier_result: object,
    *,
    binding: Mapping[str, str],
) -> ConclusiveFailure | None:
    worker_evidence = derive_attempt_evidence(
        worker_report,
        data_status="satisfied",
    )
    if not isinstance(worker_evidence, StrongCandidate):
        return None
    if not isinstance(falsifier_result, Mapping):
        return None
    try:
        validate_named_schema("falsifier_result", dict(falsifier_result))
    except (TypeError, ValueError):
        return None
    expected = {
        "contract_id": binding.get("contract_id"),
        "attempt_id": binding.get("attempt_id"),
        "direction_id": binding.get("direction_id"),
        "node_id": binding.get("node_id"),
        "manifest_id": binding.get("manifest_id"),
    }
    if any(falsifier_result.get(key) != value for key, value in expected.items()):
        return None
    if (
        falsifier_result.get("produced_by") != "harness_falsifier_module"
        or falsifier_result.get("kind") != "real_holdout"
        or falsifier_result.get("verdict") != "failed"
        or falsifier_result.get("passed") is not False
        or falsifier_result.get("transfer_evidence_admissible") is not False
    ):
        return None
    measurements = {
        "worker_evidence_sha256": worker_evidence.evidence_digest,
        "falsifier_result": dict(falsifier_result),
    }
    evidence_sha256 = f"sha256:{_sha256(measurements)}"
    receipt_payload = {
        "version": 1,
        "node_id": expected["node_id"],
        "evidence_sha256": evidence_sha256,
        "baseline_failure_count": 0,
        "strong_gate_failure_count": 1,
    }
    digest = _sha256(receipt_payload)
    receipt = FailureEvidenceReceipt(
        version=1,
        receipt_id=f"failure_receipt_{digest}",
        receipt_sha256=f"sha256:{digest}",
        node_id=str(expected["node_id"]),
        evidence_sha256=evidence_sha256,
        baseline_failure_count=0,
        strong_gate_failure_count=1,
    )
    lesson = {
        "kind": "bound_real_holdout_failure",
        "attempt_id": expected["attempt_id"],
        "direction_id": expected["direction_id"],
        "falsifier_result_sha256": f"sha256:{_sha256(falsifier_result)}",
    }
    return ConclusiveFailure(
        receipt=receipt,
        private_lesson_sha256=f"sha256:{_sha256(lesson)}",
    )
