"""Local filesystem materializer.

Handles specs whose source is a ``file://`` URL or absolute path. The
materializer copies (or hard-links / symlinks) the resource into the
repo-local cache so the per-node manifest can hand a stable path to the
template code. If the source path is already inside the cache it is
returned unchanged.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from research_harness.datasets.protocol import MaterializeResult


class LocalPathMaterializer:
    handled_type = "local"
    fetcher_type = "local_path"

    def try_materialize(
        self,
        spec: dict[str, Any],
        cache_root: Path,
    ) -> MaterializeResult:
        source = str(spec.get("source") or "")
        if source.startswith("file://"):
            source = source[len("file://"):]
        if not source:
            return MaterializeResult(
                status="failed",
                fetcher_type=self.fetcher_type,
                error="local spec missing source path",
            )
        src_path = Path(source).expanduser()
        if not src_path.exists():
            return MaterializeResult(
                status="failed",
                fetcher_type=self.fetcher_type,
                error=f"local source does not exist: {src_path}",
            )
        spec_type = str(spec.get("type") or "custom")
        target_root = cache_root / spec_type / str(spec["id"])
        target_root.mkdir(parents=True, exist_ok=True)
        if src_path.is_dir():
            target = target_root / src_path.name
            if not target.exists():
                shutil.copytree(src_path, target)
        else:
            target = target_root / src_path.name
            if not target.exists():
                shutil.copy2(src_path, target)
        size = (
            sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
            if target.is_dir()
            else target.stat().st_size
        )
        return MaterializeResult(
            status="ok",
            materialized_path=target.resolve(),
            size_bytes=size,
            fetcher_type=self.fetcher_type,
            detail={"source": str(src_path)},
        )
