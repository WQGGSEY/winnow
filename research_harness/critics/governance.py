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


def select_critics(repo_root: Path, node: dict[str, Any]) -> dict[str, Any]:
    """Select critics through deterministic folder routing.

    The orchestrator submits node metadata. It does not choose individual
    critics. The routes are derived from filesystem structure plus node fields.
    """

    _validate_routing_surface(node)
    critics_root = repo_root / "critics"
    routes = [
        ("always", critics_root / "always"),
        ("by_node_type", critics_root / "by_node_type" / node["type"]),
        ("by_domain", critics_root / "by_domain" / node["domain"]),
        ("by_stage", critics_root / "by_stage" / node["stage"]),
    ]

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
                    "path": str(path.relative_to(repo_root)),
                    "route": route_name,
                }
            )
        routing_trace.append(
            {
                "route": route_name,
                "path": str(route_path.relative_to(repo_root)),
                "applied_count": len(files),
            }
        )

    if not any(critic["route"] == "always" for critic in applied):
        raise CriticGovernanceError("at least one critics/always profile is required")

    return {
        "applied_critics": applied,
        "skipped_critics_with_reason": [],
        "routing_trace": routing_trace,
    }
