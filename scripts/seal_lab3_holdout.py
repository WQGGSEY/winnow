#!/usr/bin/env python3
"""Prepare one concealed, unregistered Lab3 evaluation bank without a learner."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_harness.evaluation_vault import ensure_evaluation_vault


def seal(bundle: Path, thread: Path, count: int = 6) -> dict:
    if count <= 0:
        raise ValueError('count must be positive')
    thread = thread.resolve()
    if not (thread / 'production/feasibility_envelope.json').is_file():
        raise ValueError('A registered research thread is required')
    destination = thread / 'production/evaluation_bank.json'
    private = ensure_evaluation_vault() / thread.name / 'bank.json'
    if destination.exists():
        record = json.loads(destination.read_text())
        if record['game_bundle'] != str(bundle.resolve()) or record['layout_count'] != count:
            raise ValueError('This thread already has a bank with a different sampling request')
        if hashlib.sha256(private.read_bytes()).hexdigest() != record['private_sha256']:
            raise ValueError('Sealed bank content changed')
        return record
    if private.exists():
        raise ValueError('An interrupted bank preparation exists; preserve it for recovery instead of resampling')
    sampler_source = Path(__file__).read_bytes()
    sampler_digest = hashlib.sha256(sampler_source).hexdigest()
    manifest = json.loads((bundle / 'input_manifest.json').read_text())
    if manifest['provided_training_code'] or manifest['provided_policies']:
        raise ValueError('Only a game-only bundle is allowed')
    for name, digest in manifest['files'].items():
        path = (bundle / name).resolve()
        path.relative_to(bundle.resolve())
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Game bundle content changed')
    sys.path.insert(0, str(bundle.resolve()))
    spec = importlib.util.spec_from_file_location('sealed_game_simulator', bundle / 'simulator.py')
    simulator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(simulator)
    game = simulator.Simulator()
    cells = []
    for index in range(count):
        seed = secrets.randbelow((1 << 128) - 1) + 1
        observation = game.reset(layout_seed=seed, starting_agent=0)
        geometry = {key: observation[key] for key in ('width', 'height', 'walls', 'food', 'capsules', 'agents')}
        cells.append({'layout_id': f'sealed_{index:03d}', 'layout_seed': seed,
                      'geometry_sha256': hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()})
    payload = json.dumps({'bundle': str(bundle.resolve()), 'cells': cells, 'sampler_sha256': sampler_digest}, sort_keys=True).encode()
    digest = hashlib.sha256(payload).hexdigest()
    record = {
        'bank_id': 'bank_' + digest, 'private_sha256': digest,
        'status': 'sealed_unregistered', 'created_at': datetime.now(timezone.utc).isoformat(),
        'thread_id': thread.name, 'layout_count': count,
        'unique_layout_count': len({cell['geometry_sha256'] for cell in cells}),
        'layout_ids': [cell['layout_id'] for cell in cells], 'starting_agents': [0, 1],
        'game_bundle': str(bundle.resolve()),
        'input_manifest_sha256': hashlib.sha256((bundle / 'input_manifest.json').read_bytes()).hexdigest(),
        'generator_sha256': manifest['files']['mazeGenerator.py'],
        'upstream_revision': manifest['upstream_revision'],
        'sampling': 'Independent uniform 128-bit positive maze seeds, one draw per layout, no performance filtering.',
        'scope': 'New generated-map distribution, not the original hand-authored partition. '
                 'Requires prospective protocol review; supplies no learning or confirmation result.',
    }
    private.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with private.open('xb') as handle:
        handle.write(payload)
    private.chmod(0o600)
    provenance_root = thread / 'production/evaluation_sampling_provenance'
    provenance_root.mkdir(exist_ok=True)
    (provenance_root / 'sampler.py').write_bytes(sampler_source)
    provenance = {
        'bank_id': record['bank_id'], 'recorded_at': record['created_at'],
        'provenance_status': 'Recorded by the sampler during bank preparation, before public registration.',
        'sampler_source': str((provenance_root / 'sampler.py').resolve()), 'sampler_sha256': sampler_digest,
        'sampling_frame': 'Pushforward of independent uniform positive 128-bit integer seeds through the pinned Lab3 maze generator. '
                          'This defines the procedural-map population; it is not uniform sampling over all Pacman maps.',
        'acceptance': 'Exactly one draw per layout. Simulator.reset parses the board and requires four agents. '
                      'An exception aborts preparation. No retries, connectivity filtering, learner execution or performance selection.',
    }
    (provenance_root / 'receipt.json').write_text(json.dumps(provenance, indent=2) + '\n')
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, indent=2) + '\n')
    temporary.replace(destination)
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('thread', type=Path)
    parser.add_argument('--count', type=int, default=6)
    args = parser.parse_args()
    print(json.dumps(seal(args.bundle, args.thread, args.count), indent=2))
