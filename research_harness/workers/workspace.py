from __future__ import annotations

from pathlib import Path


class WorkspaceGuardError(ValueError):
    """Raised when a worker path would escape its allowed sandbox."""


def resolve_existing_or_parent(path: Path) -> Path:
    """Resolve a path even when the final file does not exist yet."""

    if path.exists():
        return path.resolve()
    parent = path.parent
    if not parent.exists():
        return parent.resolve() / path.name
    return parent.resolve() / path.name


def ensure_path_inside(path: Path, root: Path, label: str) -> Path:
    resolved_path = resolve_existing_or_parent(path)
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise WorkspaceGuardError(
            f"{label} must stay under {resolved_root}: {resolved_path}"
        ) from exc
    return resolved_path


def prepare_node_workspace(run_dir: Path, node_id: str) -> dict[str, Path]:
    workspace = run_dir / "nodes" / node_id / "workspace"
    artifacts = run_dir / "nodes" / node_id / "artifacts"
    workspace.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return {
        "workspace": workspace.resolve(),
        "artifacts": artifacts.resolve(),
        "node_root": (run_dir / "nodes" / node_id).resolve(),
    }

