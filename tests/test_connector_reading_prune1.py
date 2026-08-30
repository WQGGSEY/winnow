"""Tests for reading (field-forced) + prune-1 (Slice B, generation-loop core)."""

from __future__ import annotations

import json
import subprocess

import pytest

from research_harness.connector.prune1 import prune1_check
from research_harness.connector.reading import generate_reading


def _envelope(inner_obj):
    return "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(inner_obj)}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 7}}),
        ]
    )


class FakeClaude:
    def __init__(self, inner_objs):
        self._queue = list(inner_objs)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"input": kwargs.get("input")})
        return subprocess.CompletedProcess(cmd, 0, _envelope(self._queue.pop(0)), "")


_ABSTRACTION = (
    "A set of interacting units emits an ongoing stream of indicators; one seeks "
    "to anticipate a future aggregate value from irregularities in that stream."
)
_FIELD = {"code": "q-bio.PE", "name": "Populations and Evolution", "archive": "q-bio"}


# ------------------------------------------------------------------- reading


def test_generate_reading_returns_fields_and_echoes_field():
    fake = FakeClaude([
        {
            "reading": "Treat the units as a population...",
            "field_mechanism": "replicator dynamics",
            "predicted_behavior": "selection pressure shifts the aggregate",
        }
    ])
    out = generate_reading(_ABSTRACTION, _FIELD, model="m",
                           codex_path="codex", runner=fake)
    assert out["field"]["code"] == "q-bio.PE"
    assert out["field_mechanism"] == "replicator dynamics"
    assert out["predicted_behavior"]
    # firewall demonstration: the only substantive content handed to the model
    # is the abstraction + the assigned field — never P (no P param exists).
    sent = fake.calls[0]["input"]
    assert "Populations and Evolution" in sent
    assert "interacting units" in sent


def test_generate_reading_empty_reading_raises():
    fake = FakeClaude([
        {"reading": "  ", "field_mechanism": "x", "predicted_behavior": "y"}
    ])
    with pytest.raises(ValueError):
        generate_reading(_ABSTRACTION, _FIELD, model="m",
                         codex_path="codex", runner=fake)


# -------------------------------------------------------------------- prune-1


def _reading_obj():
    return {
        "field": _FIELD,
        "reading": "population reading",
        "field_mechanism": "replicator dynamics",
        "predicted_behavior": "selection predicts aggregate",
    }


def test_prune1_passes_with_enough_pairs():
    fake = FakeClaude([
        {"correspondence": [
            {"abstraction_element": "units", "reading_counterpart": "individuals"},
            {"abstraction_element": "stream", "reading_counterpart": "generations"},
            {"abstraction_element": "aggregate", "reading_counterpart": "mean fitness"},
        ], "constructible": True, "note": "clean"}
    ])
    out = prune1_check(_ABSTRACTION, _reading_obj(), model="m",
                       codex_path="codex", runner=fake)
    assert out["passed"] is True
    assert out["num_pairs"] == 3


def test_prune1_cuts_when_too_few_pairs():
    fake = FakeClaude([
        {"correspondence": [
            {"abstraction_element": "units", "reading_counterpart": "individuals"}
        ], "constructible": True, "note": "thin"}
    ])
    out = prune1_check(_ABSTRACTION, _reading_obj(), model="m",
                       codex_path="codex", runner=fake)
    assert out["passed"] is False
    assert out["num_pairs"] == 1


def test_prune1_verdict_follows_construction_not_the_flag():
    # constructed 3 real pairs but model SELF-REPORTS constructible=False:
    # we trust the demonstration (pairs), so it PASSES.
    fake = FakeClaude([
        {"correspondence": [
            {"abstraction_element": "a", "reading_counterpart": "1"},
            {"abstraction_element": "b", "reading_counterpart": "2"},
            {"abstraction_element": "c", "reading_counterpart": "3"},
        ], "constructible": False, "note": "model hedged"}
    ])
    out = prune1_check(_ABSTRACTION, _reading_obj(), model="m",
                       codex_path="codex", runner=fake)
    assert out["passed"] is True
    assert out["llm_constructible_flag"] is False  # flag recorded, not used


def test_prune1_empty_correspondence_with_true_flag_still_cut():
    # the inverse: model SELF-REPORTS constructible=True but built nothing →
    # CUT. No declared value can substitute for the demonstration.
    fake = FakeClaude([{"correspondence": [], "constructible": True, "note": "claims ok"}])
    out = prune1_check(_ABSTRACTION, _reading_obj(), model="m",
                       codex_path="codex", runner=fake)
    assert out["passed"] is False
    assert out["num_pairs"] == 0


def test_prune1_ignores_malformed_pairs():
    fake = FakeClaude([
        {"correspondence": [
            {"abstraction_element": "a", "reading_counterpart": "1"},
            {"abstraction_element": "b"},  # missing counterpart → not counted
            {"reading_counterpart": "3"},  # missing element → not counted
            "garbage",
        ], "constructible": True, "note": "mixed"}
    ])
    out = prune1_check(_ABSTRACTION, _reading_obj(), model="m",
                       codex_path="codex", runner=fake)
    assert out["num_pairs"] == 1
    assert out["passed"] is False
