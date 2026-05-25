"""research_thread CRUD on the filesystem.

Threads live under ``runs/threads/<thread_id>/`` with a denormalized
``thread.json`` index file at the root. The frontend sidebar reads
nothing but ``thread.json`` files to build its list — phase artifacts
are loaded lazily when their accordion panel is expanded.

See ``CONTEXT.md`` → research_thread, thread_id, thread.json.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PHASES: tuple[str, ...] = ("grilling", "market", "production")
PHASE_STATUSES: tuple[str, ...] = (
    "idle",
    "running",
    "awaiting_input",
    "complete",
    "failed",
)
OUTCOMES: tuple[str | None, ...] = (None, "accept", "reject", "inconclusive")


class ThreadError(ValueError):
    """Raised when a thread directory or thread.json is malformed."""


def threads_root(repo_root: Path) -> Path:
    return (repo_root / "runs" / "threads").resolve()


def new_thread_id() -> str:
    return "thread_" + uuid.uuid4().hex[:8]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_thread(
    repo_root: Path,
    *,
    user_goal: str,
    title: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Mint a fresh thread directory + thread.json. Returns the index dict."""

    if not user_goal or not user_goal.strip():
        raise ThreadError("user_goal must be a non-empty string")
    thread_id = thread_id or new_thread_id()
    root = threads_root(repo_root) / thread_id
    if root.exists():
        raise ThreadError(f"thread_id {thread_id} already exists at {root}")
    root.mkdir(parents=True, exist_ok=False)
    created = _now()
    index: dict[str, Any] = {
        "thread_id": thread_id,
        "title": (title or user_goal.strip())[:140],
        "created_at": created,
        "updated_at": created,
        "current_phase": "grilling",
        "phase_status": "idle",
        "outcome": None,
        "domain": None,
        "user_goal": user_goal.strip(),
        "execute_acks": [],
    }
    _write_index(root, index)
    return index


def load_thread(repo_root: Path, thread_id: str) -> dict[str, Any]:
    """Load and validate a thread.json. Raises ThreadError if malformed."""
    root = threads_root(repo_root) / thread_id
    index_path = root / "thread.json"
    if not index_path.exists():
        raise ThreadError(f"thread.json missing at {index_path}")
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ThreadError(f"thread.json at {index_path} is not valid JSON: {exc}") from exc
    _validate_index(data, source=str(index_path))
    return data


def list_threads(repo_root: Path) -> list[dict[str, Any]]:
    """Sidebar source: every readable thread.json under runs/threads/.

    Skips directories that have no thread.json or whose JSON fails
    validation — the sidebar must never crash because of one bad file.
    Results are sorted by ``updated_at`` descending so the freshest
    thread is at the top.
    """
    root = threads_root(repo_root)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        index_path = child / "thread.json"
        if not index_path.exists():
            continue
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            _validate_index(data, source=str(index_path))
        except (json.JSONDecodeError, ThreadError):
            continue
        out.append(data)
    out.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
    return out


DOMAIN_STATES: tuple[str, ...] = (
    "matched",
    "scaffolding",
    "scaffold_complete",
    "scaffold_failed",
)


def update_thread(
    repo_root: Path,
    thread_id: str,
    *,
    title: str | None = None,
    current_phase: str | None = None,
    phase_status: str | None = None,
    outcome: str | None = None,
    domain: str | None = None,
    domain_state: str | None = None,
    mcp_model: str | None = None,
    append_execute_ack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a partial update to thread.json. Returns the new index."""
    index = load_thread(repo_root, thread_id)
    if title is not None:
        index["title"] = title[:140]
    if current_phase is not None:
        if current_phase not in PHASES:
            raise ThreadError(f"unknown current_phase {current_phase!r}")
        index["current_phase"] = current_phase
    if phase_status is not None:
        if phase_status not in PHASE_STATUSES:
            raise ThreadError(f"unknown phase_status {phase_status!r}")
        index["phase_status"] = phase_status
    if outcome is not None:
        if outcome not in {"accept", "reject", "inconclusive"}:
            raise ThreadError(f"unknown outcome {outcome!r}")
        index["outcome"] = outcome
    if domain is not None:
        index["domain"] = domain
    if domain_state is not None:
        if domain_state not in DOMAIN_STATES:
            raise ThreadError(f"unknown domain_state {domain_state!r}")
        index["domain_state"] = domain_state
    if mcp_model is not None:
        if not mcp_model.strip():
            raise ThreadError("mcp_model must not be empty")
        index["mcp_model"] = mcp_model.strip()
    if append_execute_ack is not None:
        index.setdefault("execute_acks", []).append(append_execute_ack)
    index["updated_at"] = _now()
    _write_index(threads_root(repo_root) / thread_id, index)
    return index


def delete_thread(repo_root: Path, thread_id: str) -> None:
    """Remove a thread directory entirely from disk.

    Safety: callers must ensure the thread is not currently holding the
    [[single_active_run]] lock; deleting the run dir out from under a
    live agent worker would race with its file writes. The server route
    enforces this guard before invoking us.
    """
    root = threads_root(repo_root) / thread_id
    if not root.exists():
        raise ThreadError(f"thread {thread_id} does not exist at {root}")
    # Sanity: refuse to delete anything that doesn't look like a thread
    # directory (e.g. a stray symlink dropped into runs/threads/).
    if not (root / "thread.json").exists():
        raise ThreadError(
            f"refusing to delete {root}: no thread.json marker; this is "
            "not a recognised thread directory"
        )
    shutil.rmtree(root)


def phase_dir(repo_root: Path, thread_id: str, phase: str) -> Path:
    if phase not in PHASES:
        raise ThreadError(f"unknown phase {phase!r}")
    return threads_root(repo_root) / thread_id / phase


def boot_repair(repo_root: Path) -> list[str]:
    """Run on server start: heal threads that died mid-flight.

    Two transitions:

    1. ``phase_status == "running"`` and the on-disk artifact is still
       ``in_progress`` → demote to ``awaiting_input``. The agent task
       died with the process; surfacing a Resume button lets the
       operator pick the conversation back up.
    2. ``phase_status in {running, awaiting_input}`` and the on-disk
       artifact is ``aborted`` (or the artifact is missing entirely on
       a non-grilling phase) → demote to ``failed``. The previous
       launcher couldn't write phase_status=failed before the server
       went down, leaving the thread in a confusing orphan state with
       no path forward; flipping to failed lights up the Retry button.

    Returns the list of thread ids that were repaired.
    """
    repaired: list[str] = []
    for index in list_threads(repo_root):
        tid = index["thread_id"]
        phase = index.get("current_phase")
        ps = index.get("phase_status")
        artifact_status = _read_phase_artifact_status(
            repo_root, tid, phase
        ) if phase else None
        # Case 1: thread says running/awaiting_input but the artifact says
        # aborted — demote to failed so the Retry button lights up.
        if ps in {"running", "awaiting_input"} and artifact_status == "aborted":
            update_thread(repo_root, tid, phase_status="failed")
            repaired.append(tid)
            continue
        # Case 2: thread says running but the artifact is still in_progress —
        # the launcher died between create_task and the worker writing
        # complete. Demote to awaiting_input so the Resume button surfaces.
        if ps == "running":
            update_thread(repo_root, tid, phase_status="awaiting_input")
            repaired.append(tid)
            continue
        # Case 3: thread says idle/complete/failed but the artifact is still
        # in_progress and has a pending_ask. This happens when a prior bug
        # set phase_status=idle (e.g. spurious LockBusyError) while the
        # session was alive. Promote to awaiting_input so the operator can
        # Resume / reconnect instead of being stuck without any button.
        if ps in {"idle", "complete", "failed"} and artifact_status == "in_progress":
            if _phase_artifact_has_pending(repo_root, tid, phase):
                update_thread(repo_root, tid, phase_status="awaiting_input")
                repaired.append(tid)
    return repaired


def _phase_artifact_has_pending(
    repo_root: Path, thread_id: str, phase: str | None
) -> bool:
    """Check whether the phase artifact has a pending_ask waiting for reply."""
    if not phase:
        return False
    primary = {"grilling": "grilling_session.json"}.get(phase)
    if primary is None:
        return False
    fp = threads_root(repo_root) / thread_id / phase / primary
    if not fp.exists():
        return False
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("pending_ask"))


def _read_phase_artifact_status(
    repo_root: Path, thread_id: str, phase: str
) -> str | None:
    """Best-effort read of the ``status`` field on the phase's primary
    artifact. Returns None if the file is missing or unreadable.

    Currently only grilling has a status field. Market and production
    indicate success via the presence of their summary artifact, not a
    status field — for those phases boot_repair only handles the simple
    running→awaiting_input transition.
    """
    primary = {
        "grilling": "grilling_session.json",
    }.get(phase)
    if primary is None:
        return None
    pdir = threads_root(repo_root) / thread_id / phase
    fp = pdir / primary
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8")).get("status")
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- helpers


def _write_index(root: Path, index: dict[str, Any]) -> None:
    """Validate and atomically write thread.json.

    Atomicity matters: two HTTP requests can read thread.json while a third
    is updating it (e.g. the agent worker thread bumping phase_status while
    a reply POST is validating the thread). A non-atomic write briefly
    exposes a truncated / empty file and the reader raises JSONDecodeError.
    Write to a sibling temp file and rename() — os.replace is atomic on
    POSIX and on Windows since 3.3.
    """
    _validate_index(index, source=str(root / "thread.json"))
    target = root / "thread.json"
    payload = json.dumps(index, indent=2, sort_keys=True) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        prefix=".thread.", suffix=".json.tmp", dir=str(root)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


_REQUIRED_KEYS: tuple[str, ...] = (
    "thread_id",
    "title",
    "created_at",
    "updated_at",
    "current_phase",
    "phase_status",
    "user_goal",
)


def _validate_index(index: Any, *, source: str) -> None:
    if not isinstance(index, dict):
        raise ThreadError(f"{source}: top-level must be an object")
    for key in _REQUIRED_KEYS:
        if key not in index:
            raise ThreadError(f"{source}: missing required field {key!r}")
    if not isinstance(index["thread_id"], str) or not index["thread_id"].startswith("thread_"):
        raise ThreadError(f"{source}: thread_id must be a string starting with 'thread_'")
    if index["current_phase"] not in PHASES:
        raise ThreadError(f"{source}: unknown current_phase {index['current_phase']!r}")
    if index["phase_status"] not in PHASE_STATUSES:
        raise ThreadError(f"{source}: unknown phase_status {index['phase_status']!r}")
    outcome = index.get("outcome")
    if outcome not in {None, "accept", "reject", "inconclusive"}:
        raise ThreadError(f"{source}: unknown outcome {outcome!r}")


def iter_phase_artifacts(repo_root: Path, thread_id: str, phase: str) -> Iterable[Path]:
    """Yield top-level files inside the given phase directory, if any."""
    pdir = phase_dir(repo_root, thread_id, phase)
    if not pdir.exists():
        return []
    return sorted(p for p in pdir.iterdir() if p.is_file())
