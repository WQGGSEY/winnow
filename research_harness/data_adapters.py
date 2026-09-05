from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


AdapterScope = Literal["project", "operator"]
MATERIALIZER_TYPES = frozenset(
    {"raw_data", "benchmark", "model_weights", "factor_set", "custom"}
)
DATASET_ROLES = frozenset(
    {"training", "evaluation", "baseline_reference", "baseline_implementation", "other"}
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class AdapterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdapterDeclaration:
    adapter_id: str
    materializer_type: str
    role: str
    source: str
    provenance: str
    scope: AdapterScope


@dataclass(frozen=True, slots=True)
class AdapterProblem:
    adapter_id: str | None
    scope: AdapterScope
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class AdapterSnapshot:
    snapshot_id: str
    adapter_id: str
    materializer_type: str
    role: str
    source: str
    provenance: str
    source_scope: AdapterScope
    content_sha256: str
    size_bytes: int
    entry_count: int


@dataclass(frozen=True, slots=True)
class ResolvedAdapters:
    ready: dict[str, AdapterDeclaration]
    unavailable: dict[str, AdapterProblem]
    unkeyed_problems: tuple[AdapterProblem, ...]


def _load_registered(path: Path, scope: AdapterScope) -> tuple[list[Any], AdapterProblem | None]:
    if not path.exists():
        return [], None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], AdapterProblem(None, scope, "settings_unreadable", str(exc))
    registered = (raw.get("data_adapters") or {}).get("registered") if isinstance(raw, dict) else None
    if registered is None:
        return [], None
    if not isinstance(registered, list):
        return [], AdapterProblem(
            None,
            scope,
            "malformed_registry",
            "data_adapters.registered must be an array",
        )
    return registered, None


def _parse_source(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise AdapterError("source is required")
    source = raw.strip()
    path_text = source[7:] if source.startswith("file://") else source
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise AdapterError("source must be a file:// URL or absolute path")
    return str(path)


def _parse_entry(raw: object, scope: AdapterScope) -> AdapterDeclaration | AdapterProblem:
    if not isinstance(raw, dict):
        return AdapterProblem(None, scope, "malformed", "adapter entry must be an object")
    raw_id = raw.get("id")
    adapter_id = raw_id if isinstance(raw_id, str) and raw_id else None
    if adapter_id is None or not _ID_RE.fullmatch(adapter_id):
        return AdapterProblem(adapter_id, scope, "invalid_id", "adapter id is invalid")
    materializer_type = raw.get("materializer_type")
    role = raw.get("role")
    provenance = raw.get("provenance")
    try:
        source = _parse_source(raw.get("source"))
    except AdapterError as exc:
        return AdapterProblem(adapter_id, scope, "invalid_source", str(exc))
    if materializer_type not in MATERIALIZER_TYPES:
        return AdapterProblem(
            adapter_id,
            scope,
            "unsupported_type",
            f"materializer_type must be one of {sorted(MATERIALIZER_TYPES)}",
        )
    if role not in DATASET_ROLES:
        return AdapterProblem(
            adapter_id,
            scope,
            "invalid_role",
            f"role must be one of {sorted(DATASET_ROLES)}",
        )
    if not isinstance(provenance, str) or not provenance.strip():
        return AdapterProblem(adapter_id, scope, "invalid_provenance", "provenance is required")
    return AdapterDeclaration(
        adapter_id=adapter_id,
        materializer_type=materializer_type,
        role=role,
        source=source,
        provenance=provenance.strip(),
        scope=scope,
    )


def _parse_scope(
    entries: list[Any], scope: AdapterScope
) -> tuple[dict[str, AdapterDeclaration | AdapterProblem], list[AdapterProblem]]:
    keyed: dict[str, AdapterDeclaration | AdapterProblem] = {}
    unkeyed: list[AdapterProblem] = []
    for raw in entries:
        parsed = _parse_entry(raw, scope)
        if isinstance(parsed, AdapterProblem) and parsed.adapter_id is None:
            unkeyed.append(parsed)
            continue
        adapter_id = parsed.adapter_id
        if adapter_id in keyed:
            keyed[adapter_id] = AdapterProblem(
                adapter_id,
                scope,
                "duplicate",
                f"duplicate adapter id {adapter_id!r} in {scope} settings",
            )
        else:
            keyed[adapter_id] = parsed
    return keyed, unkeyed


def resolve_registered_adapters(repo_root: Path) -> ResolvedAdapters:
    project_raw, project_load_problem = _load_registered(repo_root / "settings.json", "project")
    operator_raw, operator_load_problem = _load_registered(
        repo_root / "settings.local.json", "operator"
    )
    project, unkeyed_project = _parse_scope(project_raw, "project")
    operator, unkeyed_operator = _parse_scope(operator_raw, "operator")
    merged = {**project, **operator}
    ready = {
        adapter_id: item
        for adapter_id, item in merged.items()
        if isinstance(item, AdapterDeclaration)
    }
    unavailable = {
        adapter_id: item
        for adapter_id, item in merged.items()
        if isinstance(item, AdapterProblem)
    }
    load_problems = tuple(
        problem
        for problem in (project_load_problem, operator_load_problem)
        if problem is not None
    )
    return ResolvedAdapters(
        ready=ready,
        unavailable=unavailable,
        unkeyed_problems=tuple(unkeyed_project + unkeyed_operator) + load_problems,
    )


def _fingerprint_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def fingerprint_path(path: Path) -> tuple[str, int, int]:
    from research_harness.evaluation_vault import reject_private_evaluation_input

    try:
        reject_private_evaluation_input(path)
    except ValueError as exc:
        raise AdapterError(str(exc)) from exc
    path = path.expanduser()
    if path.is_symlink():
        raise AdapterError(f"source is a symlink: {path}")
    path = path.resolve()
    if not path.exists():
        raise AdapterError(f"source does not exist or is a symlink: {path}")
    if path.is_file():
        digest, size = _fingerprint_file(path)
        return digest, size, 1
    if not path.is_dir():
        raise AdapterError(f"source is not a regular file or directory: {path}")
    tree = hashlib.sha256()
    total_size = 0
    count = 0
    for entry in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        if entry.is_symlink():
            raise AdapterError(f"directory source contains a symlink: {entry}")
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise AdapterError(f"directory source contains a special file: {entry}")
        file_digest, size = _fingerprint_file(entry)
        relative = entry.relative_to(path).as_posix()
        tree.update(f"{relative}\0{size}\0{file_digest}\n".encode("utf-8"))
        total_size += size
        count += 1
    return tree.hexdigest(), total_size, count


def _snapshot(declaration: AdapterDeclaration) -> AdapterSnapshot:
    digest, size_bytes, entry_count = fingerprint_path(Path(declaration.source))
    identity = {
        "adapter_id": declaration.adapter_id,
        "materializer_type": declaration.materializer_type,
        "role": declaration.role,
        "provenance": declaration.provenance,
        "content_sha256": digest,
    }
    snapshot_id = "as_" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AdapterSnapshot(
        snapshot_id=snapshot_id,
        adapter_id=declaration.adapter_id,
        materializer_type=declaration.materializer_type,
        role=declaration.role,
        source=declaration.source,
        provenance=declaration.provenance,
        source_scope=declaration.scope,
        content_sha256=digest,
        size_bytes=size_bytes,
        entry_count=entry_count,
    )


def probe_registered_adapters(repo_root: Path) -> dict[str, Any]:
    resolved = resolve_registered_adapters(repo_root)
    snapshots: list[AdapterSnapshot] = []
    problems = list(resolved.unavailable.values()) + list(resolved.unkeyed_problems)
    for adapter_id in sorted(resolved.ready):
        declaration = resolved.ready[adapter_id]
        try:
            snapshots.append(_snapshot(declaration))
        except (AdapterError, OSError) as exc:
            problems.append(
                AdapterProblem(adapter_id, declaration.scope, "unreadable_source", str(exc))
            )
    document = {
        "schema_version": 1,
        "snapshots": [asdict(snapshot) for snapshot in snapshots],
        "problems": [asdict(problem) for problem in problems],
    }
    from research_harness.schemas.validator import validate_named_schema
    validate_named_schema("adapter_snapshots", document)
    return document


def ensure_thread_adapter_snapshots(repo_root: Path, thread_dir: Path) -> dict[str, Any]:
    path = thread_dir / "production" / "adapter_snapshots.json"
    if path.exists():
        return load_thread_adapter_snapshots(thread_dir)
    document = probe_registered_adapters(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return document


def load_thread_adapter_snapshots(thread_dir: Path) -> dict[str, Any]:
    path = thread_dir / "production" / "adapter_snapshots.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"cannot read thread adapter snapshots: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise AdapterError("thread adapter snapshots have an unsupported schema")
    if not isinstance(document.get("snapshots"), list) or not isinstance(
        document.get("problems"), list
    ):
        raise AdapterError("thread adapter snapshots are malformed")
    from research_harness.schemas.validator import SchemaValidationError, validate_named_schema
    try:
        validate_named_schema("adapter_snapshots", document)
    except SchemaValidationError as exc:
        raise AdapterError(f"thread adapter snapshots are malformed: {exc}") from exc
    return document


def select_snapshot(
    document: dict[str, Any], requested_adapter_id: str | None
) -> dict[str, Any] | None:
    by_id = {item["adapter_id"]: item for item in document["snapshots"]}
    if requested_adapter_id:
        try:
            return by_id[requested_adapter_id]
        except KeyError as exc:
            raise AdapterError(
                f"adapter {requested_adapter_id!r} is not ready; ready adapters: {sorted(by_id)}"
            ) from exc
    if len(by_id) == 1:
        return next(iter(by_id.values()))
    if not by_id:
        return None
    raise AdapterError(
        "multiple adapters are ready; select one explicitly: " + ", ".join(sorted(by_id))
    )


def require_thread_snapshot(
    thread_dir: Path, *, adapter_id: str, snapshot_id: str
) -> dict[str, Any]:
    document = load_thread_adapter_snapshots(thread_dir)
    for snapshot in document["snapshots"]:
        if snapshot.get("adapter_id") != adapter_id:
            continue
        if snapshot.get("snapshot_id") != snapshot_id:
            raise AdapterError(
                f"adapter {adapter_id!r} does not match persisted snapshot {snapshot_id!r}"
            )
        return snapshot
    raise AdapterError(f"adapter {adapter_id!r} is absent from the thread snapshot")


def adapter_status_rows(repo_root: Path) -> list[dict[str, Any]]:
    resolved = resolve_registered_adapters(repo_root)
    document = probe_registered_adapters(repo_root)
    snapshots = {snapshot["adapter_id"]: snapshot for snapshot in document["snapshots"]}
    problems = {
        problem["adapter_id"]: problem
        for problem in document["problems"]
        if problem.get("adapter_id")
    }
    rows: list[dict[str, Any]] = []
    for adapter_id, declaration in resolved.ready.items():
        snapshot = snapshots.get(adapter_id)
        if snapshot is not None:
            rows.append({
                **snapshot,
                "id": adapter_id,
                "status": "ready",
                "error": None,
                "_scope": snapshot["source_scope"],
            })
            continue
        problem = problems[adapter_id]
        rows.append({
            "id": adapter_id,
            "adapter_id": adapter_id,
            "materializer_type": declaration.materializer_type,
            "role": declaration.role,
            "source": declaration.source,
            "provenance": declaration.provenance,
            "status": "unavailable",
            "error": problem["message"],
            "_scope": declaration.scope,
        })
    rows.extend({
        "id": adapter_id,
        "adapter_id": adapter_id,
        "status": "unavailable",
        "error": problem.message,
        "_scope": problem.scope,
    } for adapter_id, problem in resolved.unavailable.items())
    rows.extend({
        "id": "(invalid entry)",
        "adapter_id": None,
        "status": "unavailable",
        "error": problem.message,
        "_scope": problem.scope,
    } for problem in resolved.unkeyed_problems)
    return sorted(rows, key=lambda row: str(row.get("adapter_id") or ""))


__all__ = [
    "AdapterError",
    "DATASET_ROLES",
    "MATERIALIZER_TYPES",
    "adapter_status_rows",
    "ensure_thread_adapter_snapshots",
    "fingerprint_path",
    "load_thread_adapter_snapshots",
    "probe_registered_adapters",
    "require_thread_snapshot",
    "resolve_registered_adapters",
    "select_snapshot",
]
