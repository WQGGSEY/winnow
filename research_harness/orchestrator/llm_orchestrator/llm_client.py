"""LLM client abstraction.

Two implementations:

- `MockLLMClient` — deterministic stand-in. Used by default and by every test.
  No network, no billing. The reasoning is real (it reads worker_report,
  critic_reviews, claim_contract) and produces structured decisions; it just
  doesn't call out to a model.

- `AnthropicLLMClient` — wraps anthropic SDK. Only constructible when the
  operator has both billing_ack and execution_ack environment variables set,
  mirroring the existing live_gate policy. The tree never auto-elevates to
  this client; settings.json picks the implementation.

Both share the same `chat(messages) -> LLMResponse` surface so Professor and
GradStudent code is identical regardless of backend.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class LLMResponse:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    structured: dict[str, Any] | None = None


class LLMClient:
    """Minimal chat interface; subclasses implement `_chat_impl`."""

    name: str = "abstract"

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None = None,
        json_schema_hint: str | None = None,
    ) -> LLMResponse:
        response = self._chat_impl(messages, system=system, json_schema_hint=json_schema_hint)
        if json_schema_hint and response.structured is None:
            response.structured = _try_parse_json(response.text)
        # Tag the response so downstream code + dialog logs know whether the
        # text came from real reasoning (anthropic) or a deterministic stub.
        response.raw.setdefault("backend", self.name)
        response.raw.setdefault("is_mock", self.name == "mock")
        return response

    def _chat_impl(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None,
        json_schema_hint: str | None,
    ) -> LLMResponse:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Mock implementation                                                          #
# --------------------------------------------------------------------------- #


MockHandler = Callable[[list[dict[str, str]], str | None, str | None], LLMResponse]


class MockLLMClient(LLMClient):
    """Deterministic LLM stand-in.

    `register(intent, handler)` lets Professor/GradStudent register one
    handler per logical task ("professor.reduce", "professor.respond",
    "grad_student.report"). The intent is passed via system prompt prefix
    `[intent:<name>]`. The harness invariants live in the handler signatures,
    not in the model: a mock that returns garbage will still go through
    schema validation downstream.
    """

    name = "mock"

    def __init__(self) -> None:
        self._handlers: dict[str, MockHandler] = {}

    def register(self, intent: str, handler: MockHandler) -> None:
        self._handlers[intent] = handler

    def _chat_impl(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None,
        json_schema_hint: str | None,
    ) -> LLMResponse:
        intent = _extract_intent(system or "")
        if intent and intent in self._handlers:
            return self._handlers[intent](messages, system, json_schema_hint)
        # Fallback: deterministic stub so the tree keeps moving instead of
        # hard-crashing on an unregistered intent.
        digest = hashlib.sha256(
            json.dumps(messages, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:8]
        return LLMResponse(
            text=f"[mock fallback for intent={intent or 'unknown'} hash={digest}]",
            raw={"intent": intent, "fallback": True},
        )


# --------------------------------------------------------------------------- #
# Real Anthropic implementation                                                #
# --------------------------------------------------------------------------- #


class AnthropicLLMClient(LLMClient):
    """Real anthropic SDK wrapper.

    Constructed only when both billing_ack and execution_ack env vars are set,
    matching the live_gate policy used by live_smoke_runner. Throws at
    construction time otherwise so the harness fails loudly if settings
    accidentally request a live client without operator approval.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        billing_ack_env: str,
        billing_ack_value: str,
        execution_ack_env: str = "anthropic_live_orchestrator_ack",
        execution_ack_value: str = "I_understand_costs",
    ) -> None:
        billing = os.environ.get(billing_ack_env)
        if billing != billing_ack_value:
            raise RuntimeError(
                f"AnthropicLLMClient requires {billing_ack_env}={billing_ack_value} "
                f"(got: {billing!r}); see live_gate policy"
            )
        execution = os.environ.get(execution_ack_env)
        if execution != execution_ack_value:
            raise RuntimeError(
                f"AnthropicLLMClient requires {execution_ack_env}={execution_ack_value} "
                f"(got: {execution!r}); operator must enable orchestrator mode"
            )
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "AnthropicLLMClient needs the `anthropic` python package installed"
            ) from exc
        self.model = model
        # Lazy import keeps mock-mode tests free of optional dependencies.
        from anthropic import Anthropic

        self._client = Anthropic()

    def _chat_impl(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None,
        json_schema_hint: str | None,
    ) -> LLMResponse:
        full_system = system or ""
        if json_schema_hint:
            full_system += (
                f"\n\nRespond ONLY as JSON matching this schema:\n{json_schema_hint}"
            )
        result = self._client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=full_system,
            messages=[
                {"role": m["role"], "content": m["content"]} for m in messages
            ],
        )
        # Concatenate text blocks.
        text = "".join(
            block.text for block in result.content if getattr(block, "type", "") == "text"
        )
        return LLMResponse(text=text, raw={"model": self.model})


# --------------------------------------------------------------------------- #
# Claude CLI subprocess implementation (uses subscription OAuth, no API key)   #
# --------------------------------------------------------------------------- #


class ClaudeCliLLMClient(LLMClient):
    """Invoke `claude -p ...` for every LLM call.

    Uses the same subscription OAuth path as the worker invoker
    (`claude_code_invoker.py`) — no ANTHROPIC_API_KEY needed, calls are
    billed against the operator's Pro/Max subscription.

    Trade-offs vs the SDK client:
      + No pip install, no API key.
      + Free under the operator's existing subscription.
      - Each call boots a CLI subprocess (~2-5s overhead).
      - No streaming.
    """

    name = "claude_cli"

    def __init__(
        self,
        *,
        model: str,
        billing_ack_env: str,
        billing_ack_value: str,
        execution_ack_env: str,
        execution_ack_value: str,
        claude_path: str = "claude",
        timeout_seconds: int = 180,
        max_budget_usd: str = "0.50",
    ) -> None:
        # Reuse the same ack-env contract as AnthropicLLMClient so settings
        # stays a single switch ("backend": "claude_cli" vs "anthropic").
        billing = os.environ.get(billing_ack_env)
        if billing != billing_ack_value:
            raise RuntimeError(
                f"ClaudeCliLLMClient requires {billing_ack_env}={billing_ack_value} "
                f"(got: {billing!r}); see live_gate policy"
            )
        execution = os.environ.get(execution_ack_env)
        if execution != execution_ack_value:
            raise RuntimeError(
                f"ClaudeCliLLMClient requires {execution_ack_env}={execution_ack_value} "
                f"(got: {execution!r}); operator must enable orchestrator mode"
            )
        self.model = model
        self.claude_path = claude_path
        self.timeout_seconds = int(timeout_seconds)
        self.max_budget_usd = str(max_budget_usd)

    def _chat_impl(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None,
        json_schema_hint: str | None,
    ) -> LLMResponse:
        import subprocess

        full_system = system or ""
        if json_schema_hint:
            full_system += (
                "\n\nRespond ONLY as a single JSON object (no markdown fences) "
                "matching this schema:\n" + json_schema_hint
            )
        # Concatenate the message turns into one user prompt. The CLI uses a
        # single-shot `-p` mode so prior assistant turns just get prepended.
        user_chunks: list[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "user":
                user_chunks.append(content)
            else:
                user_chunks.append(f"[{role}]\n{content}")
        user_prompt = "\n\n".join(user_chunks)

        cmd = [
            self.claude_path,
            "-p",
            "--model",
            self.model,
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",  # no tools: pure text completion
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--system-prompt",
            full_system,
            "--output-format",
            "json",
            "--input-format",
            "text",
            "--no-session-persistence",
            "--max-budget-usd",
            self.max_budget_usd,
        ]
        env = dict(os.environ)
        # Subscription OAuth path forbids ANTHROPIC_API_KEY (matches the
        # worker invoker policy).
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)  # subscription-only: strip base_url override too
        completed = subprocess.run(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env=env,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"claude CLI failed (rc={completed.returncode}): "
                f"{completed.stderr[:400]}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"claude CLI returned non-JSON stdout: {completed.stdout[:400]}"
            ) from exc
        text = ""
        if isinstance(payload, dict):
            text = str(payload.get("result") or "")
        return LLMResponse(
            text=text,
            raw={"model": self.model, "cli_subtype": payload.get("subtype")},
        )


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #


def build_llm_client(
    settings: dict[str, Any] | None,
    *,
    role: str = "default",
) -> LLMClient:
    """Pick an implementation from settings.runtime.llm_orchestrator.

    settings shape:
      runtime.llm_orchestrator:
        enabled: bool
        backend: "mock" | "anthropic"
        agent_models:
          default: "claude-sonnet-4-6"
          professor: "claude-opus-4-7"
          grad_student: "claude-sonnet-4-6"
          critic: "claude-sonnet-4-6"
          ac: "claude-opus-4-7"
        billing_ack_env / billing_ack_value
        execution_ack_env / execution_ack_value

    `role` picks which model to use ("professor" / "grad_student" / "critic" /
    "ac"). Falls back to `agent_models.default`, then to a sonnet default.

    Default: MockLLMClient when backend != "anthropic". Real anthropic only
    when both operator ack env vars are present.
    """
    cfg = (settings or {}).get("runtime", {}).get("llm_orchestrator", {}) or {}
    backend = str(cfg.get("backend", "mock")).lower()
    if backend == "mcp":
        # MCP mode: the harness does NOT call any LLM directly — Claude Code
        # interactive drives all reasoning via the MCP server (see
        # `research_harness.mcp_server`). Any code path that still asks for
        # a LLMClient inside the harness (e.g. during a CLI-driven test or
        # legacy production_runner call without MCP wired up) gets a mock
        # client so the codepath remains exercisable.
        return MockLLMClient()
    if backend == "claude_cli":
        agent_models = cfg.get("agent_models", {}) or {}
        model = str(
            agent_models.get(role)
            or agent_models.get("default")
            or "claude-sonnet-4-6"
        )
        return ClaudeCliLLMClient(
            model=model,
            billing_ack_env=str(cfg.get("billing_ack_env", "anthropic_orchestrator_billing_ack")),
            billing_ack_value=str(cfg.get("billing_ack_value", "I_authorize_anthropic_API_charges")),
            execution_ack_env=str(cfg.get("execution_ack_env", "anthropic_live_orchestrator_ack")),
            execution_ack_value=str(cfg.get("execution_ack_value", "I_understand_costs")),
            claude_path=str(cfg.get("claude_path", "claude")),
            timeout_seconds=int(cfg.get("timeout_seconds", 180)),
            max_budget_usd=str(cfg.get("max_budget_usd", "0.50")),
        )
    if backend == "anthropic":
        agent_models = cfg.get("agent_models", {}) or {}
        model = str(
            agent_models.get(role)
            or agent_models.get("default")
            or "claude-sonnet-4-6"
        )
        return AnthropicLLMClient(
            model=model,
            billing_ack_env=str(cfg.get("billing_ack_env", "anthropic_orchestrator_billing_ack")),
            billing_ack_value=str(cfg.get("billing_ack_value", "I_authorize_anthropic_API_charges")),
            execution_ack_env=str(cfg.get("execution_ack_env", "anthropic_live_orchestrator_ack")),
            execution_ack_value=str(cfg.get("execution_ack_value", "I_understand_costs")),
        )
    return MockLLMClient()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _extract_intent(system_prompt: str) -> str | None:
    marker = "[intent:"
    idx = system_prompt.find(marker)
    if idx < 0:
        return None
    end = system_prompt.find("]", idx)
    if end < 0:
        return None
    return system_prompt[idx + len(marker) : end].strip()


def _try_parse_json(text: str) -> dict[str, Any] | None:
    # Strip markdown code fences if present.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
