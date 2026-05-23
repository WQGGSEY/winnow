from __future__ import annotations

from pathlib import Path
from typing import Any

from research_harness.orchestrator.experiment_plan import (
    build_demo_experiment_plan,
    build_job_manifest_from_experiment_plan,
)


def build_demo_job_manifest(node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Compatibility wrapper for tests that exercise the runner directly."""

    experiment_plan = build_demo_experiment_plan(node, run_dir)
    return build_job_manifest_from_experiment_plan(node, experiment_plan, run_dir)
