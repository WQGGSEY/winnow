"""Dispatch table mapping ``dataset_spec.type`` to materializer instances.

The registry intentionally hardcodes the four built-in fetchers because
the project keeps standard-library-first dependency policy. Operators
that need a new fetcher (S3, Snowflake, internal data lake) add their
materializer class and register it here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research_harness.datasets.huggingface import HuggingFaceMaterializer
from research_harness.datasets.local_path import LocalPathMaterializer
from research_harness.datasets.protocol import (
    DatasetMaterializer,
    MaterializeResult,
)
from research_harness.datasets.synthetic import SyntheticMaterializer


def _build_default_registry() -> dict[str, DatasetMaterializer]:
    hf = HuggingFaceMaterializer()
    local = LocalPathMaterializer()
    synth = SyntheticMaterializer()
    return {
        "raw_data": hf,
        "benchmark": hf,
        "model_weights": hf,
        "factor_set": local,
        "synthetic": synth,
        "custom": local,
    }


REGISTRY: dict[str, DatasetMaterializer] = _build_default_registry()


def repo_cache_root(repo_root: Path) -> Path:
    """Where local / synthetic / s3 fetchers materialize into."""

    target = (repo_root / ".dataset_cache").resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def available_materializers() -> list[str]:
    return sorted(REGISTRY.keys())


def materialize(
    spec: dict[str, Any],
    *,
    repo_root: Path,
) -> MaterializeResult:
    """Dispatch one dataset_spec through its type-keyed materializer."""

    spec_type = str(spec.get("type") or "")
    fetcher = REGISTRY.get(spec_type)
    if fetcher is None:
        return MaterializeResult(
            status="unsupported_type",
            fetcher_type="unknown",
            error=f"no materializer registered for type {spec_type!r}",
        )
    source = str(spec.get("source") or "")
    if spec_type in {"raw_data", "benchmark", "model_weights"}:
        if source.startswith(("huggingface://", "hf://")):
            return fetcher.try_materialize(spec, repo_cache_root(repo_root))
        # explicit local file path => use local
        if source.startswith("file://") or (source.startswith("/") and source[:1] == "/"):
            return REGISTRY["custom"].try_materialize(spec, repo_cache_root(repo_root))
        return MaterializeResult(
            status="needs_user_input",
            fetcher_type=fetcher.fetcher_type,
            error=(
                f"{spec_type} source {source!r} is not understood; supply "
                "hf://, file://, or rewrite as type=custom"
            ),
        )
    if spec_type in {"factor_set", "custom"}:
        # factor_set / custom historically defaulted to LocalPath but agents
        # often emit specs with public-URL sources (Kenneth French Library,
        # AQR, Quandl …). Without this guard the materializer reports
        # "local source does not exist" and the agent has no recourse. Now
        # we surface a clear needs_user_input message that tells the agent
        # to either point to a local download or pivot to synthetic.
        if not source:
            return MaterializeResult(
                status="needs_user_input",
                fetcher_type=fetcher.fetcher_type,
                error=(
                    f"{spec_type} requires a source field. Use "
                    "'file:///abs/path' for a local download, or pivot to "
                    "type=synthetic if no local data is available."
                ),
            )
        if source.startswith("file://") or source.startswith("/"):
            return fetcher.try_materialize(spec, repo_cache_root(repo_root))
        # Any other shape (bare label like "Kenneth French Data Library",
        # http(s) URL, hf:// for the wrong type, etc.) — refuse with a
        # clear message instead of pretending it's a local path.
        return MaterializeResult(
            status="needs_user_input",
            fetcher_type=fetcher.fetcher_type,
            error=(
                f"{spec_type} source {source!r} is not a local path. The "
                "built-in fetcher does not download arbitrary URLs. Options: "
                "(a) the user downloads the file locally and you re-emit "
                "with 'file:///abs/path' source; (b) pivot to type=synthetic "
                "with a fully-specified synthetic_recipe."
            ),
        )
    return fetcher.try_materialize(spec, repo_cache_root(repo_root))
