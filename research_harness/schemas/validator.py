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
    "frontier_item": "frontier_item.schema.json",
    "node_transition": "node_transition.schema.json",
    "search_state": "search_state.schema.json",
    "runner_result": "runner_result.schema.json",
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

    if "enum" in schema and data not in schema["enum"]:
        raise SchemaValidationError(f"{path}: {data!r} not in enum {schema['enum']!r}")

    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            raise SchemaValidationError(f"{path}: string shorter than minLength")
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
        required = schema.get("required", [])
        for key in required:
            if key not in data:
                raise SchemaValidationError(f"{path}: missing required key {key!r}")

        properties = schema.get("properties", {})
        for key, value in data.items():
            if key in properties:
                _validate(properties[key], value, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise SchemaValidationError(f"{path}: unexpected key {key!r}")
