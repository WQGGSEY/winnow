from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when data does not satisfy a JSON-schema subset."""


SCHEMA_FILES = {
    "node": "node.schema.json",
    "experiment_plan": "experiment_plan.schema.json",
    "job_manifest": "job_manifest.schema.json",
    "worker_task": "worker_task.schema.json",
    "worker_task_result": "worker_task_result.schema.json",
    "worker_report": "worker_report.schema.json",
    "critic_review": "critic_review.schema.json",
    "ac_decision": "ac_decision.schema.json",
    "invocation_envelope": "invocation_envelope.schema.json",
    "baseline_dossier": "baseline_dossier.schema.json",
    "manual_live_smoke_plan": "manual_live_smoke_plan.schema.json",
    "live_node_dispatch": "live_node_dispatch.schema.json",
    "live_reduction_apply_summary": "live_reduction_apply_summary.schema.json",
    "live_reduction_bundle": "live_reduction_bundle.schema.json",
    "live_memory_update_summary": "live_memory_update_summary.schema.json",
    "live_rebuttal_session": "live_rebuttal_session.schema.json",
    "live_smoke_run_summary": "live_smoke_run_summary.schema.json",
    "frontier_item": "frontier_item.schema.json",
    "node_transition": "node_transition.schema.json",
    "search_state": "search_state.schema.json",
    "runner_result": "runner_result.schema.json",
    "grilling_session": "grilling_session.schema.json",
    "connector_session": "connector_session.schema.json",
    "market_research_brief": "market_research_brief.schema.json",
    "reference_paper": "reference_paper.schema.json",
    "lesson_distillation_summary": "lesson_distillation_summary.schema.json",
    "user_experiment_plan_metadata": "user_experiment_plan_metadata.schema.json",
    "refined_research_plan": "refined_research_plan.schema.json",
    "dataset_manifest": "dataset_manifest.schema.json",
    "adapter_snapshots": "adapter_snapshots.schema.json",
    "runtime_inputs": "runtime_inputs.schema.json",
    "camera_ready_revision": "camera_ready_revision.schema.json",
    "paper_outline": "paper_outline.schema.json",
    "paper_section": "paper_section.schema.json",
    "paper_figure_request": "paper_figure_request.schema.json",
    "user_goal_attestation": "user_goal_attestation.schema.json",
    "alternative_root_proposal": "alternative_root_proposal.schema.json",
    "feasibility_envelope": "feasibility_envelope.schema.json",
    "falsifier_result": "falsifier_result.schema.json",
    "frozen_question": "frozen_question.schema.json",
    "construct_adversary_report": "construct_adversary_report.schema.json",
    "solution_contract": "solution_contract.schema.json",
    "research_goal": "research_goal.schema.json",
    "reorientation_state": "reorientation_state.schema.json",
    "direction_fingerprint": "direction_fingerprint.schema.json",
    "generation_request": "generation_request.schema.json",
    "direction_draft": "direction_draft.schema.json",
    "blind_direction_proposal": "blind_direction_proposal.schema.json",
    "structural_axis_relations": "structural_axis_relations.schema.json",
    "failure_evidence_receipt": "failure_evidence_receipt.schema.json",
}


def _schemas_dir() -> Path:
    return Path(__file__).resolve().parent


def load_schema(name: str) -> dict[str, Any]:
    try:
        filename = SCHEMA_FILES[name]
    except KeyError as exc:
        raise SchemaValidationError(f"unknown schema: {name}") from exc
    return json.loads((_schemas_dir() / filename).read_text(encoding="utf-8"))


def validate_named_schema(name: str, data: Any) -> None:
    validate_schema(load_schema(name), data)


def validate_schema(schema: dict[str, Any], data: Any) -> None:
    _validate(schema, data, "$")


def _type_name(data: Any) -> str:
    if data is None:
        return "null"
    if isinstance(data, bool):
        return "boolean"
    if isinstance(data, int):
        return "integer"
    if isinstance(data, float):
        return "number"
    if isinstance(data, str):
        return "string"
    if isinstance(data, list):
        return "array"
    if isinstance(data, dict):
        return "object"
    return type(data).__name__


def _matches_type(data: Any, expected: str) -> bool:
    if expected == "null":
        return data is None
    if expected == "boolean":
        return isinstance(data, bool)
    if expected == "integer":
        return isinstance(data, int) and not isinstance(data, bool)
    if expected == "number":
        return (isinstance(data, int) or isinstance(data, float)) and not isinstance(
            data, bool
        )
    if expected == "string":
        return isinstance(data, str)
    if expected == "array":
        return isinstance(data, list)
    if expected == "object":
        return isinstance(data, dict)
    return False


def _matches_const(data: Any, expected: Any) -> bool:
    if isinstance(data, bool) or isinstance(expected, bool):
        return (
            isinstance(data, bool)
            and isinstance(expected, bool)
            and data == expected
        )
    if (
        isinstance(data, (int, float))
        and not isinstance(data, bool)
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
    ):
        return data == expected
    if isinstance(data, list) and isinstance(expected, list):
        return len(data) == len(expected) and all(
            _matches_const(item, expected[index])
            for index, item in enumerate(data)
        )
    if isinstance(data, dict) and isinstance(expected, dict):
        return data.keys() == expected.keys() and all(
            _matches_const(data[key], expected[key]) for key in data
        )
    return type(data) is type(expected) and data == expected


def _validate(schema: dict[str, Any], data: Any, path: str) -> None:
    if not schema:
        return

    if "type" in schema:
        expected_types = schema["type"]
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not any(_matches_type(data, expected) for expected in expected_types):
            expected = " | ".join(expected_types)
            raise SchemaValidationError(
                f"{path}: expected {expected}, got {_type_name(data)}"
            )

    if "const" in schema and not _matches_const(data, schema["const"]):
        raise SchemaValidationError(
            f"{path}: expected constant {schema['const']!r}, got {data!r}"
        )

    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                _validate(branch, data, path)
            except SchemaValidationError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaValidationError(
                f"{path}: expected exactly one oneOf branch, matched {matches}"
            )

    if "enum" in schema and data not in schema["enum"]:
        raise SchemaValidationError(f"{path}: {data!r} not in enum {schema['enum']!r}")

    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            raise SchemaValidationError(f"{path}: string shorter than minLength")
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            raise SchemaValidationError(f"{path}: string longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], data) is None:
            raise SchemaValidationError(f"{path}: does not match {schema['pattern']!r}")

    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            raise SchemaValidationError(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            raise SchemaValidationError(f"{path}: above maximum {schema['maximum']}")

    if isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            raise SchemaValidationError(f"{path}: fewer than minItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(data):
                _validate(item_schema, item, f"{path}[{index}]")

    if isinstance(data, dict):
        # Report ALL missing required keys at once instead of one at a time.
        # Otherwise an LLM filling fields reactively will re-submit N times
        # for N missing fields, with the previous N-1 retries all wasted on
        # the same trivial fix. Listing the full set lets the next attempt
        # close the gap in one shot.
        required = schema.get("required", [])
        missing = [k for k in required if k not in data]
        if missing:
            if len(missing) == 1:
                raise SchemaValidationError(
                    f"{path}: missing required key {missing[0]!r}"
                )
            raise SchemaValidationError(
                f"{path}: missing required keys {missing!r}"
            )

        # Likewise report all unexpected keys together when
        # additionalProperties=False, instead of bailing on the first one.
        properties = schema.get("properties", {})
        unexpected: list[str] = []
        if schema.get("additionalProperties") is False:
            unexpected = [k for k in data.keys() if k not in properties]
        if unexpected:
            if len(unexpected) == 1:
                raise SchemaValidationError(
                    f"{path}: unexpected key {unexpected[0]!r}"
                )
            raise SchemaValidationError(
                f"{path}: unexpected keys {unexpected!r}"
            )
        for key, value in data.items():
            if key in properties:
                _validate(properties[key], value, f"{path}.{key}")
