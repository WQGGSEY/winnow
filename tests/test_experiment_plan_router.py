from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from research_harness.orchestrator.experiment_plan import (
    ExperimentPlanError,
    FALLBACK_TEMPLATE_ID,
    build_experiment_plan_for_node,
)
from research_harness.production_runner import run_production_pipeline


REPO_ROOT = Path(__file__).resolve().parents[1]


def _demo_node_with_domain(domain: str) -> dict:
    from research_harness.orchestrator.demo import _demo_node

    node = copy.deepcopy(_demo_node())
    node["domain"] = domain
    return node


def _valid_plan_json(*, plan_id_suffix: str = "custom") -> dict:
    return {
        "plan_id_suffix": plan_id_suffix,
        "task_class": "smoke_test",
        "objective": "custom domain template",
        "entrypoint": {"command": ["python"], "args": ["src/experiment.py"]},
        "resources": {"timeout_sec": 60, "gpu": None, "cpu": 1, "memory_gb": 1},
        "inputs": {"datasets": [], "snapshots": []},
        "expected_outputs": {
            "metrics_files": ["artifacts/metrics.json"],
            "logs": ["artifacts/run.log"],
            "artifact_dirs": ["artifacts/"],
        },
        "baseline_evidence_requirements": [
            {"role": "current_best_known", "metric_key": "bounded_worker_success_rate",
             "baseline_key": "current_best_known", "operator": "greater_than",
             "margin": 0, "required": True},
            {"role": "naive", "metric_key": "bounded_worker_success_rate",
             "baseline_key": "naive_direct_port", "operator": "greater_than",
             "margin": 0, "required": True},
            {"role": "random_or_null", "metric_key": "bounded_worker_success_rate",
             "baseline_key": "random_or_null", "operator": "greater_than",
             "margin": 0, "required": True},
        ],
    }


_TRIVIAL_EXPERIMENT_PY = (
    "import json\n"
    "from pathlib import Path\n"
    "art = Path('artifacts'); art.mkdir(exist_ok=True)\n"
    "(art/'metrics.json').write_text(json.dumps({\n"
    "    'metrics': {'bounded_worker_success_rate': 0.99},\n"
    "    'baselines': {'current_best_known': 0.8, 'naive_direct_port': 0.4, 'random_or_null': 0.05},\n"
    "    'claim_verdict_candidate': 'supported',\n"
    "    'disproof_conditions_hit': [],\n"
    "    'unexpected_observations': [],\n"
    "}, indent=2))\n"
)


def _stage_template(root: Path, dirname: str, domain: str, *, plan: dict | None = None, src_files: dict[str, str] | None = None) -> Path:
    target = root / dirname / domain
    target.mkdir(parents=True, exist_ok=True)
    (target / "plan.json").write_text(json.dumps(plan or _valid_plan_json()), encoding="utf-8")
    src_dir = target / "src"
    src_dir.mkdir(exist_ok=True)
    files = src_files or {"experiment.py": _TRIVIAL_EXPERIMENT_PY}
    for path, content in files.items():
        target_file = src_dir / path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content, encoding="utf-8")
    return target


class ExperimentPlanRouterTests(unittest.TestCase):
    def test_no_template_falls_back_to_demo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            node = _demo_node_with_domain("unmatched_domain_xyz")
            plan, used = build_experiment_plan_for_node(
                Path(tmp), node, Path(tmp) / "run", settings=None
            )
            self.assertEqual(used, FALLBACK_TEMPLATE_ID)
            self.assertEqual(plan["task_class"], "smoke_test")

    def test_matching_directory_template_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stage_template(root, "experiment_plan_templates", "customdomain")
            node = _demo_node_with_domain("customdomain")
            plan, used = build_experiment_plan_for_node(root, node, root / "run", settings=None)
            self.assertEqual(used, "customdomain")
            self.assertEqual(plan["objective"], "custom domain template")
            # source files include the src/ tree
            paths = [f["path"] for f in plan["source_files"]]
            self.assertIn("src/experiment.py", paths)

    def test_missing_src_dir_is_treated_as_no_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "experiment_plan_templates" / "halfbaked").mkdir(parents=True)
            (root / "experiment_plan_templates" / "halfbaked" / "plan.json").write_text(
                json.dumps(_valid_plan_json())
            )
            node = _demo_node_with_domain("halfbaked")
            plan, used = build_experiment_plan_for_node(root, node, root / "run", settings=None)
            self.assertEqual(used, FALLBACK_TEMPLATE_ID)

    def test_invalid_plan_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "experiment_plan_templates" / "brokenplan"
            target.mkdir(parents=True)
            (target / "plan.json").write_text("{not json")
            (target / "src").mkdir()
            (target / "src" / "experiment.py").write_text("print('x')")
            node = _demo_node_with_domain("brokenplan")
            with self.assertRaises(ExperimentPlanError):
                build_experiment_plan_for_node(root, node, root / "run", settings=None)

    def test_plan_missing_required_field_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = dict(_valid_plan_json())
            bad.pop("objective")
            _stage_template(root, "experiment_plan_templates", "missingfield", plan=bad)
            node = _demo_node_with_domain("missingfield")
            with self.assertRaises(ExperimentPlanError):
                build_experiment_plan_for_node(root, node, root / "run", settings=None)

    def test_entrypoint_arg_pointing_at_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = dict(_valid_plan_json())
            plan["entrypoint"] = {"command": ["python"], "args": ["src/missing.py"]}
            _stage_template(root, "experiment_plan_templates", "ptrwrong", plan=plan)
            node = _demo_node_with_domain("ptrwrong")
            with self.assertRaises(ExperimentPlanError):
                build_experiment_plan_for_node(root, node, root / "run", settings=None)

    def test_unsafe_domain_name_is_treated_as_no_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            node = _demo_node_with_domain("../escape")
            plan, used = build_experiment_plan_for_node(
                Path(tmp), node, Path(tmp) / "run", settings=None
            )
            self.assertEqual(used, FALLBACK_TEMPLATE_ID)


class ProductionRunnerTemplateSurfacingTests(unittest.TestCase):
    def test_production_summary_uses_professor_template_by_default(self) -> None:
        """With the LLM orchestrator enabled (the new default), the Professor
        materializes a thread-specific template on demand — there is no
        falling-back-to-demo case. templates_used should reflect that."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            summary = run_production_pipeline(REPO_ROOT, run_dir, publish=False)
            self.assertIn("templates_used", summary)
            self.assertIn("n_demo_001", summary["templates_used"])
            self.assertEqual(
                summary["templates_used"]["n_demo_001"], "professor_generated"
            )
            # Professor-generated templates are real code (no fallback flag).
            self.assertEqual(summary["fallback_node_ids"], [])
            self.assertFalse(summary["evidence_is_fake"])


if __name__ == "__main__":
    unittest.main()
