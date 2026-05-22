from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from research_harness.orchestrator.demo import _demo_node
from research_harness.runner.job_manifest import build_demo_job_manifest
from research_harness.runner.local_runner import LocalRunner, RunnerValidationError
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.workspace import WorkspaceGuardError


class RunnerManifestTests(unittest.TestCase):
    def test_demo_job_manifest_is_schema_valid_and_runner_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            node = _demo_node()
            manifest = build_demo_job_manifest(node, run_dir)

            validate_named_schema("job_manifest", manifest)
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

    def test_manifest_claim_contract_cannot_omit_baselines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest = build_demo_job_manifest(_demo_node(), run_dir)
            manifest["claim_contract"] = copy.deepcopy(manifest["claim_contract"])
            manifest["claim_contract"]["mandatory_baselines"] = []

            with self.assertRaisesRegex(RunnerValidationError, "mandatory_baselines"):
                LocalRunner(run_dir).validate_or_raise(manifest)


if __name__ == "__main__":
    unittest.main()

