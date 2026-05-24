"""HuggingFace Hub / datasets materializer.

Handles both ``benchmark`` and ``raw_data`` and ``model_weights`` specs
when ``spec.source`` starts with ``huggingface://`` or ``hf://``. The
fetch reuses HuggingFace's own cache so multi-GB datasets are not
re-downloaded across runs or projects.

The huggingface_hub / datasets dependencies are optional; if either is
missing the materializer reports a clean failure rather than crashing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research_harness.datasets.protocol import MaterializeResult


class HuggingFaceMaterializer:
    handled_type = "huggingface"
    fetcher_type = "huggingface"

    def __init__(self, *, dry_run_only: bool = False) -> None:
        self.dry_run_only = dry_run_only

    def try_materialize(
        self,
        spec: dict[str, Any],
        cache_root: Path,
    ) -> MaterializeResult:
        source = str(spec.get("source") or "")
        repo_id = _strip_scheme(source)
        if not repo_id:
            return MaterializeResult(
                status="failed",
                fetcher_type=self.fetcher_type,
                error="HuggingFace spec missing source with hf://<repo_id>",
            )
        revision = spec.get("revision") or None
        spec_type = str(spec.get("type") or "")

        if self.dry_run_only:
            return MaterializeResult(
                status="ok",
                materialized_path=Path(f"<dry_run_only:hf:{repo_id}>"),
                size_bytes=None,
                fetcher_type=self.fetcher_type,
                detail={"repo_id": repo_id, "revision": revision, "dry_run": True},
            )

        if spec_type in {"benchmark", "raw_data"}:
            return self._materialize_dataset(repo_id, revision, spec)
        if spec_type == "model_weights":
            return self._materialize_model(repo_id, revision, spec)
        return MaterializeResult(
            status="unsupported_type",
            fetcher_type=self.fetcher_type,
            error=f"HuggingFace materializer does not handle type {spec_type!r}",
        )

    def _materialize_dataset(
        self,
        repo_id: str,
        revision: str | None,
        spec: dict[str, Any],
    ) -> MaterializeResult:
        try:
            from datasets import load_dataset  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on env
            return MaterializeResult(
                status="failed",
                fetcher_type=self.fetcher_type,
                error=f"datasets library not installed: {exc}",
            )
        split = spec.get("split") or None
        try:
            ds = load_dataset(repo_id, split=split, revision=revision)
        except Exception as exc:
            return MaterializeResult(
                status="failed",
                fetcher_type=self.fetcher_type,
                error=f"load_dataset failed: {exc}",
            )
        size = None
        try:
            size = int(getattr(ds, "dataset_size", None) or 0) or None
        except Exception:
            size = None
        path = None
        try:
            cache_files = getattr(ds, "cache_files", None) or []
            if cache_files:
                path = Path(cache_files[0]["filename"]).parent
        except Exception:
            path = None
        return MaterializeResult(
            status="ok",
            materialized_path=path,
            size_bytes=size,
            fetcher_type=self.fetcher_type,
            detail={"repo_id": repo_id, "revision": revision, "split": split},
        )

    def _materialize_model(
        self,
        repo_id: str,
        revision: str | None,
        spec: dict[str, Any],
    ) -> MaterializeResult:
        try:
            from huggingface_hub import snapshot_download  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on env
            return MaterializeResult(
                status="failed",
                fetcher_type=self.fetcher_type,
                error=f"huggingface_hub not installed: {exc}",
            )
        try:
            local_dir = snapshot_download(repo_id=repo_id, revision=revision)
        except Exception as exc:
            msg = str(exc)
            status = "needs_credential" if "401" in msg or "gated" in msg.lower() else "failed"
            return MaterializeResult(
                status=status,
                fetcher_type=self.fetcher_type,
                error=msg,
            )
        path = Path(local_dir)
        size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        return MaterializeResult(
            status="ok",
            materialized_path=path,
            size_bytes=size,
            fetcher_type=self.fetcher_type,
            detail={"repo_id": repo_id, "revision": revision},
        )


def _strip_scheme(source: str) -> str:
    for prefix in ("huggingface://", "hf://"):
        if source.startswith(prefix):
            return source[len(prefix):]
    return ""
