"""Natural-language dialog log between Professor and GradStudent.

Every node run can carry zero or more dialog entries. They are written to
node_run_dir/dialog.json next to worker_report.json so the operator (and the
paper-html renderer) can read the conversation that produced the node's
verdict.

Each entry is small and structured (speaker, intent, text). The body text
is the LLM's natural-language reasoning, mirroring how a real advisor / grad
student email exchange would read.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DialogEntry:
    speaker: str  # "professor" | "grad_student"
    intent: str  # short tag like "task_brief", "concern", "response", "verdict"
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DialogLog:
    node_id: str
    entries: list[DialogEntry] = field(default_factory=list)

    def append(self, entry: DialogEntry) -> None:
        self.entries.append(entry)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "entries": [asdict(entry) for entry in self.entries],
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
