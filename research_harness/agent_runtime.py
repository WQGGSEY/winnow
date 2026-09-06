from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping


def model_reasoning_effort(model: str) -> str | None:
    """Explicit model/effort pairs authorized for the harness evaluation."""
    return {"gpt-5.6-sol": "low", "gpt-5.6-luna": "max"}.get(model)


def research_model() -> str:
    model = os.environ.get('RESEARCH_HARNESS_MODEL', 'gpt-5.6-sol')
    if model_reasoning_effort(model) is None:
        raise ValueError('RESEARCH_HARNESS_MODEL must select Sol low or Luna max.')
    return model


@dataclass(frozen=True)
class AgentUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
        }


@dataclass(frozen=True)
class AgentPrompt:
    instructions: str
    input: str


@dataclass(frozen=True)
class CompletionRequest:
    prompt: AgentPrompt
    model: str
    timeout_seconds: int = 180
    output_schema: Path | None = None
    cwd: Path | None = None
    label: str = "agent"
    allow_local_tools: bool = True
    allow_web_search: bool = False


@dataclass(frozen=True)
class CompletionResult:
    text: str
    usage: AgentUsage
    thread_id: str | None
    events: tuple["AgentEvent", ...] = ()


@dataclass(frozen=True)
class RuntimeAuthResult:
    ok: bool
    mode: str
    reason: str | None = None
    details: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEvent:
    kind: Literal["message", "tool", "status", "usage", "diagnostic"]
    summary: str
    usage: AgentUsage | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ResearchHarnessMcp:
    command: str
    args: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)
    tool_timeout_seconds: float = 1200.0

    @classmethod
    def from_settings(
        cls,
        repo_root: Path,
        settings: Mapping[str, Any],
    ) -> "ResearchHarnessMcp":
        raw = (
            settings.get("runtime", {})
            .get("llm_orchestrator", {})
            .get("mcp", {})
        )
        command = raw.get("server_command")
        args = raw.get("server_args")
        environment = raw.get("server_env", {})
        if not isinstance(command, str) or not command.strip():
            raise ValueError("runtime.llm_orchestrator.mcp.server_command must be a string")
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("runtime.llm_orchestrator.mcp.server_args must be a string list")
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValueError("runtime.llm_orchestrator.mcp.server_env must be a string map")
        resolved_args = tuple(arg.replace("{repo_root}", str(repo_root)) for arg in args)
        runner_timeouts = settings.get("runtime", {}).get("runner_timeouts", {})
        tool_timeout = max([600.0, *(float(value) for value in runner_timeouts.values())]) + 600.0
        return cls(
            command=command.replace("{repo_root}", str(repo_root)),
            args=resolved_args,
            environment=dict(environment),
            tool_timeout_seconds=tool_timeout,
        )
