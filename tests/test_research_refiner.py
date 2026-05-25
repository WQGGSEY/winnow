from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from research_harness.agents.research_refiner import (
    RefinerError,
    run_research_refiner,
)
from research_harness.schemas.validator import validate_named_schema


REPO_ROOT = Path(__file__).resolve().parents[1]


def _grilling_session() -> dict:
    return {
        "session_id": "grill_refiner_test",
        "status": "done",
        "user_goal": "improve retrieval calibration on legal QA",
        "max_rounds": 8,
        "model": "sonnet",
        "created_at": "2026-05-25T00:00:00+00:00",
        "rounds": [],
        "extracted": {
            "root_goal_id": "rg_refiner_test",
            "domain": "retrieval",
            "node_type": "capability",
            "claim_under_test": "Calibrated dense retrieval improves nDCG@10 on LegalBench over BGE-large.",
            "mandatory_baselines": [
                "current_best_known: BGE-large",
                "naive: BM25",
                "random_or_null: random ranking",
            ],
            "success_criteria": ["nDCG@10 +3pp on LegalBench retrieval test split"],
            "disproof_conditions": ["nDCG@10 within noise of BM25"],
            "goal_facets": ["performance"],
            "taste_constraints": ["claim_first"],
            "search_query_seed": "retrieval calibration legal QA",
        },
        "usage_estimate": {
            "rounds_used": 1,
            "total_cost_usd": 0.01,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
        },
    }


def _market_brief(tmp: Path) -> dict:
    md_path = tmp / "baseline_analysis.md"
    md_path.write_text(
        "## Current-best\nBGE-large (https://arxiv.org/abs/...) nDCG@10 0.42 on LegalBench.\n\n## Naive\nBM25.\n\n## Random/Null\nRandom ranking.\n",
        encoding="utf-8",
    )
    return {
        "brief_id": "mrb_refiner_test",
        "type": "market_research_brief",
        "status": "completed",
        "created_at": "2026-05-25T00:00:00+00:00",
        "grilling_session_id": "grill_refiner_test",
        "search_query_seed": "retrieval calibration legal QA",
        "sources_used": ["arxiv"],
        "papers": [
            {"title": "BGE-large paper", "url": "https://arxiv.org/abs/2305.xxxxx"},
        ],
        "baseline_dossier_id": "bd_refiner_test_20260525",
        "baseline_dossier_candidate_path": str(tmp / "candidate.yaml"),
        "baseline_dossier_path": str(tmp / "candidate.yaml"),
        "reference_papers_dir": str(tmp / "refs"),
        "brief_path": str(tmp / "brief.json"),
        "baseline_analysis_md_path": str(md_path),
        "baseline_analysis_source": "deterministic_metadata",
        "baseline_analysis_usage": {"cost_usd": None, "input_tokens": 0, "output_tokens": 0},
        "usage": {
            "papers_found": 1,
            "papers_downloaded": 0,
            "papers_failed": 0,
            "sources_attempted": ["arxiv"],
        },
    }


def _wrap_assistant_text(text: str, *, cost: float = 0.001) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "result": text,
            "total_cost_usd": cost,
            "usage": {"input_tokens": 50, "output_tokens": 30},
        }
    )


def _make_completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")


class _FakeRunner:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, cmd, *, input, capture_output, text, timeout, check, env):
        self.calls.append({"cmd": cmd, "input": input})
        if not self.responses:
            raise AssertionError("FakeRunner exhausted")
        return _make_completed(self.responses.pop(0))


_GOOD_VP = {
    "primary_metric": {"name": "ndcg_at_10", "operator": "greater_than", "threshold": 0.03},
    "splits": {"train": "legalbench/retrieval/train", "eval": "legalbench/retrieval/test"},
    "statistical_test": {"name": "paired_t_test", "alpha": 0.05},
    "n_seeds": 5,
    "decision_rule": "all_baselines_beaten_and_p_lt_0.05",
}


_SYNTHETIC_SPEC = {
    "id": "ds_synth_calibration_eval",
    "type": "synthetic",
    "role": "evaluation",
    "synthetic_recipe": {
        "shape": "tabular",
        "rows": 100,
        "seed": 7,
        "columns": [
            {"name": "score", "distribution": "normal", "params": {"mu": 0, "sigma": 1}},
            {"name": "label", "distribution": "bernoulli", "params": {"p": 0.3}},
        ],
    },
}


class RefinerGateTests(unittest.TestCase):
    def test_billing_ack_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = run_research_refiner(
                REPO_ROOT,
                grilling_session=_grilling_session(),
                market_research_brief=_market_brief(Path(tmp)),
                run_dir=Path(tmp) / "refine",
                billing_ack=False,
            )
            self.assertEqual(plan["status"], "blocked_by_gate")

    def test_execution_ack_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = run_research_refiner(
                REPO_ROOT,
                grilling_session=_grilling_session(),
                market_research_brief=_market_brief(Path(tmp)),
                run_dir=Path(tmp) / "refine",
                billing_ack=True,
                execution_ack=False,
            )
            self.assertEqual(plan["status"], "blocked_by_execution_ack")


class RefinerSuccessTests(unittest.TestCase):
    def test_done_first_round_writes_plan_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            done_payload = {
                "action": "DONE",
                "plan": {
                    "claim_under_test": "Calibrated dense retrieval improves nDCG@10 on LegalBench over BGE-large by ≥3 points (paired t-test p<0.05).",
                    "mandatory_baselines": ["BGE-large", "BM25", "random ranking"],
                    "success_criteria": ["nDCG@10 +3pp"],
                    "disproof_conditions": ["within noise of BM25"],
                    "validation_procedure": _GOOD_VP,
                    "dataset_specs": [],
                    "unresolved_dataset_specs": [],
                    "sota_reconciliations": [],
                    "acknowledged_limitations": [],
                },
            }
            responses = [_wrap_assistant_text(json.dumps(done_payload))]
            runner = _FakeRunner(responses)
            plan = run_research_refiner(
                REPO_ROOT,
                grilling_session=_grilling_session(),
                market_research_brief=_market_brief(tmp_path),
                run_dir=tmp_path / "refine",
                billing_ack=True,
                execution_ack=True,
                command_runner=runner,
                input_provider=lambda q: "",
                allowed_domains=["retrieval"],
            )
            self.assertEqual(plan["status"], "done")
            self.assertEqual(plan["validation_procedure"]["n_seeds"], 5)
            self.assertTrue(Path(plan["plan_path"]).exists())
            self.assertTrue(Path(plan["dataset_manifest_path"]).exists())
            manifest = json.loads(Path(plan["dataset_manifest_path"]).read_text())
            self.assertEqual(manifest["datasets"], {})
            validate_named_schema("refined_research_plan", plan)

    def test_ask_propose_done_chain_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            done_payload = {
                "action": "DONE",
                "plan": {
                    "claim_under_test": "Calibrated dense retrieval improves nDCG@10 on synthetic calibration eval (paired t-test p<0.05).",
                    "mandatory_baselines": ["BGE-large", "BM25", "random"],
                    "success_criteria": ["nDCG@10 +3pp on synth"],
                    "disproof_conditions": ["within noise of BM25"],
                    "validation_procedure": _GOOD_VP,
                    "dataset_specs": [_SYNTHETIC_SPEC],
                    "unresolved_dataset_specs": [],
                    "sota_reconciliations": [
                        {
                            "conflict": "User claim of novelty conflicts with BGE-large arXiv:2305.xxxxx prior work.",
                            "evidence_paper_url": "https://arxiv.org/abs/2305.xxxxx",
                            "user_choice": "narrow_claim",
                            "resolution": "Narrowed to legal calibration with synthetic eval split.",
                            "resolved_at_round": 0,
                        }
                    ],
                    "acknowledged_limitations": ["Toy synthetic data — production rerun required."],
                },
            }
            responses = [
                _wrap_assistant_text(
                    json.dumps(
                        {
                            "action": "ASK",
                            "question": "User claim sounds novel but BGE-large arxiv 2305 already does this on legal — narrow_claim, change_baseline, or acknowledge_limitation?",
                        }
                    )
                ),
                _wrap_assistant_text(
                    json.dumps({"action": "PROPOSE_DATASET", "spec": _SYNTHETIC_SPEC})
                ),
                _wrap_assistant_text(json.dumps(done_payload)),
            ]
            runner = _FakeRunner(responses)
            plan = run_research_refiner(
                REPO_ROOT,
                grilling_session=_grilling_session(),
                market_research_brief=_market_brief(tmp_path),
                run_dir=tmp_path / "refine",
                billing_ack=True,
                execution_ack=True,
                command_runner=runner,
                input_provider=lambda q: "narrow_claim",
                allowed_domains=["retrieval"],
            )
            self.assertEqual(plan["status"], "done")
            self.assertEqual(len(plan["rounds"]), 3)
            self.assertEqual(plan["rounds"][0]["action"], "ASK")
            self.assertEqual(plan["rounds"][0]["user_response"], "narrow_claim")
            self.assertEqual(plan["rounds"][1]["action"], "PROPOSE_DATASET")
            self.assertEqual(plan["rounds"][1]["materialize_result"]["status"], "ok")
            self.assertEqual(plan["rounds"][2]["action"], "DONE")
            self.assertEqual(len(plan["dataset_specs"]), 1)
            self.assertEqual(plan["dataset_specs"][0]["id"], _SYNTHETIC_SPEC["id"])
            self.assertEqual(len(plan["sota_reconciliations"]), 1)
            manifest = json.loads(Path(plan["dataset_manifest_path"]).read_text())
            self.assertIn(_SYNTHETIC_SPEC["id"], manifest["datasets"])

    def test_failed_propose_does_not_appear_in_dataset_specs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_spec = {
                "id": "ds_unsupported",
                "type": "benchmark",
                "role": "evaluation",
                "source": "s3://team-bucket/legal-eval/",
            }
            done_payload = {
                "action": "DONE",
                "plan": {
                    "claim_under_test": "Calibrated retrieval works.",
                    "mandatory_baselines": ["a"],
                    "success_criteria": ["b"],
                    "disproof_conditions": ["c"],
                    "validation_procedure": _GOOD_VP,
                    "dataset_specs": [bad_spec],
                    "unresolved_dataset_specs": [],
                    "sota_reconciliations": [],
                    "acknowledged_limitations": [],
                },
            }
            responses = [
                _wrap_assistant_text(
                    json.dumps({"action": "PROPOSE_DATASET", "spec": bad_spec})
                ),
                _wrap_assistant_text(json.dumps(done_payload)),
            ]
            runner = _FakeRunner(responses)
            plan = run_research_refiner(
                REPO_ROOT,
                grilling_session=_grilling_session(),
                market_research_brief=_market_brief(tmp_path),
                run_dir=tmp_path / "refine",
                billing_ack=True,
                execution_ack=True,
                command_runner=runner,
                input_provider=lambda q: "",
                allowed_domains=["retrieval"],
            )
            # bad_spec materialize returned needs_user_input -> not verified
            # _assemble_plan drops un-verified declared specs from dataset_specs
            # and moves them into unresolved_dataset_specs.
            self.assertEqual(plan["dataset_specs"], [])
            self.assertEqual(
                [s["id"] for s in plan["unresolved_dataset_specs"]],
                [bad_spec["id"]],
            )


class RefinerPendingAskTests(unittest.TestCase):
    """Regression: an emitted refiner ASK used to live only on the SSE
    stream + worker's local variable. Navigating away between Q-emit and
    user-reply hid the question forever. Persist to pending_ask on disk
    so any page reload can show the in-flight question."""

    def test_pending_ask_is_on_disk_when_asker_blocks(self) -> None:
        captured: dict = {}

        def _spy_asker(question: str) -> str:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            captured["question"] = (plan.get("pending_ask") or {}).get("question")
            captured["rounds_at_emit"] = len(plan["rounds"])
            return "narrow_claim"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            done_payload = {
                "action": "DONE",
                "plan": {
                    "claim_under_test": "x",
                    "mandatory_baselines": ["a", "b", "c"],
                    "success_criteria": ["x"],
                    "disproof_conditions": ["y"],
                    "validation_procedure": _GOOD_VP,
                    "dataset_specs": [_SYNTHETIC_SPEC],
                    "unresolved_dataset_specs": [],
                    "sota_reconciliations": [
                        {
                            "conflict": "c",
                            "evidence_paper_url": "https://arxiv.org/abs/x",
                            "user_choice": "narrow_claim",
                            "resolution": "r",
                            "resolved_at_round": 0,
                        }
                    ],
                    "acknowledged_limitations": ["x"],
                },
            }
            responses = [
                _wrap_assistant_text(
                    json.dumps({"action": "ASK", "question": "narrow or acknowledge?"})
                ),
                _wrap_assistant_text(
                    json.dumps({"action": "PROPOSE_DATASET", "spec": _SYNTHETIC_SPEC})
                ),
                _wrap_assistant_text(json.dumps(done_payload)),
            ]
            run_dir = tmp_path / "refine"
            plan_path = run_dir / "refined_research_plan.json"
            run_research_refiner(
                REPO_ROOT,
                grilling_session=_grilling_session(),
                market_research_brief=_market_brief(tmp_path),
                run_dir=run_dir,
                billing_ack=True,
                execution_ack=True,
                command_runner=_FakeRunner(responses),
                input_provider=_spy_asker,
            )
            # At the moment asker was called, the pending question was on disk
            # and rounds was still empty.
            self.assertEqual(captured["question"], "narrow or acknowledge?")
            self.assertEqual(captured["rounds_at_emit"], 0)
            # And after DONE the pending_ask is cleared.
            final = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertIsNone(final.get("pending_ask"))


class RefinerErrorTests(unittest.TestCase):
    def test_unsupported_action_aborts_with_schema_valid_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runner = _FakeRunner(
                [_wrap_assistant_text(json.dumps({"action": "GIBBERISH"}))]
            )
            with self.assertRaises(RefinerError):
                run_research_refiner(
                    REPO_ROOT,
                    grilling_session=_grilling_session(),
                    market_research_brief=_market_brief(tmp_path),
                    run_dir=tmp_path / "refine",
                    billing_ack=True,
                    execution_ack=True,
                    command_runner=runner,
                    input_provider=lambda q: "",
                    allowed_domains=["retrieval"],
                )
            saved = json.loads((tmp_path / "refine" / "refined_research_plan.json").read_text())
            self.assertEqual(saved["status"], "aborted")
            self.assertIn("unsupported refiner action", saved["error"])
            validate_named_schema("refined_research_plan", saved)


if __name__ == "__main__":
    unittest.main()
