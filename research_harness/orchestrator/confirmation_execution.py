"""Run a frozen, agent-authored measurement program on newly drawn inputs."""
from __future__ import annotations

import math
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from research_harness.confirmation_sampling import _hash, active_sampling_registration, read_sampling_spec
from research_harness.evaluation_vault import ensure_evaluation_vault
from research_harness.orchestrator.research_control import (
    PLANNING_POLICY_VERSION, StaleResearchWork, _digest, _read, _write, current_work, development_evidence,
)
from research_harness.orchestrator.research_review import review_research_packet
from research_harness.orchestrator.strong_result import verify_strong_execution_evidence
from research_harness.workers.workspace import ensure_path_inside


def _command(command: list[str], *, thread: Path, logical_workspace: Path,
             private_workspace: Path, frozen: dict[Path, Path], bank: Path | None) -> list[str]:
    bubblewrap = shutil.which('bwrap')
    if not bubblewrap:
        raise ValueError('Private confirmation requires bubblewrap')
    args = [bubblewrap, '--die-with-parent', '--new-session', '--ro-bind', '/', '/',
            '--unshare-net', '--unshare-pid', '--unshare-ipc', '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--tmpfs', str(ensure_evaluation_vault()), '--tmpfs', str(thread),
            '--bind', str(private_workspace), str(logical_workspace),
            '--ro-bind', str(private_workspace / '.frozen'), str(logical_workspace / '.frozen')]
    for original, snapshot in frozen.items():
        args.extend(['--ro-bind', str(snapshot), str(original)])
    if bank:
        args.extend(['--ro-bind', str(bank), str(logical_workspace / 'confirmation_bank.json')])
    for device in [Path('/dev/dxg'), *Path('/dev').glob('nvidia*')]:
        if device.exists():
            args.extend(['--dev-bind', str(device), str(device)])
    return [*args, '--chdir', str(logical_workspace), '--', *command]


def _measurement(workspace: Path, metrics_files: list[str], metric: str) -> float:
    found = []
    for relative in metrics_files:
        path = workspace / relative
        ensure_path_inside(path, workspace, 'confirmation metrics')
        value = _read(path).get('metrics', {}).get(metric)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('Confirmation metric is not a finite number')
            found.append(float(value))
    if len(found) != 1:
        raise ValueError('Exactly one declared metrics artifact must contain the registered confirmation metric')
    return found[0]


def verified_confirmation_receipt(thread: Path, binding: dict[str, str]) -> dict[str, Any]:
    receipt = _read(thread / 'production/confirmation_execution.json')
    if receipt.get('binding') != binding or receipt.get('status') != 'completed':
        raise ValueError('No completed private confirmation execution matches this active attempt')
    if receipt['protocol_digest'] != _digest(_read(thread / 'production/feasibility_envelope.json')):
        raise ValueError('Protocol changed after confirmation')
    root = ensure_evaluation_vault() / thread.name / 'confirmation' / receipt['request_digest']
    for relative, digest in receipt['private_artifacts'].items():
        path = root / relative
        ensure_path_inside(path, root, 'confirmation artifact')
        if _hash(path) != digest:
            raise ValueError('Private confirmation evidence changed after execution')
    for relative, digest in receipt.get('released_artifacts', {}).items():
        path = thread / relative
        ensure_path_inside(path, thread / 'production/confirmation_artifacts', 'released confirmation artifact')
        if _hash(path) != digest:
            raise ValueError('Released confirmation artifact changed')
    if _measurement(root / 'evaluation', receipt['metrics_files'], receipt['metric']) != receipt['observed']:
        raise ValueError('Confirmation observation differs from the measured artifact')
    return receipt


def execute_confirmation_experiment(repo: Path, thread: Path, *, work_id: str, reference_scope: str,
                                    reference_node_id: str, additional_files: list[dict[str, str]],
                                    binding: dict[str, str], settings: dict[str, Any]) -> dict[str, Any]:
    thread = thread.resolve()
    from research_harness.memory.baseline_review import require_goal_baseline_approval
    require_goal_baseline_approval(repo, thread, _read(thread / 'production/reorientation/goal_contract.json'))
    work = current_work(thread)
    prior = _read(thread / 'production/confirmation_execution.json')
    if prior:
        if (prior.get('work_id') != work_id or prior.get('reference_scope') != reference_scope
                or prior.get('reference_node_id') != reference_node_id or prior.get('additional_files') != additional_files):
            raise ValueError('Confirmation is already reserved; a new adaptive evaluation is forbidden')
        return verified_confirmation_receipt(thread, binding)
    if work.get('work_id') != work_id or work.get('status') != 'planned' or work['decision']['kind'] != 'confirmation':
        raise ValueError('Plan the confirmation work before requesting private evaluation')
    envelope = _read(thread / 'production/feasibility_envelope.json')
    if (work.get('planning_policy_version') != PLANNING_POLICY_VERSION
            or work.get('protocol_digest') != _digest(envelope)
            or work['evidence_digest'] != _digest(development_evidence(thread))):
        raise StaleResearchWork('Research evidence or protocol changed; plan the confirmation work again')
    registration = active_sampling_registration(thread)
    spec = read_sampling_spec(thread)
    if not registration or registration['replacement_sampling_spec'] != spec:
        raise ValueError('The future sampling procedure must have independent protocol approval')
    if reference_scope not in {'nodes', 'baseline_preflight'}:
        raise ValueError('Invalid public reference scope')
    tree = thread / 'production/tree'
    reference = tree / reference_scope / reference_node_id
    ensure_path_inside(reference, tree / reference_scope, 'reference execution')
    plan = _read(reference / 'experiment_plan.json')
    node = _read(reference / 'node.json') or next(
        (item for item in _read(tree / 'search_state.json').get('nodes', []) if item['id'] == reference_node_id), {})
    report = _read(reference / 'worker_report.json')
    proof = verify_strong_execution_evidence(node=node, experiment_plan=plan, worker_report=report,
                                           node_dir=reference, tree_dir=tree, settings=settings)
    metric = envelope['external_falsifier']['predicate']['metric']
    logical_workspace = Path(plan['workspace']).resolve()
    metrics_files = plan['expected_outputs']['metrics_files']
    _measurement(logical_workspace, metrics_files, metric)
    files = {Path(path).resolve(): _hash(Path(path)) for path in proof['runner_result']['source_files']}
    input_manifest = (proof['runner_result'].get('input_evidence') or {}).get('manifest_path')
    if input_manifest:
        manifest_path = logical_workspace / input_manifest
        manifest = _read(manifest_path)
        dataset = logical_workspace / manifest['primary_dataset']['relative_path']
        for path in [manifest_path, *([dataset] if dataset.is_file() else list(dataset.rglob('*')))]:
            if path.is_file():
                files[path.resolve()] = _hash(path)
    for item in additional_files:
        path = Path(item['path']).resolve()
        ensure_path_inside(path, thread, 'frozen dependency')
        if _hash(path) != item['sha256']:
            raise ValueError('A declared frozen dependency does not match its hash')
        files[path] = item['sha256']
    for path in files:
        ensure_path_inside(path, thread, 'frozen research source or checkpoint')
    timeout = work['decision']['max_runtime_seconds']
    if timeout > envelope['compute_budget']['max_runner_seconds_per_node']:
        raise ValueError('Confirmation exceeds the registered runtime limit')
    elapsed = sum(_read(path).get('elapsed_sec', 0) for group in ('nodes', 'baseline_preflight')
                  for path in (tree / group).glob('*/workspace/runner_result.json'))
    if elapsed + timeout > envelope['compute_budget']['max_total_node_hours'] * 3600:
        raise ValueError('Insufficient registered compute remains for confirmation')
    elapsed += sum(_read(path).get('elapsed_sec', 0) for path in (thread / 'production/research_control/work').glob('*/confirmation_dry_runs/*.json'))
    if elapsed + timeout > envelope['compute_budget']['max_total_node_hours'] * 3600:
        raise ValueError('Insufficient registered compute after public dry runs')
    packet = {'binding': binding, 'work_id': work_id, 'work': work['decision'], 'registered_protocol': envelope,
              'sampling_registration': registration, 'reference_directory': str(reference),
              'reference_execution_sha256': proof['runner_result_sha256'],
              'frozen_files': {str(path): digest for path, digest in files.items()},
              'baseline_qualification': str(thread / 'market/baseline_qualification.json')}
    request_digest = _digest(packet)
    directory = thread / 'production/research_control/work' / work_id / 'confirmation_review'
    review = review_research_packet(repo, directory, packet, purpose=(
        'Review the exact agent-authored program and frozen dependencies for a single final confirmation. '
        'It has a successful public reference run. Trace its actual source and checkpoint selection against qualification and protocol history. '
        'Require evaluation only: no training, method selection, checkpoint tuning, or outcome-dependent retries. '
        'All nonstandard thread-local dependencies and checkpoints must be in frozen_files; other thread files are hidden at runtime. '
        'The same program first runs in isolation on its public fixture, then on fresh confirmation inputs without an intervening agent decision. '
        'It must read RESEARCH_HARNESS_CONFIRMATION_BANK when set and use its actual cells, never a hardcoded development partition. '
        'The supplied sampler describes the JSON format. Preserve all paired endpoints, units, cells, seeds, opponents, checkpoints and uncertainty rules. '
        'Check that the output registered metric is computed from actual game measurements, not constants or author assertions. '
        'A successful process is insufficient. No private data exist yet; do not demand access to them or a realized bank ID.'
    ))
    work['implementation_review'] = review['assessment']
    _write(thread / 'production/research_control/current.json', work)
    _write(thread / 'production/research_control/work' / work_id / 'work.json', work)
    if review['assessment']['decision'] != 'approve':
        return {'status': 'rejected', 'review': review, 'reconsideration_available': True}
    if read_sampling_spec(thread) != spec or _read(thread / 'production/feasibility_envelope.json') != envelope:
        raise StaleResearchWork('Sampling or protocol changed before confirmation')
    root = ensure_evaluation_vault() / thread.name / 'confirmation' / request_digest
    workspace = root / 'evaluation'
    frozen_root = workspace / '.frozen'
    frozen_root.mkdir(parents=True, exist_ok=True)
    frozen = {}
    for index, (source, digest) in enumerate(files.items()):
        if _hash(source) != digest:
            raise StaleResearchWork('A source or checkpoint changed during review')
        target = frozen_root / str(index)
        shutil.copyfile(source, target)
        if _hash(target) != digest:
            raise StaleResearchWork('A source or checkpoint changed while freezing it')
        frozen[source] = target
    command = proof['runner_result']['command']
    environment = os.environ.copy()
    environment.update(proof['runner_result'].get('environment_overrides', {}))
    if input_manifest:
        environment['RESEARCH_HARNESS_INPUT_MANIFEST'] = str(logical_workspace / input_manifest)
    environment.pop('RESEARCH_HARNESS_CONFIRMATION_BANK', None)
    started = time.monotonic()

    def run(args: list[str], label: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError('Confirmation work budget exhausted')
        with (root / f'{label}.stdout').open('w') as stdout, (root / f'{label}.stderr').open('w') as stderr:
            return subprocess.run(args, stdout=stdout, stderr=stderr, timeout=remaining, check=False, env=env)

    diagnostic = None
    try:
        dry_run = run(_command(command, thread=thread, logical_workspace=logical_workspace,
                               private_workspace=workspace, frozen=frozen, bank=None), 'public_dry_run', environment)
        if dry_run.returncode:
            raise ValueError('The isolated public dry run failed')
        _measurement(workspace, metrics_files, metric)
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        diagnostic = str(exc)
    finally:
        _write(thread / 'production/research_control/work' / work_id / 'confirmation_dry_runs' / f'{time.time_ns()}.json',
               {'elapsed_sec': time.monotonic() - started, 'error': diagnostic, 'request_digest': request_digest})
    if diagnostic:
        assessment = {'decision': 'reject', 'reason': diagnostic,
                      'required_work': ['Repair the public measurement program before generating confirmation inputs.'],
                      'evidence': [(root / 'public_dry_run.stderr').read_text()[-4000:]]}
        work.update(implementation_review=assessment, reconsideration_available=True)
        _write(thread / 'production/research_control/current.json', work)
        _write(thread / 'production/research_control/work' / work_id / 'work.json', work)
        return {'status': 'rejected', 'reason': diagnostic, 'review': assessment, 'confirmation_data_generated': False}
    # No writable state from the fixture execution enters confirmation.
    for path in workspace.iterdir():
        if path.name != '.frozen':
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    reservation = {'status': 'running', 'binding': binding, 'work_id': work_id, 'request_digest': request_digest,
                   'reference_scope': reference_scope, 'reference_node_id': reference_node_id, 'additional_files': additional_files,
                   'protocol_digest': _digest(envelope), 'sampling_spec_id': spec['spec_id'], 'metric': metric, 'metrics_files': metrics_files,
                   'reference_execution_sha256': proof['runner_result_sha256'], 'review': review}
    ledger = thread / 'production/confirmation_execution.json'
    _write(ledger, reservation)
    bank = root / 'bank.json'
    try:
        sampled = run([spec['python'], '-B', spec['sampler_source'],
                       *[str(bank) if arg == '{output}' else arg for arg in spec['arguments']]], 'sampling', environment)
        if sampled.returncode or not bank.is_file():
            raise ValueError('The registered sampler did not produce confirmation inputs')
        environment['RESEARCH_HARNESS_CONFIRMATION_BANK'] = str(logical_workspace / 'confirmation_bank.json')
        result = run(_command(command, thread=thread, logical_workspace=logical_workspace,
                              private_workspace=workspace, frozen=frozen, bank=bank), 'confirmation', environment)
        if result.returncode:
            raise ValueError('The frozen confirmation program failed; the bank cannot be reused adaptively')
        observed = _measurement(workspace, metrics_files, metric)
        protected = [bank, *root.glob('*.stdout'), *root.glob('*.stderr'), *frozen.values(), *[workspace / relative for relative in metrics_files]]
        for original, snapshot in frozen.items():
            if _hash(snapshot) != files[original]:
                raise ValueError('Frozen source or checkpoint was modified')
        released = thread / 'production/confirmation_artifacts'
        release_sources = {workspace / relative for relative in metrics_files}
        for relative in plan['expected_outputs'].get('artifact_dirs', []):
            directory = workspace / relative
            ensure_path_inside(directory, workspace, 'confirmation artifact directory')
            release_sources.update(path for path in directory.rglob('*') if path.is_file() and '.frozen' not in path.relative_to(workspace).parts)
        released_hashes = {}
        for source in sorted(release_sources):
            ensure_path_inside(source, workspace, 'confirmation output')
            destination = released / source.relative_to(workspace)
            ensure_path_inside(destination, released, 'released confirmation output')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            released_hashes[str(destination.relative_to(thread))] = _hash(destination)
        # Development is permanently closed before sampled inputs become public.
        shutil.copyfile(bank, released / 'sampled_inputs.json')
        released_hashes[str((released / 'sampled_inputs.json').relative_to(thread))] = _hash(bank)
        reservation['released_artifacts'] = released_hashes
        reservation.update(status='completed', observed=observed,
                           private_artifacts={str(path.relative_to(root)): _hash(path) for path in protected})
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        reservation.update(status='failed', reason=str(exc))
    reservation['elapsed_sec'] = round(time.monotonic() - started, 6)
    _write(ledger, reservation)
    work.update(status='completed', outcome={'execution_result': 'confirmation_' + reservation['status'],
                'confirmation_receipt': str(ledger), 'new_observation': reservation['status'] == 'completed', 'scientific_verdict': 'unverified'},
                next_tool_to_call='compute_falsifier_result')
    _write(thread / 'production/research_control/current.json', work)
    _write(thread / 'production/research_control/work' / work_id / 'work.json', work)
    return {**reservation, 'research_work_checkpoint': work_id, 'next_tool_to_call': 'compute_falsifier_result'}
