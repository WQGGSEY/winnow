from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from research_harness.adapters.call_budget import reserve_call, record_failed_call
from research_harness.evaluation_vault import ensure_evaluation_vault

from research_harness.agent_runtime import (
    AgentEvent,
    AgentPrompt,
    AgentUsage,
    CompletionRequest,
    CompletionResult,
    ResearchHarnessMcp,
    RuntimeAuthResult,
    model_reasoning_effort,
)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
PopenFactory = Callable[..., subprocess.Popen[str]]


class CodexCliError(RuntimeError):
    pass


class CodexCliAdapter:
    def __init__(
        self,
        *,
        codex_path: str = "codex",
        runner: CommandRunner = subprocess.run,
        popen: PopenFactory = subprocess.Popen,
    ) -> None:
        self._codex_path = codex_path
        self._runner = runner
        self._popen = popen

    def auth_status(self, *, timeout_seconds: int = 10) -> RuntimeAuthResult:
        try:
            completed = self._runner(
                [self._codex_path, "login", "status"],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=_codex_environment(),
            )
        except FileNotFoundError:
            return RuntimeAuthResult(
                ok=False,
                mode="codex_cli_missing",
                reason="codex CLI was not found for the login status probe.",
            )
        except subprocess.TimeoutExpired:
            return RuntimeAuthResult(
                ok=False,
                mode="auth_status_timeout",
                reason="codex login status timed out.",
            )

        status = " ".join(
            part.strip()
            for part in (completed.stdout or "", completed.stderr or "")
            if part.strip()
        )
        if completed.returncode != 0:
            return RuntimeAuthResult(
                ok=False,
                mode="not_logged_in",
                reason="codex login status reports no active login.",
                details={"status": "not_logged_in"},
            )
        if "chatgpt" not in status.lower():
            return RuntimeAuthResult(
                ok=False,
                mode="non_chatgpt_auth",
                reason="codex login status is not using ChatGPT authentication.",
                details={"status": "logged_in", "auth_method": "other"},
            )
        return RuntimeAuthResult(
            ok=True,
            mode="chatgpt_login",
            details={"status": "logged_in", "auth_method": "chatgpt"},
        )

    def complete(self, request: CompletionRequest) -> CompletionResult:
        command = self._build_exec_command(request=request)
        remaining = reserve_call(model=request.model, prompt=self._compose_prompt(request.prompt), label=request.label)
        timeout = min(request.timeout_seconds, remaining) if remaining is not None else request.timeout_seconds
        try:
            runner = (lambda command, **kwargs: self._run_owned(command, event_log_path=request.event_log_path, **kwargs)) if self._runner is subprocess.run else self._runner
            completed = runner(
                command,
                input=self._compose_prompt(request.prompt),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=_codex_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            reason = f"{request.label}: codex CLI timed out after {timeout}s"
            record_failed_call(label=request.label, reason=reason, partial_output=exc.output or "")
            raise CodexCliError(reason) from exc
        if completed.returncode not in (0, None):
            record_failed_call(label=request.label, reason=f"CLI exited {completed.returncode}",
                               partial_output=completed.stdout or "")
            detail = " ".join(
                part.strip()
                for part in (completed.stderr or "", completed.stdout or "")
                if part.strip()
            )
            raise CodexCliError(
                f"{request.label}: codex CLI exited with code "
                f"{completed.returncode}: {detail[:400]}"
            )
        try:
            return self.parse_completion(completed.stdout or "", label=request.label)
        except CodexCliError as exc:
            record_failed_call(label=request.label, reason=str(exc), partial_output=completed.stdout or "")
            raise

    def _run_owned(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Persist events while the child runs, including when its caller is killed."""
        import uuid
        from contextlib import ExitStack
        from research_harness.adapters.process_tree import OwnedProcessTree

        token = uuid.uuid4().hex
        env = {**kwargs['env'], 'RESEARCH_HARNESS_SESSION': token}
        with ExitStack() as stack:
            trace = None
            if kwargs.get('event_log_path') is not None:
                path = Path(kwargs['event_log_path'])
                path.parent.mkdir(parents=True, exist_ok=True)
                trace = stack.enter_context(path.open('w+', encoding='utf-8', errors='replace'))
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                       stdout=trace if trace is not None else subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
            owned = OwnedProcessTree(process.pid, token)
            try:
                stdout, stderr = process.communicate(kwargs['input'], timeout=kwargs['timeout'])
                if trace is not None:
                    stdout = path.read_text(errors="replace")
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired as exc:
                if trace is not None:
                    exc.output = path.read_text(errors="replace")
                raise
            finally:
                owned.terminate(force=True)
                process.wait()

    def parse_completion(self, raw: str, *, label: str) -> CompletionResult:
        """Parse one Codex ``exec --json`` stream into its terminal result."""

        return self._parse_completion(raw, label=label)

    def start_session(
        self,
        *,
        prompt: AgentPrompt,
        model: str,
        cwd: Path,
        mcp: ResearchHarnessMcp,
        env: dict[str, str] | None = None,
    ) -> "CodexProcessSession":
        import uuid

        session_token = uuid.uuid4().hex
        session_env = dict(env or _codex_environment())
        session_env['RESEARCH_HARNESS_SESSION'] = session_token
        command = self._build_exec_command(
            request=None,
            model=model,
            cwd=cwd,
            mcp=mcp,
        )
        reserve_call(model=model, prompt=self._compose_prompt(prompt), label='research supervisor')
        process = self._popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            close_fds=True,
            start_new_session=True,
            env=session_env,
        )
        if process.stdin is None:
            process.terminate()
            raise CodexCliError("codex session did not expose stdin")
        process.stdin.write(self._compose_prompt(prompt))
        process.stdin.close()
        return CodexProcessSession(process, self, session_token=session_token)

    def build_worker_command(
        self,
        *,
        model: str,
        cwd: Path,
        output_schema: Path,
    ) -> list[str]:
        return self._build_exec_command(
            request=CompletionRequest(
                prompt=AgentPrompt(instructions="", input=""),
                model=model,
                output_schema=output_schema,
                cwd=cwd,
            )
        )

    def _build_exec_command(
        self,
        *,
        request: CompletionRequest | None,
        model: str | None = None,
        cwd: Path | None = None,
        mcp: ResearchHarnessMcp | None = None,
    ) -> list[str]:
        if request is not None:
            model = request.model
            cwd = request.cwd
        if not model:
            raise ValueError("Codex requests require a model")
        approval = ["--ask-for-approval", "never"]
        command = [
            self._codex_path,
        ]
        if os.environ.get('RESEARCH_HARNESS_CALL_BUDGET') or (request is not None and not request.allow_local_tools):
            command.extend(['--disable', 'multi_agent', '--disable', 'multi_agent_v2',
                            '--enable', 'skip_host_skill_discovery', '--disable', 'skill_search'])
        if request is not None and request.allow_web_search:
            command.append('--search')
        if request is not None and not request.allow_local_tools:
            for feature in (
                "shell_tool",
                "unified_exec",
                "js_repl",
                "code_mode",
                "code_mode_host",
                "view_image",
            ):
                command.extend(["--disable", feature])
        command.extend([
            *approval,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--json",
            "--model",
            model,
            "--skip-git-repo-check",
        ])
        effort = model_reasoning_effort(model)
        if effort is not None:
            command.extend(["-c", "model_reasoning_effort=" + _toml_string(effort)])
        vault = ensure_evaluation_vault()
        command.extend([
            "-c", 'default_permissions="research-development"',
            "-c", 'permissions.research-development.filesystem={\":root\"="read",'
            + _toml_string(str(vault)) + '="deny",'
            + _toml_string(str(Path.home() / '.agents/skills')) + '="deny",'
            + _toml_string(str(Path.home() / '.codex/skills')) + '="deny"}',
        ])
        if cwd is not None:
            command.extend(["--cd", str(cwd)])
        if request is not None and request.output_schema is not None:
            command.extend(["--output-schema", str(request.output_schema)])
        if mcp is not None:
            command.extend(
                [
                    "-c",
                    "mcp_servers.research_harness.command=" + _toml_string(mcp.command),
                    "-c",
                    "mcp_servers.research_harness.args=" + json.dumps(list(mcp.args)),
                    "-c",
                    'mcp_servers.research_harness.default_tools_approval_mode="approve"',
                    "-c",
                    f"mcp_servers.research_harness.tool_timeout_sec={mcp.tool_timeout_seconds}",
                ]
            )
            for key, value in sorted(mcp.environment.items()):
                command.extend(
                    [
                        "-c",
                        f"mcp_servers.research_harness.env.{key}={_toml_string(value)}",
                    ]
                )
        command.append("-")
        return command

    def _compose_prompt(self, prompt: AgentPrompt) -> str:
        instructions = prompt.instructions.strip()
        user_input = prompt.input.strip()
        if not instructions:
            return user_input
        return (
            "System instructions:\n"
            f"{instructions}\n\n"
            "User input:\n"
            f"{user_input}"
        )

    def _parse_completion(self, raw: str, *, label: str) -> CompletionResult:
        events = tuple(self._parse_jsonl(raw.splitlines()))
        final_text: str | None = None
        usage: AgentUsage | None = None
        thread_id: str | None = None
        for event in events:
            if event.kind == "message":
                final_text = event.summary
            if event.usage is not None:
                usage = event.usage
            raw_event = event.raw
            if raw_event.get("type") == "thread.started":
                candidate = raw_event.get("thread_id")
                if isinstance(candidate, str):
                    thread_id = candidate
        if final_text is None:
            raise CodexCliError(
                f"{label}: codex CLI JSONL did not contain a final agent message"
            )
        if usage is None:
            raise CodexCliError(f"{label}: codex CLI JSONL did not contain turn usage")
        return CompletionResult(
            text=final_text,
            usage=usage,
            thread_id=thread_id,
            events=events,
        )

    def _parse_jsonl(self, lines: Iterable[str]) -> Iterator[AgentEvent]:
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                yield AgentEvent(kind="diagnostic", summary=line[:400])
                continue
            if not isinstance(event, dict):
                yield AgentEvent(kind="diagnostic", summary=str(event)[:400])
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                yield AgentEvent(
                    kind="status",
                    summary=f"thread started {event.get('thread_id', '')}".strip(),
                    raw=event,
                )
            elif event_type == "turn.started":
                yield AgentEvent(kind="status", summary="turn started", raw=event)
            elif event_type == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    yield AgentEvent(kind="diagnostic", summary="completed item missing payload", raw=event)
                    continue
                item_type = item.get("type")
                if item_type == "agent_message" and isinstance(item.get("text"), str):
                    yield AgentEvent(kind="message", summary=item["text"], raw=event)
                elif item_type == "error":
                    message = str(item.get('message', 'CLI item error'))
                    notice = message.startswith('Under-development features enabled:')
                    yield AgentEvent(kind='status' if notice else 'diagnostic', summary=message, raw=event)
                elif item_type in {"mcp_tool_call", "command_execution", "tool_call"}:
                    name = item.get("server") or item.get("name") or item.get("tool") or item_type
                    yield AgentEvent(kind="tool", summary=str(name), raw=event)
                else:
                    yield AgentEvent(kind="status", summary=f"completed {item_type}", raw=event)
            elif event_type == "turn.completed":
                raw_usage = event.get("usage")
                usage = _parse_usage(raw_usage if isinstance(raw_usage, dict) else {})
                yield AgentEvent(kind="usage", summary="turn completed", usage=usage, raw=event)
            elif event_type in {"error", "turn.failed"}:
                message = event.get("message") or event.get("error") or event_type
                yield AgentEvent(kind="diagnostic", summary=str(message)[:400], raw=event)
            else:
                yield AgentEvent(kind="status", summary=str(event_type or "event"), raw=event)


class CodexProcessSession:
    def __init__(self, process: subprocess.Popen[str], adapter: CodexCliAdapter, *, session_token: str = '') -> None:
        from research_harness.adapters.process_tree import OwnedProcessTree

        self._process = process
        self._adapter = adapter
        self._owned_tree = OwnedProcessTree(process.pid, session_token)

    @property
    def pid(self) -> int:
        return self._process.pid

    def events(self) -> Iterator[AgentEvent]:
        if self._process.stdout is None:
            return
        yield from self._adapter._parse_jsonl(self._process.stdout)

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def terminate(self, *, force: bool = False) -> None:
        import signal

        self._owned_tree.terminate(force=force)
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(os.getpgid(self.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self._process.send_signal(sig)
            except ProcessLookupError:
                pass


def _parse_usage(raw: dict[str, Any]) -> AgentUsage:
    return AgentUsage(
        input_tokens=int(raw.get("input_tokens") or 0),
        cached_input_tokens=int(raw.get("cached_input_tokens") or 0),
        cache_write_input_tokens=int(raw.get("cache_write_input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        reasoning_output_tokens=int(raw.get("reasoning_output_tokens") or 0),
    )


def _codex_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_BASE_URL", None)
    return env


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
