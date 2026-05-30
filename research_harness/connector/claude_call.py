"""Shared subscription-only `claude` CLI call for the connector's LLM steps.

Mirrors the proven invocation in ``agents/grilling.py`` and
``agents/market_research.py`` (subscription pool only — ``ANTHROPIC_API_KEY``
is popped; injectable ``runner`` for tests). Every connector step that talks to
the model funnels through here, which is also where the [[firewall]] is
enforced *structurally*: this function only ever sees the ``system_prompt`` /
``user_prompt`` the caller hands it, so a P-blind step (reading, prune-1,
per-reading market) simply never passes P into its prompts and P cannot leak.

Each step returns a single JSON object as its inner result; this helper parses
and returns it together with a usage record.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable

CommandRunner = Callable[..., subprocess.CompletedProcess]

DEFAULT_TIMEOUT_SECONDS = 180


class ConnectorLLMError(RuntimeError):
    """Raised when a connector LLM call fails or returns an unusable payload."""


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def call_claude_json(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_budget: str,
    claude_path: str,
    runner: CommandRunner,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    label: str = "connector",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one `claude -p` round and return ``(parsed_inner_json, usage)``.

    The model is instructed (by the caller's prompts) to emit exactly one JSON
    object. ``label`` is only used to make errors say which step failed.
    """
    cmd = [
        claude_path,
        "-p",
        "--model",
        model,
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--system-prompt",
        system_prompt,
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--no-session-persistence",
        "--max-budget-usd",
        max_budget,
    ]
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        completed = runner(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConnectorLLMError(
            f"{label}: claude CLI timed out after {timeout_seconds}s"
        ) from exc
    if completed.returncode not in (0, None):
        raise ConnectorLLMError(
            f"{label}: claude CLI exit code {completed.returncode}: "
            f"{(completed.stderr or '').strip()[:200]}"
        )
    try:
        cli_result = json.loads(completed.stdout or "")
    except json.JSONDecodeError as exc:
        raise ConnectorLLMError(
            f"{label}: claude CLI did not return JSON: {(completed.stdout or '')[:200]!r}"
        ) from exc
    if not isinstance(cli_result, dict) or cli_result.get("type") != "result":
        raise ConnectorLLMError(f"{label}: claude CLI returned unexpected payload")
    if cli_result.get("is_error"):
        raise ConnectorLLMError(
            f"{label}: claude CLI reported error subtype {cli_result.get('subtype')!r}"
        )
    inner = cli_result.get("result")
    if not isinstance(inner, str) or not inner.strip():
        raise ConnectorLLMError(f"{label}: claude CLI returned empty assistant text")
    action_text = _strip_fence(inner.strip())
    try:
        parsed = json.loads(action_text)
    except json.JSONDecodeError as exc:
        raise ConnectorLLMError(
            f"{label}: model output was not parseable JSON: {action_text[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConnectorLLMError(f"{label}: model output must be a JSON object")
    usage_raw = cli_result.get("usage") if isinstance(cli_result.get("usage"), dict) else {}
    usage = {
        "cost_usd": float(cli_result.get("total_cost_usd") or 0.0),
        "input_tokens": int(usage_raw.get("input_tokens") or 0),
        "output_tokens": int(usage_raw.get("output_tokens") or 0),
    }
    return parsed, usage
