"""ADR 0006 rev.2 — the harness-owned behavioral-distance probe.

``falsifier.py`` is pure (no I/O). The behavioral-distance guard needs to RUN
the two generators, which is I/O, so it lives here. Given generator A and
generator B as harness-loadable synthetic recipes, this materializes both on a
single HARNESS-FIXED probe (the seed and row count the worker does NOT choose)
and returns the measured divergence of their output distributions.

The worker supplies WHICH recipes; the harness executes them. So the
distinctness of B from A is MEASURED, never a label/hash the worker asserted —
the thread_c8919361 leak (a passing rho computed against a non-distinct B) is
closed because identical generators on the fixed seed produce distance 0.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from research_harness import falsifier as _F
from research_harness.datasets.registry import materialize

# Harness-chosen probe. Fixed so the worker cannot pre-cook the divergence, and
# shared by A and B so an identical DGP yields distance 0.
PROBE_SEED = 1729
PROBE_ROWS = 512


def _numeric_columns(csv_path: Path) -> dict[str, list[float]]:
    samples: dict[str, list[float]] = {}
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            for key, raw in row.items():
                try:
                    samples.setdefault(key, []).append(float(raw))
                except (TypeError, ValueError):
                    continue  # non-numeric column — skipped
    return samples


def _materialize_recipe(
    recipe: dict[str, Any], *, repo_root: Path, probe_id: str
) -> dict[str, list[float]] | None:
    probe_recipe = {**(recipe or {}), "seed": PROBE_SEED, "rows": PROBE_ROWS}
    spec = {"type": "synthetic", "id": probe_id, "synthetic_recipe": probe_recipe}
    result = materialize(spec, repo_root=repo_root)
    if result.status != "ok" or not result.materialized_path:
        return None
    out = Path(result.materialized_path)
    csv_path = out / "data.csv"
    if not csv_path.exists():
        candidates = sorted(out.glob("*.csv"))
        if not candidates:
            return None
        csv_path = candidates[0]
    return _numeric_columns(csv_path)


def measure_behavioral_distance(
    generator_a: Any,
    generator_b: Any,
    *,
    repo_root: Path,
    thread_id: str,
) -> dict[str, Any]:
    """Run A and B on the fixed probe and return {distance, ...}. ``distance``
    is None when either generator is absent or un-materializable — the harness
    could not certify distinctness, so the verdict will be uninformative."""
    if not isinstance(generator_a, dict) or not isinstance(generator_b, dict):
        return {
            "distance": None,
            "reason": (
                "no harness-loadable generator_a / generator_b synthetic recipes "
                "supplied — transfer distinctness cannot be measured air-gapped"
            ),
        }
    samples_a = _materialize_recipe(generator_a, repo_root=repo_root, probe_id=f"_fprobe_{thread_id}_a")
    samples_b = _materialize_recipe(generator_b, repo_root=repo_root, probe_id=f"_fprobe_{thread_id}_b")
    if samples_a is None or samples_b is None:
        return {
            "distance": None,
            "known_baseline_transfer": None,
            "reason": "could not materialize one or both generators on the probe",
        }
    distance = _F.behavioral_distance(samples_a, samples_b)
    # ADR 0010 (T2-03): the STRONG harness-constructed known-only baseline — the
    # strongest standard-statistic transfer on the SAME probe. Worker never
    # supplies it, so it cannot be a weak strawman.
    known_baseline_transfer = _F.known_method_transfer(samples_a, samples_b)
    return {
        "distance": distance,
        "known_baseline_transfer": known_baseline_transfer,
        "detail": {"columns_a": sorted(samples_a), "columns_b": sorted(samples_b)},
    }
