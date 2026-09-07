"""Prospective output bindings and per-prediction measurement support."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def observation_contract(decision: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    from research_harness.schemas.validator import load_schema, validate_schema
    required = decision.get('required_observations', [])
    bindings = plan.get('observation_bindings', {})
    validate_schema(load_schema('experiment_plan')['properties']['observation_bindings'], bindings)
    if set(bindings) != set(required):
        raise ValueError('Bind every required_observations name exactly once in experiment_plan.observation_bindings before review.')
    outputs = plan['expected_outputs']['metrics_files']
    for name, binding in bindings.items():
        path = Path(binding['artifact_path'])
        if path.is_absolute() or '..' in path.parts or binding['artifact_path'] not in outputs:
            raise ValueError('Observation binding must name a declared metrics file: ' + name)
        pointer = binding['json_pointer']
        if not pointer.startswith('/') or any('~' in part.replace('~0', '').replace('~1', '') for part in pointer.split('/')):
            raise ValueError('Observation binding requires a valid JSON pointer: ' + name)
    return bindings


def _lookup(payload: Any, pointer: str) -> Any:
    value = payload
    for part in pointer[1:].split('/'):
        key = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list):
            if not key.isdigit() or str(int(key)) != key:
                raise KeyError(key)
            value = value[int(key)]
        else:
            value = value[key]
    return value


def collect_observation_support(decision: dict[str, Any], plan: dict[str, Any],
                                metrics: dict[str, Any], thread: Path) -> dict[str, Any]:
    """Resolve declared values, never guess aliases, sum cells or infer scientific validity."""
    required = decision.get('required_observations', [])
    bindings = plan.get('observation_bindings', {})
    rows = {}
    for name in required:
        binding = bindings.get(name)
        row: dict[str, Any] = {'value': None, 'status': 'missing'}
        if binding is None:
            # Historical runs retain their original top-level contract.
            value = metrics.get(name)
            row['source'] = 'reported metrics.' + name
        else:
            workspace = Path(plan['workspace']).resolve()
            path = (workspace / binding['artifact_path']).resolve()
            path.relative_to(workspace)
            path.relative_to(thread.resolve())
            row.update(binding)
            row['artifact_path'] = str(path)
            value = None
            if path.is_file():
                raw = path.read_bytes()
                row['artifact_sha256'] = hashlib.sha256(raw).hexdigest()
                try:
                    value = _lookup(json.loads(raw), binding['json_pointer'])
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            row['status'] = 'missing' if value is None else 'invalid'
        else:
            row['value'] = value
            row['status'] = ('available' if binding and binding.get('value_kind') == 'measurement'
                             else 'available' if value > 0 else 'empty' if value == 0 else 'invalid')
        rows[name] = row
    missing = [name for name, row in rows.items() if row['status'] != 'available']
    return {'counts': {name: row['value'] for name, row in rows.items()},
            'observations': rows, 'missing_or_empty': missing,
            'evaluable': bool(rows) and not missing,
            'scope': 'Positive support counts and finite prospectively declared measurements permit interpretation of their dependent predictions only. They do not establish power, correctness, a causal effect or qualification.'}


def prediction_support(previous: dict[str, Any]) -> list[dict[str, Any]]:
    support = previous.get('outcome', {}).get('measurement_support')
    answered_analysis = (previous.get('decision', {}).get('kind') == 'analysis'
                         and previous.get('outcome', {}).get('execution_result') == 'analysis_completed')
    rows = []
    for index, prediction in enumerate(previous.get('decision', {}).get('alternatives', [])):
        dependencies = prediction.get('required_observations', previous['decision'].get('required_observations', []))
        counts = (support or {}).get('counts', {})
        observations = (support or {}).get('observations', {})
        unavailable = [name for name in dependencies
                       if (observations[name]['status'] != 'available' if name in observations else
                           isinstance(counts.get(name), bool) or not isinstance(counts.get(name), (int, float))
                           or not math.isfinite(counts[name]) or counts[name] <= 0)]
        rows.append({'alternative_index': index, 'required_observations': dependencies,
                     'missing_or_empty': unavailable,
                     'eligible_for_interpretation': True if answered_analysis else not unavailable and bool(dependencies) if support is not None else None,
                     'support_basis': 'answered_source_analysis' if answered_analysis else 'declared_measurement_counts'})
    return rows


def measurement_facts(path: Path, *, max_depth: int = 4) -> dict[str, Any]:
    """Expose complete shallow values instead of cutting raw JSON in the middle."""
    raw = path.read_bytes()
    facts: dict[str, Any] = {}
    deferred = []
    def visit(value: Any, pointer: str, depth: int) -> None:
        if isinstance(value, (dict, list)):
            if depth >= max_depth or len(facts) >= 120:
                deferred.append(pointer)
                return
            items = value.items() if isinstance(value, dict) else enumerate(value)
            for key, item in items:
                visit(item, pointer + '/' + str(key).replace('~', '~0').replace('/', '~1'), depth + 1)
        elif len(facts) < 120 and (not isinstance(value, str) or len(value) <= 500):
            facts[pointer] = value
        else:
            deferred.append(pointer)
    visit(json.loads(raw), '', 0)
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
            'facts': facts, 'deferred_paths': deferred,
            'scope': 'Raw reported values with exact JSON pointers, not verified scientific inferences. Existing-source analysis can inspect deferred paths without rerunning the experiment. Do not silently treat a field as another declared count.'}
