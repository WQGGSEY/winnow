from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from research_harness.agents.grilling import (
    GrillingError,
    load_resumable_session,
    run_grilling_session,
)
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["codex"], returncode=returncode, stdout=stdout, stderr=""
    )


def _wrap_assistant_text(text: str, cost: float = 0.001) -> str:
    del cost
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "test"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": text},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 2,
                        "cache_write_input_tokens": 1,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 3,
                    },
                }
            ),
        ]
    )


class FakeClaudeRunner:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, cmd, *, input, capture_output, text, timeout, check, env):
        self.calls.append({"cmd": cmd, "input": input, "env_keys": list(env.keys())})
        if not self.responses:
            raise AssertionError("Fake runner exhausted responses")
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
            self.assertIn("RESEARCH_HARNESS_ALLOW_CODEX_LIVE", session["error"])
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
        runner = FakeClaudeRunner(responses)
        with tempfile.TemporaryDirectory() as tmp:
            session = run_grilling_session(
                REPO_ROOT,
                user_goal="improve retrieval on legal QA",
                run_dir=Path(tmp),
                billing_ack=True,
                execution_ack=True,
                command_runner=runner,
                input_provider=lambda q: "",
            )
            self.assertEqual(session["status"], "done")
            self.assertEqual(session["extracted"]["domain"], "retrieval")
            self.assertEqual(session["usage_estimate"]["rounds_used"], 1)
            self.assertEqual(session["rounds"], [])
            command = runner.calls[0]["cmd"]
            disabled = {command[i + 1] for i, value in enumerate(command) if value == "--disable"}
            self.assertTrue({"shell_tool", "unified_exec", "view_image"} <= disabled)
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
            self.assertNotIn("total_cost_usd", session["usage_estimate"])
            self.assertEqual(session["usage_estimate"]["cached_input_tokens"], 6)

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


class GrillingPersistenceTests(unittest.TestCase):
    def test_each_round_flushes_in_progress_snapshot(self) -> None:
        extracted = {
            "root_goal_id": "rg_persist",
            "domain": "x",
            "node_type": "capability",
            "claim_under_test": "claim",
            "mandatory_baselines": ["a"],
            "success_criteria": ["b"],
            "disproof_conditions": ["c"],
            "goal_facets": [],
            "taste_constraints": [],
            "search_query_seed": "x",
        }
        responses = [
            _wrap_assistant_text(json.dumps({"action": "ASK", "question": "q1?"})),
            _wrap_assistant_text(json.dumps({"action": "DONE", "extracted": extracted})),
        ]
        snapshots: list[dict] = []

        def _spy_input(question: str) -> str:
            session_path = Path(tmp_name) / "grilling_session.json"
            snapshots.append(json.loads(session_path.read_text()))
            return "answer to " + question

        with tempfile.TemporaryDirectory() as tmp:
            tmp_name = tmp
            run_grilling_session(
                REPO_ROOT,
                user_goal="persistence test",
                run_dir=Path(tmp),
                billing_ack=True,
                execution_ack=True,
                command_runner=FakeClaudeRunner(responses),
                input_provider=_spy_input,
                allowed_domains=[],
            )

        # Before the user replies on round 1, the session file already exists
        # in "in_progress" status with rounds=[] — that's the pre-loop flush.
        self.assertEqual(snapshots[0]["status"], "in_progress")
        self.assertEqual(snapshots[0]["rounds"], [])

    def test_resume_with_initial_rounds_continues_loop(self) -> None:
        extracted = {
            "root_goal_id": "rg_resume",
            "domain": "x",
            "node_type": "capability",
            "claim_under_test": "claim",
            "mandatory_baselines": ["a"],
            "success_criteria": ["b"],
            "disproof_conditions": ["c"],
            "goal_facets": [],
            "taste_constraints": [],
            "search_query_seed": "x",
        }
        # Only one new Claude call needed — the resume seeds the prior round.
        responses = [
            _wrap_assistant_text(json.dumps({"action": "DONE", "extracted": extracted})),
        ]
        initial_rounds = [
            {
                "round_index": 0,
                "question": "previously asked?",
                "user_response": "previously answered",
                "raw_action": "{}",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            session = run_grilling_session(
                REPO_ROOT,
                user_goal="resume test",
                run_dir=Path(tmp),
                billing_ack=True,
                execution_ack=True,
                command_runner=FakeClaudeRunner(responses),
                input_provider=lambda q: "should not be called",
                allowed_domains=[],
                initial_rounds=initial_rounds,
            )
            self.assertEqual(session["status"], "done")
            self.assertEqual(len(session["rounds"]), 1)
            self.assertEqual(session["rounds"][0]["question"], "previously asked?")

    def test_load_resumable_returns_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Trigger the pre-loop flush by starting and aborting.
            responses = [_wrap_assistant_text(json.dumps({"action": "ASK", "question": "q?"}))]

            def _abort_after_question(q: str) -> str:
                raise KeyboardInterrupt

            try:
                run_grilling_session(
                    REPO_ROOT,
                    user_goal="boom",
                    run_dir=Path(tmp),
                    billing_ack=True,
                    execution_ack=True,
                    command_runner=FakeClaudeRunner(responses),
                    input_provider=_abort_after_question,
                    allowed_domains=[],
                )
            except KeyboardInterrupt:
                pass

            session_path = Path(tmp) / "grilling_session.json"
            loaded = load_resumable_session(session_path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["status"], "in_progress")

    def test_load_resumable_returns_none_for_done(self) -> None:
        extracted = {
            "root_goal_id": "rg_done",
            "domain": "x",
            "node_type": "capability",
            "claim_under_test": "claim",
            "mandatory_baselines": ["a"],
            "success_criteria": ["b"],
            "disproof_conditions": ["c"],
            "goal_facets": [],
            "taste_constraints": [],
            "search_query_seed": "x",
        }
        responses = [
            _wrap_assistant_text(json.dumps({"action": "DONE", "extracted": extracted}))
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run_grilling_session(
                REPO_ROOT,
                user_goal="x",
                run_dir=Path(tmp),
                billing_ack=True,
                execution_ack=True,
                command_runner=FakeClaudeRunner(responses),
                input_provider=lambda q: "",
                allowed_domains=[],
            )
            self.assertIsNone(load_resumable_session(Path(tmp) / "grilling_session.json"))

    def test_pending_ask_is_persisted_before_user_reply(self) -> None:
        """Regression: an emitted ASK used to live only on the SSE stream
        and in the worker's local variable. If the user navigated away to
        another thread between ASK emit and user reply, the question was
        gone forever — returning to the thread showed an empty
        awaiting_input chat. After the fix, the question is flushed to
        ``grilling_session.json.pending_ask`` immediately before the agent
        blocks on user input."""

        # The asker captures the on-disk pending_ask at the moment it is
        # called — i.e., right when the question would be presented to the
        # user. That snapshot must contain the question.
        captured: dict = {}

        def _spy_asker(question: str) -> str:
            session = json.loads(session_path.read_text(encoding="utf-8"))
            captured["question_in_session"] = (session.get("pending_ask") or {}).get(
                "question"
            )
            captured["rounds_at_emit"] = len(session["rounds"])
            return "answer"

        extracted = {
            "root_goal_id": "rg_pending",
            "domain": "x",
            "node_type": "capability",
            "claim_under_test": "x",
            "mandatory_baselines": ["a"],
            "success_criteria": ["b"],
            "disproof_conditions": ["c"],
            "goal_facets": [],
            "taste_constraints": [],
            "search_query_seed": "x",
        }
        responses = [
            _wrap_assistant_text(
                json.dumps({"action": "ASK", "question": "what would falsify the claim?"})
            ),
            _wrap_assistant_text(json.dumps({"action": "DONE", "extracted": extracted})),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            session_path = tmp_path / "grilling_session.json"
            run_grilling_session(
                REPO_ROOT,
                user_goal="x",
                run_dir=tmp_path,
                billing_ack=True,
                execution_ack=True,
                command_runner=FakeClaudeRunner(responses),
                input_provider=_spy_asker,
                allowed_domains=[],
            )
            # While the user was deciding what to type, the question was
            # present on disk and rounds was still empty (no reply yet).
            self.assertEqual(
                captured["question_in_session"],
                "what would falsify the claim?",
            )
            self.assertEqual(captured["rounds_at_emit"], 0)
            # And after the loop completed (DONE), pending_ask is cleared.
            final = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertIsNone(final.get("pending_ask"))
            self.assertEqual(len(final["rounds"]), 1)

    def test_non_ascii_user_goal_still_persists_rounds(self) -> None:
        """Regression: a Korean (or any non-ASCII) user_goal used to make
        ``_slugify`` emit a root_goal_id containing Hangul, which the
        grilling_session schema regex ``^rg_[A-Za-z0-9_\\-]+$`` rejects.
        That made ``_flush_in_progress`` swallow every flush silently, so
        the on-disk session never reflected any rounds — from the user's
        view, grilling appeared to "lose" all answers after a server
        restart or page reload."""
        extracted = {
            "root_goal_id": "rg_korean_alpha",
            "domain": "alpha_factor_combo",
            "node_type": "capability",
            "claim_under_test": "Korean claim",
            "mandatory_baselines": ["a"],
            "success_criteria": ["b"],
            "disproof_conditions": ["c"],
            "goal_facets": [],
            "taste_constraints": [],
            "search_query_seed": "x",
        }
        responses = [
            _wrap_assistant_text(json.dumps({"action": "ASK", "question": "Q1?"})),
            _wrap_assistant_text(json.dumps({"action": "DONE", "extracted": extracted})),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_grilling_session(
                REPO_ROOT,
                user_goal="나는 머신 알파의 강건성 검증을 제안하고 싶어",
                run_dir=tmp_path,
                billing_ack=True,
                execution_ack=True,
                command_runner=FakeClaudeRunner(responses),
                input_provider=lambda q: "answer",
                allowed_domains=["alpha_factor_combo"],
            )
            session_path = tmp_path / "grilling_session.json"
            # The critical check: file exists with rounds preserved.
            self.assertTrue(session_path.exists(), "session JSON must be written")
            session = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(len(session["rounds"]), 1)
            self.assertEqual(session["rounds"][0]["user_response"], "answer")
            self.assertEqual(session["status"], "done")
            # And the slug is ASCII-only.
            self.assertTrue(
                session["extracted"]["root_goal_id"].startswith("rg_"),
            )
            self.assertTrue(
                session["extracted"]["root_goal_id"].isascii(),
                f"root_goal_id should be ASCII, got {session['extracted']['root_goal_id']!r}",
            )

    def test_slugify_is_ascii_only_for_hangul(self) -> None:
        """Direct unit test of the slug filter."""
        from research_harness.agents.grilling import _slugify
        slug = _slugify("나는 머신 알파 my alpha test")
        self.assertTrue(slug.isascii())
        self.assertIn("my_alpha_test", slug)

    def test_load_resumable_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_resumable_session(Path(tmp) / "grilling_session.json"))


class GrillingScaffoldingTests(unittest.TestCase):
    """Cover the deep-interview / PROPOSE_FILE / atomic-finalize loop."""

    PLAN_JSON = json.dumps(
        {
            "plan_id_suffix": "video_eval",
            "task_class": "eval",
            "objective": "Eval video classification on synthetic frames.",
            "entrypoint": {"command": ["python"], "args": ["-m", "src.experiment"]},
            "resources": {"timeout_sec": 120, "gpu": None, "cpu": 1, "memory_gb": 1},
            "inputs": {"datasets": [], "snapshots": []},
            "expected_outputs": {
                "metrics_files": ["artifacts/metrics.json"],
                "logs": ["artifacts/run.log"],
                "artifact_dirs": ["artifacts/"],
            },
            "baseline_evidence_requirements": [
                {
                    "role": "current_best_known",
                    "metric_key": "top1",
                    "baseline_key": "current_best_known",
                    "operator": "greater_than",
                    "margin": 0,
                    "required": True,
                },
                {
                    "role": "naive",
                    "metric_key": "top1",
                    "baseline_key": "naive",
                    "operator": "greater_than",
                    "margin": 0,
                    "required": True,
                },
                {
                    "role": "random_or_null",
                    "metric_key": "top1",
                    "baseline_key": "random_or_null",
                    "operator": "greater_than",
                    "margin": 0,
                    "required": True,
                },
            ],
        }
    )

    # Minimal but importable Python content for each manifest entry.
    PY_INIT = ""
    PY_EVAL_INIT = ""
    PY_TOP1 = "def top1(preds, truth):\n    return sum(p == t for p, t in zip(preds, truth)) / max(1, len(preds))\n"
    PY_BASELINES_INIT = ""
    PY_RANDOM = "def random_pred(n=10, seed=0):\n    import random; random.seed(seed); return [random.randint(0,1) for _ in range(n)]\n"
    PY_NAIVE = "def naive_pred(n=10):\n    return [0] * n\n"
    PY_CURRENT_BEST = "def current_best_pred(n=10):\n    return [1] * n\n"
    PY_DATA = "def truth(n=10):\n    return [i % 2 for i in range(n)]\n"
    PY_PROPOSED = "def proposed_pred(n=10):\n    return [(i+1) % 2 for i in range(n)]\n"
    PY_EXPERIMENT = "from pathlib import Path\nimport json\ndef main():\n    Path('artifacts').mkdir(exist_ok=True)\n    Path('artifacts/metrics.json').write_text(json.dumps({'top1': 0.5}))\n"

    DONE_EXTRACTED = {
        "root_goal_id": "rg_video",
        "domain": "video_classification",
        "node_type": "capability",
        "claim_under_test": "Proposed video classifier beats baselines on synth top1.",
        "mandatory_baselines": ["current_best", "naive", "random"],
        "success_criteria": ["top1 +5%"],
        "disproof_conditions": ["top1 within noise"],
        "goal_facets": ["performance"],
        "taste_constraints": [],
        "search_query_seed": "video classification",
    }

    def _wrap(self, text: str, cost: float = 0.001) -> str:
        return _wrap_assistant_text(text, cost=cost)

    def _propose(self, slug: str, path: str, content: str, purpose: str = "") -> str:
        return self._wrap(
            json.dumps(
                {
                    "action": "PROPOSE_FILE",
                    "file": {
                        "relative_path": f"{slug}/{path}",
                        "purpose": purpose,
                        "content": content,
                    },
                }
            )
        )

    def _full_scaffold_program(self, slug: str) -> list[str]:
        # Order matches the system prompt's recommended bottom-up order.
        return [
            self._propose(slug, "plan.json", self.PLAN_JSON, "metadata"),
            self._propose(slug, "src/__init__.py", self.PY_INIT, "pkg root"),
            self._propose(slug, "src/eval/__init__.py", self.PY_EVAL_INIT, "eval pkg"),
            self._propose(slug, "src/eval/top1.py", self.PY_TOP1, "metric"),
            self._propose(slug, "src/baselines/__init__.py", self.PY_BASELINES_INIT, "baselines pkg"),
            self._propose(slug, "src/baselines/random_baseline.py", self.PY_RANDOM, "random baseline"),
            self._propose(slug, "src/baselines/naive.py", self.PY_NAIVE, "naive baseline"),
            self._propose(slug, "src/baselines/current_best.py", self.PY_CURRENT_BEST, "current best"),
            self._propose(slug, "src/data.py", self.PY_DATA, "data"),
            self._propose(slug, "src/proposed.py", self.PY_PROPOSED, "proposed"),
            self._propose(slug, "src/experiment.py", self.PY_EXPERIMENT, "entrypoint"),
            self._wrap(json.dumps({"action": "DONE", "extracted": self.DONE_EXTRACTED})),
        ]

    def test_full_scaffold_loop_finalizes_to_disk(self) -> None:
        slug = "video_classification"
        with tempfile.TemporaryDirectory() as repo_tmp:
            repo = Path(repo_tmp)
            # Mirror the minimum repo shape: schemas live in the package, but
            # final_destination is computed as repo_root/experiment_plan_templates.
            (repo / "experiment_plan_templates").mkdir()
            run_dir = repo / "runs" / "threads" / "thread_test001" / "grilling"
            run_dir.mkdir(parents=True)
            session = run_grilling_session(
                REPO_ROOT,  # for schemas/settings
                user_goal="video classification on synth frames",
                run_dir=run_dir,
                billing_ack=True,
                execution_ack=True,
                command_runner=FakeClaudeRunner(self._full_scaffold_program(slug)),
                input_provider=lambda q: "",
                allowed_domains=["retrieval"],  # not in the enum -> scaffold path
                max_rounds=20,
            )
            self.assertEqual(session["status"], "done")
            self.assertEqual(session["extracted"]["domain"], slug)
            self.assertIn("scaffold_state", session)
            self.assertTrue(session["scaffold_state"]["finalized"])
            # The repo we read schemas from is REPO_ROOT, but final_destination
            # is computed against the run_dir's project root. Locate the moved
            # directory by following scaffold_state.
            final_dest = Path(session["scaffold_state"]["final_destination"])
            self.assertTrue(final_dest.is_dir())
            self.assertTrue((final_dest / "plan.json").is_file())
            self.assertTrue((final_dest / "src" / "experiment.py").is_file())
            self.assertTrue((final_dest / ".scaffold_origin.json").is_file())
            # Cleanup: remove the scaffolded directory we just dropped into REPO_ROOT.
            shutil.rmtree(final_dest, ignore_errors=True)

    def test_propose_file_syntax_error_records_failed_status(self) -> None:
        # Unit-test the PROPOSE_FILE handler directly — running the full
        # loop just to validate one bad file is overkill since
        # _handle_propose_file is the entire validation surface.
        from research_harness.agents.grilling import (
            _handle_propose_file,
            _new_scaffold_state,
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "grilling"
            run_dir.mkdir(parents=True)
            repo = Path(tmp)
            (repo / "experiment_plan_templates").mkdir()
            state = _new_scaffold_state("broken_scaffold", run_dir, repo)
            result = _handle_propose_file(
                file_payload={
                    "relative_path": "broken_scaffold/src/__init__.py",
                    "purpose": "trigger error",
                    "content": "def broken(:",  # SyntaxError
                },
                scaffold_state=state,
                round_index=0,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["checks"]["syntax"], "error")
            # Not added to manifest_staged because it failed validation.
            self.assertEqual(state["manifest_staged"], [])
            # Recorded under proposed_files for transcript visibility.
            self.assertEqual(state["proposed_files"][0]["status"], "failed")

    def test_slug_collision_with_existing_domain_rejected(self) -> None:
        with self.assertRaises(GrillingError):
            from research_harness.agents.grilling import _validate_new_slug
            _validate_new_slug("retrieval", ["retrieval", "alpha_factor_combo"])

    def test_reserved_slug_rejected(self) -> None:
        from research_harness.agents.grilling import _validate_new_slug
        with self.assertRaises(GrillingError):
            _validate_new_slug("_fallback_demo", [])

    def test_slug_pattern_rejected(self) -> None:
        from research_harness.agents.grilling import _validate_new_slug
        for bad in ("UpperCase", "with-dash", "1leading_digit", "trailing_", "__double"):
            with self.subTest(bad=bad):
                with self.assertRaises(GrillingError):
                    _validate_new_slug(bad, [])


if __name__ == "__main__":
    unittest.main()
