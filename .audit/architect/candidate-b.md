# Candidate B: Codex runtime boundary

## Caller usage

### One-shot JSON completion for grilling

```python
from research_harness.agent_runtime import JsonCompletionRequest, CodexRuntime

runtime = CodexRuntime.from_settings(settings, codex_path=detected_codex, runner=runner)
result = runtime.complete_json(
    JsonCompletionRequest(
        label="grilling",
        model=resolve_agent_model(settings, "grilling_agent"),
        system_prompt=_grilling_system_prompt(allowed_domains),
        user_prompt=_grilling_user_prompt(user_goal, rounds, force_extract=False),
        timeout_seconds=round_timeout_seconds,
    )
)

action = result.payload
usage["total_input_tokens"] += result.usage.input_tokens
usage["total_output_tokens"] += result.usage.output_tokens
```

### Connector JSON calls

```python
from research_harness.agent_runtime import JsonCompletionRequest, CodexRuntime

runtime = CodexRuntime.from_settings(settings, codex_path=codex_path, runner=runner)
parsed, usage = runtime.complete_json(
    JsonCompletionRequest(
        label="connector.reading",
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        timeout_seconds=180,
    )
).as_legacy_tuple()
```

### Production supervisor session with MCP

```python
from research_harness.agent_runtime import CodexRuntime, ProductionSessionRequest

runtime = CodexRuntime.from_settings(resolve_for_thread(repo, tid), codex_path="codex")
exit_code = runtime.run_production_session(
    ProductionSessionRequest(
        thread_id=tid,
        repo_root=repo,
        model=resolve_supervisor_model(repo, tid),
        prompt=build_resume_prompt(repo, tid, cycle),
        log_path=tdir / "codex_subprocess.log",
        active_child_ref=active_child,
        state_path=state_path,
        stall_timeout_seconds=stall_timeout,
        experiment_hard_cap_seconds=experiment_hard_cap,
    )
)
```

### Bounded worker envelope

```python
from research_harness.agent_runtime import CodexRuntime, WorkerInvocationRequest

runtime = CodexRuntime.from_settings(settings, codex_path="codex")
envelope = runtime.build_worker_invocation(
    WorkerInvocationRequest(
        node=node,
        workspace=workspace,
        repo_root=repo_root,
        worker_task=worker_task,
        output_schema_path=repo_root / "research_harness/schemas/worker_task_result.schema.json",
        live=False,
    )
)
```

## Types and signatures

```python
# research_harness/agent_runtime/types.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
import subprocess

CommandRunner = Callable[..., subprocess.CompletedProcess]
Backend = Literal["codex_dry_run", "codex_live"]

@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    cost_usd: float = 0.0

@dataclass(frozen=True)
class TextCompletionResult:
    text: str
    usage: TokenUsage
    raw_events: tuple[dict[str, Any], ...] = ()

@dataclass(frozen=True)
class JsonCompletionResult:
    payload: dict[str, Any]
    text: str
    usage: TokenUsage
    raw_events: tuple[dict[str, Any], ...] = ()

    def as_legacy_tuple(self) -> tuple[dict[str, Any], dict[str, Any]]: ...

@dataclass(frozen=True)
class RuntimeSettings:
    auth_provider: Literal["codex"]
    default_backend: Backend | Literal["mock"]
    mcp_server_command: tuple[str, ...]
    mcp_server_env: dict[str, str]
    worker_live_model: str
    worker_dry_run_model: str
    worker_sandbox: str = "read-only"

@dataclass(frozen=True)
class JsonCompletionRequest:
    label: str
    model: str
    system_prompt: str
    user_prompt: str
    timeout_seconds: int
    output_schema_path: Path | None = None

@dataclass(frozen=True)
class ProductionSessionRequest:
    thread_id: str
    repo_root: Path
    model: str
    prompt: str
    log_path: Path | None
    active_child_ref: dict[str, int | None] | None
    state_path: Path | None
    stall_timeout_seconds: float
    experiment_hard_cap_seconds: float | None

@dataclass(frozen=True)
class WorkerInvocationRequest:
    node: dict[str, Any]
    workspace: Path
    repo_root: Path
    worker_task: dict[str, Any]
    output_schema_path: Path
    live: bool

# research_harness/agent_runtime/codex.py
class CodexRuntime:
    @classmethod
    def from_settings(
        cls,
        settings: dict[str, Any],
        *,
        codex_path: str = "codex",
        runner: CommandRunner = subprocess.run,
    ) -> "CodexRuntime": ...

    def auth_preflight(self, *, probe_cli_status: bool = False) -> AuthPreflightResult: ...
    def complete_text(self, request: JsonCompletionRequest) -> TextCompletionResult: ...
    def complete_json(self, request: JsonCompletionRequest) -> JsonCompletionResult: ...
    def build_worker_invocation(self, request: WorkerInvocationRequest) -> dict[str, Any]: ...
    def run_production_session(self, request: ProductionSessionRequest) -> int: ...

def build_codex_exec_command(
    *,
    codex_path: str,
    model: str,
    prompt_from_stdin: bool = True,
    output_schema_path: Path | None = None,
    cwd: Path | None = None,
    sandbox: str | None = "read-only",
    approve_for_me: bool = False,
    mcp_server_command: tuple[str, ...] = (),
    mcp_server_env: dict[str, str] | None = None,
) -> list[str]: ...

def parse_codex_jsonl(raw_stdout: str, *, label: str) -> TextCompletionResult: ...
def strip_json_fence(text: str) -> str: ...
```

## Module map

- `research_harness/agent_runtime/types.py`: provider-neutral request/result dataclasses, `TokenUsage`, and `RuntimeSettings`.
- `research_harness/agent_runtime/codex.py`: the only module that knows Codex CLI flags, JSONL event names, auth-status probing, Anthropic env scrubbing, output-schema flags, and per-invocation MCP config.
- `research_harness/agent_cli.py`: temporary compatibility import surface that re-exports `build_codex_exec_command`, `parse_codex_jsonl`, and `CodexRuntime.complete_text` helpers for a short migration. Delete once active callers move.
- `research_harness/agents/grilling.py`: owns the grilling loop and JSON action validation only. Replace `_call_claude_round` with `_call_agent_round(runtime, ...)`.
- `research_harness/connector/llm_call.py`: renamed from `connector/claude_call.py`; owns connector-specific JSON-object expectations and firewall-preserving prompts, but delegates execution/parsing to `CodexRuntime.complete_json`.
- `research_harness/thread_supervisor.py`: keeps lifecycle, locks, terminal checks, stall watchdog, and resume prompt. Replace `spawn_claude_session` with `spawn_codex_session` or a thin call to `CodexRuntime.run_production_session`.
- `research_harness/workers/codex_invoker.py`: renamed bounded-worker builder. It owns worker prompt/workspace policy and asks `CodexRuntime.build_worker_invocation` for command shape.
- `research_harness/workers/live_gate*.py` and `schemas/invocation_envelope.schema.json`: accept only `codex_dry_run`/`codex_live`; no `claude_code_live` live gate path remains.
- `settings_scoped.py` and `settings.json`: move provider enum/defaults from Claude names to Codex names; keep research-domain schemas unchanged unless a field persists provider identity.

## Rationale

The active flow is `grilling -> connector -> production`; all three need the same runtime facts: how to call Codex, how to parse JSONL final text and usage, how to scrub Anthropic env, and how to pass the repository MCP server per invocation. Candidate B concentrates those facts in `CodexRuntime`, then leaves each caller with its domain responsibility. That gives a deeper interface than the current scattered helpers: grilling no longer knows CLI contracts, connector no longer exposes Claude names, supervisor no longer reads `~/.claude.json`, and worker envelopes no longer carry stale provider labels.

Validation lives at the shell boundary. `RuntimeSettings.from_settings` parses repository settings into typed fields, `build_codex_exec_command` serializes only those fields to CLI args, and `parse_codex_jsonl` normalizes Codex events into `TextCompletionResult`. Inside the pipeline, callers trust `TokenUsage` and parsed JSON payloads. This follows boundary discipline and avoids leaking Codex event shapes into grilling, connector, supervisor, or workers.

The MCP invariant has one source of truth: `settings.runtime.llm_orchestrator.mcp.server_command` plus a new sibling env map such as `settings.runtime.llm_orchestrator.mcp.server_env`. The supervisor supplies both through `codex exec -c mcp_servers.research_harness...` on every production session. It does not depend on global Codex registration and does not read another client config.

The public surface is intentionally small: `complete_text`, `complete_json`, `build_worker_invocation`, and `run_production_session`. Behind that surface sit CLI flag ordering, JSONL parsing, schema-constrained output, MCP injection, auth probing, and process watchdog integration. Callers still own prompts, schemas, persistence, and domain validation, because those are not runtime concerns.

## Tradeoffs

- We accept one new runtime package in exchange for deleting Claude coupling from four active paths with one shared command/parser source.
- We accept a compatibility re-export in `agent_cli.py` during the migration in exchange for ordered, testable caller moves; it should be deleted in the same migration wave once active callers are moved.
- We accept keeping supervisor lifecycle code in `thread_supervisor.py` in exchange for not forcing the runtime adapter to own thread terminal policy, locks, or publication semantics.
- We accept preserving legacy tuple returns behind `JsonCompletionResult.as_legacy_tuple()` temporarily in exchange for migrating connector call sites without changing their JSON semantics.
- We accept that `cost_usd` is always `0.0` for Codex JSONL unless Codex starts reporting cost, in exchange for not pretending the removed USD budget flag enforces a limit.

## Alternatives

- Rename files and patch strings in place. This hides little complexity: every caller would still know Codex CLI flags, JSONL parsing, env scrubbing, and usage normalization. It loses on interface depth.
- Keep `agent_cli.py` as a bag of functions and teach each caller the missing production/session cases. This hides command construction but exposes orchestration policy and MCP injection details to supervisor and worker callers. It also invites pass-through helpers.
- Define a generic multi-provider `AgentRuntime` with Claude and Codex implementations. This looks flexible but preserves an inactive Claude axis in the active new-thread path, contradicts the migration goal, and leaks provider negotiation into settings and tests.
- Move all prompting and validation into the runtime adapter. This would hide too much: connector firewall policy, grilling action semantics, and worker scope locks are domain invariants, not CLI invariants.

## Risks and open questions

- Should `settings.runtime.llm_orchestrator.mcp.server_env` be project-only, operator-only, or merged like data adapters? The grounding says repository settings, but not which scope owns machine-local paths.
- Can the frontend tolerate immediate provider enum renames from `claude_code_live` to `codex_live`, or does it need a one-time settings migration for existing threads?
- `codex exec --approve-for-me` completed the MCP call in probes, while `approval_policy=never` refused it. Which production permission mode should be recorded in logs so operators can audit that choice?
- The worker live gate schema already permits `codex_live`, while validation still expects `claude_code_live`; this mismatch should be fixed before any live-worker acceptance test.
- Existing tests import `ClaudeCodeInvoker` and `call_claude_json`; they should move to behavior names, or the stale imports will keep Claude coupling alive even after runtime behavior changes.

## Next implementation step

Create `research_harness/agent_runtime/` with `RuntimeSettings`, `CodexRuntime.complete_text`, `complete_json`, `build_codex_exec_command`, and `parse_codex_jsonl`, then migrate grilling and connector first because their tests can prove normalized final text, JSON parsing, and usage without starting the supervisor.
