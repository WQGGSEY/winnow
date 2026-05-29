"""Operator-scope data adapter management (Phase 1b).

Provides the disk-side and settings-side mechanics behind the
``/datasets`` page in the operator frontend:

  - Stream-upload a file into ``.dataset_cache/operator_uploads/<adapter_id>/``
    with sha256 + size + extension allowlist enforcement.
  - Register a ``data_adapters.registered`` entry inside
    ``settings.local.json`` (operator scope; merges atop project's
    ``settings.json`` per ADR 0005).
  - List the merged set of project + operator adapters for the UI.
  - Delete an operator-scope adapter (project-scope entries are
    untouchable from the frontend — those are checked-in defaults).

The actual fetcher dispatch lives in ``research_harness/datasets/``;
this module only handles *registration*. Once an adapter is registered,
``LocalPathMaterializer`` picks up the local source and the existing
materialize() pipeline works unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

# Extension allowlist — explicit deny by default. Executables, shells,
# and notebooks are intentionally excluded; the operator can still drop
# such files manually and use "register by path" if they really need to.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {
        # tabular / structured
        ".csv", ".tsv", ".parquet", ".arrow", ".feather",
        ".json", ".jsonl", ".ndjson",
        # numeric arrays
        ".npy", ".npz",
        # model weights
        ".pt", ".pth", ".safetensors", ".bin", ".ckpt", ".h5", ".hdf5",
        # archives (multi-file datasets)
        ".tar", ".gz", ".tgz", ".zip", ".bz2", ".xz",
        # plain text / reference
        ".txt", ".md",
    }
)

ALLOWED_KINDS: frozenset[str] = frozenset(
    {"raw_data", "benchmark", "model_weights", "factor_set", "custom", "real_panel"}
)

# Streaming chunk size for upload writes. Tuned to keep memory bounded
# while not paying too many small-syscall costs.
CHUNK_SIZE = 1 << 20  # 1 MiB

# Hard cap. Uploads beyond this are rejected with a recommendation to
# place the file on disk manually and use "register by path" instead.
MAX_UPLOAD_BYTES = 2 * (1 << 30)  # 2 GiB

# adapter_id slug — alphanumeric + underscore + hyphen, 1..64 chars.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


class DatasetError(ValueError):
    """Raised on invalid upload / registration input."""


@dataclass(frozen=True)
class UploadResult:
    adapter_id: str
    materialized_path: Path
    size_bytes: int
    sha256: str
    original_filename: str
    uploaded_at: str


# ---------------------------------------------------------------------------
# Disk helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_adapter_id(adapter_id: str) -> None:
    if not _ID_RE.match(adapter_id):
        raise DatasetError(
            f"invalid adapter_id {adapter_id!r}: must be 1..64 chars "
            "of [A-Za-z0-9_-], starting with alphanumeric"
        )


def _validate_filename(filename: str) -> str:
    """Strip path components, enforce extension allowlist. Returns the safe name."""
    safe = Path(filename).name  # drop any directory prefix
    if not safe or safe.startswith("."):
        raise DatasetError(f"invalid filename {filename!r}")
    # Compound extension support: a.tar.gz matches .gz; lowercase compare.
    lower = safe.lower()
    if not any(lower.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        raise DatasetError(
            f"file extension not allowed for {safe!r}; allowed: "
            f"{sorted(ALLOWED_EXTENSIONS)}"
        )
    return safe


def upload_root(repo_root: Path) -> Path:
    """``.dataset_cache/operator_uploads/`` — same parent as other materializer caches.

    Created on first call. ``LocalPathMaterializer`` recognises files
    already inside ``.dataset_cache/`` and skips the redundant copy, so
    placing uploads here avoids multi-GB duplication.
    """
    root = (repo_root / ".dataset_cache" / "operator_uploads").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def upload_file(
    repo_root: Path,
    *,
    adapter_id: str,
    original_filename: str,
    source: BinaryIO,
) -> UploadResult:
    """Stream-write ``source`` into ``.dataset_cache/operator_uploads/<adapter_id>/``.

    Computes sha256 during the write so multi-GB files do not need a
    second pass. Rejects oversized files mid-stream. Uses an atomic
    ``rename`` so a partial / interrupted upload never leaves a half-file
    visible to the materializer.
    """
    _validate_adapter_id(adapter_id)
    safe_name = _validate_filename(original_filename)

    target_dir = upload_root(repo_root) / adapter_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_name
    tmp = target_dir / (safe_name + ".upload-tmp")

    sha = hashlib.sha256()
    written = 0
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise DatasetError(
                        f"upload exceeds {MAX_UPLOAD_BYTES} bytes; place the "
                        "file on disk manually and use 'register by path' instead"
                    )
                sha.update(chunk)
                out.write(chunk)
        tmp.replace(target)
    except BaseException:
        # Cleanup partial file on any failure (DatasetError, IOError, KeyboardInterrupt).
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise

    return UploadResult(
        adapter_id=adapter_id,
        materialized_path=target.resolve(),
        size_bytes=written,
        sha256=sha.hexdigest(),
        original_filename=safe_name,
        uploaded_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# settings.local.json read/write — operator-scope data_adapters
# ---------------------------------------------------------------------------


def _local_settings_path(repo_root: Path) -> Path:
    return (repo_root / "settings.local.json").resolve()


def _load_local(repo_root: Path) -> dict[str, Any]:
    p = _local_settings_path(repo_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_local_atomic(repo_root: Path, data: dict[str, Any]) -> None:
    p = _local_settings_path(repo_root)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(p)


def _operator_adapters(local: dict[str, Any]) -> list[dict[str, Any]]:
    da = local.get("data_adapters")
    if not isinstance(da, dict):
        return []
    reg = da.get("registered")
    return list(reg) if isinstance(reg, list) else []


def _load_project_adapters(repo_root: Path) -> list[dict[str, Any]]:
    p = (repo_root / "settings.json").resolve()
    if not p.exists():
        return []
    try:
        settings = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    da = settings.get("data_adapters") if isinstance(settings, dict) else None
    if not isinstance(da, dict):
        return []
    reg = da.get("registered")
    return list(reg) if isinstance(reg, list) else []


# ---------------------------------------------------------------------------
# Register / list / delete
# ---------------------------------------------------------------------------


def register_adapter(
    repo_root: Path,
    *,
    adapter_id: str,
    kind: str,
    source: str,
    provenance: str,
    upload_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append (or replace) an operator-scope ``data_adapters.registered`` entry.

    ``source`` must be a ``file://`` URL or absolute path that the existing
    ``LocalPathMaterializer`` can resolve. If ``upload_meta`` is provided,
    a ``_upload`` sub-dict is recorded for audit (sha256, size, original
    filename, uploaded_at) and the operator-edited distinguisher.

    Conflict policy: if ``adapter_id`` already exists in *project* scope,
    we still allow registering at operator scope — the operator override
    wins per the scope resolver. If it already exists at operator scope,
    we replace in-place (no duplicate entries).
    """
    _validate_adapter_id(adapter_id)
    if kind not in ALLOWED_KINDS:
        raise DatasetError(
            f"invalid kind {kind!r}; allowed: {sorted(ALLOWED_KINDS)}"
        )
    if not source.strip():
        raise DatasetError("source is required")
    if not (source.startswith("file://") or source.startswith("/")):
        raise DatasetError(
            f"source {source!r} must be a file:// URL or absolute path"
        )
    if not provenance.strip():
        raise DatasetError("provenance is required (be specific about what this is)")

    entry: dict[str, Any] = {
        "id": adapter_id,
        "kind": kind,
        "source": source,
        "provenance": provenance,
        "module": "research_harness.datasets.local_path",
    }
    if upload_meta is not None:
        entry["_upload"] = upload_meta

    local = _load_local(repo_root)
    da = local.get("data_adapters")
    if not isinstance(da, dict):
        da = {}
        local["data_adapters"] = da
    registered = da.get("registered")
    if not isinstance(registered, list):
        registered = []
        da["registered"] = registered

    # replace in-place if id already exists
    for i, existing in enumerate(registered):
        if isinstance(existing, dict) and existing.get("id") == adapter_id:
            registered[i] = entry
            break
    else:
        registered.append(entry)

    _save_local_atomic(repo_root, local)
    return entry


def list_adapters(repo_root: Path) -> list[dict[str, Any]]:
    """Return the merged project + operator adapter list, with each entry
    tagged ``_scope`` so the UI can show provenance.

    Operator entries with the same ``id`` as a project entry replace it
    (whole-array operator override per ADR 0005, applied per-id here as
    a UX convenience so operators see one row instead of two).
    """
    by_id: dict[str, dict[str, Any]] = {}
    for entry in _load_project_adapters(repo_root):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        by_id[entry["id"]] = {**entry, "_scope": "project"}
    for entry in _operator_adapters(_load_local(repo_root)):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        by_id[entry["id"]] = {**entry, "_scope": "operator"}
    return sorted(by_id.values(), key=lambda e: e.get("id", ""))


def delete_adapter(repo_root: Path, adapter_id: str) -> bool:
    """Remove an operator-scope adapter and its uploaded files, if any.

    Project-scope adapters cannot be deleted from the frontend (they live
    in checked-in ``settings.json``). Returns True iff something was
    actually removed.
    """
    _validate_adapter_id(adapter_id)
    local = _load_local(repo_root)
    da = local.get("data_adapters")
    if not isinstance(da, dict):
        return False
    registered = da.get("registered")
    if not isinstance(registered, list):
        return False

    removed: dict[str, Any] | None = None
    new_list: list[Any] = []
    for entry in registered:
        if isinstance(entry, dict) and entry.get("id") == adapter_id and removed is None:
            removed = entry
            continue
        new_list.append(entry)
    if removed is None:
        return False

    da["registered"] = new_list
    _save_local_atomic(repo_root, local)

    # Best-effort cleanup of the upload directory. Only remove if the
    # adapter pointed inside our own operator_uploads tree; never touch
    # arbitrary paths the operator may have registered manually.
    src = removed.get("source", "")
    if isinstance(src, str):
        path_str = src[len("file://") :] if src.startswith("file://") else src
        try:
            p = Path(path_str).resolve()
            uploads = upload_root(repo_root)
            if uploads in p.parents:
                # Walk up to the adapter_id directory and delete the whole tree.
                rel = p.relative_to(uploads)
                if rel.parts:
                    target = uploads / rel.parts[0]
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
        except (OSError, ValueError):
            pass

    return True


__all__ = [
    "ALLOWED_EXTENSIONS",
    "ALLOWED_KINDS",
    "DatasetError",
    "MAX_UPLOAD_BYTES",
    "UploadResult",
    "delete_adapter",
    "list_adapters",
    "register_adapter",
    "upload_file",
    "upload_root",
]
