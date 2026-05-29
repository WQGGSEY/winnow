"""Three-scope settings (ADR 0005) — Phase 1 infrastructure.

This module introduces the registry-driven, scope-aware settings system
described in ``docs/adr/0005-three-scope-settings-with-per-phase-snapshot.md``
and the matching CONTEXT.md entries (``setting_scope``, ``field_registry``,
``resolved_settings``, ``resolved_settings_snapshot``, ``thread_settings_file``,
``settings_validation``).

Phase 1 scope:
  - ``FieldSpec`` + ``FIELD_REGISTRY`` (representative starter set)
  - ``ResolvedSettings``: read-only Mapping view over project + operator +
    thread scopes, compatible with the existing ``settings: dict[str, Any]``
    interface so unmigrated call sites keep working.
  - ``resolve_for_thread`` and ``write_snapshot``: build a snapshot and
    persist it under the thread's per-phase directory.
  - Write-time validation (type / range / enum / regex / writable_at).

Phase 1 explicitly does NOT:
  - Replace ``load_settings()`` or the ad-hoc resolvers in ``config.py``.
  - Migrate the ~22 call sites that read settings directly.
  - Implement the cross-field resolution-time validator (stub returns []).
  - Render the settings UI.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

Scope = Literal["project", "operator", "thread"]
SnapshotSource = Literal["project", "operator", "thread", "schema_default"]

# Sentinel for "field has no schema default at all"; distinct from None,
# which is itself a valid default for nullable fields like
# ``frontend.subscription_ack_at``.
_UNSET: Any = object()


# ---------------------------------------------------------------------------
# FieldSpec + FIELD_REGISTRY
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """One row of the FIELD_REGISTRY.

    ``path`` is a dotted JSON path. A path ending in ``*`` (e.g.
    ``runtime.agent_max_rounds.*``) covers a family of dynamically-keyed
    leaves; the glob matches one path segment, never deeper.

    ``writable_at`` restricts which scopes are allowed to set the field at
    write time. ``ui_editable_in`` is a subset of ``writable_at`` controlling
    which scopes the frontend exposes for editing; structural fields that
    *technically* live at project scope but should not be flipped from the
    UI (e.g. auth policy) set ``ui_editable_in=()``.
    """

    path: str
    writable_at: tuple[Scope, ...]
    type: str
    default: Any = _UNSET
    description: str = ""

    enum: tuple[Any, ...] | None = None
    enum_source: str | None = None
    min: int | float | None = None
    max: int | float | None = None
    regex: str | None = None

    ui_editable_in: tuple[Scope, ...] | None = None
    custom_widget: str | None = None

    @property
    def effective_ui_editable_in(self) -> tuple[Scope, ...]:
        return self.writable_at if self.ui_editable_in is None else self.ui_editable_in

    @property
    def is_glob(self) -> bool:
        return "*" in self.path

    def matches(self, path: str) -> bool:
        if not self.is_glob:
            return self.path == path
        pat = "^" + re.escape(self.path).replace(r"\*", r"[^.]+") + "$"
        return re.match(pat, path) is not None


# Representative starter set. Populated incrementally per ADR 0005.
# Adding a new field is a single registry entry; no other code change
# is required for the field to participate in resolution, snapshotting,
# or (later) the settings UI.
FIELD_REGISTRY: tuple[FieldSpec, ...] = (
    # ---- Project-only structural (read-only in UI) ----
    FieldSpec(
        path="runtime.auth_policy.provider",
        writable_at=("project",),
        type="enum",
        enum=("claude_code",),
        default="claude_code",
        ui_editable_in=(),
        description="Auth provider. Structural; edit settings.json directly.",
    ),
    FieldSpec(
        path="runtime.auth_policy.require_no_anthropic_api_key",
        writable_at=("project",),
        type="boolean",
        default=True,
        ui_editable_in=(),
        description="Disallow ANTHROPIC_API_KEY at boot. Security policy.",
    ),
    FieldSpec(
        path="runtime.llm_orchestrator.mcp.allowed_models",
        writable_at=("project",),
        type="list",
        default=[],
        ui_editable_in=("project",),
        description="Model IDs the harness recognises. Edit list to add a new model.",
    ),
    # ---- Operator-only ----
    FieldSpec(
        path="frontend.subscription_ack_at",
        writable_at=("operator",),
        type="timestamp",
        default=None,
        description="Operator's one-time consent timestamp for live Claude calls.",
    ),
    FieldSpec(
        path="frontend.full_auto_mode",
        writable_at=("operator", "thread"),
        type="boolean",
        default=False,
        description=(
            "Suppress per-phase execute_ack modal. Thread scope may tighten "
            "(disable) but cannot enable when operator left it off."
        ),
    ),
    FieldSpec(
        path="data_adapters.registered",
        writable_at=("operator",),
        type="list",
        default=[],
        custom_widget="data_adapters_editor",
        description="Real-data adapters installed on this machine.",
    ),
    # ---- Project default + operator override ----
    FieldSpec(
        path="runtime.default_backend",
        writable_at=("project", "operator"),
        type="enum",
        enum=("mock", "claude_code_dry_run", "claude_code_live"),
        default="mock",
        description="Worker backend the harness uses by default.",
    ),
    FieldSpec(
        path="runtime.llm_orchestrator.backend",
        writable_at=("project", "operator"),
        type="enum",
        enum=("mcp", "mock", "claude_cli", "anthropic"),
        default="mcp",
        description="LLM orchestrator backend.",
    ),
    # ---- Project default + thread override ----
    FieldSpec(
        path="runtime.llm_orchestrator.mcp.default_model",
        writable_at=("project", "thread"),
        type="enum",
        enum_source="runtime.llm_orchestrator.mcp.allowed_models",
        default="claude-opus-4-7",
        description="MCP default model. Per-thread override in frontend.",
    ),
    FieldSpec(
        path="runtime.agent_max_rounds.*",
        writable_at=("project", "thread"),
        type="int_or_unlimited",
        min=1,
        max=10000,
        description="Per-agent round cap. Positive integer or 'unlimited'.",
    ),
    FieldSpec(
        path="runtime.agent_models.*",
        writable_at=("project", "thread"),
        type="string",
        description="Per-agent model name (used by claude_cli/anthropic backends).",
    ),
    FieldSpec(
        path="runtime.agent_budgets.*",
        writable_at=("project", "thread"),
        type="string",
        regex=r"^\d+(\.\d+)?$",
        description="Per-agent USD budget cap (e.g. '0.50').",
    ),
    FieldSpec(
        path="runtime.runner_timeouts.*",
        writable_at=("project", "thread"),
        type="integer",
        min=1,
        max=86400,
        description="Per-runner timeout seconds.",
    ),
    FieldSpec(
        path="publication_gate.enabled",
        writable_at=("project", "thread"),
        type="boolean",
        default=True,
        description="Master switch for the publication gate.",
    ),
    FieldSpec(
        path="publication_gate.ac_agent.accept_thresholds.validity",
        writable_at=("project", "thread"),
        type="integer",
        default=7,
        min=0,
        max=10,
        description="AC accept threshold for validity (0-10).",
    ),
    FieldSpec(
        path="publication_gate.ac_agent.accept_thresholds.reproducibility",
        writable_at=("project", "thread"),
        type="integer",
        default=6,
        min=0,
        max=10,
        description="AC accept threshold for reproducibility (0-10).",
    ),
    FieldSpec(
        path="publication_gate.ac_agent.accept_thresholds.necessity",
        writable_at=("project", "thread"),
        type="integer",
        default=6,
        min=0,
        max=10,
        description="AC accept threshold for necessity (0-10).",
    ),
    FieldSpec(
        path="publication_gate.ac_agent.accept_thresholds.taste_alignment",
        writable_at=("project", "thread"),
        type="integer",
        default=7,
        min=0,
        max=10,
        description="AC accept threshold for taste_alignment (0-10).",
    ),
    FieldSpec(
        path="publication_gate.rebuttal.max_depth",
        writable_at=("project", "thread"),
        type="integer",
        default=2,
        min=0,
        max=10,
        description="Rebuttal recursion depth cap.",
    ),
    FieldSpec(
        path="production_termination.soft_milestone_cycle_count",
        writable_at=("project", "thread"),
        type="integer",
        default=10,
        min=1,
        max=1000,
        description="Soft milestone cycle count.",
    ),
    FieldSpec(
        path="memory.failure_retrieval_top_k",
        writable_at=("project", "thread"),
        type="integer",
        default=5,
        min=0,
        max=100,
        description="How many past failures to surface to agents.",
    ),
    # ---- Three-way ----
    FieldSpec(
        path="runtime.llm_orchestrator.enabled",
        writable_at=("project", "operator", "thread"),
        type="boolean",
        default=True,
        description="Master switch for the LLM orchestrator.",
    ),
)


def find_spec(path: str) -> FieldSpec | None:
    """Return the FieldSpec governing ``path`` (exact or glob match), or None."""
    for spec in FIELD_REGISTRY:
        if spec.matches(path):
            return spec
    return None


# ---------------------------------------------------------------------------
# Dotted-path utilities
# ---------------------------------------------------------------------------


def _get_dotted(d: Mapping[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_dotted(d: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _enumerate_glob_leaves(merged: Mapping[str, Any], spec: FieldSpec) -> list[str]:
    """For a glob FieldSpec, return every leaf path currently present in merged.

    ``*`` matches one path segment only; underscore-prefixed keys (``_comment``,
    ``_example``) are skipped because they are metadata, not real fields.
    """
    parts = spec.path.split(".")
    star_idx = parts.index("*")
    prefix_parts = parts[:star_idx]
    suffix_parts = parts[star_idx + 1 :]

    cur: Any = merged
    for p in prefix_parts:
        if not isinstance(cur, Mapping) or p not in cur:
            return []
        cur = cur[p]
    if not isinstance(cur, Mapping):
        return []

    results: list[str] = []
    for key in cur.keys():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        full_path = ".".join(prefix_parts + [key] + suffix_parts)
        if suffix_parts:
            if _get_dotted(merged, full_path, _UNSET) is _UNSET:
                continue
        results.append(full_path)
    return results


# ---------------------------------------------------------------------------
# Merge: project -> operator -> thread
# ---------------------------------------------------------------------------


def _deep_overlay(
    target: dict[str, Any],
    source: Mapping[str, Any],
    scope: Scope,
    sources: dict[str, SnapshotSource],
    prefix: str = "",
) -> None:
    """Recursively overlay ``source`` onto ``target``.

    Dicts recurse. Scalars and lists whole-replace. Each leaf write records
    its originating scope in ``sources``.
    """
    for k, v in source.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, Mapping):
            existing = target.get(k)
            if not isinstance(existing, dict):
                existing = {}
                target[k] = existing
            _deep_overlay(existing, v, scope, sources, path)
        else:
            target[k] = copy.deepcopy(v)
            sources[path] = scope


def _apply_thread_flat(
    target: dict[str, Any],
    flat: Mapping[str, Any],
    sources: dict[str, SnapshotSource],
) -> None:
    """Apply a flat dotted-path map (thread_settings.json shape) onto ``target``."""
    for path, value in flat.items():
        _set_dotted(target, path, copy.deepcopy(value))
        sources[path] = "thread"


def _build_merged(
    project: Mapping[str, Any],
    operator: Mapping[str, Any],
    thread_flat: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, SnapshotSource]]:
    merged: dict[str, Any] = {}
    sources: dict[str, SnapshotSource] = {}
    _deep_overlay(merged, project, "project", sources)
    _deep_overlay(merged, operator, "operator", sources)
    _apply_thread_flat(merged, thread_flat, sources)
    return merged, sources


# ---------------------------------------------------------------------------
# ResolvedSettings
# ---------------------------------------------------------------------------


class ResolvedSettings(Mapping[str, Any]):
    """Dict-like view over merged project + operator + thread settings.

    Compatible with the existing ``settings: dict[str, Any]`` signature: code
    that does ``settings["runtime"]["agent_max_rounds"]["grilling_agent"]``
    keeps working. New code prefers the dotted accessors:

      - ``resolved.get_dotted("runtime.agent_max_rounds.grilling_agent")``
      - ``resolved.source_of(path)`` -> project | operator | thread | None

    ``to_dict()`` deep-copies the merged tree for safe subprocess hand-off
    (LocalRunner, experiment template entrypoints).
    """

    def __init__(
        self,
        *,
        project: Mapping[str, Any],
        operator: Mapping[str, Any],
        thread: Mapping[str, Any],
        thread_id: str | None = None,
        phase: str | None = None,
    ) -> None:
        merged, sources = _build_merged(project, operator, thread)
        self._merged = merged
        self._sources = sources
        self.thread_id = thread_id
        self.phase = phase

    # Mapping interface
    def __getitem__(self, key: str) -> Any:
        return self._merged[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._merged)

    def __len__(self) -> int:
        return len(self._merged)

    # Dotted-path accessors
    def get_dotted(self, path: str, default: Any = None) -> Any:
        return _get_dotted(self._merged, path, default)

    def source_of(self, path: str) -> SnapshotSource | None:
        return self._sources.get(path)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._merged)


# ---------------------------------------------------------------------------
# Write-time validation
# ---------------------------------------------------------------------------


def validate_write(
    path: str,
    value: Any,
    scope: Scope,
    *,
    enum_resolver: Callable[[str], list[Any] | None] | None = None,
) -> None:
    """Raise ``ValueError`` if writing ``value`` to ``path`` at ``scope`` violates the registry.

    Unregistered paths are permitted silently in Phase 1 (registry is being
    populated incrementally). They will become strict in Phase 4 once
    whole-surface coverage is reached.

    ``enum_resolver`` is consulted when a FieldSpec uses ``enum_source`` to
    look up its allowed values at another path (e.g. ``mcp.default_model``
    pulling its options from ``mcp.allowed_models``). Pass a lambda that
    reads the current resolved value at that path.
    """
    spec = find_spec(path)
    if spec is None:
        return
    if scope not in spec.writable_at:
        raise ValueError(
            f"field {path!r} is not writable at scope {scope!r} "
            f"(allowed: {list(spec.writable_at)})"
        )
    _validate_value(spec, path, value, enum_resolver=enum_resolver)


def _validate_value(
    spec: FieldSpec,
    path: str,
    value: Any,
    *,
    enum_resolver: Callable[[str], list[Any] | None] | None,
) -> None:
    if value is None:
        # Nullable when the schema default itself is None (e.g. timestamps
        # before they have been set).
        if spec.default is None:
            return
        raise ValueError(f"{path!r} does not accept null")

    t = spec.type
    if t == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path!r} expects string, got {type(value).__name__}")
        if spec.regex and not re.match(spec.regex, value):
            raise ValueError(f"{path!r}={value!r} does not match regex {spec.regex!r}")
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path!r} expects integer, got {type(value).__name__}")
        _check_range(spec, path, value)
    elif t == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path!r} expects number, got {type(value).__name__}")
        _check_range(spec, path, value)
    elif t == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path!r} expects boolean, got {type(value).__name__}")
    elif t == "enum":
        allowed: list[Any] | None
        if spec.enum is not None:
            allowed = list(spec.enum)
        elif spec.enum_source and enum_resolver is not None:
            allowed = enum_resolver(spec.enum_source)
        else:
            allowed = None
        if allowed is not None and value not in allowed:
            raise ValueError(f"{path!r}={value!r} not in allowed {allowed}")
    elif t == "list":
        if not isinstance(value, list):
            raise ValueError(f"{path!r} expects list, got {type(value).__name__}")
    elif t == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path!r} expects object, got {type(value).__name__}")
    elif t == "int_or_unlimited":
        if isinstance(value, str):
            if value != "unlimited":
                raise ValueError(f"{path!r}={value!r}: string must be 'unlimited'")
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path!r} expects integer or 'unlimited'")
        else:
            _check_range(spec, path, value)
    elif t == "timestamp":
        if not isinstance(value, str):
            raise ValueError(f"{path!r} expects ISO timestamp string")


def _check_range(spec: FieldSpec, path: str, value: int | float) -> None:
    if spec.min is not None and value < spec.min:
        raise ValueError(f"{path!r}={value} < min {spec.min}")
    if spec.max is not None and value > spec.max:
        raise ValueError(f"{path!r}={value} > max {spec.max}")


# Cross-field, resolution-time validation. Phase 2 invariants below.
# Phase-launch-specific checks (e.g. deployment-scope claim requires a
# registered data adapter) live next to where the claim is built — they
# need claim context the resolver does not have. Things checked here are
# invariants that can be evaluated from the resolved settings alone.


def validate_resolved(resolved: ResolvedSettings) -> list[str]:
    """Return a list of human-readable violation messages, empty if all OK.

    Each message is sufficient to identify both the offending field and
    the reason, so the frontend can surface it inline with a link to the
    field per [[settings_ui]].
    """
    violations: list[str] = []

    # 1. mcp.default_model must lie inside mcp.allowed_models — but only
    #    when the active orchestrator backend is "mcp". Other backends
    #    pick their model through ``runtime.agent_models``, not the MCP
    #    dropdown, so the constraint does not apply.
    backend = resolved.get_dotted("runtime.llm_orchestrator.backend")
    if backend == "mcp":
        default_model = resolved.get_dotted(
            "runtime.llm_orchestrator.mcp.default_model"
        )
        allowed = resolved.get_dotted(
            "runtime.llm_orchestrator.mcp.allowed_models", []
        )
        if default_model is not None and isinstance(allowed, list) and allowed:
            if default_model not in allowed:
                source = resolved.source_of(
                    "runtime.llm_orchestrator.mcp.default_model"
                )
                violations.append(
                    f"runtime.llm_orchestrator.mcp.default_model="
                    f"{default_model!r} (from {source or 'schema_default'}) "
                    f"is not in runtime.llm_orchestrator.mcp.allowed_models "
                    f"({allowed}). Either pick a listed model or add this one "
                    "to allowed_models at project scope."
                )

    return violations


# ---------------------------------------------------------------------------
# Disk I/O: resolve + snapshot
# ---------------------------------------------------------------------------


def _load_json_or_empty(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def thread_id_from_run_dir(run_dir: Path) -> str | None:
    """Return the thread_id implied by ``run_dir``, or None for non-thread layouts.

    Frontend-launched phases use the layout ``runs/threads/<thread_id>/<phase>/``.
    CLI users may pick arbitrary run_dirs (e.g. ``runs/grilling/<session_id>/``);
    those have no thread context and callers should fall through to project +
    operator scope only.
    """
    parts = run_dir.resolve().parts
    if "threads" in parts:
        idx = parts.index("threads")
        if idx + 1 < len(parts):
            tid = parts[idx + 1]
            if tid:
                return tid
    return None


def resolve_for_thread(
    repo_root: Path,
    thread_id: str | None = None,
    *,
    phase: str | None = None,
) -> ResolvedSettings:
    """Build a ResolvedSettings for ``thread_id`` (or operator-only if None)."""
    project = _load_json_or_empty(repo_root / "settings.json")
    operator = _load_json_or_empty(repo_root / "settings.local.json")
    thread_flat: dict[str, Any] = {}
    if thread_id is not None:
        thread_flat = _load_json_or_empty(
            repo_root / "runs" / "threads" / thread_id / "thread_settings.json"
        )
    return ResolvedSettings(
        project=project,
        operator=operator,
        thread=thread_flat,
        thread_id=thread_id,
        phase=phase,
    )


def write_snapshot(
    resolved: ResolvedSettings,
    thread_dir: Path,
    phase: str,
) -> Path:
    """Write ``<thread_dir>/<phase>/resolved_settings_snapshot.json``.

    Records ``{value, source}`` for every registered field. Glob entries are
    expanded against the merged tree's current leaves. Unregistered keys are
    excluded — the snapshot's contract is "the registry-governed view of the
    settings the harness will use", not a verbatim dump of the merged dict.
    """
    snapshot: dict[str, dict[str, Any]] = {}
    for spec in FIELD_REGISTRY:
        if spec.is_glob:
            for leaf in _enumerate_glob_leaves(resolved._merged, spec):
                snapshot[leaf] = {
                    "value": resolved.get_dotted(leaf),
                    "source": resolved.source_of(leaf) or "schema_default",
                }
            continue
        value = resolved.get_dotted(spec.path, _UNSET)
        if value is _UNSET:
            if spec.default is _UNSET:
                continue
            snapshot[spec.path] = {"value": spec.default, "source": "schema_default"}
        else:
            snapshot[spec.path] = {
                "value": value,
                "source": resolved.source_of(spec.path) or "schema_default",
            }

    phase_dir = thread_dir / phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    out = phase_dir / "resolved_settings_snapshot.json"
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(out)
    return out


# ---------------------------------------------------------------------------
# Form input coercion + per-scope writes (Phase 3)
# ---------------------------------------------------------------------------


def coerce_input(spec: FieldSpec, raw: str) -> Any:
    """Convert an HTML form string into the FieldSpec's native type.

    HTML forms hand the server everything as strings; the registry knows the
    intended type. This converts on the way in, before ``validate_write`` runs.
    The empty string is treated as "use schema default" — i.e. clear the
    override at the current scope rather than write an empty value.
    """
    if raw == "" and spec.type not in {"string", "list", "object"}:
        # Sentinel: caller should *remove* the field at this scope rather
        # than write an empty value.
        return _UNSET
    t = spec.type
    if t in ("string", "enum", "timestamp"):
        return raw
    if t == "boolean":
        return raw.lower() in {"true", "on", "1", "yes"}
    if t == "integer":
        return int(raw)
    if t == "float":
        return float(raw)
    if t == "int_or_unlimited":
        if raw.strip().lower() == "unlimited":
            return "unlimited"
        return int(raw)
    if t in ("list", "object"):
        return json.loads(raw) if raw else ([] if t == "list" else {})
    return raw


def _enum_resolver_for(repo_root: Path) -> Callable[[str], list[Any] | None]:
    """Build a callable that resolves enum_source paths against the current
    on-disk project+operator settings (no thread context needed for enum
    sourcing — allowed_models lives at project scope)."""
    cached: dict[str, list[Any] | None] = {}

    def _resolve(path: str) -> list[Any] | None:
        if path in cached:
            return cached[path]
        resolved = resolve_for_thread(repo_root, None)
        val = resolved.get_dotted(path)
        result = list(val) if isinstance(val, list) else None
        cached[path] = result
        return result

    return _resolve


def write_setting(
    repo_root: Path,
    scope: Scope,
    path: str,
    value: Any,
    *,
    thread_id: str | None = None,
) -> None:
    """Validate and persist a single setting at the requested scope.

    ``value`` may be the ``_UNSET`` sentinel, in which case the field is
    *removed* from the target file (i.e. the operator's override is cleared
    and resolution falls back to the next-broader scope).

    Project / operator scopes deep-merge into a nested ``settings.json`` /
    ``settings.local.json`` shape; thread scope flat-writes into
    ``runs/threads/<tid>/thread_settings.json`` keyed by the dotted path.
    """
    if scope == "thread" and not thread_id:
        raise ValueError("thread_id is required when writing at scope='thread'")

    if value is _UNSET:
        _remove_setting(repo_root, scope, path, thread_id=thread_id)
        return

    validate_write(
        path, value, scope, enum_resolver=_enum_resolver_for(repo_root)
    )

    if scope == "thread":
        assert thread_id is not None
        thread_file = (
            repo_root / "runs" / "threads" / thread_id / "thread_settings.json"
        )
        thread_file.parent.mkdir(parents=True, exist_ok=True)
        data = _load_json_or_empty(thread_file)
        data[path] = value
        _atomic_write_json(thread_file, data)
        return

    target = (
        repo_root / "settings.json" if scope == "project"
        else repo_root / "settings.local.json"
    )
    data = _load_json_or_empty(target)
    _set_dotted(data, path, value)
    _atomic_write_json(target, data)


def _remove_setting(
    repo_root: Path,
    scope: Scope,
    path: str,
    *,
    thread_id: str | None,
) -> None:
    if scope == "thread":
        if not thread_id:
            raise ValueError("thread_id is required when removing at scope='thread'")
        thread_file = (
            repo_root / "runs" / "threads" / thread_id / "thread_settings.json"
        )
        if not thread_file.exists():
            return
        data = _load_json_or_empty(thread_file)
        if path in data:
            del data[path]
            _atomic_write_json(thread_file, data)
        return

    target = (
        repo_root / "settings.json" if scope == "project"
        else repo_root / "settings.local.json"
    )
    if not target.exists():
        return
    data = _load_json_or_empty(target)
    if _pop_dotted(data, path):
        _atomic_write_json(target, data)


def _pop_dotted(d: dict[str, Any], path: str) -> bool:
    """Delete ``path`` from nested ``d``. Returns True iff something was removed."""
    parts = path.split(".")
    cur: Any = d
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        return False
    del cur[parts[-1]]
    return True


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def fields_for_scope(scope: Scope) -> list[FieldSpec]:
    """Every field that should *appear* on the settings UI tab for ``scope``.

    A field appears in a scope's tab when that scope can write the field
    (or could once another scope sets it up — for read-only display). The
    Thread tab uses a different selection rule (currently-overridden only)
    so this helper is not called for thread tab rendering.
    """
    return [spec for spec in FIELD_REGISTRY if scope in spec.writable_at]


def field_groups(specs: list[FieldSpec]) -> dict[str, list[FieldSpec]]:
    """Group field specs by their top-level path segment.

    ``runtime.foo.bar`` and ``runtime.foo.baz`` end up under ``runtime``;
    ``publication_gate.*`` under ``publication_gate``; etc. Order within a
    group follows registry order (which encodes intent).
    """
    out: dict[str, list[FieldSpec]] = {}
    for spec in specs:
        top = spec.path.split(".", 1)[0]
        out.setdefault(top, []).append(spec)
    return out


def list_thread_overrides(repo_root: Path, thread_id: str) -> dict[str, Any]:
    """Return the flat dotted-path -> value map currently set at thread scope."""
    return _load_json_or_empty(
        repo_root / "runs" / "threads" / thread_id / "thread_settings.json"
    )


__all__ = [
    "FIELD_REGISTRY",
    "FieldSpec",
    "ResolvedSettings",
    "Scope",
    "SnapshotSource",
    "coerce_input",
    "field_groups",
    "fields_for_scope",
    "find_spec",
    "list_thread_overrides",
    "resolve_for_thread",
    "thread_id_from_run_dir",
    "validate_resolved",
    "validate_write",
    "write_setting",
    "write_snapshot",
]
