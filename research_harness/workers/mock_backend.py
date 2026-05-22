from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class MockWorkerBackend:
    """Deterministic v0 backend for schema and orchestration tests."""

    def run(self, node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
        artifacts_dir = run_dir / "artifacts" / node["id"]
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        contract = node["claim_contract"]
        missing_fields = [
            key
            for key in ("mandatory_baselines", "success_criteria", "disproof_conditions")
            if not contract.get(key)
        ]
        if missing_fields:
            return {
                "node_id": node["id"],
                "status": "blocked_preflight",
                "claim_verdict_candidate": "not_evaluable",
                "metrics": {},
                "baselines": {},
                "disproof_conditions_hit": [],
                "artifacts": [],
                "unexpected_observations": [],
                "failure_record_candidate": {
                    "category": "confounded_result",
                    "tags": ["missing_claim_contract_field"],
                    "missing_fields": missing_fields,
                },
            }

        metrics = {"bounded_worker_success_rate": 0.92, "schema_validity": 1.0}
        baselines = {
            "current_best_known": 0.80,
            "naive_direct_port": 0.45,
            "random_or_null": 0.05,
        }
        metrics_path = artifacts_dir / "metrics.json"
        metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")

        report = {
            "node_id": node["id"],
            "status": "completed",
            "claim_verdict_candidate": "supported",
            "metrics": metrics,
            "baselines": baselines,
            "disproof_conditions_hit": [],
            "artifacts": [str(metrics_path.relative_to(run_dir))],
            "unexpected_observations": [
                {
                    "observation": "The runtime envelope is the main differentiator from a direct API port.",
                    "evidence": "Naive direct port baseline lacks scope, permission, and output-schema controls.",
                    "suggested_branch_type": "validity",
                    "scope_relation": "directly_explains_success",
                }
            ],
            "failure_record_candidate": None,
        }
        (run_dir / "worker_report.json").write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8",
        )
        return report

