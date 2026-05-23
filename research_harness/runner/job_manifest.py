from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def build_demo_job_manifest(node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    workspace = run_dir / "nodes" / node["id"] / "workspace"
    experiment_path = workspace / "experiment.py"
    experiment_path.parent.mkdir(parents=True, exist_ok=True)
    experiment_path.write_text(_demo_experiment_source(), encoding="utf-8")
    return {
        "job_id": f"job_{node['id']}_smoke",
        "node_id": node["id"],
        "task_class": "smoke_test",
        "workspace": str(workspace.resolve()),
        "source_files": ["experiment.py"],
        "entrypoint": {
            "command": [sys.executable],
            "args": ["experiment.py"],
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


def _demo_experiment_source() -> str:
    return "\n".join(
        [
            "import json",
            "from pathlib import Path",
            "",
            "artifacts = Path('artifacts')",
            "artifacts.mkdir(exist_ok=True)",
            "payload = {",
            "    'metrics': {",
            "        'bounded_worker_success_rate': 0.92,",
            "        'schema_validity': 1.0,",
            "    },",
            "    'baselines': {",
            "        'current_best_known': 0.80,",
            "        'naive_direct_port': 0.45,",
            "        'random_or_null': 0.05,",
            "    },",
            "    'claim_verdict_candidate': 'supported',",
            "    'disproof_conditions_hit': [],",
            "    'unexpected_observations': [",
            "        {",
            "            'observation': 'The runtime envelope is the main differentiator from a direct API port.',",
            "            'evidence': 'Naive direct port baseline lacks scope, permission, and output-schema controls.',",
            "            'suggested_branch_type': 'validity',",
            "            'scope_relation': 'directly_explains_success',",
            "        }",
            "    ],",
            "}",
            "(artifacts / 'metrics.json').write_text(json.dumps(payload, indent=2) + '\\n')",
            "print('runner smoke metrics written')",
            "",
        ]
    )
