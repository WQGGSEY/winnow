from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

from research_harness.config import load_settings
from research_harness.orchestrator.demo import _demo_node
from research_harness.runner.job_manifest import build_demo_job_manifest
from research_harness.runner.local_runner import LocalRunner, RunnerValidationError
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.workspace import WorkspaceGuardError


class RunnerManifestTests(unittest.TestCase):
    def test_experiment_cannot_modify_validator_or_inputs(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            validator = root / "validator.py"
            validator.write_text("original")
            manifest = build_demo_job_manifest(_demo_node(), root / "run")
            workspace = Path(manifest["workspace"])
            inputs = workspace / "inputs"
            inputs.mkdir()
            game = inputs / "game.py"
            game.write_text("original")
            (workspace / "escape").symlink_to(validator)
            source = (
                "from pathlib import Path\n"
                "for path in [Path('escape'), Path('inputs/game.py')]:\n"
                "    assert path.read_text() == 'original'\n"
                "    try:\n"
                "        path.write_text('tampered')\n"
                "    except OSError:\n"
                "        pass\n"
                "    else:\n"
                "        raise AssertionError('protected file was writable')\n"
                "Path('result.txt').write_text('experiment output')\n"
            )
            (workspace / "experiment.py").write_text(source)
            result = LocalRunner(root / "run").execute(manifest)
            self.assertEqual(result["status"], "completed", Path(result["stderr_path"]).read_text())
            self.assertEqual(validator.read_text(), "original")
            self.assertEqual(game.read_text(), "original")
            self.assertEqual((workspace / "result.txt").read_text(), "experiment output")

    def test_demo_job_manifest_is_schema_valid_and_runner_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            node = _demo_node()
            manifest = build_demo_job_manifest(node, run_dir)

            validate_named_schema("job_manifest", manifest)
            self.assertEqual(manifest["experiment_plan_id"], "plan_n_demo_001_smoke")
            self.assertEqual(manifest["source_files"], ["experiment.py"])
            self.assertEqual(
                {
                    requirement["role"]
                    for requirement in manifest["baseline_evidence_requirements"]
                },
                {"current_best_known", "naive", "random_or_null"},
            )
            self.assertTrue((Path(manifest["workspace"]) / "experiment.py").is_file())
            self.assertEqual(manifest["entrypoint"]["args"], ["experiment.py"])
            LocalRunner(run_dir).validate_or_raise(manifest)

    def test_workspace_must_stay_under_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            node = _demo_node()
            manifest = build_demo_job_manifest(node, run_dir)
            manifest["workspace"] = str(Path(tmp) / "outside")

            with self.assertRaisesRegex(WorkspaceGuardError, "job workspace"):
                LocalRunner(run_dir).validate_or_raise(manifest)

    def test_entrypoint_executable_must_be_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["entrypoint"]["command"] = ["bash"]

            with self.assertRaisesRegex(RunnerValidationError, "allowlisted"):
                LocalRunner(run_dir).validate_or_raise(manifest)

    def test_shell_control_tokens_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["entrypoint"]["args"] = ["-c", "print(1); rm -rf /"]

            with self.assertRaisesRegex(RunnerValidationError, "shell control token"):
                LocalRunner(run_dir).validate_or_raise(manifest)

    def test_outputs_must_be_relative_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["outputs"]["metrics_files"] = ["/tmp/metrics.json"]

            with self.assertRaisesRegex(RunnerValidationError, "relative"):
                LocalRunner(run_dir).validate_or_raise(manifest)

    def test_source_files_must_be_relative_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["source_files"] = ["/tmp/experiment.py"]

            with self.assertRaisesRegex(RunnerValidationError, "source_files"):
                LocalRunner(run_dir).validate_or_raise(manifest)

    def test_source_files_must_exist_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            Path(manifest["workspace"], "experiment.py").unlink()

            with self.assertRaisesRegex(RunnerValidationError, "source file is missing"):
                LocalRunner(run_dir).validate_or_raise(manifest)

    def test_manifest_claim_contract_cannot_omit_baselines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["claim_contract"] = copy.deepcopy(manifest["claim_contract"])
            manifest["claim_contract"]["mandatory_baselines"] = []

            with self.assertRaisesRegex(RunnerValidationError, "mandatory_baselines"):
                LocalRunner(run_dir).validate_or_raise(manifest)

    def test_runner_executes_successful_command_and_writes_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["entrypoint"]["args"] = ["-c", "from pathlib import Path\nprint('runner-ok', flush=True)\nassert Path('stdout.log').read_text() == 'runner-ok\\n'"]

            result = LocalRunner(run_dir).execute(manifest)

            validate_named_schema("runner_result", result)
            self.assertEqual(result["experiment_plan_id"], manifest["experiment_plan_id"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(
                result["source_files"],
                [str(Path(manifest["workspace"]) / "experiment.py")],
            )
            self.assertEqual(
                Path(result["stdout_path"]).read_text(encoding="utf-8"),
                "runner-ok\n",
            )
            self.assertTrue((Path(manifest["workspace"]) / "runner_result.json").exists())

    def test_runner_uses_operator_owned_executable_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["entrypoint"]["args"] = [
                "-c",
                "import os\nprint(os.environ['RUNNER_TEST_VALUE'])",
            ]
            settings = {
                "runtime": {
                    "runner_executable_overrides": {
                        "python": sys.executable,
                    },
                    "runner_environment_overrides": {
                        "RUNNER_TEST_VALUE": "override-ok",
                    },
                }
            }

            result = LocalRunner(run_dir, settings=settings).execute(manifest)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["command"][0], str(Path(sys.executable).resolve()))
            self.assertEqual(
                result["environment_overrides"],
                {"RUNNER_TEST_VALUE": "override-ok"},
            )
            self.assertEqual(
                Path(result["stdout_path"]).read_text(encoding="utf-8"),
                "override-ok\n",
            )

    def test_runner_rejects_relative_executable_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            settings = {
                "runtime": {
                    "runner_executable_overrides": {
                        "python": "venv/bin/python",
                    }
                }
            }

            with self.assertRaisesRegex(
                RunnerValidationError,
                "absolute executable file",
            ):
                LocalRunner(run_dir, settings=settings).validate_or_raise(manifest)

    def test_runner_rejects_invalid_environment_override_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            settings = {
                "runtime": {
                    "runner_environment_overrides": {
                        "BAD-NAME": "value",
                    }
                }
            }

            with self.assertRaisesRegex(RunnerValidationError, "invalid name"):
                LocalRunner(run_dir, settings=settings).validate_or_raise(manifest)

    def test_runner_records_failed_command_as_failure_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["entrypoint"]["args"] = ["-c", "raise SystemExit(3)"]

            result = LocalRunner(run_dir).execute(manifest)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["exit_code"], 3)
            self.assertEqual(
                result["failure_record_candidate"]["category"],
                "invalid_experiment",
            )
            self.assertIn("runner", result["failure_record_candidate"]["tags"])

    def test_runner_records_timeout_without_killing_test_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["resources"]["timeout_sec"] = 1
            manifest["entrypoint"]["args"] = [
                "-c",
                "import time\nprint('start', flush=True)\ntime.sleep(2)",
            ]

            result = LocalRunner(run_dir).execute(manifest)

            self.assertEqual(result["status"], "timeout")
            self.assertIsNone(result["exit_code"])
            self.assertIn("timeout", result["failure_record_candidate"]["tags"])
            self.assertEqual(
                Path(result["stdout_path"]).read_text(encoding="utf-8"),
                "start\n",
            )

    def test_runner_records_start_failure_as_failure_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["entrypoint"]["command"] = ["missing_python_for_runner_test"]

            result = LocalRunner(
                run_dir,
                allowed_executables={"missing_python_for_runner_test"},
            ).execute(manifest)

            self.assertEqual(result["status"], "failed")
            self.assertIsNone(result["exit_code"])
            self.assertIn("could not start", result["failure_record_candidate"]["reason"])
            self.assertTrue(Path(result["stderr_path"]).read_text(encoding="utf-8"))

    def test_runner_uses_task_class_timeout_cap_from_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["resources"]["timeout_sec"] = 60
            settings = copy.deepcopy(load_settings(Path(__file__).resolve().parents[1]))
            settings["runtime"]["runner_timeouts"]["smoke_test"] = 1
            manifest["entrypoint"]["args"] = [
                "-c",
                "import time\ntime.sleep(2)",
            ]

            result = LocalRunner(run_dir, settings=settings).execute(manifest)

            self.assertEqual(result["status"], "timeout")
            self.assertEqual(result["timeout_sec"], 1)


if __name__ == "__main__":
    unittest.main()
