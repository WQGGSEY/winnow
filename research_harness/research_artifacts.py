"""Bounded, non-executing inspection for research agents."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def read_research_artifact(thread: Path, args: dict[str, Any], *, denied_paths: tuple[Path, ...] = (),
                           additional_roots: tuple[Path, ...] = ()) -> dict[str, Any]:
    root = thread.resolve()
    path = (root / args.get('path', '.')).resolve()
    roots = (root, *(item.resolve() for item in additional_roots))
    if not any(path.is_relative_to(item) for item in roots):
        raise ValueError('The artifact is outside the inspection roots.')
    denied = tuple(item.resolve() for item in denied_paths)
    if any(path.is_relative_to(item) for item in denied):
        raise ValueError('This artifact is excluded from the inspection.')
    start, limit = args.get('start_line', 1), args.get('max_lines', 120)
    if type(start) is not int or start < 1 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('Use start_line >= 1 and max_lines between 1 and 500.')
    if path.is_dir():
        entries = [item for item in sorted(path.iterdir()) if any(item.resolve().is_relative_to(scope) for scope in roots)
                   and not any(item.resolve().is_relative_to(excluded) for excluded in denied)]
        return {'path': str(path), 'entries': [
            {'name': item.name, 'kind': 'directory' if item.is_dir() else 'file'}
            for item in entries[:200]], 'truncated': len(entries) > 200}
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Choose a text artifact no larger than 32 MiB; inspect indexed receipts instead of full event logs.')
    data = path.read_bytes()
    text = data.decode('utf-8')
    result = {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}
    if 'json_pointer' in args:
        from research_harness.orchestrator.research_observations import _lookup
        pointer = args['json_pointer']
        if not isinstance(pointer, str) or (pointer and not pointer.startswith('/')):
            raise ValueError('json_pointer must be empty or start with /.')
        payload = json.loads(text)
        value = _lookup(payload, pointer) if pointer else payload
        if len(json.dumps(value, ensure_ascii=False).encode()) > 24000:
            raise ValueError('Selected JSON value is too large; select a child with json_pointer.')
        return {**result, 'json_pointer': args['json_pointer'], 'value': value}
    query = args.get('query')
    if query is not None and (not isinstance(query, str) or not query):
        raise ValueError('query must be a nonempty literal substring.')
    lines = text.splitlines()
    selected = []
    used = 0
    truncated = False
    for number, line in enumerate(lines, start=1):
        if number < start or (query is not None and query not in line):
            continue
        if len(selected) >= limit or used + len(line.encode()) > 24000:
            truncated = True
            break
        selected.append({'line': number, 'text': line})
        used += len(line.encode())
    return {**result, 'lines': selected, 'total_lines': len(lines), 'truncated': truncated,
            'next_start_line': selected[-1]['line'] + 1 if selected and truncated else None}
