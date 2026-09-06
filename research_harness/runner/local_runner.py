from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from research_harness.evaluation_vault import ensure_evaluation_vault

from research_harness.schemas.validator import SchemaValidationError, validate_named_schema
from research_harness.runtime_inputs import (
    RuntimeInputError,
    runtime_input_environment,
    validate_runtime_input_reference,
)
from research_harness.workers.workspace import WorkspaceGuardError, ensure_path_inside


class RunnerValidationError(ValueError):
    """Raised when a deterministic runner manifest is unsafe or invalid."""


def isolated_runner_command(command: list[str], workspace: Path) -> list[str]:
    """Allow experiment writes only inside its workspace and private temporary files."""
    bubblewrap = shutil.which("bwrap")
    if bubblewrap is None:
        raise RunnerValidationError("LocalRunner requires bubblewrap for experiment isolation")
    if shutil.which(command[0]) is None:
        raise FileNotFoundError(command[0])
    launch = [
        bubblewrap, "--die-with-parent", "--new-session",
        "--ro-bind", "/", "/", "--unshare-net", "--unshare-pid", "--unshare-ipc",
        "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--bind", str(workspace), str(workspace),
    ]
    for device in [Path("/dev/dxg"), *Path("/dev").glob("nvidia*")]:
        if device.exists():
            launch.extend(["--dev-bind", str(device), str(device)])
    for name in ("inputs", "runtime_inputs.json"):
        path = workspace / name
        if path.exists():
            launch.extend(["--ro-bind", str(path), str(path)])
    launch.extend(["--tmpfs", str(ensure_evaluation_vault())])
    return [*launch, "--chdir", str(workspace), "--", *command]


def resolve_runner_command(
    manifest: dict[str, Any],
    settings: dict[str, Any] | None = None,
) -> list[str]:
    """Resolve a declared executable through an operator-owned runtime map."""

    declared = [
        *manifest["entrypoint"]["command"],
        *manifest["entrypoint"]["args"],
    ]
    overrides = (settings or {}).get("runtime", {}).get(
        "runner_executable_overrides",
        {},
    )
    if not isinstance(overrides, dict):
        raise RunnerValidationError(
            "runtime.runner_executable_overrides must be an object"
        )
    executable_name = Path(declared[0]).name
    replacement = overrides.get(executable_name)
    if replacement is None:
        return declared
    replacement_path = Path(str(replacement))
    if (
        not replacement_path.is_absolute()
        or not replacement_path.is_file()
        or not os.access(replacement_path, os.X_OK)
    ):
        raise RunnerValidationError(
            f"runner executable override for {executable_name!r} is not an "
            f"absolute executable file: {replacement!r}"
        )
    return [str(replacement_path.resolve()), *declared[1:]]


def resolve_runner_environment(
    settings: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Validate environment values supplied by the operator to every job."""

    overrides = (settings or {}).get("runtime", {}).get(
        "runner_environment_overrides",
        {},
    )
    if not isinstance(overrides, dict):
        raise RunnerValidationError(
            "runtime.runner_environment_overrides must be an object"
        )
    resolved: dict[str, str] = {}
    for raw_name, raw_value in overrides.items():
        name = str(raw_name)
        value = str(raw_value)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise RunnerValidationError(
                f"runner environment override has an invalid name: {name!r}"
            )
        if "\x00" in value:
            raise RunnerValidationError(
                f"runner environment override contains NUL: {name!r}"
            )
        resolved[name] = value
    return resolved


class LocalRunner:
    """Deterministic bounded runner for manifest-defined jobs."""

    DEFAULT_ALLOWED_EXECUTABLES = {
        "python",
        "python3",
        Path(sys.executable).name,
        "pytest",
        "node",
        "npm",
        "rg",
    }
    FORBIDDEN_TOKENS = {
        "&&",
        "||",
        ";",
        "|",
        ">",
        ">>",
        "<",
        "$(",
        "`",
    }

    def __init__(
        self,
        run_dir: Path,
        allowed_executables: set[str] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.allowed_executables = allowed_executables or self.DEFAULT_ALLOWED_EXECUTABLES
        self.settings = settings or {}

    def validate_manifest(self, manifest: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        try:
            self.validate_or_raise(manifest)
        except (
            RunnerValidationError,
            RuntimeInputError,
            WorkspaceGuardError,
            SchemaValidationError,
        ) as exc:
            errors.append(str(exc))
        return errors

    def validate_or_raise(self, manifest: dict[str, Any]) -> dict[str, str] | None:
        validate_named_schema("job_manifest", manifest)
        workspace = Path(manifest["workspace"]).resolve()
        ensure_path_inside(workspace, self.run_dir, "job workspace")

        declared_executable = Path(manifest["entrypoint"]["command"][0]).name
        if declared_executable not in self.allowed_executables:
            raise RunnerValidationError(
                f"entrypoint executable is not allowlisted: {declared_executable}"
            )
        command_parts = resolve_runner_command(manifest, self.settings)
        resolve_runner_environment(self.settings)

        for part in command_parts:
            if any(token in part for token in self.FORBIDDEN_TOKENS):
                raise RunnerValidationError(f"shell control token is forbidden in command: {part}")

        measurement_only = manifest.get('measurement_only', False)
        if measurement_only and (manifest['baseline_evidence_requirements'] or manifest['claim_contract'].get('mandatory_baselines')):
            raise RunnerValidationError('measurement-only jobs cannot declare baseline comparisons')
        self._validate_claim_contract(manifest["claim_contract"], measurement_only=measurement_only)
        self._validate_source_files(manifest, workspace)
        self._validate_output_paths(manifest, workspace)
        return validate_runtime_input_reference(
            manifest.get("inputs", {}), workspace=workspace
        )

    def _validate_claim_contract(self, contract: dict[str, Any], *, measurement_only: bool = False) -> None:
        required = ["claim_under_test", "success_criteria", "disproof_conditions"]
        if not measurement_only:
            required.append('mandatory_baselines')
        missing = [key for key in required if not contract.get(key)]
        if missing:
            raise RunnerValidationError(
                "job manifest claim_contract missing: " + ", ".join(missing)
            )

    def _validate_output_paths(self, manifest: dict[str, Any], workspace: Path) -> None:
        outputs = manifest.get("outputs", {})
        for key in ("metrics_files", "logs", "artifact_dirs"):
            for raw_path in outputs.get(key, []):
                output_path = Path(raw_path)
                if output_path.is_absolute():
                    raise RunnerValidationError(f"{key} must use relative workspace paths")
                ensure_path_inside(workspace / output_path, workspace, key)

    def _validate_source_files(self, manifest: dict[str, Any], workspace: Path) -> None:
        for raw_path in manifest.get("source_files", []):
            source_path = Path(raw_path)
            if source_path.is_absolute():
                raise RunnerValidationError("source_files must use relative workspace paths")
            resolved = (workspace / source_path).resolve()
            ensure_path_inside(resolved, workspace, "source_files")
            if not resolved.is_file():
                raise RunnerValidationError(f"source file is missing: {raw_path}")

    def execute(self, manifest: dict[str, Any]) -> dict[str, Any]:
        input_evidence = self.validate_or_raise(manifest)
        workspace = Path(manifest["workspace"]).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        stdout_path = workspace / "stdout.log"
        stderr_path = workspace / "stderr.log"
        result_path = workspace / "runner_result.json"
        timeout_sec = self._effective_timeout(manifest)
        command = resolve_runner_command(manifest, self.settings)
        environment_overrides = resolve_runner_environment(self.settings)
        source_files = [
            str((workspace / Path(raw_path)).resolve())
            for raw_path in manifest.get("source_files", [])
        ]
        child_env = os.environ.copy()
        child_env.update(environment_overrides)
        child_env.update(runtime_input_environment(input_evidence, workspace=workspace))

        started = time.monotonic()
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
                completed = subprocess.run(
                    isolated_runner_command(command, workspace),
                    cwd=workspace,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout_sec,
                    check=False,
                    env=child_env,
                )
            elapsed_sec = round(time.monotonic() - started, 6)
            status = "completed" if completed.returncode == 0 else "failed"
            result = self._runner_result(
                manifest,
                workspace,
                command,
                status=status,
                exit_code=completed.returncode,
                elapsed_sec=elapsed_sec,
                timeout_sec=timeout_sec,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                source_files=source_files,
                failure_reason=None
                if completed.returncode == 0
                else f"runner command exited with code {completed.returncode}",
                input_evidence=input_evidence,
                environment_overrides=environment_overrides,
            )
        except subprocess.TimeoutExpired:
            elapsed_sec = round(time.monotonic() - started, 6)
            result = self._runner_result(
                manifest,
                workspace,
                command,
                status="timeout",
                exit_code=None,
                elapsed_sec=elapsed_sec,
                timeout_sec=timeout_sec,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                source_files=source_files,
                failure_reason=f"runner command exceeded timeout_sec={timeout_sec}",
                input_evidence=input_evidence,
                environment_overrides=environment_overrides,
            )
        except OSError as exc:
            elapsed_sec = round(time.monotonic() - started, 6)
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            result = self._runner_result(
                manifest,
                workspace,
                command,
                status="failed",
                exit_code=None,
                elapsed_sec=elapsed_sec,
                timeout_sec=timeout_sec,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                source_files=source_files,
                failure_reason=f"runner command could not start: {exc}",
                input_evidence=input_evidence,
                environment_overrides=environment_overrides,
            )

        validate_named_schema("runner_result", result)
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return result

    def _runner_result(
        self,
        manifest: dict[str, Any],
        workspace: Path,
        command: list[str],
        *,
        status: str,
        exit_code: int | None,
        elapsed_sec: float,
        timeout_sec: int,
        stdout_path: Path,
        stderr_path: Path,
        source_files: list[str],
        failure_reason: str | None,
        input_evidence: dict[str, str] | None,
        environment_overrides: dict[str, str],
    ) -> dict[str, Any]:
        failure_record_candidate = None
        if failure_reason:
            failure_record_candidate = {
                "category": "invalid_experiment",
                "tags": [
                    "runner",
                    status,
                    manifest["task_class"],
                    *manifest.get("failure_index_hints", {}).get("risk_tags", []),
                ],
                "reason": failure_reason,
            }
        return {
            "job_id": manifest["job_id"],
            "experiment_plan_id": manifest["experiment_plan_id"],
            "node_id": manifest["node_id"],
            "status": status,
            "exit_code": exit_code,
            "elapsed_sec": elapsed_sec,
            "timeout_sec": timeout_sec,
            "workspace": str(workspace),
            "source_files": source_files,
            "command": command,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "failure_record_candidate": failure_record_candidate,
            "input_evidence": input_evidence,
            "environment_overrides": environment_overrides,
        }

    def _effective_timeout(self, manifest: dict[str, Any]) -> int:
        manifest_timeout = int(manifest["resources"]["timeout_sec"])
        configured = (
            self.settings.get("runtime", {})
            .get("runner_timeouts", {})
            .get(manifest["task_class"])
        )
        if configured is None:
            return manifest_timeout
        return min(manifest_timeout, int(configured))
