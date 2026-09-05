from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from research_harness.acquisition.cache import ContentAddressedCache
from research_harness.acquisition.model import PinnedNodeManifest
from research_harness.data_adapters import AdapterError, fingerprint_path
from research_harness.datasets import materialize
from research_harness.evaluation_vault import reject_private_evaluation_input
from research_harness.schemas.validator import SchemaValidationError, validate_named_schema
from research_harness.workers.workspace import WorkspaceGuardError, ensure_path_inside


RUNTIME_INPUT_MANIFEST = "runtime_inputs.json"
RUNTIME_INPUT_ENV = "RESEARCH_HARNESS_INPUT_MANIFEST"


class RuntimeInputError(ValueError):
    pass


_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def _ensure_runtime_path(path: Path, root: Path, label: str) -> None:
    try:
        ensure_path_inside(path, root, label)
    except WorkspaceGuardError as exc:
        raise RuntimeInputError(str(exc)) from exc


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
    try:
        reject_private_evaluation_input(source)
    except ValueError as exc:
        raise RuntimeInputError(str(exc)) from exc
    if target.exists():
        try:
            _verify_content(target, expected_digest)
            return
        except RuntimeInputError:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
    elif target.is_symlink():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent)
        )
        try:
            shutil.copytree(source, temporary, dirs_exist_ok=True)
            _verify_content(temporary, expected_digest)
            temporary.replace(target)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(raw_temporary)
    try:
        shutil.copy2(source, temporary)
        _verify_content(temporary, expected_digest)
        temporary.replace(target)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if temporary.exists():
            temporary.unlink()
        raise


def _bind_json_document(
    path: Path,
    payload: object,
    *,
    workspace: Path,
    label: str,
) -> None:
    _ensure_runtime_path(path.parent, workspace, f"{label} parent")
    legacy_temporary = path.with_suffix(path.suffix + ".tmp")
    if legacy_temporary.is_symlink():
        legacy_temporary.unlink()
    elif legacy_temporary.exists():
        if legacy_temporary.is_dir():
            shutil.rmtree(legacy_temporary)
        else:
            legacy_temporary.unlink()
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if existing == payload:
            return
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    _write_json_atomic(path, payload)


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
    _ensure_runtime_path(target.parent, workspace, "runtime dataset parent")
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
    _bind_json_document(
        manifest_path,
        payload,
        workspace=workspace,
        label="runtime input manifest",
    )
    return {
        "runtime_manifest": RUNTIME_INPUT_MANIFEST,
        "snapshot_id": snapshot["snapshot_id"],
    }


def bind_acquisition_manifest(
    *,
    manifest: PinnedNodeManifest,
    cache_root: Path,
    workspace: Path,
) -> dict[str, str]:
    if not isinstance(manifest, PinnedNodeManifest):
        raise RuntimeInputError("acquisition runtime input requires a pinned manifest")
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    snapshot_id = "as_" + manifest.manifest_id.removeprefix("acqmanifest_")
    aggregate = workspace / "inputs" / snapshot_id
    _ensure_runtime_path(
        aggregate.parent,
        workspace,
        "acquisition runtime dataset parent",
    )
    if aggregate.is_symlink():
        aggregate.unlink()
    elif aggregate.exists() and not aggregate.is_dir():
        aggregate.unlink()
    aggregate.mkdir(parents=True, exist_ok=True)
    cache = ContentAddressedCache(cache_root)
    needs: list[dict[str, object]] = []
    for acquired in manifest.acquired_needs:
        source = cache.object_path(acquired.receipt.cache_object)
        expected = acquired.receipt.cache_object.content_sha256.removeprefix(
            "sha256:"
        )
        _verify_content(source, expected)
        suffix = Path(urlsplit(acquired.receipt.source_uri).path).suffix
        if _SAFE_SUFFIX.fullmatch(suffix) is None:
            suffix = ""
        target_name = f"need_{acquired.need_index:03d}"
        target = aggregate / (
            target_name if source.is_dir() else target_name + suffix
        )
        _stage(source, target, expected)
        needs.append(
            {
                "need_index": acquired.need_index,
                "description": acquired.description,
                "relative_path": target.relative_to(aggregate).as_posix(),
                "receipt_id": acquired.receipt.receipt_id,
                "source_kind": acquired.receipt.source_kind,
                "source_uri": acquired.receipt.source_uri,
                "content_sha256": acquired.receipt.cache_object.content_sha256,
            }
        )
    index_payload = {
        "manifest_id": manifest.manifest_id,
        "direction_id": manifest.direction_id,
        "needs": needs,
    }
    index_path = aggregate / "index.json"
    _bind_json_document(
        index_path,
        index_payload,
        workspace=workspace,
        label="acquisition runtime index",
    )
    expected_names = {
        Path(item["relative_path"]).parts[0]
        for item in needs
    } | {index_path.name}
    for entry in aggregate.iterdir():
        if entry.name in expected_names:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    content_sha256, size_bytes, _ = _fingerprint_runtime_path(aggregate)
    payload = {
        "schema_version": 1,
        "primary_dataset": {
            "adapter_id": "acquisition",
            "snapshot_id": snapshot_id,
            "relative_path": aggregate.relative_to(workspace).as_posix(),
            "content_sha256": content_sha256,
            "size_bytes": size_bytes,
            "fetcher_type": "pinned_acquisition_manifest",
            "provenance": f"acquisition manifest {manifest.manifest_id}",
        },
    }
    validate_named_schema("runtime_inputs", payload)
    manifest_path = workspace / RUNTIME_INPUT_MANIFEST
    _bind_json_document(
        manifest_path,
        payload,
        workspace=workspace,
        label="runtime input manifest",
    )
    return {
        "runtime_manifest": RUNTIME_INPUT_MANIFEST,
        "snapshot_id": snapshot_id,
    }


def _fingerprint_runtime_path(path: Path) -> tuple[str, int, int]:
    try:
        return fingerprint_path(path)
    except (AdapterError, OSError) as exc:
        raise RuntimeInputError(str(exc)) from exc


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
    _ensure_runtime_path(manifest_path, workspace, "runtime input manifest")
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
    _ensure_runtime_path(dataset_path, workspace, "runtime dataset")
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
    "bind_acquisition_manifest",
    "bind_runtime_input",
    "runtime_input_environment",
    "validate_runtime_input_reference",
]
