"""Tests for the shared connector LLM call + abstraction/firewall (Slice B)."""

from __future__ import annotations

import json
import subprocess

import pytest

from research_harness.connector.abstraction import (
    extract_domain_terms,
    generate_abstraction,
    scan_for_leak,
)
from research_harness.connector.codex_call import (
    ConnectorLLMError,
    call_agent_json,
)


def _envelope(inner_obj, *, is_error=False, subtype=None, cost=0.01):
    del cost
    if is_error:
        return json.dumps({"type": "turn.failed", "error": subtype or "error"})
    text = json.dumps(inner_obj) if inner_obj is not None else ""
    return "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 22}}),
        ]
    )


class FakeClaude:
    """Records each invocation and returns queued inner JSON objects in order."""

    def __init__(self, inner_objs, *, returncode=0, raw_stdout=None):
        self._queue = list(inner_objs)
        self._returncode = returncode
        self._raw_stdout = raw_stdout
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "input": kwargs.get("input"), "env": kwargs.get("env")})
        if self._raw_stdout is not None:
            stdout = self._raw_stdout
        else:
            stdout = _envelope(self._queue.pop(0))
        return subprocess.CompletedProcess(cmd, self._returncode, stdout, "")


# ----------------------------------------------------------- claude_call


def test_call_agent_json_parses_inner_and_pops_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://injected.example/proxy")
    fake = FakeClaude([{"abstraction": "x", "domain_terms_stripped": []}])
    parsed, usage = call_agent_json(
        system_prompt="sys",
        user_prompt="usr",
        model="m",
        codex_path="codex",
        runner=fake,
    )
    assert parsed["abstraction"] == "x"
    assert usage.input_tokens == 11 and usage.output_tokens == 22
    # subscription-only: BOTH the API key and base-URL override must be stripped
    # from the child env so the call uses the default subscription path.
    assert "ANTHROPIC_API_KEY" not in fake.calls[0]["env"]
    assert "ANTHROPIC_BASE_URL" not in fake.calls[0]["env"]


def test_call_agent_json_raises_on_cli_error_event():
    fake = FakeClaude([None])
    fake._queue = []  # force raw envelope path below
    fake._raw_stdout = _envelope({"x": 1}, is_error=True, subtype="budget")
    with pytest.raises(ConnectorLLMError):
        call_agent_json(
            system_prompt="s", user_prompt="u", model="m",
            codex_path="codex", runner=fake,
        )


def test_call_agent_json_raises_on_nonzero_exit():
    fake = FakeClaude([{"a": 1}], returncode=2)
    with pytest.raises(ConnectorLLMError):
        call_agent_json(
            system_prompt="s", user_prompt="u", model="m",
            codex_path="codex", runner=fake,
        )


def test_call_agent_json_raises_on_non_json_inner():
    fake = FakeClaude([], raw_stdout=_envelope("not json at all"))
    # inner result is the literal string "not json at all" → JSON parse fails.
    with pytest.raises(ConnectorLLMError):
        call_agent_json(
            system_prompt="s", user_prompt="u", model="m",
            codex_path="codex", runner=fake,
        )


# ----------------------------------------------------------- domain terms / scan


def _extracted():
    return {
        "domain": "stock_market",
        "claim_under_test": "It is possible to predict equity returns from order flow imbalance",
        "goal_facets": ["robustness"],
    }


def test_extract_domain_terms_keeps_domain_nouns_drops_structural():
    terms = set(extract_domain_terms(_extracted()))
    for want in {"stock", "market", "equity", "returns", "order", "flow", "imbalance", "predict"}:
        assert want in terms
    for drop in {"it", "is", "possible", "from", "robustness"}:
        assert drop not in terms


def test_scan_for_leak_is_word_boundary():
    assert scan_for_leak("a fresh start to the day", ["art"]) == []
    assert scan_for_leak("the stock fell", ["stock"]) == ["stock"]


# ----------------------------------------------------------- generate_abstraction

_CLEAN = (
    "A set of interacting units emits an ongoing stream of indicators; one "
    "seeks to anticipate a future aggregate value from irregularities in that stream."
)
_LEAKY = "One seeks to predict future returns of a stock from order patterns."


def test_generate_abstraction_clean_on_first_try():
    fake = FakeClaude([{"abstraction": _CLEAN, "domain_terms_stripped": ["stock"]}])
    out = generate_abstraction(
        {"extracted": _extracted()}, model="m",
        codex_path="codex", runner=fake,
    )
    assert out["firewall_clean"] is True
    assert out["residual_leaked_terms"] == []
    assert out["regen_attempts"] == 1
    assert out["abstraction"] == _CLEAN


def test_generate_abstraction_regenerates_then_clean():
    fake = FakeClaude([
        {"abstraction": _LEAKY, "domain_terms_stripped": []},
        {"abstraction": _CLEAN, "domain_terms_stripped": ["stock", "returns"]},
    ])
    out = generate_abstraction(
        {"extracted": _extracted()}, model="m",
        codex_path="codex", runner=fake, max_regen=2,
    )
    assert out["regen_attempts"] == 2
    assert out["firewall_clean"] is True
    # usage aggregated across both calls.
    assert out["usage"]["output_tokens"] == 44


def test_generate_abstraction_proceeds_with_residual_leak_after_budget():
    fake = FakeClaude([
        {"abstraction": _LEAKY, "domain_terms_stripped": []},
        {"abstraction": _LEAKY, "domain_terms_stripped": []},
    ])
    out = generate_abstraction(
        {"extracted": _extracted()}, model="m",
        codex_path="codex", runner=fake, max_regen=1,
    )
    assert out["regen_attempts"] == 2  # max_regen=1 → up to 2 attempts
    assert out["firewall_clean"] is False
    assert set(out["residual_leaked_terms"]) & {"stock", "returns", "order", "predict"}


def test_generate_abstraction_empty_text_raises():
    fake = FakeClaude([{"abstraction": "   ", "domain_terms_stripped": []}])
    with pytest.raises(ValueError):
        generate_abstraction(
            {"extracted": _extracted()}, model="m",
            codex_path="codex", runner=fake,
        )
