"""Deterministic interpreter — Sakana parity in shape, LocalRunner underneath.

Sakana's Interpreter wraps a subprocess that runs LLM-generated code. We do
not let an LLM run code in the tree — instead, the Interpreter is a thin
adapter around runner.local_runner.LocalRunner that owns the same
"execute a manifest, return result" surface so AgentManager / ParallelAgent
can pretend it's calling a Sakana interpreter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research_harness.runner.local_runner import LocalRunner


class Interpreter:
    """Adapter that gives LocalRunner a Sakana-shaped interface.

    Sakana's Interpreter.run(code) returns (result, exec_time, exc_type, ...).
    Ours takes a fully-validated job_manifest (the harness rule: no LLM-authored
    code touches the runner) and returns the runner_result dict directly.
    """

    def __init__(
        self,
        run_dir: Path,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self._runner = LocalRunner(run_dir=run_dir, settings=settings or {})
        self.run_dir = run_dir

    def run(self, job_manifest: dict[str, Any]) -> dict[str, Any]:
        return self._runner.execute(job_manifest)

    def validate_or_raise(self, job_manifest: dict[str, Any]) -> None:
        self._runner.validate_or_raise(job_manifest)
