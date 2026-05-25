"""single_active_run lock — at most one live thread executes at a time.

In-process asyncio lock with thread_id holder tracking. Attempts to
acquire while held are **refused** (not queued) — the operator should
see "another thread is live; wait or stop it" rather than have actions
queue invisibly.

See ``CONTEXT.md`` → single_active_run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone


class LockBusyError(RuntimeError):
    """Raised when acquire() is attempted while another holder is live."""

    def __init__(self, holder: "LockHolder") -> None:
        super().__init__(
            f"single_active_run is held by thread_id={holder.thread_id} "
            f"phase={holder.phase} since {holder.acquired_at}"
        )
        self.holder = holder


@dataclass(frozen=True)
class LockHolder:
    thread_id: str
    phase: str
    acquired_at: str


class SingleActiveRunLock:
    """asyncio-based mutex that tracks which thread/phase holds it."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._holder: LockHolder | None = None

    @property
    def holder(self) -> LockHolder | None:
        return self._holder

    def held(self) -> bool:
        return self._holder is not None

    def acquire(self, thread_id: str, phase: str) -> "_LockContext":
        """Return an async context manager that refuses on contention."""
        return _LockContext(self, thread_id, phase)


class _LockContext:
    def __init__(self, parent: SingleActiveRunLock, thread_id: str, phase: str) -> None:
        self._parent = parent
        self._thread_id = thread_id
        self._phase = phase

    async def __aenter__(self) -> LockHolder:
        if self._parent._holder is not None:
            raise LockBusyError(self._parent._holder)
        # Attempt to grab the underlying lock without waiting.
        if self._parent._lock.locked():
            # Holder exists somewhere; treat as busy.
            raise LockBusyError(
                self._parent._holder
                or LockHolder("<unknown>", "<unknown>", _now())
            )
        await self._parent._lock.acquire()
        holder = LockHolder(self._thread_id, self._phase, _now())
        self._parent._holder = holder
        return holder

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._parent._holder = None
        self._parent._lock.release()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
