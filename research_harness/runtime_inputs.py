from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from research_harness.data_adapters import AdapterError, fingerprint_path
from research_harness.datasets import materialize
from research_harness.schemas.validator import SchemaValidationError, validate_named_schema
from research_harness.workers.workspace import ensure_path_inside


RUNTIME_INPUT_MANIFEST = "runtime_inputs.json"
RUNTIME_INPUT_ENV = "RESEARCH_HARNESS_INPUT_MANIFEST"


class RuntimeInputError(ValueError):
    pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_content(path: Path, expected_digest: str) -> tuple[int, int]:
    try:
        digest, size_bytes, entry_count = fingerprint_path(path)
    except (AdapterError, OSError) as exc:
        raise RuntimeInputError(str(exc)) from exc
    if digest != expected_digest:
        raise RuntimeInputError(
            f"dataset content hash mismatch: expected {expected_digest}, got {digest}"
        )
    return size_bytes, entry_count


def _stage(source: Path, target: Path, expected_digest: str) -> None:
    if target.exists():
        _verify_content(target, expected_digest)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".staging")
    if temporary.exists():
        if temporary.is_dir():
            shutil.rmtree(temporary)
        else:
            temporary.unlink()
    if source.is_dir():
        shutil.copytree(source, temporary)
    else:
        shutil.copy2(source, temporary)
    _verify_content(temporary, expected_digest)
    temporary.replace(target)


def bind_runtime_input(
    *,
    snapshot: dict[str, Any],
    workspace: Path,
    repo_root: Path,
) -> dict[str, str]:
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    source = Path(snapshot["source"]).expanduser()
    _verify_content(source, snapshot["content_sha256"])
    spec = {
        "id": snapshot["snapshot_id"],
        "type": snapshot["materializer_type"],
        "role": snapshot["role"],
        "source": f"file://{source}",
    }
    result = materialize(spec, repo_root=repo_root)
    if result.status != "ok" or result.materialized_path is None:
        raise RuntimeInputError(result.error or f"materializer returned {result.status}")
    materialized = result.materialized_path.resolve()
    _verify_content(materialized, snapshot["content_sha256"])

    target = workspace / "inputs" / snapshot["snapshot_id"] / materialized.name
    ensure_path_inside(target, workspace, "runtime dataset")
    _stage(materialized, target, snapshot["content_sha256"])
    relative_path = target.relative_to(workspace).as_posix()
    payload = {
        "schema_version": 1,
        "primary_dataset": {
            "adapter_id": snapshot["adapter_id"],
            "snapshot_id": snapshot["snapshot_id"],
            "relative_path": relative_path,
            "content_sha256": snapshot["content_sha256"],
            "size_bytes": snapshot["size_bytes"],
            "fetcher_type": result.fetcher_type,
            "provenance": snapshot["provenance"],
        },
    }
    validate_named_schema("runtime_inputs", payload)
    manifest_path = workspace / RUNTIME_INPUT_MANIFEST
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeInputError(f"existing runtime input manifest is invalid: {exc}") from exc
        if existing != payload:
            raise RuntimeInputError("workspace already contains a different runtime input manifest")
    else:
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(manifest_path)
    return {
        "runtime_manifest": RUNTIME_INPUT_MANIFEST,
        "snapshot_id": snapshot["snapshot_id"],
    }


def validate_runtime_input_reference(
    inputs: object,
    *,
    workspace: Path,
) -> dict[str, str] | None:
    if inputs in ({}, {"datasets": [], "snapshots": []}):
        return None
    if not isinstance(inputs, dict) or set(inputs) != {"runtime_manifest", "snapshot_id"}:
        raise RuntimeInputError("job inputs must be empty or a typed runtime input reference")
    raw_manifest = inputs["runtime_manifest"]
    snapshot_id = inputs["snapshot_id"]
    if not isinstance(raw_manifest, str) or not isinstance(snapshot_id, str):
        raise RuntimeInputError("runtime input reference fields must be strings")
    relative_manifest = Path(raw_manifest)
    if relative_manifest.is_absolute() or ".." in relative_manifest.parts:
        raise RuntimeInputError("runtime input manifest must use a relative workspace path")
    workspace = workspace.resolve()
    manifest_path = (workspace / relative_manifest).resolve()
    ensure_path_inside(manifest_path, workspace, "runtime input manifest")
    if not manifest_path.is_file():
        raise RuntimeInputError(f"runtime input manifest is missing: {raw_manifest}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        dataset = payload["primary_dataset"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeInputError(f"runtime input manifest is malformed: {exc}") from exc
    try:
        validate_named_schema("runtime_inputs", payload)
    except SchemaValidationError as exc:
        raise RuntimeInputError(f"runtime input manifest is malformed: {exc}") from exc
    required = {
        "adapter_id",
        "snapshot_id",
        "relative_path",
        "content_sha256",
        "size_bytes",
        "fetcher_type",
        "provenance",
    }
    if payload.get("schema_version") != 1 or not isinstance(dataset, dict) or set(dataset) != required:
        raise RuntimeInputError("runtime input manifest has an unsupported shape")
    if dataset["snapshot_id"] != snapshot_id:
        raise RuntimeInputError("job snapshot_id does not match runtime input manifest")
    relative_dataset = Path(dataset["relative_path"])
    if relative_dataset.is_absolute() or ".." in relative_dataset.parts:
        raise RuntimeInputError("runtime dataset must use a relative workspace path")
    dataset_path = (workspace / relative_dataset).resolve()
    ensure_path_inside(dataset_path, workspace, "runtime dataset")
    size_bytes, _ = _verify_content(dataset_path, dataset["content_sha256"])
    if size_bytes != dataset["size_bytes"]:
        raise RuntimeInputError(
            "runtime dataset size does not match the runtime input manifest"
        )
    return {
        "adapter_id": dataset["adapter_id"],
        "snapshot_id": snapshot_id,
        "content_sha256": dataset["content_sha256"],
        "manifest_sha256": _file_sha256(manifest_path),
        "manifest_path": relative_manifest.as_posix(),
    }


def runtime_input_environment(
    evidence: dict[str, str] | None,
    *,
    workspace: Path,
) -> dict[str, str]:
    if evidence is None:
        return {}
    return {RUNTIME_INPUT_ENV: str((workspace / evidence["manifest_path"]).resolve())}


__all__ = [
    "RUNTIME_INPUT_ENV",
    "RUNTIME_INPUT_MANIFEST",
    "RuntimeInputError",
    "bind_runtime_input",
    "runtime_input_environment",
    "validate_runtime_input_reference",
]
