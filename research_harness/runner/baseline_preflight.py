"""Execute one baseline while its scientific qualification is pending."""
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


def resolve_preflight_role(plan: dict[str, Any], requested: str | None = None) -> str:
    requirements = plan.get('baseline_evidence_requirements', [])
    if not requirements and not plan.get('mandatory_baselines'):
        role = 'diagnostic'
    elif (len(requirements) == 1 and requirements[0].get('required')
          and requirements[0].get('role') in {'current_best_known', 'naive', 'random_or_null'}):
        role = requirements[0]['role']
    else:
        raise ValueError('preflight must measure exactly one required baseline or have no comparisons for a diagnostic')
    if requested is not None and requested != role:
        raise ValueError('preflight role conflicts with experiment_plan.baseline_evidence_requirements')
    return role


def build_preflight_node(thread_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Derive preparation metadata without duplicating the author's experiment contract."""
    from research_harness.orchestrator.adaptive_search import build_research_goal

    def read(relative: str) -> dict[str, Any]:
        path = thread_dir / relative
        return json.loads(path.read_text()) if path.exists() else {}

    envelope = read('production/feasibility_envelope.json')
    goal = read('production/tree/search_state.json').get('adaptive', {}).get('goal')
    if not goal:
        goal = build_research_goal(thread=read('thread.json'),
                                  grilling=read('grilling/grilling_session.json'), envelope=envelope)
    contract = {key: plan[key] for key in (
        'claim_under_test', 'mandatory_baselines', 'success_criteria', 'disproof_conditions')}
    intent = envelope.get('operator_intent', {})
    contract.update({key: intent[key] for key in ('data_source_anchor', 'data_source_snapshot_id') if key in intent})
    contract['deploy_grade_scope'] = intent.get('target_deploy_grade_scope', 'directional')
    node = {
        'id': plan['node_id'], 'type': 'operational', 'status': 'ready',
        'domain': plan['objective'], 'stage': 'experimentation', 'claim_contract': contract,
        'lineage': {'root_goal_id': goal['id'], 'covers_goal_facets': [],
                    'inherited_assumptions': [], 'introduced_assumptions': [], 'taste_constraints_applied': []},
        'baseline_refs': [], 'runtime_profile': {'worker_type': 'experiment_worker', 'timeout_policy': 'hard'},
        'failure_retrieval': {'query_tags': plan['failure_index_hints'].get('risk_tags', []), 'selected_fail_files': []},
        'outputs': {'artifacts': [], 'verdict': None},
    }
    validate_named_schema('node', node)
    return node


def baseline_preparation_state(thread_dir: Path) -> dict[str, Any]:
    """Project preparation receipts without treating them as claim evidence."""
    root = thread_dir / 'production/tree/baseline_preflight'
    attempts = []
    unreadable = []

    def read(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError('expected object')
            return value
        except (OSError, ValueError):
            unreadable.append(str(path.relative_to(thread_dir)))
            return {}

    for path in sorted(root.glob('*/node.json'), key=lambda p: (p.stat().st_mtime_ns, str(p))):
        directory = path.parent
        plan = read(directory / 'experiment_plan.json')
        report = read(directory / 'worker_report.json')
        result = read(directory / 'workspace/runner_result.json')
        requirements = plan.get('baseline_evidence_requirements', [])
        status = 'incomplete'
        if result.get('status') == 'completed' and report.get('status') == 'completed':
            status = ('comparison_failed' if report.get('baseline_evidence_status', {}).get('overall') == 'failed'
                      else 'executed_unreviewed')
        elif (result and result.get('status') != 'completed') or (report and report.get('status') != 'completed'):
            status = 'execution_failed'
        attempts.append({
            'node_id': directory.name,
            'role': requirements[0]['role'] if requirements else None,
            'status': status,
            'elapsed_sec': result.get('elapsed_sec', 0),
            'metrics': report.get('metrics', {}),
            'failure': report.get('failure_record_candidate') or result.get('failure_record_candidate'),
            'report_path': str((directory / 'worker_report.json').relative_to(thread_dir)) if report else None,
        })
    return {
        'attempt_count': len(attempts),
        'elapsed_sec': sum(row['elapsed_sec'] for row in attempts),
        'status_counts': {status: sum(row['status'] == status for row in attempts)
                          for status in ('incomplete', 'execution_failed', 'comparison_failed', 'executed_unreviewed')},
        'recent_attempts': attempts[-5:],
        'qualification_recorded': (thread_dir / 'market/baseline_qualification.json').exists(),
        'unreadable_artifacts': unreadable,
        'evidence_scope': 'Preparation only. Completion is not scientific approval; incomplete does not imply a live process. Read bound artifacts before choosing the next diagnostic.',
    }


def execute_baseline_preflight(
    repo: Path, thread_dir: Path, *, node: dict[str, Any], plan: dict[str, Any],
    role: str, settings: dict[str, Any], research_work_id: str | None = None,
) -> dict[str, Any]:
    node = json.loads(json.dumps(node))
    plan = json.loads(json.dumps(plan))
    validate_named_schema('node', node)
    role = resolve_preflight_role(plan, role)
    requirements = plan.get('baseline_evidence_requirements', [])
    if role == 'diagnostic':
        from research_harness.orchestrator.research_control import current_work
        work = current_work(thread_dir)
        if (not research_work_id or work.get('work_id') != research_work_id
                or work.get('decision', {}).get('kind') not in {'diagnostic_experiment', 'competence'}):
            raise ValueError('measurement-only preflight requires selected diagnostic_experiment or competence work')
    tree = thread_dir / 'production/tree'
    node_dir = tree / 'baseline_preflight' / node['id']
    node_dir.resolve().relative_to(tree.resolve())
    workspace = node_dir / 'workspace'
    plan['workspace'] = str(workspace.resolve())
    node['type'] = 'operational'
    envelope = json.loads((thread_dir / 'production/feasibility_envelope.json').read_text())
    intent = envelope.get('operator_intent', {})
    if intent.get('data_source_anchor'):
        contract = node['claim_contract']
        for key in ('data_source_anchor', 'data_source_snapshot_id'):
            if contract.get(key) != intent.get(key):
                raise ValueError(f'preflight must use the operator-selected input snapshot: '
                                 f'{key} must be {intent.get(key)!r}, got {contract.get(key)!r}')
        snapshot = require_thread_snapshot(thread_dir, adapter_id=intent['data_source_anchor'], snapshot_id=intent['data_source_snapshot_id'])
        plan['inputs'] = bind_runtime_input(snapshot=snapshot, workspace=workspace, repo_root=repo)
    node_dir.mkdir(parents=True, exist_ok=True)
    plan_path = node_dir / 'experiment_plan.json'
    if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
        raise ValueError('preflight node already has another plan; use a new node ID')
    report_path = node_dir / 'worker_report.json'
    if not report_path.exists():
        budget = envelope['compute_budget']
        timeout = plan['resources']['timeout_sec']
        if timeout > budget['max_runner_seconds_per_node']:
            raise ValueError('preflight exceeds registered per-node compute budget')
        elapsed = sum(json.loads(p.read_text()).get('elapsed_sec', 0) for p in (tree / 'baseline_preflight').glob('*/workspace/runner_result.json'))
        if elapsed + timeout > budget['max_total_node_hours'] * 3600:
            raise ValueError('preflight exceeds remaining registered compute budget')
        manifest = build_job_manifest_from_experiment_plan(node, plan, tree, preflight_role=role)
        if research_work_id is not None:
            from research_harness.orchestrator.research_control import review_work_implementation
            review_work_implementation(repo, thread_dir, research_work_id, node, plan)
        for name, value in [('node.json', node), ('experiment_plan.json', plan), ('job_manifest.json', manifest)]:
            (node_dir / name).write_text(json.dumps(value, indent=2) + '\n')
        if research_work_id:
            from research_harness.orchestrator.research_control import mark_experiment_running
            mark_experiment_running(thread_dir, research_work_id)
        result = LocalRunner(tree, settings=settings).execute(manifest)
        report = build_worker_report_from_runner_evidence(node, manifest, result, tree).worker_report
        report_path.write_text(json.dumps(report, indent=2) + '\n')
    report = json.loads(report_path.read_text())
    if report.get('status') != 'completed':
        return {'status': 'execution_failed', 'node_dir': str(node_dir), 'worker_report': report}
    expected_baselines = {requirements[0]['baseline_key']} if requirements else set()
    if set(report.get('baselines', {})) != expected_baselines:
        raise ValueError('preflight must report only its actually executed baseline key')
    verify_strong_execution_evidence(node=node, experiment_plan=plan, worker_report=report, node_dir=node_dir, tree_dir=tree, settings=settings)
    relative = node_dir.relative_to(tree).as_posix()
    return {
        'status': 'executed', 'scientific_approval': False,
        'reproducibility_receipt': {
            'node_path': relative + '/node.json', 'experiment_plan_path': relative + '/experiment_plan.json',
            'worker_report_path': relative + '/worker_report.json', 'node_dir': relative, 'tree_dir': '.',
            **({'metric_id': requirements[0]['metric_key'], 'baseline_key': requirements[0]['baseline_key'],
                'metric_value': report['baselines'][requirements[0]['baseline_key']]} if requirements else {}),
        },
        'worker_report': report,
    }
