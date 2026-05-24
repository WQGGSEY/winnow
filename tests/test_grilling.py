from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from research_harness.agents.grilling import (
    GrillingError,
    run_grilling_session,
)
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=""
    )


def _wrap_assistant_text(text: str, cost: float = 0.001) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "result": text,
        "total_cost_usd": cost,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    return json.dumps(payload)


class FakeClaudeRunner:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, cmd, *, input, capture_output, text, timeout, check, env):
        self.calls.append({"cmd": cmd, "input": input, "env_keys": list(env.keys())})
        if not self.responses:
            raise AssertionError("FakeClaudeRunner exhausted responses")
        return _make_completed(self.responses.pop(0))


class GrillingTests(unittest.TestCase):
    def test_billing_gate_blocks_without_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = run_grilling_session(
                REPO_ROOT,
                user_goal="improve retrieval calibration on legal QA",
                run_dir=Path(tmp),
                billing_ack=False,
            )
            self.assertEqual(session["status"], "blocked_by_gate")
            self.assertIn("RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE", session["error"])
            self.assertTrue((Path(tmp) / "grilling_session.json").exists())

    def test_execution_gate_blocks_after_billing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = run_grilling_session(
                REPO_ROOT,
                user_goal="x",
                run_dir=Path(tmp),
                billing_ack=True,
                execution_ack=False,
            )
            self.assertEqual(session["status"], "blocked_by_execution_ack")

    def test_done_on_first_round_writes_session_with_extracted(self) -> None:
        extracted = {
            "root_goal_id": "rg_test_001",
            "domain": "retrieval",
            "node_type": "capability",
            "claim_under_test": "Our method beats supervised dense baseline on legal QA.",
            "mandatory_baselines": [
                "current_best_known: BGE-large dense retriever",
                "naive: BM25",
                "random_or_null: random ranking",
            ],
            "success_criteria": ["nDCG@10 +5% vs current best"],
            "disproof_conditions": ["nDCG@10 within noise of BM25"],
            "goal_facets": ["performance"],
            "taste_constraints": ["claim_first"],
            "search_query_seed": "dense retrieval legal QA",
        }
        responses = [
            _wrap_assistant_text(json.dumps({"action": "DONE", "extracted": extracted}))
        ]
        with tempfile.TemporaryDirectory() as tmp:
            session = run_grilling_session(
                REPO_ROOT,
                user_goal="improve retrieval on legal QA",
                run_dir=Path(tmp),
                billing_ack=True,
                execution_ack=True,
                command_runner=FakeClaudeRunner(responses),
                input_provider=lambda q: "",
            )
            self.assertEqual(session["status"], "done")
            self.assertEqual(session["extracted"]["domain"], "retrieval")
            self.assertEqual(session["usage_estimate"]["rounds_used"], 1)
            self.assertEqual(session["rounds"], [])
            validate_named_schema("grilling_session", session)

    def test_multi_round_ask_then_done(self) -> None:
        extracted = {
            "root_goal_id": "rg_multiturn",
            "domain": "summarization",
            "node_type": "capability",
            "claim_under_test": "Method X improves factuality of long-doc summaries.",
            "mandatory_baselines": ["BART-large", "lead-3 baseline", "random sentence"],
            "success_criteria": ["FactScore +3 vs BART"],
            "disproof_conditions": ["FactScore within 1 point of BART"],
            "goal_facets": ["performance", "interpretability"],
            "taste_constraints": ["claim_first"],
            "search_query_seed": "long document factual summarization",
        }
        responses = [
            _wrap_assistant_text(json.dumps({"action": "ASK", "question": "what domain?"})),
            _wrap_assistant_text(json.dumps({"action": "ASK", "question": "primary metric?"})),
            _wrap_assistant_text(json.dumps({"action": "DONE", "extracted": extracted})),
        ]
        answers = iter(["summarization of long docs", "FactScore"])
        with tempfile.TemporaryDirectory() as tmp:
            session = run_grilling_session(
                REPO_ROOT,
                user_goal="reduce hallucination in summaries",
                run_dir=Path(tmp),
                billing_ack=True,
                execution_ack=True,
                command_runner=FakeClaudeRunner(responses),
                input_provider=lambda q: next(answers),
                allowed_domains=[],
            )
            self.assertEqual(session["status"], "done")
            self.assertEqual(len(session["rounds"]), 2)
            self.assertEqual(session["rounds"][0]["question"], "what domain?")
            self.assertEqual(session["rounds"][0]["user_response"], "summarization of long docs")
            self.assertEqual(session["usage_estimate"]["rounds_used"], 3)
            self.assertGreater(session["usage_estimate"]["total_cost_usd"], 0)

    def test_max_rounds_forces_extract(self) -> None:
        extracted = {
            "root_goal_id": "rg_forced",
            "domain": "rl",
            "node_type": "capability",
            "claim_under_test": "Algorithm A learns faster on gridworld.",
            "mandatory_baselines": ["DQN", "random", "tabular Q"],
            "success_criteria": ["faster convergence"],
            "disproof_conditions": ["no convergence advantage"],
            "goal_facets": ["efficiency"],
            "taste_constraints": [],
            "search_query_seed": "sample efficient gridworld rl",
        }
        responses = [
            _wrap_assistant_text(json.dumps({"action": "ASK", "question": f"q{i}"}))
            for i in range(2)
        ] + [
            _wrap_assistant_text(json.dumps({"action": "DONE", "extracted": extracted}))
        ]
        with tempfile.TemporaryDirectory() as tmp:
            session = run_grilling_session(
                REPO_ROOT,
                user_goal="learn faster",
                run_dir=Path(tmp),
                max_rounds=2,
                billing_ack=True,
                execution_ack=True,
                command_runner=FakeClaudeRunner(responses),
                input_provider=lambda q: "answer",
                allowed_domains=[],
            )
            self.assertEqual(session["status"], "max_rounds_reached")
            self.assertEqual(len(session["rounds"]), 2)

    def test_invalid_action_aborts_with_schema_valid_artifact(self) -> None:
        responses = [_wrap_assistant_text(json.dumps({"action": "GIBBERISH"}))]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GrillingError):
                run_grilling_session(
                    REPO_ROOT,
                    user_goal="x",
                    run_dir=Path(tmp),
                    billing_ack=True,
                    execution_ack=True,
                    command_runner=FakeClaudeRunner(responses),
                    input_provider=lambda q: "",
                )
            saved = json.loads((Path(tmp) / "grilling_session.json").read_text())
            self.assertEqual(saved["status"], "aborted")
            self.assertIn("unsupported action", saved["error"])
            validate_named_schema("grilling_session", saved)

    def test_extracted_missing_required_list_aborts(self) -> None:
        extracted = {
            "root_goal_id": "rg_missing",
            "domain": "x",
            "node_type": "capability",
            "claim_under_test": "x",
            "mandatory_baselines": [],
            "success_criteria": ["x"],
            "disproof_conditions": ["x"],
            "goal_facets": [],
            "taste_constraints": [],
            "search_query_seed": "x",
        }
        responses = [
            _wrap_assistant_text(json.dumps({"action": "DONE", "extracted": extracted}))
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GrillingError):
                run_grilling_session(
                    REPO_ROOT,
                    user_goal="x",
                    run_dir=Path(tmp),
                    billing_ack=True,
                    execution_ack=True,
                    command_runner=FakeClaudeRunner(responses),
                    input_provider=lambda q: "",
                )

    def test_unsets_anthropic_api_key_in_env(self) -> None:
        captured: dict = {}

        def _runner(cmd, *, input, capture_output, text, timeout, check, env):
            captured["env"] = env
            return _make_completed(
                _wrap_assistant_text(
                    json.dumps(
                        {
                            "action": "DONE",
                            "extracted": {
                                "root_goal_id": "rg_env",
                                "domain": "x",
                                "node_type": "capability",
                                "claim_under_test": "x",
                                "mandatory_baselines": ["a"],
                                "success_criteria": ["b"],
                                "disproof_conditions": ["c"],
                                "goal_facets": [],
                                "taste_constraints": [],
                                "search_query_seed": "x",
                            },
                        }
                    )
                )
            )

        import os

        original = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "secret-must-be-unset"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run_grilling_session(
                    REPO_ROOT,
                    user_goal="x",
                    run_dir=Path(tmp),
                    billing_ack=True,
                    execution_ack=True,
                    command_runner=_runner,
                    input_provider=lambda q: "",
                    allowed_domains=[],
                )
            self.assertNotIn("ANTHROPIC_API_KEY", captured["env"])
        finally:
            if original is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = original


if __name__ == "__main__":
    unittest.main()
