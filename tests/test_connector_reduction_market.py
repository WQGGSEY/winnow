"""Tests for per-reading far-method market (P-blind) + reduction (P-aware)."""

from __future__ import annotations

import json
import subprocess

from research_harness.connector.far_method_market import research_far_method
from research_harness.connector.reduction import reduce_to_claim

_FIELD = {"code": "q-bio.PE", "name": "Populations and Evolution", "archive": "q-bio"}

_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Replicator Dynamics on Networks</title>
    <summary>We analyze replicator dynamics over interaction graphs.</summary>
    <published>2021-03-01T00:00:00Z</published>
    <id>http://arxiv.org/abs/2103.00001</id>
    <link href="http://arxiv.org/abs/2103.00001" rel="alternate" type="text/html"/>
    <author><name>A. Researcher</name></author>
  </entry>
  <entry>
    <title>Evolutionary Stability and Aggregates</title>
    <summary>Selection pressure and mean-field aggregates.</summary>
    <published>2020-01-01T00:00:00Z</published>
    <id>http://arxiv.org/abs/2001.00002</id>
    <link href="http://arxiv.org/abs/2001.00002" rel="alternate" type="text/html"/>
    <author><name>B. Scientist</name></author>
  </entry>
</feed>
"""


# ------------------------------------------------------ far-method market


def test_research_far_method_parses_papers_and_query_is_p_blind():
    captured = {}

    def fetcher(url: str) -> bytes:
        captured["url"] = url
        return _ATOM.encode("utf-8")

    out = research_far_method(_FIELD, "replicator dynamics", http_fetcher=fetcher)
    assert out["num_papers"] == 2
    assert out["papers"][0]["title"] == "Replicator Dynamics on Networks"
    # query carries only the method + field name — never P.
    assert "replicator dynamics" in out["query"].lower()
    assert "Populations and Evolution" in out["query"]


def test_research_far_method_empty_method_skips_search():
    out = research_far_method(_FIELD, "   ", http_fetcher=lambda u: _ATOM.encode())
    assert out["num_papers"] == 0
    assert any("no field_method" in w for w in out["warnings"])


def test_research_far_method_degrades_on_fetch_error():
    def boom(url: str) -> bytes:
        raise RuntimeError("network down")

    out = research_far_method(_FIELD, "replicator dynamics", http_fetcher=boom)
    assert out["num_papers"] == 0
    assert any("failed" in w for w in out["warnings"])


# ------------------------------------------------------------- reduction


def _envelope(inner_obj):
    return json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": json.dumps(inner_obj),
            "total_cost_usd": 0.02,
            "usage": {"input_tokens": 9, "output_tokens": 13},
        }
    )


class FakeClaude:
    def __init__(self, inner_objs):
        self._queue = list(inner_objs)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"input": kwargs.get("input")})
        return subprocess.CompletedProcess(cmd, 0, _envelope(self._queue.pop(0)), "")


def _grilling():
    return {
        "extracted": {
            "domain": "stock_market",
            "claim_under_test": "It is possible to predict equity returns from order flow",
            "goal_facets": [],
        }
    }


def _reading():
    return {
        "field": _FIELD,
        "reading": "treat units as a population",
        "field_mechanism": "replicator dynamics",
        "predicted_behavior": "selection predicts aggregate",
    }


def _prune1():
    return {
        "field": _FIELD,
        "passed": True,
        "correspondence": [
            {"abstraction_element": "units", "reading_counterpart": "individuals"},
            {"abstraction_element": "aggregate", "reading_counterpart": "mean fitness"},
        ],
    }


_WELL_FORMED = {
    "reducible": True,
    "correspondence_table": [{"method_part": "fitness", "p_observable": "expected return"}],
    "claim_under_test": "Replicator dynamics over traded instruments predicts equity returns.",
    "mandatory_baselines": ["current_best: momentum factor", "naive: buy-and-hold", "random: coin-flip"],
    "success_criteria": ["out-of-sample Sharpe beats momentum"],
    "disproof_conditions": ["no improvement over momentum on holdout"],
    "far_ness_note": "keeps the evolutionary selection mechanism, not a generic regressor",
}


def test_reduce_produces_claim_contract_when_well_formed():
    fake = FakeClaude([_WELL_FORMED])
    out = reduce_to_claim(_grilling(), _reading(), _prune1(), model="m", max_budget="1",
                          claude_path="claude", runner=fake,
                          method_research={"papers": [{"title": "Replicator Dynamics on Networks", "url": "u"}]})
    assert out["reduced"] is True
    cc = out["claim_contract"]
    assert cc["claim_under_test"].startswith("Replicator dynamics")
    assert len(cc["mandatory_baselines"]) == 3
    assert cc["success_criteria"] and cc["disproof_conditions"]
    # P-aware: P's claim AND the method paper both appear in the prompt.
    sent = fake.calls[0]["input"]
    assert "predict equity returns from order flow" in sent
    assert "Replicator Dynamics on Networks" in sent
    assert "field_mechanism: replicator dynamics" in sent
    assert "predicted_behavior: selection predicts aggregate" in sent


def test_reduce_malformed_contract_kills_reading():
    bad = dict(_WELL_FORMED, mandatory_baselines=[])  # well-formedness fails
    fake = FakeClaude([bad])
    out = reduce_to_claim(_grilling(), _reading(), _prune1(), model="m", max_budget="1",
                          claude_path="claude", runner=fake)
    assert out["reduced"] is False
    assert out["claim_contract"] is None
    assert out["well_formed"] is False


def test_reduce_explicit_decline_kills_even_if_fields_present():
    declined = dict(_WELL_FORMED, reducible=False)  # all fields present but declined
    fake = FakeClaude([declined])
    out = reduce_to_claim(_grilling(), _reading(), _prune1(), model="m", max_budget="1",
                          claude_path="claude", runner=fake)
    assert out["reduced"] is False
    assert out["claim_contract"] is None
    assert out["well_formed"] is True  # fields were fine; the model declined
    assert out["llm_reducible_flag"] is False
