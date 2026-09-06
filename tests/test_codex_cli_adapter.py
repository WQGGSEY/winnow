from __future__ import annotations

import json
import subprocess
import shutil
import sys
from pathlib import Path

import pytest

from research_harness.agent_runtime import (
    AgentPrompt,
    CompletionRequest,
    ResearchHarnessMcp,
)
from research_harness.adapters.codex_cli import CodexCliAdapter, CodexCliError


def test_session_stop_kills_detached_descendant_and_preserves_unrelated_process(tmp_path):
    import os
    import time
    import uuid
    from research_harness.adapters.process_tree import OwnedProcessTree

    ready = tmp_path / 'child.pid'
    child_code = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'
    parent_code = ('import subprocess,sys,time; from pathlib import Path; '
                   f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}],start_new_session=True); '
                   f'Path({str(ready)!r}).write_text(str(p.pid)); time.sleep(60)')
    token = uuid.uuid4().hex
    parent = subprocess.Popen([sys.executable, '-c', parent_code], start_new_session=True,
                              env={**os.environ, 'RESEARCH_HARNESS_SESSION': token})
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    owned = OwnedProcessTree(parent.pid, token)
    child = None
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        child = int(ready.read_text())
        owned.terminate()
        parent.wait(timeout=3)
        owned.terminate(force=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            stat = Path('/proc') / str(child) / 'stat'
            if not stat.exists() or stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                break
            time.sleep(.01)
        else:
            pytest.fail('Detached descendant survived session cancellation')
        assert unrelated.poll() is None
    finally:
        owned.terminate(force=True)
        parent.wait(timeout=3)
        unrelated.kill()
        unrelated.wait(timeout=3)


@pytest.mark.parametrize('backend', ['codex', 'runner'])
def test_development_subprocess_cannot_read_evaluation_vault(tmp_path, monkeypatch, backend):
    from research_harness import evaluation_vault
    from research_harness.runner.local_runner import isolated_runner_command

    if shutil.which('codex' if backend == 'codex' else 'bwrap') is None:
        pytest.skip('Sandbox executable is unavailable')
    monkeypatch.setattr(evaluation_vault, 'evaluation_vault_root', lambda: tmp_path / 'private')
    private = evaluation_vault.ensure_evaluation_vault() / 'canary.txt'
    private.write_text('held-out canary')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    public = workspace / 'public.txt'
    public.write_text('development input')
    script = (
        'from pathlib import Path\n'
        f'assert Path({str(public)!r}).read_text() == "development input"\n'
        'try:\n'
        f'    Path({str(private)!r}).read_text()\n'
        'except (PermissionError, FileNotFoundError):\n'
        '    print("private read blocked")\n'
        'else:\n'
        '    raise AssertionError("private evaluation input exposed")\n'
    )
    command = [sys.executable, '-c', script]
    if backend == 'runner':
        command = isolated_runner_command(command, workspace)
    else:
        adapter_command = CodexCliAdapter()._build_exec_command(request=None, model='gpt-5.6-sol', cwd=workspace)
        profile = adapter_command[adapter_command.index('default_permissions="research-development"') - 1:adapter_command.index('--cd')]
        command = ['codex', 'sandbox', *profile, '--', *command]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'private read blocked'


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
    assert 'default_permissions="research-development"' in command
    assert "--skip-git-repo-check" in command
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


def test_production_command_uses_read_only_shell_and_per_call_mcp(tmp_path: Path) -> None:
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

    assert command[:7] == [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
    ]
    assert "--approve-for-me" not in command
    assert 'default_permissions="research-development"' in command
    assert 'model_reasoning_effort="low"' in command
    assert 'mcp_servers.research_harness.command="python"' in command
    assert 'mcp_servers.research_harness.default_tools_approval_mode="approve"' in command
    assert 'mcp_servers.research_harness.args=["-m", "research_harness.mcp_server", "--repo-root", "' in " ".join(command)
    assert 'mcp_servers.research_harness.env.COIN_DATA_DIR="/data/coin"' in command
    assert command[-1] == "-"


def test_mcp_transport_timeout_covers_configured_experiment(tmp_path: Path) -> None:
    settings = {"runtime": {"runner_timeouts": {"training": 900}, "llm_orchestrator": {"mcp": {"server_command": "python", "server_args": ["-m", "research_harness.mcp_server"]}}}}
    mcp = ResearchHarnessMcp.from_settings(tmp_path, settings)
    command = CodexCliAdapter()._build_exec_command(request=None, model="gpt-5.6-sol", cwd=tmp_path, mcp=mcp)
    assert mcp.tool_timeout_seconds > 900
    assert f"mcp_servers.research_harness.tool_timeout_sec={mcp.tool_timeout_seconds}" in command
