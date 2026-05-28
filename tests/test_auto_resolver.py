"""Unit tests for the auto_resolver policy + safety guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_harness.orchestrator.auto_resolver import (
    LOOP_DETECTION_WINDOW,
    MAX_AUTO_ACTIONS_PER_THREAD,
    chain_safety_check,
    pick_auto_action,
    record_auto_action,
)


def _suggestion(tool="seed_alternative_root_formulation", args=None, confidence="high"):
    return {
        "tool": tool,
        "args": args or {"thread_id": "t", "formulation_id": "acf_x"},
        "source_rail": "rail_5_must_revise_root",
        "rationale": "n_x collapsed; acf_x next priority",
        "confidence": confidence,
    }


def test_pick_auto_action_returns_action_when_well_formed():
    resp = {"status": "must_revise_root", "auto_action_suggestion": _suggestion()}
    action = pick_auto_action(resp)
    assert action is not None
    assert action.tool == "seed_alternative_root_formulation"
    assert action.confidence == "high"
    assert action.source_rail == "rail_5_must_revise_root"


def test_pick_auto_action_returns_none_when_field_missing():
    assert pick_auto_action({"status": "ok"}) is None


def test_pick_auto_action_returns_none_when_malformed():
    bad_cases = [
        {"auto_action_suggestion": "not a dict"},
        {"auto_action_suggestion": {"tool": ""}},
        {"auto_action_suggestion": {"tool": "x", "args": "not dict"}},
        {"auto_action_suggestion": {"tool": "x", "args": {}, "source_rail": "", "rationale": "y", "confidence": "high"}},
        {"auto_action_suggestion": {"tool": "x", "args": {}, "source_rail": "y", "rationale": "z", "confidence": "totally-made-up"}},
    ]
    for resp in bad_cases:
        assert pick_auto_action(resp) is None, resp


def test_safety_check_refuses_non_high_confidence(tmp_path):
    action = pick_auto_action({"auto_action_suggestion": _suggestion(confidence="medium")})
    verdict = chain_safety_check(tmp_path, action)
    assert not verdict.ok
    assert "confidence" in verdict.reason


def test_safety_check_clears_clean_thread(tmp_path):
    action = pick_auto_action({"auto_action_suggestion": _suggestion()})
    verdict = chain_safety_check(tmp_path, action)
    assert verdict.ok
    assert verdict.history_length == 0


def test_safety_check_refuses_loop(tmp_path):
    action = pick_auto_action({"auto_action_suggestion": _suggestion()})
    # Simulate a prior failed attempt with the same fingerprint.
    record_auto_action(tmp_path, action, outcome="rejected",
                       dispatch_result={"status": "rejected"})
    verdict = chain_safety_check(tmp_path, action)
    assert not verdict.ok
    assert "identical auto_action" in verdict.reason


def test_safety_check_allows_retry_after_successful_outcome(tmp_path):
    action = pick_auto_action({"auto_action_suggestion": _suggestion()})
    record_auto_action(tmp_path, action, outcome="ok",
                       dispatch_result={"status": "ok"})
    # Same fingerprint but prior outcome was ok → not blocked.
    verdict = chain_safety_check(tmp_path, action)
    assert verdict.ok


def test_safety_check_refuses_when_history_exceeds_cap(tmp_path):
    action = pick_auto_action({"auto_action_suggestion": _suggestion()})
    # Fill history with distinct ok actions so loop-detection doesn't fire.
    for i in range(MAX_AUTO_ACTIONS_PER_THREAD):
        distinct = pick_auto_action({"auto_action_suggestion": _suggestion(
            args={"thread_id": "t", "formulation_id": f"acf_{i}"},
        )})
        record_auto_action(tmp_path, distinct, outcome="ok",
                           dispatch_result={"status": "ok"})
    verdict = chain_safety_check(tmp_path, action)
    assert not verdict.ok
    assert f"MAX_AUTO_ACTIONS_PER_THREAD={MAX_AUTO_ACTIONS_PER_THREAD}" in verdict.reason


def test_record_auto_action_writes_jsonl_entry(tmp_path):
    action = pick_auto_action({"auto_action_suggestion": _suggestion()})
    record_auto_action(tmp_path, action, outcome="ok",
                       dispatch_result={"status": "ok", "new_root_id": "n_x"})
    path = tmp_path / "production" / "auto_actions.jsonl"
    assert path.exists()
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "ok"
    assert entries[0]["tool"] == "seed_alternative_root_formulation"
    assert entries[0]["dispatch_status"] == "ok"


def test_record_auto_action_captures_refusal_reason(tmp_path):
    action = pick_auto_action({"auto_action_suggestion": _suggestion()})
    record_auto_action(tmp_path, action,
                       outcome="refused_by_safety_check",
                       refusal_reason="loop detected")
    entries = [json.loads(line) for line in
               (tmp_path / "production" / "auto_actions.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert entries[0]["refusal_reason"] == "loop detected"


def test_fingerprint_stable_across_arg_order():
    a = pick_auto_action({"auto_action_suggestion": _suggestion(
        args={"thread_id": "t", "formulation_id": "acf_x"},
    )})
    b = pick_auto_action({"auto_action_suggestion": _suggestion(
        args={"formulation_id": "acf_x", "thread_id": "t"},
    )})
    assert a.fingerprint() == b.fingerprint()
