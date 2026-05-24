from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from research_harness.datasets import (
    MaterializeResult,
    materialize,
    repo_cache_root,
)
from research_harness.datasets.huggingface import HuggingFaceMaterializer
from research_harness.datasets.local_path import LocalPathMaterializer
from research_harness.datasets.synthetic import SyntheticMaterializer


class HuggingFaceDryRunTests(unittest.TestCase):
    def test_dry_run_returns_ok_without_network(self) -> None:
        mat = HuggingFaceMaterializer(dry_run_only=True)
        result = mat.try_materialize(
            {
                "id": "ds_x",
                "type": "benchmark",
                "role": "evaluation",
                "source": "hf://nguha/legalbench",
                "split": "test",
            },
            cache_root=Path("/tmp"),
        )
        self.assertEqual(result.status, "ok")
        self.assertIn("legalbench", str(result.materialized_path))
        self.assertEqual(result.fetcher_type, "huggingface")

    def test_missing_source_fails_cleanly(self) -> None:
        mat = HuggingFaceMaterializer(dry_run_only=True)
        result = mat.try_materialize(
            {"id": "ds_x", "type": "benchmark", "role": "evaluation", "source": ""},
            cache_root=Path("/tmp"),
        )
        self.assertEqual(result.status, "failed")
        self.assertIn("missing source", result.error or "")


class LocalPathMaterializerTests(unittest.TestCase):
    def test_copies_directory_into_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "src"
            src.mkdir()
            (src / "a.txt").write_text("hello")
            (src / "b.txt").write_text("world")

            mat = LocalPathMaterializer()
            cache_root = tmp_path / "cache"
            result = mat.try_materialize(
                {
                    "id": "ds_local_001",
                    "type": "factor_set",
                    "role": "training",
                    "source": f"file://{src}",
                },
                cache_root=cache_root,
            )
            self.assertEqual(result.status, "ok")
            self.assertIsNotNone(result.materialized_path)
            self.assertTrue(result.materialized_path.exists())
            self.assertEqual(
                (result.materialized_path / "a.txt").read_text(),
                "hello",
            )
            self.assertGreater(result.size_bytes, 0)

    def test_missing_source_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mat = LocalPathMaterializer()
            result = mat.try_materialize(
                {"id": "ds_x", "type": "factor_set", "role": "training", "source": "/does/not/exist"},
                cache_root=Path(tmp),
            )
            self.assertEqual(result.status, "failed")
            self.assertIn("does not exist", result.error or "")


class SyntheticMaterializerTests(unittest.TestCase):
    def test_tabular_generation_creates_csv_with_expected_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mat = SyntheticMaterializer()
            spec = {
                "id": "ds_synth_001",
                "type": "synthetic",
                "role": "training",
                "synthetic_recipe": {
                    "shape": "tabular",
                    "rows": 50,
                    "seed": 7,
                    "columns": [
                        {"name": "x", "distribution": "normal", "params": {"mu": 0, "sigma": 1}},
                        {"name": "y", "distribution": "uniform", "params": {"low": -1, "high": 1}},
                        {"name": "z", "distribution": "bernoulli", "params": {"p": 0.3}},
                    ],
                },
            }
            result = mat.try_materialize(spec, cache_root=Path(tmp))
            self.assertEqual(result.status, "ok")
            csv_path = result.materialized_path / "data.csv"
            self.assertTrue(csv_path.exists())
            with csv_path.open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 50)
            self.assertEqual(set(rows[0].keys()), {"x", "y", "z"})
            manifest = json.loads((result.materialized_path / "synthetic_manifest.json").read_text())
            self.assertEqual(manifest["rows"], 50)

    def test_seed_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = {
                "id": "ds_seed_a",
                "type": "synthetic",
                "role": "training",
                "synthetic_recipe": {
                    "shape": "tabular",
                    "rows": 10,
                    "seed": 123,
                    "columns": [{"name": "x", "distribution": "normal"}],
                },
            }
            r1 = SyntheticMaterializer().try_materialize(spec, cache_root=Path(tmp) / "a")
            spec_b = dict(spec, id="ds_seed_b")
            r2 = SyntheticMaterializer().try_materialize(spec_b, cache_root=Path(tmp) / "b")
            first_rows_a = (r1.materialized_path / "data.csv").read_text().splitlines()[1:]
            first_rows_b = (r2.materialized_path / "data.csv").read_text().splitlines()[1:]
            self.assertEqual(first_rows_a, first_rows_b)

    def test_missing_columns_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = {
                "id": "ds_bad",
                "type": "synthetic",
                "role": "training",
                "synthetic_recipe": {"shape": "tabular", "rows": 10, "seed": 0},
            }
            result = SyntheticMaterializer().try_materialize(spec, cache_root=Path(tmp))
            self.assertEqual(result.status, "failed")
            self.assertIn("columns", result.error)

    def test_user_generator_module_is_invoked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            gen = tmp_path / "gen.py"
            gen.write_text(
                "def generate(recipe, out_dir):\n"
                "    (out_dir / 'custom.txt').write_text('hi')\n"
                "    return 1\n"
            )
            spec = {
                "id": "ds_user_gen",
                "type": "synthetic",
                "role": "training",
                "synthetic_recipe": {"shape": "tabular", "generator_module": str(gen)},
            }
            result = SyntheticMaterializer().try_materialize(spec, cache_root=tmp_path)
            self.assertEqual(result.status, "ok")
            self.assertTrue((result.materialized_path / "custom.txt").exists())


class RegistryDispatchTests(unittest.TestCase):
    def test_synthetic_dispatch_through_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = {
                "id": "ds_synth_reg",
                "type": "synthetic",
                "role": "training",
                "synthetic_recipe": {
                    "shape": "tabular",
                    "rows": 5,
                    "seed": 1,
                    "columns": [{"name": "x", "distribution": "normal"}],
                },
            }
            result = materialize(spec, repo_root=Path(tmp))
            self.assertEqual(result.status, "ok")

    def test_unknown_type_returns_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = {"id": "ds_x", "type": "no_such_type_xyz", "role": "training"}
            result = materialize(spec, repo_root=Path(tmp))
            self.assertEqual(result.status, "unsupported_type")

    def test_benchmark_with_unknown_source_scheme_needs_user_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = {
                "id": "ds_x",
                "type": "benchmark",
                "role": "evaluation",
                "source": "s3://team-bucket/legal-eval/",
            }
            result = materialize(spec, repo_root=Path(tmp))
            self.assertEqual(result.status, "needs_user_input")

    def test_repo_cache_root_is_under_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = repo_cache_root(Path(tmp))
            self.assertTrue(cache.exists())
            self.assertTrue(str(cache).endswith(".dataset_cache"))


if __name__ == "__main__":
    unittest.main()
