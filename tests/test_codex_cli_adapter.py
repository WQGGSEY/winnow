from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from research_harness.agent_runtime import (
    AgentPrompt,
    CompletionRequest,
    ResearchHarnessMcp,
)
from research_harness.adapters.codex_cli import CodexCliAdapter, CodexCliError


def _jsonl(message: str = "done") -> str:
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread_123"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": message},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 13,
                        "cached_input_tokens": 7,
                        "cache_write_input_tokens": 3,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 2,
                    },
                }
            ),
        ]
    )


class RecordingRunner:
    def __init__(self, stdout: str, *, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            stdout=self.stdout,
            stderr="",
        )


@pytest.mark.parametrize("model,effort", [("gpt-5.6-sol", "low"), ("gpt-5.6-luna", "max")])
def test_one_shot_command_order_prompt_and_schema(tmp_path: Path, model: str, effort: str) -> None:
    runner = RecordingRunner(_jsonl())
    adapter = CodexCliAdapter(codex_path="/bin/codex", runner=runner)
    schema = tmp_path / "output.schema.json"

    adapter.complete(
        CompletionRequest(
            prompt=AgentPrompt(instructions="follow rules", input="do work"),
            model=model,
            output_schema=schema,
            cwd=tmp_path,
        )
    )

    command, kwargs = runner.calls[0]
    assert command[:8] == [
        "/bin/codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
    ]
    assert command[-1] == "-"
    assert f'model_reasoning_effort="{effort}"' in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--output-schema") + 1] == str(schema)
    assert kwargs["input"] == "System instructions:\nfollow rules\n\nUser input:\ndo work"


def test_one_shot_can_disable_every_local_inspection_tool(tmp_path: Path) -> None:
    runner = RecordingRunner(_jsonl())
    adapter = CodexCliAdapter(codex_path="/bin/codex", runner=runner)

    adapter.complete(
        CompletionRequest(
            prompt=AgentPrompt(instructions="use only input", input="{}"),
            model="gpt-5.6-sol",
            cwd=tmp_path,
            allow_local_tools=False,
        )
    )

    command, _ = runner.calls[0]
    disabled = {
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--disable"
    }
    assert disabled == {
        "shell_tool",
        "unified_exec",
        "js_repl",
        "code_mode",
        "code_mode_host",
        "view_image",
    }


def test_final_message_and_all_usage_fields_are_normalized() -> None:
    adapter = CodexCliAdapter(runner=RecordingRunner(_jsonl("final answer")))
    result = adapter.complete(
        CompletionRequest(
            prompt=AgentPrompt(instructions="", input="prompt"),
            model="gpt-5.6-sol",
        )
    )

    assert result.text == "final answer"
    assert result.thread_id == "thread_123"
    assert result.usage.as_dict() == {
        "input_tokens": 13,
        "cached_input_tokens": 7,
        "cache_write_input_tokens": 3,
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
    }


def test_missing_final_message_is_an_error() -> None:
    raw = json.dumps({"type": "turn.completed", "usage": {}})
    adapter = CodexCliAdapter(runner=RecordingRunner(raw))
    with pytest.raises(CodexCliError, match="final agent message"):
        adapter.complete(
            CompletionRequest(
                prompt=AgentPrompt(instructions="", input="prompt"),
                model="gpt-5.6-sol",
            )
        )


def test_auth_accepts_chatgpt_and_redacts_raw_status() -> None:
    runner = RecordingRunner("Logged in using ChatGPT\n")
    result = CodexCliAdapter(runner=runner).auth_status()

    assert result.ok is True
    assert result.mode == "chatgpt_login"
    assert result.details == {"status": "logged_in", "auth_method": "chatgpt"}
    assert runner.calls[0][0] == ["codex", "login", "status"]


def test_production_command_uses_automatic_review_and_per_call_mcp(tmp_path: Path) -> None:
    adapter = CodexCliAdapter(codex_path="codex")
    command = adapter._build_exec_command(
        request=None,
        model="gpt-5.6-sol",
        cwd=tmp_path,
        mcp=ResearchHarnessMcp(
            command="python",
            args=("-m", "research_harness.mcp_server", "--repo-root", str(tmp_path)),
            environment={"COIN_DATA_DIR": "/data/coin"},
        ),
    )

    assert command[:6] == [
        "codex",
        "--approve-for-me",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
    ]
    assert "--ask-for-approval" not in command
    assert "--sandbox" not in command
    assert 'model_reasoning_effort="low"' in command
    assert 'mcp_servers.research_harness.command="python"' in command
    assert 'mcp_servers.research_harness.args=["-m", "research_harness.mcp_server", "--repo-root", "' in " ".join(command)
    assert 'mcp_servers.research_harness.env.COIN_DATA_DIR="/data/coin"' in command
    assert command[-1] == "-"
