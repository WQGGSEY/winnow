"""Operator-prompt queue: file-based bidirectional channel.

The frontend renders pending prompts in the production phase view and POSTs
operator responses back. The Codex subprocess polls ``take_pending_response``
to consume them.

Storage is a single append-only JSONL at
``<thread_dir>/production/operator_prompts.jsonl``. Each entry is one of:

  - ``{event: "enqueued", event_id, kind, prompt, options, source_rail, created_at}``
  - ``{event: "responded", event_id, response, responded_at}``
  - ``{event: "consumed", event_id, consumed_at}``

The "current state" of a prompt is derived by folding the entries left-to-
right (latest wins). This keeps writes append-only and audit-friendly
without a separate state file.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_KINDS = {
    "decision_request",
    "context_request",
}


def queue_path(thread_dir: Path) -> Path:
    return thread_dir / "production" / "operator_prompts.jsonl"


def enqueue_prompt(
    thread_dir: Path,
    *,
    kind: str,
    prompt: str,
    options: list[str] | None = None,
    source_rail: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Append an ``enqueued`` entry. Returns the event_id and full record.

    Caller is responsible for choosing a stable event_id when they want
    idempotency across retries; passing None generates a fresh uuid hex.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    options = list(options or [])
    event_id = event_id or f"opr_{uuid.uuid4().hex[:12]}"
    existing = _fold(thread_dir).get(event_id)
    if existing is not None:
        expected = {"kind": kind, "prompt": prompt.strip(), "options": options, "source_rail": source_rail}
        if any(existing.get(key) != value for key, value in expected.items()):
            raise ValueError(f"event_id {event_id!r} already belongs to a different prompt")
        return existing
    record = {
        "event": "enqueued",
        "event_id": event_id,
        "kind": kind,
        "prompt": prompt.strip(),
        "options": options,
        "source_rail": source_rail,
        "created_at": _now(),
    }
    _append(thread_dir, record)
    return record


def submit_response(
    thread_dir: Path,
    *,
    event_id: str,
    response: str,
) -> dict[str, Any]:
    """Append a ``responded`` entry. Caller (frontend POST) supplies the
    operator's free-text response. ValueError if event_id is unknown or
    already consumed.
    """
    if not isinstance(response, str):
        raise ValueError("response must be a string")
    state = _fold(thread_dir)
    record = state.get(event_id)
    if record is None:
        raise ValueError(f"unknown event_id {event_id!r}")
    if record.get("status") == "consumed":
        raise ValueError(f"event {event_id!r} already consumed")
    entry = {
        "event": "responded",
        "event_id": event_id,
        "response": response,
        "responded_at": _now(),
    }
    _append(thread_dir, entry)
    return entry


def list_pending(thread_dir: Path) -> list[dict[str, Any]]:
    """Return prompts that are enqueued and not yet consumed.

    Each item is the original enqueue record augmented with current status
    ('pending' | 'responded') and the operator response if any.
    """
    state = _fold(thread_dir)
    pending: list[dict[str, Any]] = []
    for event_id, record in state.items():
        if record.get("status") == "consumed":
            continue
        pending.append(record)
    pending.sort(key=lambda r: r.get("created_at") or "")
    return pending


def list_responses(thread_dir: Path) -> list[dict[str, Any]]:
    """Research decisions survive delivery acknowledgements across sessions."""
    return sorted(
        (item for item in _fold(thread_dir).values() if "response" in item),
        key=lambda item: item.get("responded_at") or "",
    )


def take_pending_response(
    thread_dir: Path, *, event_id: str | None = None
) -> dict[str, Any] | None:
    """Consume one responded prompt. If event_id is given, target that one
    specifically; otherwise consume the oldest responded entry. Returns the
    record (with response) and writes a ``consumed`` entry, or None if
    nothing is ready.
    """
    state = _fold(thread_dir)
    candidates = [
        r for r in state.values()
        if r.get("status") == "responded"
        and (event_id is None or r.get("event_id") == event_id)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.get("created_at") or "")
    chosen = candidates[0]
    _append(
        thread_dir,
        {
            "event": "consumed",
            "event_id": chosen["event_id"],
            "consumed_at": _now(),
        },
    )
    return chosen


def _append(thread_dir: Path, record: dict[str, Any]) -> None:
    path = queue_path(thread_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _fold(thread_dir: Path) -> dict[str, dict[str, Any]]:
    """Read the JSONL log and fold into {event_id: current_record}.

    'current_record' is the enqueue record augmented with status + response
    + timestamps reflecting the latest event observed for that event_id.
    """
    path = queue_path(thread_dir)
    state: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return state
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        eid = entry.get("event_id")
        if not eid:
            continue
        ev = entry.get("event")
        if ev == "enqueued":
            state[eid] = {**entry, "status": "pending"}
        elif ev == "responded" and eid in state:
            state[eid].update({
                "status": "responded",
                "response": entry.get("response"),
                "responded_at": entry.get("responded_at"),
            })
        elif ev == "consumed" and eid in state:
            state[eid].update({
                "status": "consumed",
                "consumed_at": entry.get("consumed_at"),
            })
    return state


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
