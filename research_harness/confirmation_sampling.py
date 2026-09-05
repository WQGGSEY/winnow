"""Register a sampling procedure before its future confirmation data exist."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def register_sampling_spec(thread: Path, *, sampler: Path, arguments: list[str],
                           input_files: list[Path], description: dict[str, Any]) -> dict[str, Any]:
    """Operator provisioning of simulation I/O, not a research approval or draw."""
    if arguments.count('{output}') != 1:
        raise ValueError('A sampler must have exactly one private output placeholder')
    source = sampler.read_bytes()
    payload = {'sampler_sha256': hashlib.sha256(source).hexdigest(), 'arguments': arguments,
               'input_commitments': {str(path.resolve()): _hash(path) for path in input_files},
               'description': description, 'python': str(Path(sys.executable).resolve())}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    root = thread / 'production/confirmation_sampling'
    path = root / 'spec.json'
    if path.exists():
        existing = read_sampling_spec(thread)
        if existing['spec_id'] != 'sampling_' + digest:
            raise ValueError('This thread already has a different sampling specification')
        return existing
    root.mkdir(parents=True, exist_ok=True)
    source_path = root / 'sampler.py'
    source_path.write_bytes(source)
    record = {**payload, 'spec_id': 'sampling_' + digest, 'sampler_source': str(source_path.resolve()),
              'generation_timing': 'Only after implementation, checkpoints and evaluation program are frozen; no data drawn by registration.'}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, indent=2) + '\n')
    temporary.replace(path)
    return record


def read_sampling_spec(thread: Path) -> dict[str, Any] | None:
    path = thread / 'production/confirmation_sampling/spec.json'
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    payload = {key: record[key] for key in ('sampler_sha256', 'arguments', 'input_commitments', 'description', 'python')}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if record['spec_id'] != 'sampling_' + digest:
        raise ValueError('Confirmation sampling specification changed')
    if _hash(Path(record['sampler_source'])) != record['sampler_sha256']:
        raise ValueError('Confirmation sampling program changed')
    for source, digest in record['input_commitments'].items():
        if _hash(Path(source)) != digest:
            raise ValueError('Confirmation sampler input changed')
    return record


def active_sampling_registration(thread: Path) -> dict[str, Any] | None:
    records = [json.loads(path.read_text()) for path in (thread / 'production/protocol_revisions').glob('*/approved.json')]
    for record in sorted(records, key=lambda item: item['recorded_at'], reverse=True):
        if record.get('replacement_sampling_spec'):
            return record
        if record.get('replacement_holdout_bank'):
            return None
    return None
