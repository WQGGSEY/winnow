# Candidate A — one runtime facade, one Codex adapter

## Usage (caller's view)

Callers describe prompts, output, and capabilities; they never build flags or JSONL.

```python
# grilling.py: bounded, tool-free text/JSON turn
runtime = create_agent_runtime(settings, run=command_runner)
reply = runtime.complete(CompletionRequest(
    prompt=AgentPrompt(instructions=system_prompt, input=user_prompt),
    model=settings.agent_model("grilling_agent"),
    output=JsonObject(),
    timeout_seconds=round_timeout_seconds,
))
action = reply.require_json_object()
usage.add(reply.usage)
```
```python
# connector/llm_call.py: the P-blind caller still controls prompt contents
reply = runtime.complete(CompletionRequest(
    prompt=AgentPrompt(instructions=system_prompt, input=user_prompt),
    model=model,
    output=JsonObject(),
    timeout_seconds=timeout_seconds,
))
return reply.require_json_object(), reply.usage.as_persisted_dict()
```
```python
# thread_supervisor.py: one production cycle with repository MCP, per invocation
session = runtime.start(SessionRequest(
    prompt=AgentPrompt(input=build_resume_prompt(repo, thread_id, cycle)),
    model=resolve_supervisor_model(repo, thread_id),
    cwd=repo,
    tools=ResearchHarnessMcp.from_settings(repo, resolved_settings),
    sandbox="workspace-write",
))
active_child["pid"] = session.pid
for event in session.events():
    append_event(log_path, event)       # provider-neutral event variants
exit_code = session.wait()
```
```python
# bounded live worker: the same adapter enforces the existing result schema
reply = runtime.complete(CompletionRequest(
    prompt=AgentPrompt(input=worker_prompt),
    model=worker_model,
    output=JsonObject(schema_path=worker_task_result_schema),
    cwd=workspace,
    sandbox="read-only",
    timeout_seconds=timeout,
))
validate_named_schema("worker_task_result", reply.require_json_object())
```

## Types and signatures

```python
# research_harness/agent_runtime.py — public, provider-neutral
@dataclass(frozen=True)
class AgentPrompt:
    input: str
    instructions: str | None = None
@dataclass(frozen=True)
class TextOutput: ...
@dataclass(frozen=True)
class JsonObject:
    schema_path: Path | None = None
OutputContract = TextOutput | JsonObject
@dataclass(frozen=True)
class AgentUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    # No fabricated USD cost: Codex JSONL does not report one.
@dataclass(frozen=True)
class ResearchHarnessMcp:
    command: tuple[str, ...]
    environment: Mapping[str, str]
    @classmethod
    def from_settings(
        cls, repo_root: Path, settings: ResolvedSettings
    ) -> "ResearchHarnessMcp": ...
@dataclass(frozen=True)
class CompletionRequest:
    prompt: AgentPrompt
    model: str
    output: OutputContract = TextOutput()
    cwd: Path | None = None
    sandbox: Literal["read-only", "workspace-write"] = "read-only"
    timeout_seconds: int = 180
@dataclass(frozen=True)
class SessionRequest:
    prompt: AgentPrompt
    model: str
    cwd: Path
    tools: ResearchHarnessMcp
    sandbox: Literal["read-only", "workspace-write"]
@dataclass(frozen=True)
class CompletionResult:
    text: str
    json_object: Mapping[str, object] | None
    usage: AgentUsage
    thread_id: str
    def require_json_object(self) -> dict[str, object]: ...
@dataclass(frozen=True)
class AgentEvent:
    kind: Literal["message", "tool", "status", "usage", "diagnostic"]
    summary: str
    usage: AgentUsage | None = None
class AgentSession(Protocol):
    @property
    def pid(self) -> int: ...
    def events(self) -> Iterator[AgentEvent]: ...
    def wait(self) -> int: ...
    def terminate(self, *, force: bool = False) -> None: ...
class AgentRuntime(Protocol):
    def auth_status(self) -> AuthStatus: ...
    def complete(self, request: CompletionRequest) -> CompletionResult: ...
    def start(self, request: SessionRequest) -> AgentSession: ...
def create_agent_runtime(
    settings: ResolvedSettings,
    *,
    run: CommandRunner = subprocess.run,
    popen: PopenFactory = subprocess.Popen,
) -> AgentRuntime: ...
```

```python
# research_harness/adapters/codex_cli.py — private transport boundary
class CodexCliRuntime(AgentRuntime):
    def auth_status(self) -> AuthStatus: ...
    def complete(self, request: CompletionRequest) -> CompletionResult: ...
    def start(self, request: SessionRequest) -> AgentSession: ...
def _build_exec_argv(request: CompletionRequest | SessionRequest) -> list[str]: ...
def _build_stdin(prompt: AgentPrompt) -> str: ...
def _parse_jsonl(lines: Iterable[str]) -> Iterator[AgentEvent]: ...
def _finalize(events: Iterable[AgentEvent], output: OutputContract) -> CompletionResult: ...
```

`_build_exec_argv` is the sole owner of Codex ordering and wire details: approval
flags precede `exec`; `--json` is always enabled; `--output-schema` is added only
for schema-constrained JSON; MCP command, args, and environment become invocation
config. `_parse_jsonl` is the sole owner of `item.completed` and
`turn.completed` shapes. Missing final message or usage is an adapter error.

## Module map

```text
agents/grilling.py ─┐
connector/llm_call.py ─┼─> agent_runtime.py ─> adapters/codex_cli.py ─> codex
thread_supervisor.py ─┤                         (command + JSONL authority)
workers/codex_invoker.py ┘
                                      └─ per-invocation mcp_server.py process
settings.json + settings_scoped.py ─> provider/model/MCP env configuration
schemas + frontend templates ───────> persisted/displayed Codex identity
```

Delete `connector/claude_call.py`, `workers/claude_code_invoker.py`, and Claude
stdout-ingest names after migrating their callers in the same wave. Do not leave
aliases or dual-provider branches on the active path. `mcp_server.py` remains
client-neutral and unchanged except where a displayed provider label is persisted.

## Rationale

The non-obvious split is between a provider-neutral caller contract and one private
Codex transport boundary. Grilling, connector, supervisor, and live worker need two
execution modes but the same prompt, output, usage, auth, and event vocabulary.
`complete` hides process execution, JSONL normalization, prompt composition (Codex
has no system-prompt flag), and schema enforcement. `start` exposes only the process
controls the supervisor genuinely owns: observe, wait, and terminate.

Validation occurs at the external boundary: settings become typed MCP configuration,
Codex events become `AgentEvent`, and JSON output is decoded before domain callers
see it, per boundary-discipline. The connector firewall remains structural because
the adapter receives only the prompt supplied by each P-blind step. The MCP command
and environment come from repository settings, never global client registration.

This is a deep interface: three runtime operations hide authentication probing,
argument ordering, prompt adaptation, subprocess environment, JSONL variants, final
message selection, token usage, schema flags, and MCP wiring. The adapter does not
own research state, supervisor retry policy, connector prompts, or worker validation.

## Synthesis decision

Candidate A uses one neutral facade and one Codex-only adapter. Per-feature wrappers
duplicate volatile CLI knowledge; a multi-provider layer preserves unwanted coupling.

## Tradeoffs accepted

- We accept `AgentSession` lifecycle methods to retain watchdog and retry policy.
- We accept composing instructions and input into one documented stdin prompt in
  exchange for honestly modeling Codex's lack of a system-prompt flag.
- We accept changing operational usage schemas to omit USD cost in exchange for not
  inventing a budget or cost signal Codex does not provide.
- We accept one explicit `ResearchHarnessMcp` capability type in exchange for making
  per-invocation tool authority visible and preventing global-registration drift.

## Alternatives considered

- **One Codex helper per feature:** locally simple, but callers must learn JSONL,
  approval ordering, usage extraction, and errors; shallow modules and leakage lose.
- **Keep Claude-shaped APIs and swap argv:** minimizes edits but exposes obsolete
  budgets, names, and envelope semantics; it hides migration work, not complexity.
- **General provider registry:** hides selection behind one facade but preserves
  unused Claude branches and configuration. With one required provider, this is a
  shallower interface than the direct Codex implementation behind neutral contracts.

## Open questions and risks

- Should persisted `total_cost_usd` become nullable or leave operational schemas?
- Which setting owns MCP environment, and must it allowlist variables to exclude secrets?
- Is `--approve-for-me` acceptable for production while bounded workers stay tool-free?
- Must startup fail when a configured Claude model lacks an explicit Codex mapping?
- Can stderr diagnostics be logged without treating them as JSONL protocol events?

## Next implementation step

Implement runtime and JSONL fixture tests, then migrate grilling as the first slice.
