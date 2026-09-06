"""Linux process ownership across subprocess session boundaries."""
from __future__ import annotations

import os
import signal
from pathlib import Path


def _identity(pid: int) -> tuple[int, str] | None:
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[1]), fields[19]  # parent PID and kernel start time
    except (OSError, ValueError, IndexError):
        return None


class OwnedProcessTree:
    def __init__(self, pid: int, token: str) -> None:
        self.pid = pid
        self.token = token
        self.root_identity = _identity(pid)
        self.owned: dict[int, tuple[int, str]] = {}

    def terminate(self, *, force: bool = False) -> None:
        processes = {}
        marker = ('RESEARCH_HARNESS_SESSION=' + self.token).encode()
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                if path.stat().st_uid != os.getuid():
                    continue
                pid = int(path.name)
                identity = _identity(pid)
                if identity is None:
                    continue
                processes[pid] = identity
                if self.token and marker in (path / 'environ').read_bytes().split(b'\0'):
                    self.owned[pid] = identity
            except OSError:
                continue
        if self.root_identity is not None and processes.get(self.pid) == self.root_identity:
            self.owned[self.pid] = self.root_identity
        # Capture detached descendants before signalling any parent. Keep their
        # identities for escalation even if they become reparented after SIGTERM.
        changed = True
        while changed:
            changed = False
            for pid, identity in processes.items():
                parent = processes.get(identity[0])
                owned_parent = self.owned.get(identity[0])
                if pid not in self.owned and parent and owned_parent and parent[1] == owned_parent[1]:
                    self.owned[pid] = identity
                    changed = True
        for pid, identity in sorted(self.owned.items(), key=lambda item: item[0] == self.pid):
            current = _identity(pid)
            if current is None or current[1] != identity[1] or pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass
