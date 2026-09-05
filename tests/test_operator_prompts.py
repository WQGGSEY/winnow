"""Unit tests for the operator_prompts queue historical records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_harness.orchestrator.operator_prompts import (
    enqueue_prompt,
    list_pending,
    submit_response,
    take_pending_response,
)


def test_enqueue_creates_pending_entry(tmp_path):
    rec = enqueue_prompt(
        tmp_path, kind="decision_request",
        prompt="Should I retry?", options=["retry", "abort"],
        source_rail="rail_x",
    )
    assert rec["event_id"].startswith("opr_")
    assert rec["event"] == "enqueued"
    pending = list_pending(tmp_path)
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
    assert pending[0]["prompt"] == "Should I retry?"


def test_enqueue_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        enqueue_prompt(tmp_path, kind="not_a_kind", prompt="x")


def test_enqueue_rejects_empty_prompt(tmp_path):
    with pytest.raises(ValueError):
        enqueue_prompt(tmp_path, kind="decision_request", prompt="   ")


def test_submit_response_marks_responded(tmp_path):
    rec = enqueue_prompt(tmp_path, kind="decision_request", prompt="?")
    submit_response(tmp_path, event_id=rec["event_id"], response="retry")
    pending = list_pending(tmp_path)
    assert len(pending) == 1
    assert pending[0]["status"] == "responded"
    assert pending[0]["response"] == "retry"


def test_take_pending_response_consumes_and_returns(tmp_path):
    rec = enqueue_prompt(tmp_path, kind="decision_request", prompt="?")
    submit_response(tmp_path, event_id=rec["event_id"], response="abort")
    taken = take_pending_response(tmp_path)
    assert taken is not None
    assert taken["response"] == "abort"
    # Subsequent take returns None (consumed).
    assert take_pending_response(tmp_path) is None
    # list_pending no longer surfaces it.
    assert list_pending(tmp_path) == []


def test_take_pending_response_targets_specific_event_id(tmp_path):
    a = enqueue_prompt(tmp_path, kind="decision_request", prompt="a")
    b = enqueue_prompt(tmp_path, kind="decision_request", prompt="b")
    submit_response(tmp_path, event_id=a["event_id"], response="ra")
    submit_response(tmp_path, event_id=b["event_id"], response="rb")
    # Default = oldest responded → a
    first = take_pending_response(tmp_path)
    assert first["event_id"] == a["event_id"]
    # Targeted = b
    second = take_pending_response(tmp_path, event_id=b["event_id"])
    assert second["event_id"] == b["event_id"]


def test_take_returns_none_when_only_pending(tmp_path):
    enqueue_prompt(tmp_path, kind="decision_request", prompt="x")
    assert take_pending_response(tmp_path) is None


def test_submit_response_rejects_unknown_event_id(tmp_path):
    with pytest.raises(ValueError, match="unknown event_id"):
        submit_response(tmp_path, event_id="opr_nonexistent", response="x")


def test_submit_response_rejects_already_consumed(tmp_path):
    rec = enqueue_prompt(tmp_path, kind="decision_request", prompt="?")
    submit_response(tmp_path, event_id=rec["event_id"], response="r")
    take_pending_response(tmp_path)
    with pytest.raises(ValueError, match="already consumed"):
        submit_response(tmp_path, event_id=rec["event_id"], response="again")


def test_event_id_idempotency_via_caller_supplied_id(tmp_path):
    """Caller-supplied event_id lets retries land on the same logical prompt
    rather than spamming new entries each time (used by _escalate_to_operator)."""
    enqueue_prompt(tmp_path, kind="decision_request", prompt="x", event_id="opr_stable")
    submit_response(tmp_path, event_id="opr_stable", response="retain original criterion")
    take_pending_response(tmp_path)
    enqueue_prompt(tmp_path, kind="decision_request", prompt="x", event_id="opr_stable")
    assert list_pending(tmp_path) == []
    with pytest.raises(ValueError, match="different prompt"):
        enqueue_prompt(tmp_path, kind="decision_request", prompt="x again", event_id="opr_stable")
    pending = list_pending(tmp_path)
    assert pending == []


# --- MCP wrappers ------------------------------------------------------- #
