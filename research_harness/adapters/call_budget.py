"""Optional local invocation budget for bounded live research checks.

Counts submitted prompt bytes and process launches, not billed tokens. A wall
clock deadline also prevents fresh calls after the bounded run ends.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
import tempfile
from pathlib import Path


class CallBudgetExhausted(ValueError):
    pass


def _write_budget(path: Path, budget: dict) -> None:
    # The sidecar lock remains stable across atomic replacements.
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(budget, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def reserve_call(*, model: str, prompt: str, label: str) -> float | None:
    path = os.environ.get('RESEARCH_HARNESS_CALL_BUDGET')
    if not path:
        return
    size = len(prompt.encode('utf-8'))
    budget_path = Path(path)
    with budget_path.with_suffix(budget_path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        budget = json.loads(budget_path.read_text())
        calls = budget.get('calls', [])
        used = sum(call['prompt_bytes'] for call in calls)
        if (budget.get('exhausted_at') or time.time() >= budget['deadline_epoch'] or len(calls) >= budget['max_calls']
                or used + size > budget['max_prompt_bytes']
                or size > budget.get('max_call_prompt_bytes', budget['max_prompt_bytes'])):
            budget['exhausted_at'] = time.time()
            budget['exhausted_label'] = label
            _write_budget(budget_path, budget)
            raise CallBudgetExhausted('Bounded research call budget exhausted; checkpoint without claiming research completion.')
        if model != budget['model']:
            raise ValueError('Bounded research requires the configured model for every call.')
        calls.append({'model': model, 'label': label, 'prompt_bytes': size, 'reserved_at': time.time()})
        budget['calls'] = calls
        _write_budget(budget_path, budget)
        return max(0.001, budget['deadline_epoch'] - time.time())


def record_failed_call(*, label: str, reason: str, partial_output: str | bytes = '') -> None:
    """A bounded check stops on transport failure instead of buying the same call again."""
    raw_path = os.environ.get('RESEARCH_HARNESS_CALL_BUDGET')
    if not raw_path:
        return
    path = Path(raw_path)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        budget = json.loads(path.read_text())
        receipt = path.parent / f'failed-call-{time.time_ns()}.jsonl'
        receipt.write_bytes(partial_output if isinstance(partial_output, bytes) else partial_output.encode())
        budget.update(exhausted_at=time.time(), exhausted_label=label,
                      failed_call={'reason': reason, 'partial_output_path': str(receipt.resolve()),
                                   'usage_status': 'unknown unless present in partial provider events'})
        _write_budget(path, budget)
