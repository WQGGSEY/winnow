from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping


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
        return cls(
            command=command.replace("{repo_root}", str(repo_root)),
            args=resolved_args,
            environment=dict(environment),
        )
