"""Auto-resolver: hands-free policy layer for the strictness rails.

Detection rails (Rail 1/2/3/4/5) name what's wrong. This module decides what
to do about it automatically — so the operator's job is to set the user_goal
once and only re-engage on terminal events (publication-ready, honest_failure,
billing exhaustion, envelope underspec).

Each detection rail can attach an ``auto_action_suggestion`` dict to its
response::

    {
        "tool": "seed_alternative_root_formulation",
        "args": {"thread_id": "...", "formulation_id": "acf_feasibility_x"},
        "source_rail": "rail_5_must_revise_root",
        "rationale": "promoted node n_x collapsed (3/3 negatives) and a "
                     "feasibility-narrowed formulation is unseeded",
        "confidence": "high",
    }

This module exposes two pure functions:

* ``pick_auto_action(response)`` — extracts a suggestion if present and well-formed.
* ``chain_safety_check(thread_dir, suggestion)`` — refuses the suggestion if
  the auto-action history shows it would loop or exceed the per-thread budget.

The MCP handlers consult these inline; when both pass, the handler dispatches
the suggestion in the same call and merges the result into the response under
``auto_resolved``. Operator never sees the intermediate failure.

The history is persisted as JSONL at
``<thread_dir>/production/auto_actions.jsonl``; every line is one attempt
(succeeded or refused), so an audit log is always available."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Hard ceiling on total auto-actions per thread. Beyond this the resolver
# refuses to chain further — forces operator escalation instead of silent
# runaway. Set generously: each rail's auto-action should be a discrete pivot,
# not a polling loop.
MAX_AUTO_ACTIONS_PER_THREAD = 8

# How many recent entries to scan for loop-detection. If the same
# (tool, args-fingerprint) appears within this window AND the prior outcome
# wasn't "ok", we refuse to retry.
LOOP_DETECTION_WINDOW = 4

_VALID_CONFIDENCES = {"high", "medium", "low"}


@dataclass
class AutoAction:
    tool: str
    args: dict[str, Any]
    source_rail: str
    rationale: str
    confidence: str

    def fingerprint(self) -> str:
        """Stable identity for loop detection. Args are sorted by key."""
        return json.dumps(
            {"tool": self.tool, "args": self.args},
            sort_keys=True, ensure_ascii=False,
        )


@dataclass
class SafetyVerdict:
    ok: bool
    reason: str
    history_length: int = 0
    recent_fingerprints: list[str] = field(default_factory=list)


def pick_auto_action(response: dict[str, Any]) -> AutoAction | None:
    """Extract a well-formed auto_action_suggestion from a handler response.

    Returns None if no suggestion present or the suggestion is malformed.
    Caller is responsible for chain_safety_check before dispatching.
    """
    suggestion = response.get("auto_action_suggestion")
    if not isinstance(suggestion, dict):
        return None
    tool = suggestion.get("tool")
    args = suggestion.get("args")
    source_rail = suggestion.get("source_rail")
    rationale = suggestion.get("rationale")
    confidence = suggestion.get("confidence", "medium")
    if not isinstance(tool, str) or not tool:
        return None
    if not isinstance(args, dict):
        return None
    if not isinstance(source_rail, str) or not source_rail:
        return None
    if not isinstance(rationale, str):
        return None
    if confidence not in _VALID_CONFIDENCES:
        return None
    return AutoAction(
        tool=tool,
        args=args,
        source_rail=source_rail,
        rationale=rationale,
        confidence=confidence,
    )


def chain_safety_check(
    thread_dir: Path, suggestion: AutoAction
) -> SafetyVerdict:
    """Decide whether the suggestion is safe to auto-dispatch.

    Rules:
    - Refuse when total history >= MAX_AUTO_ACTIONS_PER_THREAD.
    - Refuse when the same fingerprint appears within LOOP_DETECTION_WINDOW
      AND the prior outcome was not "ok" (don't retry a failed action).
    - Refuse when confidence != "high" (medium/low signals "log only, no dispatch").
    """
    if suggestion.confidence != "high":
        return SafetyVerdict(
            ok=False,
            reason=f"auto_action confidence={suggestion.confidence!r}; only 'high' is auto-dispatched",
        )
    history = _read_history(thread_dir)
    if len(history) >= MAX_AUTO_ACTIONS_PER_THREAD:
        return SafetyVerdict(
            ok=False,
            reason=(
                f"thread already at MAX_AUTO_ACTIONS_PER_THREAD={MAX_AUTO_ACTIONS_PER_THREAD}; "
                "escalating to operator instead of chaining further"
            ),
            history_length=len(history),
        )
    recent = history[-LOOP_DETECTION_WINDOW:]
    fp = suggestion.fingerprint()
    for entry in recent:
        if entry.get("fingerprint") == fp and entry.get("outcome") != "ok":
            return SafetyVerdict(
                ok=False,
                reason=(
                    f"identical auto_action already attempted in last "
                    f"{LOOP_DETECTION_WINDOW} entries with non-ok outcome"
                ),
                history_length=len(history),
                recent_fingerprints=[e.get("fingerprint") for e in recent],
            )
    return SafetyVerdict(
        ok=True, reason="cleared safety check",
        history_length=len(history),
    )


def record_auto_action(
    thread_dir: Path,
    suggestion: AutoAction,
    *,
    outcome: str,
    dispatch_result: dict[str, Any] | None = None,
    refusal_reason: str | None = None,
) -> None:
    """Append a JSONL entry capturing what was attempted and how it went.

    outcome should be one of: "ok", "rejected", "refused_by_safety_check",
    "dispatch_error". dispatch_result is the raw handler return (if any);
    refusal_reason is the SafetyVerdict.reason when outcome=="refused_by_safety_check".
    """
    path = _history_path(thread_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "tool": suggestion.tool,
        "args": suggestion.args,
        "source_rail": suggestion.source_rail,
        "rationale": suggestion.rationale,
        "confidence": suggestion.confidence,
        "fingerprint": suggestion.fingerprint(),
        "outcome": outcome,
    }
    if refusal_reason is not None:
        entry["refusal_reason"] = refusal_reason
    if dispatch_result is not None:
        entry["dispatch_status"] = dispatch_result.get("status")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _history_path(thread_dir: Path) -> Path:
    return thread_dir / "production" / "auto_actions.jsonl"


def _read_history(thread_dir: Path) -> list[dict[str, Any]]:
    path = _history_path(thread_dir)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
