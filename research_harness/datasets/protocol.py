from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


MaterializeStatus = Literal[
    "ok",
    "failed",
    "needs_credential",
    "needs_user_input",
    "unsupported_type",
]


@dataclass
class MaterializeResult:
    """Outcome of a single materializer dispatch."""

    status: MaterializeStatus
    materialized_path: Path | None = None
    size_bytes: int | None = None
    fetcher_type: str = "unknown"
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "materialized_path": (
                str(self.materialized_path) if self.materialized_path else None
            ),
            "size_bytes": self.size_bytes,
            "fetcher_type": self.fetcher_type,
            "detail": self.detail,
            "error": self.error,
        }


@runtime_checkable
class DatasetMaterializer(Protocol):
    """One materializer per dataset_spec.type.

    Implementations must:
      - declare ``handled_type`` (matches the spec.type enum value),
      - declare ``fetcher_type`` (free-form label written into the manifest),
      - return a MaterializeResult; never raise for normal failures.
    """

    handled_type: str
    fetcher_type: str

    def try_materialize(
        self,
        spec: dict[str, Any],
        cache_root: Path,
    ) -> MaterializeResult: ...
