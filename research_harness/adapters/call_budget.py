"""Optional local invocation budget for bounded live research checks.

Counts submitted prompt bytes and process launches, not billed tokens. A wall
clock deadline also prevents fresh calls after the bounded run ends.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path


def reserve_call(*, model: str, prompt: str, label: str) -> None:
    path = os.environ.get('RESEARCH_HARNESS_CALL_BUDGET')
    if not path:
        return
    size = len(prompt.encode('utf-8'))
    with Path(path).open('r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        budget = json.load(handle)
        calls = budget.get('calls', [])
        used = sum(call['prompt_bytes'] for call in calls)
        if (time.time() >= budget['deadline_epoch'] or len(calls) >= budget['max_calls']
                or used + size > budget['max_prompt_bytes']):
            raise ValueError('Bounded research call budget exhausted; checkpoint without claiming research completion.')
        if model != budget['model']:
            raise ValueError('Bounded research requires the configured model for every call.')
        calls.append({'model': model, 'label': label, 'prompt_bytes': size, 'reserved_at': time.time()})
        budget['calls'] = calls
        handle.seek(0)
        json.dump(budget, handle, indent=2)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
