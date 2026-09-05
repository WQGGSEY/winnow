#!/usr/bin/env python3
"""Draw game initial conditions once; contains no policy or learner."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.count <= 0 or args.output.exists():
        raise ValueError('Require a positive count and a fresh output path')
    sys.path.insert(0, str(args.bundle.resolve()))
    spec = importlib.util.spec_from_file_location('confirmation_game', args.bundle / 'simulator.py')
    simulator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(simulator)
    game = simulator.Simulator()
    cells = []
    for index in range(args.count):
        seed = secrets.randbelow((1 << 128) - 1) + 1
        state = game.reset(layout_seed=seed, starting_agent=0)
        geometry = {key: state[key] for key in ('width', 'height', 'walls', 'food', 'capsules', 'agents')}
        cells.append({'layout_id': f'confirmation_{index:03d}', 'layout_seed': seed,
                      'geometry_sha256': hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()})
    with args.output.open('x') as handle:
        json.dump({'game_bundle': str(args.bundle.resolve()), 'cells': cells, 'starting_agents': [0, 1]}, handle)


if __name__ == '__main__':
    main()
