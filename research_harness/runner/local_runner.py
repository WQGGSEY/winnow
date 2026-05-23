from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from research_harness.schemas.validator import SchemaValidationError, validate_named_schema
from research_harness.workers.workspace import WorkspaceGuardError, ensure_path_inside


class RunnerValidationError(ValueError):
    """Raised when a deterministic runner manifest is unsafe or invalid."""


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
        except (RunnerValidationError, WorkspaceGuardError, SchemaValidationError) as exc:
            errors.append(str(exc))
        return errors

    def validate_or_raise(self, manifest: dict[str, Any]) -> None:
        validate_named_schema("job_manifest", manifest)
        workspace = Path(manifest["workspace"]).resolve()
        ensure_path_inside(workspace, self.run_dir, "job workspace")

        executable = Path(manifest["entrypoint"]["command"][0]).name
        if executable not in self.allowed_executables:
            raise RunnerValidationError(f"entrypoint executable is not allowlisted: {executable}")

        command_parts = manifest["entrypoint"]["command"] + manifest["entrypoint"]["args"]
        for part in command_parts:
            if any(token in part for token in self.FORBIDDEN_TOKENS):
                raise RunnerValidationError(f"shell control token is forbidden in command: {part}")

        self._validate_claim_contract(manifest["claim_contract"])
        self._validate_source_files(manifest, workspace)
        self._validate_output_paths(manifest, workspace)

    def _validate_claim_contract(self, contract: dict[str, Any]) -> None:
        required = ["claim_under_test", "mandatory_baselines", "success_criteria", "disproof_conditions"]
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
        self.validate_or_raise(manifest)
        workspace = Path(manifest["workspace"]).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        stdout_path = workspace / "stdout.log"
        stderr_path = workspace / "stderr.log"
        result_path = workspace / "runner_result.json"
        timeout_sec = self._effective_timeout(manifest)
        command = manifest["entrypoint"]["command"] + manifest["entrypoint"]["args"]
        source_files = [
            str((workspace / Path(raw_path)).resolve())
            for raw_path in manifest.get("source_files", [])
        ]

        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
            elapsed_sec = round(time.monotonic() - started, 6)
            stdout_path.write_text(_output_text(completed.stdout), encoding="utf-8")
            stderr_path.write_text(_output_text(completed.stderr), encoding="utf-8")
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
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_sec = round(time.monotonic() - started, 6)
            stdout_path.write_text(_output_text(exc.stdout), encoding="utf-8")
            stderr_path.write_text(_output_text(exc.stderr), encoding="utf-8")
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


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
