"""Integration tests for the connector orchestrator (Slice C)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import research_harness.connector.orchestrator as connector_orchestrator
from research_harness.connector.orchestrator import run_domain_connector
from research_harness.schemas.validator import validate_named_schema

REPO_ROOT = Path(__file__).resolve().parents[1]

# distinctive unique substrings of each step's system prompt → routing key.
_ROUTE = [
    ("You de-domain a research problem", "abstraction"),
    ("You read a single vague structural skeleton", "reading"),
    ("cheap triage", "prune1"),
    ("construction worker", "reduction"),
]

_EMPTY_FEED = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'

# A de-domained abstraction with NONE of P's domain terms (stock/market/equity/
# returns/order/flow/predict) — so the firewall scan passes on the first try.
_CLEAN_ABSTRACTION = (
    "A set of interacting units emits an ongoing stream of indicators; one seeks "
    "to anticipate a future aggregate value from irregularities in that stream."
)


@pytest.fixture(autouse=True)
def _successful_baseline_research(monkeypatch):
    def run(_repo_root, _grilling_session, *, run_dir, http_fetcher):
        del http_fetcher
        return SimpleNamespace(
            brief={
                "status": "completed",
                "brief_path": str(run_dir / "market_research_brief.json"),
                "baseline_dossier_id": "bd_fixture",
                "usage": {"papers_found": 1},
            }
        )

    monkeypatch.setattr(connector_orchestrator, "_run_baseline_research", run)


def _envelope(inner_obj):
    return "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(inner_obj)}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}}),
        ]
    )


class RoutingFakeCodex:
    """Route each call by its composed instructions and record the prompt."""

    def __init__(self, responses):
        self.responses = responses  # kind -> dict (inner obj) or "ERROR"
        self.calls = []  # {kind, user}

    def _route(self, system: str) -> str:
        for needle, kind in _ROUTE:
            if needle in system:
                return kind
        return "unknown"

    def __call__(self, cmd, **kwargs):
        system = kwargs.get("input") or ""
        kind = self._route(system)
        self.calls.append({"kind": kind, "user": kwargs.get("input")})
        resp = self.responses.get(kind, "ERROR")
        if resp == "ERROR":
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        return subprocess.CompletedProcess(cmd, 0, _envelope(resp), "")


def _valid_grilling():
    return {
        "session_id": "grill_testconn01",
        "status": "done",
        "user_goal": "predict equity returns from order flow",
        "max_rounds": 8,
        "model": "claude-x",
        "created_at": "2026-05-30T00:00:00Z",
        "rounds": [],
        "usage_estimate": {"rounds_used": 1, "total_cost_usd": 0.0,
                            "total_input_tokens": 0, "total_output_tokens": 0},
        "extracted": {
            "root_goal_id": "rg_equity",
            "domain": "stock_market",
            "node_type": "validity",
            "claim_under_test": "It is possible to predict equity returns from order flow",
            "mandatory_baselines": ["current_best: TBD"],
            "success_criteria": ["TBD"],
            "disproof_conditions": ["TBD"],
            "goal_facets": [],
            "taste_constraints": [],
            "search_query_seed": "equity returns order flow",
        },
    }


_ABSTRACTION_OK = {"abstraction": _CLEAN_ABSTRACTION, "domain_terms_stripped": []}
_READING_OK = {
    "reading": "treat units as a population",
    "field_mechanism": "replicator dynamics",
    "predicted_behavior": "selection predicts the aggregate",
}
_PRUNE1_PASS = {"correspondence": [
    {"abstraction_element": "units", "reading_counterpart": "individuals"},
    {"abstraction_element": "aggregate", "reading_counterpart": "mean fitness"},
    {"abstraction_element": "stream", "reading_counterpart": "generations"},
], "constructible": True, "note": "ok"}
_PRUNE1_FAIL = {"correspondence": [], "constructible": False, "note": "nonsense"}
_REDUCE_OK = {
    "reducible": True,
    "correspondence_table": [{"method_part": "fitness", "p_observable": "expected return"}],
    "claim_under_test": "Replicator dynamics over instruments predicts the target.",
    "mandatory_baselines": ["current_best: momentum", "naive: buy-hold", "random: coin"],
    "success_criteria": ["beats momentum out of sample"],
    "disproof_conditions": ["no improvement on holdout"],
    "far_ness_note": "keeps the selection mechanism",
}
_REDUCE_DECLINE = dict(_REDUCE_OK, reducible=False)


def _fetcher(url):
    return _EMPTY_FEED


def test_happy_path_keeps_quota_claims_and_writes_valid_session(tmp_path):
    fake = RoutingFakeCodex({
        "abstraction": _ABSTRACTION_OK, "reading": _READING_OK,
        "prune1": _PRUNE1_PASS, "reduction": _REDUCE_OK,
    })
    out = run_domain_connector(
        REPO_ROOT, _valid_grilling(), run_dir=tmp_path / "connector",
        billing_ack=True, execution_ack=True, command_runner=fake,
        http_fetcher=_fetcher, quota=2, max_fields_tried=40, field_seed=123,
    )
    s = out.session
    assert s["status"] == "completed"
    assert s["stopped_reason"] == "quota_met"
    assert s["quota_met"] is True
    assert s["baseline_research"]["baseline_dossier_id"] == "bd_fixture"
    assert len(out.claims) == 2
    assert s["fields_tried"] == 2
    cc = out.claims[0]["claim_contract"]
    assert cc["claim_under_test"] and len(cc["mandatory_baselines"]) == 3
    reduction_calls = [call for call in fake.calls if call["kind"] == "reduction"]
    assert all("replicator dynamics" in call["user"] for call in reduction_calls)
    assert all("selection predicts the aggregate" in call["user"] for call in reduction_calls)
    # abstraction(1) + 2 fields * (reading+prune1+reduction) = 7 LLM calls.
    assert s["usage_estimate"]["llm_calls"] == 7
    # written file is schema-valid and on disk.
    written = json.loads((tmp_path / "connector" / "connector_session.json").read_text())
    validate_named_schema("connector_session", written)


def test_firewall_p_absent_from_p_blind_steps(tmp_path):
    fake = RoutingFakeCodex({
        "abstraction": _ABSTRACTION_OK, "reading": _READING_OK,
        "prune1": _PRUNE1_PASS, "reduction": _REDUCE_OK,
    })
    run_domain_connector(
        REPO_ROOT, _valid_grilling(), run_dir=tmp_path / "connector",
        billing_ack=True, execution_ack=True, command_runner=fake,
        http_fetcher=_fetcher, quota=1, max_fields_tried=40, field_seed=1,
    )
    p_marker = "equity returns"
    saw_abstraction = saw_blind = False
    for call in fake.calls:
        if call["kind"] == "abstraction":
            assert p_marker in call["user"]  # P-visible step DOES see P
            saw_abstraction = True
        if call["kind"] in {"reading", "prune1"}:
            assert p_marker not in call["user"]  # P-blind steps NEVER see P
            saw_blind = True
    assert saw_abstraction and saw_blind


def test_leaking_abstraction_aborts_before_field_or_baseline_research(tmp_path):
    fake = RoutingFakeCodex({
        "abstraction": {"abstraction": "Predict equity returns from order flow.",
                        "domain_terms_stripped": []},
        "reading": _READING_OK, "prune1": _PRUNE1_PASS, "reduction": _REDUCE_OK,
    })
    baseline_calls = []
    out = run_domain_connector(
        REPO_ROOT, _valid_grilling(), run_dir=tmp_path / "connector",
        billing_ack=True, execution_ack=True, command_runner=fake,
        http_fetcher=_fetcher, quota=1, max_fields_tried=1, max_regen=0,
        baseline_research_runner=lambda *a, **kw: baseline_calls.append(kw),
    )
    assert out.session["status"] == "aborted"
    assert out.session["abstraction"]["residual_leaked_terms"]
    assert out.session["fields_tried"] == 0
    assert out.claims == []
    assert [call["kind"] for call in fake.calls] == ["abstraction"]
    assert baseline_calls == []


def test_prune1_all_fail_yields_no_claims_and_logs_cap(tmp_path):
    fake = RoutingFakeCodex({
        "abstraction": _ABSTRACTION_OK, "reading": _READING_OK,
        "prune1": _PRUNE1_FAIL, "reduction": _REDUCE_OK,
    })
    out = run_domain_connector(
        REPO_ROOT, _valid_grilling(), run_dir=tmp_path / "connector",
        billing_ack=True, execution_ack=True, command_runner=fake,
        http_fetcher=_fetcher, quota=2, max_fields_tried=3, field_seed=7,
    )
    s = out.session
    assert s["status"] == "completed_no_claims"
    assert out.claims == []
    assert s["fields_tried"] == 3
    assert s["stopped_reason"] == "max_fields_tried"  # no silent cap
    assert s["quota_met"] is False
    assert all(a["prune1_passed"] is False for a in s["attempts"])


def test_reduction_decline_yields_no_claims(tmp_path):
    fake = RoutingFakeCodex({
        "abstraction": _ABSTRACTION_OK, "reading": _READING_OK,
        "prune1": _PRUNE1_PASS, "reduction": _REDUCE_DECLINE,
    })
    out = run_domain_connector(
        REPO_ROOT, _valid_grilling(), run_dir=tmp_path / "connector",
        billing_ack=True, execution_ack=True, command_runner=fake,
        http_fetcher=_fetcher, quota=2, max_fields_tried=2, field_seed=5,
    )
    assert out.session["status"] == "completed_no_claims"
    assert out.claims == []
    assert all(a["prune1_passed"] and not a["reduced"] for a in out.session["attempts"])


def test_blocked_by_ack_does_not_call_model(tmp_path):
    fake = RoutingFakeCodex({"abstraction": _ABSTRACTION_OK})
    out = run_domain_connector(
        REPO_ROOT, _valid_grilling(), run_dir=tmp_path / "connector",
        billing_ack=False, execution_ack=True, command_runner=fake,
        http_fetcher=_fetcher,
    )
    assert out.session["status"] == "blocked_by_gate"
    assert out.session["abstraction"] is None
    assert fake.calls == []  # never invoked the model


def test_per_field_llm_error_is_logged_not_fatal(tmp_path):
    # abstraction OK, but every reading errors → each attempt records the error,
    # the loop survives, and we end cleanly with no claims.
    fake = RoutingFakeCodex({"abstraction": _ABSTRACTION_OK, "reading": "ERROR"})
    out = run_domain_connector(
        REPO_ROOT, _valid_grilling(), run_dir=tmp_path / "connector",
        billing_ack=True, execution_ack=True, command_runner=fake,
        http_fetcher=_fetcher, quota=2, max_fields_tried=2, field_seed=9,
    )
    s = out.session
    assert s["status"] == "completed_no_claims"
    assert s["fields_tried"] == 2
    assert all(a["error"] for a in s["attempts"])


def test_emits_live_progress_events(tmp_path):
    # The event_emitter (bridged to SSE by the frontend) sees the full narrative:
    # abstraction -> per-field reading/prune-1/reduction -> claim kept.
    fake = RoutingFakeCodex({
        "abstraction": _ABSTRACTION_OK, "reading": _READING_OK,
        "prune1": _PRUNE1_PASS, "reduction": _REDUCE_OK,
    })
    events = []
    run_domain_connector(
        REPO_ROOT, _valid_grilling(), run_dir=tmp_path / "connector",
        billing_ack=True, execution_ack=True, command_runner=fake,
        http_fetcher=_fetcher, quota=1, max_fields_tried=3, field_seed=1,
        event_emitter=events.append,
    )
    types = [e["type"] for e in events]
    assert types[0] == "abstraction_start"
    for t in ("abstraction_done", "field_start", "reading_done", "prune1_done",
              "market_done", "reduction_done", "claim_kept",
              "baseline_research_start", "baseline_research_done"):
        assert t in types, f"missing event {t!r} in {types}"
    kept = next(e for e in events if e["type"] == "claim_kept")
    assert kept["kept"] == 1 and kept["claim_under_test"]
    abst = next(e for e in events if e["type"] == "abstraction_done")
    assert abst["firewall_clean"] is True and abst["abstraction"]


def test_baseline_research_failure_blocks_production_handoff(tmp_path):
    fake = RoutingFakeCodex({
        "abstraction": _ABSTRACTION_OK,
        "reading": _READING_OK,
        "prune1": _PRUNE1_PASS,
        "reduction": _REDUCE_OK,
    })

    def fail(*_args, **_kwargs):
        raise RuntimeError("source unavailable")

    out = run_domain_connector(
        REPO_ROOT,
        _valid_grilling(),
        run_dir=tmp_path / "connector",
        billing_ack=True,
        execution_ack=True,
        command_runner=fake,
        http_fetcher=_fetcher,
        quota=1,
        max_fields_tried=1,
        field_seed=1,
        baseline_research_runner=fail,
    )

    assert out.session["status"] == "aborted"
    assert "baseline research failed" in out.session["error"]
