"""Stall-watchdog: experiment-aware termination decision (the >10min-experiment fix)."""

from __future__ import annotations

import json

from research_harness.thread_supervisor import (
    _experiment_running,
    _should_terminate_stall,
)

HARD = 22200.0  # e.g. training(21600) + margin(600)
STALL = 600.0


def test_healthy_not_killed():
    assert _should_terminate_stall(100, STALL, HARD, False) is False


def test_true_hang_killed():
    # silent past stall AND no node running = genuine hang
    assert _should_terminate_stall(700, STALL, HARD, False) is True


def test_long_experiment_deferred():
    # silent way past stall, but a node experiment is running -> do NOT kill
    assert _should_terminate_stall(700, STALL, HARD, True) is False
    assert _should_terminate_stall(3600, STALL, HARD, True) is False     # 1h
    assert _should_terminate_stall(21000, STALL, HARD, True) is False    # ~5.8h, under cap


def test_unreadable_state_deferred():
    # can't tell (None) -> defer, the hard cap backstops
    assert _should_terminate_stall(700, STALL, HARD, None) is False


def test_hard_cap_kills_even_when_running():
    assert _should_terminate_stall(22300, STALL, HARD, True) is True
    assert _should_terminate_stall(22300, STALL, HARD, None) is True


def test_backcompat_no_state_kills_at_stall():
    # state_path absent -> hard_cap None + running False -> original behavior
    assert _should_terminate_stall(700, STALL, None, False) is True


def _write_state(p, statuses):
    p.write_text(json.dumps({"nodes": [{"id": f"n{i}", "status": s} for i, s in enumerate(statuses)]}))


def test_experiment_running_detects_running(tmp_path):
    p = tmp_path / "search_state.json"
    _write_state(p, ["ready", "running", "promoted"])
    assert _experiment_running(p) is True


def test_experiment_running_none_running(tmp_path):
    p = tmp_path / "search_state.json"
    _write_state(p, ["ready", "promoted", "pruned"])
    assert _experiment_running(p) is False


def test_experiment_running_missing_file(tmp_path):
    assert _experiment_running(tmp_path / "nope.json") is False


def test_experiment_running_corrupt_returns_none(tmp_path):
    p = tmp_path / "search_state.json"
    p.write_text("{ not valid json")
    assert _experiment_running(p) is None
