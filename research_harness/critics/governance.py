from __future__ import annotations

from pathlib import Path
from typing import Any

from research_harness.config import ConfigError, load_critic_profile


class CriticGovernanceError(ValueError):
    """Raised when critic routing can be influenced or becomes invalid."""


FORBIDDEN_NODE_ROUTING_KEYS = {
    "critics",
    "critic_ids",
    "skip_critics",
    "applied_critics",
    "critic_overrides",
}


def _critic_id(path: Path) -> str:
    return str(load_critic_profile(path)["critic_profile_id"])


def _collect_md(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(path.glob("*.md"))


def _validate_routing_surface(node: dict[str, Any]) -> None:
    forbidden = sorted(FORBIDDEN_NODE_ROUTING_KEYS & set(node.keys()))
    if forbidden:
        raise CriticGovernanceError(
            "node metadata cannot request critic routing changes: "
            + ", ".join(forbidden)
        )


def _extra_persona_dirs(settings: dict[str, Any] | None) -> list[Path]:
    if not settings:
        return []
    raw = settings.get("critics", {}).get("extra_persona_dirs") or []
    paths: list[Path] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            continue
        paths.append(Path(entry).expanduser())
    return paths


def select_critics(
    repo_root: Path,
    node: dict[str, Any],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select critics through deterministic folder routing.

    The orchestrator submits node metadata. It does not choose individual
    critics. The routes are derived from filesystem structure plus node
    fields. User-configured extra persona directories (settings.critics.
    extra_persona_dirs) are also scanned so operators can drop in new
    practitioner-voice critics without touching the harness.
    """

    _validate_routing_surface(node)
    critics_root = repo_root / "critics"
    routes: list[tuple[str, Path]] = [
        ("always", critics_root / "always"),
        ("by_node_type", critics_root / "by_node_type" / node["type"]),
        ("by_domain", critics_root / "by_domain" / node["domain"]),
        ("by_stage", critics_root / "by_stage" / node["stage"]),
    ]
    # Append user-supplied dirs as a separate route so they show up in the
    # routing_trace. We do NOT auto-route by stage/node-type within an extra
    # dir; the operator is expected to place files under
    # <extra_dir>/<route_name>/<value>/ or directly under <extra_dir>/always/
    # mirroring the canonical layout.
    for extra in _extra_persona_dirs(settings):
        if not extra.is_dir():
            continue
        for sub_route_name in ("always", "by_node_type", "by_stage", "by_domain"):
            sub = extra / sub_route_name
            if sub_route_name == "always":
                if sub.is_dir():
                    routes.append((f"extra:{extra.name}:always", sub))
            else:
                key = (
                    node["type"] if sub_route_name == "by_node_type"
                    else node["stage"] if sub_route_name == "by_stage"
                    else node["domain"]
                )
                target = sub / key
                if target.is_dir():
                    routes.append((f"extra:{extra.name}:{sub_route_name}", target))

    applied: list[dict[str, str]] = []
    routing_trace: list[dict[str, Any]] = []
    seen: set[Path] = set()
    critic_ids: set[str] = set()
    for route_name, route_path in routes:
        files = [path for path in _collect_md(route_path) if path not in seen]
        for path in files:
            try:
                critic_id = _critic_id(path)
            except ConfigError as exc:
                raise CriticGovernanceError(str(exc)) from exc
            if critic_id in critic_ids:
                raise CriticGovernanceError(f"duplicate critic_profile_id: {critic_id}")
            critic_ids.add(critic_id)
            seen.add(path)
            applied.append(
                {
                    "critic_id": critic_id,
                    "path": _safe_relpath(path, repo_root),
                    "route": route_name,
                }
            )
        routing_trace.append(
            {
                "route": route_name,
                "path": _safe_relpath(route_path, repo_root),
                "applied_count": len(files),
            }
        )

    if not any(critic["route"] == "always" or critic["route"].endswith(":always") for critic in applied):
        raise CriticGovernanceError("at least one critics/always profile is required")

    return {
        "applied_critics": applied,
        "skipped_critics_with_reason": [],
        "routing_trace": routing_trace,
    }


def _safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        # Extra-dir paths live outside the repo root.
        return str(path)
