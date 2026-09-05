"""Execute one baseline before a research direction or success contract exists."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_harness.data_adapters import require_thread_snapshot
from research_harness.orchestrator.experiment_plan import build_job_manifest_from_experiment_plan
from research_harness.orchestrator.strong_result import verify_strong_execution_evidence
from research_harness.runner.evidence import build_worker_report_from_runner_evidence
from research_harness.runner.local_runner import LocalRunner
from research_harness.runtime_inputs import bind_runtime_input
from research_harness.schemas.validator import validate_named_schema


def execute_baseline_preflight(
    repo: Path, thread_dir: Path, *, node: dict[str, Any], plan: dict[str, Any],
    role: str, settings: dict[str, Any],
) -> dict[str, Any]:
    node = json.loads(json.dumps(node))
    plan = json.loads(json.dumps(plan))
    validate_named_schema('node', node)
    if role not in {'current_best_known', 'naive', 'random_or_null'}:
        raise ValueError('invalid preflight role')
    requirements = plan.get('baseline_evidence_requirements', [])
    if len(requirements) != 1 or requirements[0].get('role') != role or not requirements[0].get('required'):
        raise ValueError('preflight must measure exactly one baseline role')
    tree = thread_dir / 'production/tree'
    node_dir = tree / 'baseline_preflight' / node['id']
    node_dir.resolve().relative_to(tree.resolve())
    workspace = node_dir / 'workspace'
    plan['workspace'] = str(workspace.resolve())
    node['type'] = 'operational'
    envelope = json.loads((thread_dir / 'production/feasibility_envelope.json').read_text())
    budget = envelope['compute_budget']
    timeout = plan['resources']['timeout_sec']
    if timeout > budget['max_runner_seconds_per_node']:
        raise ValueError('preflight exceeds registered per-node compute budget')
    elapsed = sum(json.loads(p.read_text()).get('elapsed_sec', 0) for p in (tree / 'baseline_preflight').glob('*/workspace/runner_result.json'))
    if elapsed + timeout > budget['max_total_node_hours'] * 3600:
        raise ValueError('preflight exceeds remaining registered compute budget')
    intent = envelope.get('operator_intent', {})
    if intent.get('data_source_anchor'):
        contract = node['claim_contract']
        for key in ('data_source_anchor', 'data_source_snapshot_id'):
            if contract.get(key) != intent.get(key):
                raise ValueError('preflight must use the operator-selected input snapshot')
        snapshot = require_thread_snapshot(thread_dir, adapter_id=intent['data_source_anchor'], snapshot_id=intent['data_source_snapshot_id'])
        plan['inputs'] = bind_runtime_input(snapshot=snapshot, workspace=workspace, repo_root=repo)
    node_dir.mkdir(parents=True, exist_ok=True)
    plan_path = node_dir / 'experiment_plan.json'
    if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
        raise ValueError('preflight node already has another plan; use a new node ID')
    report_path = node_dir / 'worker_report.json'
    if not report_path.exists():
        manifest = build_job_manifest_from_experiment_plan(node, plan, tree, preflight_role=role)
        for name, value in [('node.json', node), ('experiment_plan.json', plan), ('job_manifest.json', manifest)]:
            (node_dir / name).write_text(json.dumps(value, indent=2) + '\n')
        result = LocalRunner(tree, settings=settings).execute(manifest)
        report = build_worker_report_from_runner_evidence(node, manifest, result, tree).worker_report
        report_path.write_text(json.dumps(report, indent=2) + '\n')
    report = json.loads(report_path.read_text())
    key = requirements[0]['baseline_key']
    if report.get('status') != 'completed':
        return {'status': 'execution_failed', 'node_dir': str(node_dir), 'worker_report': report}
    if set(report.get('baselines', {})) != {key}:
        raise ValueError('preflight must report only its actually executed baseline key')
    verify_strong_execution_evidence(node=node, experiment_plan=plan, worker_report=report, node_dir=node_dir, tree_dir=tree, settings=settings)
    relative = node_dir.relative_to(tree).as_posix()
    return {
        'status': 'executed', 'scientific_approval': False,
        'reproducibility_receipt': {
            'node_path': relative + '/node.json', 'experiment_plan_path': relative + '/experiment_plan.json',
            'worker_report_path': relative + '/worker_report.json', 'node_dir': relative, 'tree_dir': '.',
            'metric_id': requirements[0]['metric_key'], 'baseline_key': key, 'metric_value': report['baselines'][key],
        },
        'worker_report': report,
    }
