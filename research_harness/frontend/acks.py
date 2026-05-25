"""subscription_ack + execute_ack + full_auto_mode policy.

State lives at ``<repo>/settings.local.json`` under the ``frontend`` key:

    {
      "frontend": {
        "subscription_ack_at": "2026-05-25T08:14:00Z",
        "full_auto_mode": false
      }
    }

This file is per-machine and is intentionally **not** checked into git.
The repo-wide ``settings.json`` stays untouched.

See ``CONTEXT.md`` → live_acks.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _local_settings_path(repo_root: Path) -> Path:
    return (repo_root / "settings.local.json").resolve()


def _load_local(repo_root: Path) -> dict[str, Any]:
    path = _local_settings_path(repo_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_local(repo_root: Path, data: dict[str, Any]) -> None:
    path = _local_settings_path(repo_root)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _frontend_section(local: dict[str, Any]) -> dict[str, Any]:
    section = local.get("frontend")
    if not isinstance(section, dict):
        section = {}
        local["frontend"] = section
    return section


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_state(repo_root: Path) -> dict[str, Any]:
    """Return the frontend section, never raising. Missing keys are absent."""
    local = _load_local(repo_root)
    return dict(_frontend_section(local))


def has_subscription_ack(repo_root: Path) -> bool:
    return bool(get_state(repo_root).get("subscription_ack_at"))


def grant_subscription_ack(repo_root: Path) -> str:
    """Persist that the operator has consented to live Claude calls.

    Returns the timestamp written. Idempotent: if already granted, the
    existing timestamp is preserved.
    """
    local = _load_local(repo_root)
    section = _frontend_section(local)
    if not section.get("subscription_ack_at"):
        section["subscription_ack_at"] = _now()
    _save_local(repo_root, local)
    return section["subscription_ack_at"]


def revoke_subscription_ack(repo_root: Path) -> None:
    local = _load_local(repo_root)
    section = _frontend_section(local)
    section.pop("subscription_ack_at", None)
    _save_local(repo_root, local)


def full_auto_mode(repo_root: Path) -> bool:
    return bool(get_state(repo_root).get("full_auto_mode"))


def set_full_auto_mode(repo_root: Path, enabled: bool) -> None:
    local = _load_local(repo_root)
    section = _frontend_section(local)
    section["full_auto_mode"] = bool(enabled)
    _save_local(repo_root, local)


def requires_modal(repo_root: Path) -> bool:
    """True iff the operator should see the per-phase execute_ack modal.

    Skipped when full_auto_mode is on. (subscription_ack is *not*
    bypassed by full_auto_mode — that's a deliberate safety choice.)
    """
    return not full_auto_mode(repo_root)


def make_execute_ack_record(phase: str, *, mode: str) -> dict[str, Any]:
    """Build the audit-log entry appended to thread.json.execute_acks."""
    if mode not in {"manual", "auto"}:
        raise ValueError(f"execute_ack mode must be manual or auto, got {mode!r}")
    return {"phase": phase, "at": _now(), "mode": mode}
