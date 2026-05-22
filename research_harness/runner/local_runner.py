from __future__ import annotations

from pathlib import Path
from typing import Any

from research_harness.schemas.validator import SchemaValidationError, validate_named_schema
from research_harness.workers.workspace import WorkspaceGuardError, ensure_path_inside


class RunnerValidationError(ValueError):
    """Raised when a deterministic runner manifest is unsafe or invalid."""


class LocalRunner:
    """Placeholder deterministic runner for long-running jobs.

    v0 does not execute training jobs. The runner boundary exists so Claude Code
    workers can prepare manifests while deterministic infrastructure owns long
    execution.
    """

    DEFAULT_ALLOWED_EXECUTABLES = {"python", "pytest", "node", "npm", "rg"}
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
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.allowed_executables = allowed_executables or self.DEFAULT_ALLOWED_EXECUTABLES

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

        executable = manifest["entrypoint"]["command"][0]
        if executable not in self.allowed_executables:
            raise RunnerValidationError(f"entrypoint executable is not allowlisted: {executable}")

        command_parts = manifest["entrypoint"]["command"] + manifest["entrypoint"]["args"]
        for part in command_parts:
            if any(token in part for token in self.FORBIDDEN_TOKENS):
                raise RunnerValidationError(f"shell control token is forbidden in command: {part}")

        self._validate_claim_contract(manifest["claim_contract"])
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

    def write_stub_result(self, job_id: str) -> Path:
        path = self.run_dir / f"{job_id}_runner_stub.txt"
        path.write_text("v0 runner stub: no long-running job executed.\n", encoding="utf-8")
        return path
