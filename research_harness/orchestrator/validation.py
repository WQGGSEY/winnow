from __future__ import annotations

from typing import Any

from research_harness.schemas.validator import SchemaValidationError, validate_named_schema


class ValidationError(ValueError):
    """Raised when a scaffold invariant is violated."""


def require_keys(obj: dict[str, Any], keys: list[str], label: str) -> None:
    missing = [key for key in keys if key not in obj]
    if missing:
        raise ValidationError(f"{label} missing required keys: {', '.join(missing)}")


def validate_claim_contract(node: dict[str, Any]) -> None:
    require_keys(node, ["claim_contract"], "node")
    contract = node["claim_contract"]
    require_keys(
        contract,
        [
            "claim_under_test",
            "mandatory_baselines",
            "success_criteria",
            "disproof_conditions",
        ],
        "claim_contract",
    )
    if not contract["mandatory_baselines"]:
        raise ValidationError("claim_contract.mandatory_baselines must not be empty")
    if not contract["success_criteria"]:
        raise ValidationError("claim_contract.success_criteria must not be empty")
    if not contract["disproof_conditions"]:
        raise ValidationError("claim_contract.disproof_conditions must not be empty")


def validate_baseline_roles(node: dict[str, Any]) -> None:
    roles: set[str] = set()
    for ref in node.get("baseline_refs", []):
        roles.update(ref.get("roles", []))

    required_roles = {"current_best_known", "naive", "random_or_null"}
    if node.get("type") in {"capability", "validity", "necessity"}:
        missing = sorted(required_roles - roles)
        if missing:
            raise ValidationError(
                "claim nodes require baseline roles: " + ", ".join(missing)
            )


def validate_worker_scope(node: dict[str, Any]) -> None:
    runtime = node.get("runtime_profile", {})
    worker_type = runtime.get("worker_type")
    if worker_type not in {"text_worker", "experiment_worker", "critic_worker", "runner_job"}:
        raise ValidationError(f"unknown worker_type: {worker_type!r}")


def validate_node_invariants(node: dict[str, Any]) -> None:
    try:
        validate_named_schema("node", node)
    except SchemaValidationError as exc:
        raise ValidationError(str(exc)) from exc

    require_keys(
        node,
        [
            "id",
            "type",
            "status",
            "domain",
            "stage",
            "lineage",
            "claim_contract",
            "baseline_refs",
            "runtime_profile",
            "failure_retrieval",
            "outputs",
        ],
        "node",
    )
    validate_claim_contract(node)
    validate_baseline_roles(node)
    validate_worker_scope(node)
