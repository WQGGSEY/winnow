"""Dataset materializers.

A materializer is a deterministic, type-keyed fetcher. The research_refiner
agent dispatches dataset specs to the matching materializer during a
PROPOSE_DATASET round; the result is injected into the next round's
transcript.

Adding a new dataset type means writing one class that conforms to
``try_materialize(spec, cache_root) -> MaterializeResult`` and registering
it in ``REGISTRY``. The refiner does not change.
"""

from research_harness.datasets.protocol import (
    MaterializeResult,
    MaterializeStatus,
    DatasetMaterializer,
)
from research_harness.datasets.registry import (
    REGISTRY,
    available_materializers,
    materialize,
    repo_cache_root,
)

__all__ = [
    "MaterializeResult",
    "MaterializeStatus",
    "DatasetMaterializer",
    "REGISTRY",
    "available_materializers",
    "materialize",
    "repo_cache_root",
]
