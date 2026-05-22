from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from research_harness.schemas.validator import SchemaValidationError, validate_named_schema


class OutputRepairError(ValueError):
    """Raised when worker output cannot be repaired without adding facts."""


@dataclass(frozen=True)
class RepairResult:
    repaired: bool
    data: dict[str, Any]
    note: str


def parse_or_repair_json(raw: str, schema_name: str) -> RepairResult:
    """Parse worker JSON, allowing only deterministic format repair.

    This function may strip markdown fences or surrounding prose and extract one
    balanced JSON object. It never invents missing fields, changes values, or
    converts a schema-invalid object into a schema-valid one.
    """

    try:
        data = _loads_object(raw)
        validate_named_schema(schema_name, data)
        return RepairResult(repaired=False, data=data, note="valid_json")
    except (json.JSONDecodeError, OutputRepairError):
        pass
    except SchemaValidationError as exc:
        raise OutputRepairError(f"schema-invalid JSON cannot be repaired: {exc}") from exc

    candidate = _extract_single_json_object(_strip_markdown_fence(raw))
    try:
        data = _loads_object(candidate)
    except json.JSONDecodeError as exc:
        raise OutputRepairError("could not parse repaired JSON candidate") from exc
    try:
        validate_named_schema(schema_name, data)
    except SchemaValidationError as exc:
        raise OutputRepairError(f"repaired JSON is schema-invalid: {exc}") from exc
    return RepairResult(repaired=True, data=data, note="format_only_repair")


def _loads_object(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise OutputRepairError("worker output must be a JSON object")
    return data


def _strip_markdown_fence(raw: str) -> str:
    stripped = raw.strip()
    if not stripped.startswith("```"):
        return raw

    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return raw


def _extract_single_json_object(raw: str) -> str:
    start = raw.find("{")
    if start < 0:
        raise OutputRepairError("no JSON object found")

    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index in range(start, len(raw)):
        char = raw[index]
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break

    if end < 0:
        raise OutputRepairError("JSON object is not balanced")

    trailing = raw[end:].strip()
    if "{" in trailing:
        raise OutputRepairError("multiple JSON objects found")
    return raw[start:end]

