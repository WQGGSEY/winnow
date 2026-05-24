"""Synthetic data materializer.

Generates data from a ``synthetic_recipe`` (shape + distribution +
dimensions + seed). The first supported shape is ``tabular`` with named
columns and per-column distributions; ``time_series`` and
``quant_factor`` are routed through ``tabular`` for now so the refiner
can decide the spec without the generator existing yet.

Operator-defined generators live in the template ``src/`` tree and are
invoked via ``synthetic_recipe.generator_module`` when given. When not
given, the materializer falls back to the built-in deterministic
generator.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

from research_harness.datasets.protocol import MaterializeResult


class SyntheticMaterializer:
    handled_type = "synthetic"
    fetcher_type = "synthetic"

    def try_materialize(
        self,
        spec: dict[str, Any],
        cache_root: Path,
    ) -> MaterializeResult:
        recipe = spec.get("synthetic_recipe") or {}
        if not isinstance(recipe, dict):
            return MaterializeResult(
                status="failed",
                fetcher_type=self.fetcher_type,
                error="synthetic spec missing synthetic_recipe object",
            )

        target_dir = cache_root / "synthetic" / str(spec["id"])
        target_dir.mkdir(parents=True, exist_ok=True)

        generator_module = recipe.get("generator_module")
        if generator_module:
            try:
                rows = _run_user_generator(generator_module, recipe, target_dir)
            except Exception as exc:
                return MaterializeResult(
                    status="failed",
                    fetcher_type=self.fetcher_type,
                    error=f"user generator {generator_module!r} failed: {exc}",
                )
        else:
            shape = str(recipe.get("shape") or "tabular")
            try:
                rows = _builtin_generate(shape, recipe, target_dir)
            except _RecipeError as exc:
                return MaterializeResult(
                    status="failed",
                    fetcher_type=self.fetcher_type,
                    error=str(exc),
                )

        manifest = target_dir / "synthetic_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "spec_id": spec["id"],
                    "recipe": recipe,
                    "rows": rows,
                    "files": sorted(
                        str(p.relative_to(target_dir))
                        for p in target_dir.rglob("*")
                        if p.is_file()
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        size = sum(p.stat().st_size for p in target_dir.rglob("*") if p.is_file())
        return MaterializeResult(
            status="ok",
            materialized_path=target_dir.resolve(),
            size_bytes=size,
            fetcher_type=self.fetcher_type,
            detail={"rows": rows, "shape": recipe.get("shape", "tabular")},
        )


class _RecipeError(ValueError):
    pass


def _builtin_generate(shape: str, recipe: dict[str, Any], target_dir: Path) -> int:
    rows = int(recipe.get("rows") or 1000)
    if rows < 1:
        raise _RecipeError("synthetic_recipe.rows must be >= 1")
    seed = int(recipe.get("seed") if recipe.get("seed") is not None else 0)
    columns = recipe.get("columns") or []
    if shape in {"tabular", "time_series", "quant_factor"}:
        if not columns:
            raise _RecipeError(
                "synthetic_recipe.columns required for tabular / time_series / quant_factor shapes"
            )
        return _generate_tabular(rows, seed, columns, target_dir / "data.csv")
    raise _RecipeError(
        f"built-in synthetic generator does not yet handle shape={shape!r}; "
        "provide a generator_module"
    )


def _generate_tabular(
    rows: int,
    seed: int,
    columns: list[dict[str, Any]],
    out_path: Path,
) -> int:
    rng = random.Random(seed)
    fieldnames = [str(col["name"]) for col in columns]
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for _ in range(rows):
            writer.writerow({col["name"]: _draw_value(rng, col) for col in columns})
    return rows


def _draw_value(rng: random.Random, col: dict[str, Any]) -> Any:
    distribution = str(col.get("distribution") or "normal").lower()
    params = col.get("params") or {}
    if distribution == "normal":
        mu = float(params.get("mu", 0.0))
        sigma = float(params.get("sigma", 1.0))
        return rng.gauss(mu, sigma)
    if distribution == "uniform":
        low = float(params.get("low", 0.0))
        high = float(params.get("high", 1.0))
        return rng.uniform(low, high)
    if distribution == "bernoulli":
        p = float(params.get("p", 0.5))
        return int(rng.random() < p)
    if distribution == "lognormal":
        mu = float(params.get("mu", 0.0))
        sigma = float(params.get("sigma", 1.0))
        return math.exp(rng.gauss(mu, sigma))
    if distribution == "categorical":
        choices = list(params.get("choices") or [])
        if not choices:
            raise _RecipeError("categorical column missing params.choices")
        return rng.choice(choices)
    raise _RecipeError(f"unsupported synthetic distribution: {distribution!r}")


def _run_user_generator(
    generator_module: str,
    recipe: dict[str, Any],
    target_dir: Path,
) -> int:
    module_path = Path(generator_module)
    if not module_path.exists():
        raise FileNotFoundError(module_path)
    spec = importlib.util.spec_from_file_location(
        f"_synthetic_user_{abs(hash(str(module_path)))}", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, "generate", None)
    if not callable(fn):
        raise RuntimeError(f"{module_path} must expose generate(recipe, out_dir) -> int")
    return int(fn(recipe, target_dir))
