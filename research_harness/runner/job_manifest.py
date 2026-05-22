from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def build_demo_job_manifest(node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    workspace = run_dir / "nodes" / node["id"] / "workspace"
    return {
        "job_id": f"job_{node['id']}_smoke",
        "node_id": node["id"],
        "task_class": "smoke_test",
        "workspace": str(workspace.resolve()),
        "entrypoint": {
            "command": [sys.executable],
            "args": ["-c", "print('runner smoke placeholder')"],
        },
        "resources": {
            "timeout_sec": 60,
            "gpu": None,
            "cpu": 1,
            "memory_gb": 1,
        },
        "inputs": {
            "datasets": [],
            "snapshots": [],
        },
        "outputs": {
            "metrics_files": ["artifacts/metrics.json"],
            "logs": ["artifacts/run.log"],
            "artifact_dirs": ["artifacts/"],
        },
        "claim_contract": node["claim_contract"],
        "failure_index_hints": {
            "domain_tags": [node["domain"]],
            "method_tags": ["bounded_worker", "deterministic_runner"],
            "risk_tags": node["failure_retrieval"]["query_tags"],
        },
        "reproducibility": {
            "seed": 0,
            "code_snapshot": "local_pre_live_scaffold",
            "data_snapshot": "none",
        },
    }
