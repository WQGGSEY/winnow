"""Run one connector completion through the private Codex adapter."""

from __future__ import annotations

import json
from typing import Any

from research_harness.agent_runtime import AgentPrompt, AgentUsage, CompletionRequest
from research_harness.adapters.codex_cli import (
    CodexCliAdapter,
    CodexCliError,
    CommandRunner,
)

DEFAULT_TIMEOUT_SECONDS = 180


class ConnectorLLMError(RuntimeError):
    """Raised when a connector completion fails or returns unusable JSON."""


def call_agent_json(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    codex_path: str,
    runner: CommandRunner,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    label: str = "connector",
) -> tuple[dict[str, Any], AgentUsage]:
    try:
        result = CodexCliAdapter(codex_path=codex_path, runner=runner).complete(
            CompletionRequest(
                prompt=AgentPrompt(instructions=system_prompt, input=user_prompt),
                model=model,
                timeout_seconds=timeout_seconds,
                label=label,
                allow_local_tools=False,
            )
        )
    except CodexCliError as exc:
        raise ConnectorLLMError(str(exc)) from exc
    action_text = _strip_fence(result.text)
    try:
        parsed = json.loads(action_text)
    except json.JSONDecodeError as exc:
        raise ConnectorLLMError(
            f"{label}: model output was not parseable JSON: {action_text[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConnectorLLMError(f"{label}: model output must be a JSON object")
    return parsed, result.usage


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()
