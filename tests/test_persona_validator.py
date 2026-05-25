"""Unit tests for the MCP-boundary persona validator.

Each rule is tested in isolation: a violating input should fail, a clean
input should pass, and the rejection message must name the failing rule so
Claude Code can correct the next attempt.
"""

from __future__ import annotations

import unittest

from research_harness.orchestrator.llm_orchestrator.persona_validator import (
    validate_claim_contract,
    validate_follow_up_strength,
    validate_grad_student_review,
)


CFG_ALL = {
    "reject_problem_restatement": True,
    "reject_placeholder_baselines": True,
    "reject_missing_measurable_success": True,
    "reject_unhittable_disproof": True,
    "require_market_grounding": True,
    "require_grad_student_challenge": True,
    "min_baseline_role_specificity_chars": 40,
}


def _good_claim() -> dict:
    return {
        "claim_under_test": (
            "A novel methodology M achieves AUROC>=0.85 for signal vs noise "
            "alpha discrimination on the synthetic ground-truth setup, "
            "exceeding the rolling-IC heuristic baseline."
        ),
        "mandatory_baselines": [
            "current_best: Rolling-IC with autocorrelation gate (Robertson 2009)",
            "naive: simple IC threshold of 0.05 without OOS correction",
            "random_or_null: Permutation null over shuffled future returns",
        ],
        "success_criteria": ["AUROC >= 0.80 on the binary signal-vs-noise task"],
        "disproof_conditions": ["AUROC < 0.55 in any synthetic ground-truth setup"],
    }


def _good_market_ctx() -> dict:
    return {
        "baseline_dossier_yaml": (
            "current_best: Rolling-IC with autocorrelation gate Robertson 2009\n"
            "naive: simple IC threshold without OOS correction\n"
            "random_or_null: Permutation null shuffled future returns\n"
        ),
        "baseline_analysis_md": "Robertson 2009 rolling IC is the strongest published baseline.",
        "reference_papers": [{"filename": "robertson_2009_rolling_ic.pdf"}],
    }


class PersonaValidatorTests(unittest.TestCase):
    def test_clean_claim_with_market_passes(self) -> None:
        r = validate_claim_contract(
            new_claim=_good_claim(),
            problem_statement="how to test machine alpha robustness",
            market_context=_good_market_ctx(),
            config=CFG_ALL,
        )
        self.assertTrue(r.ok, msg=[v.rule for v in r.violations])

    def test_problem_restatement_rejected(self) -> None:
        problem = (
            "We want a methodology that distinguishes genuine signal from "
            "overfit alpha in machine-generated factors."
        )
        claim = dict(_good_claim())
        claim["claim_under_test"] = problem  # exact restatement
        r = validate_claim_contract(
            new_claim=claim,
            problem_statement=problem,
            market_context=_good_market_ctx(),
            config=CFG_ALL,
        )
        self.assertFalse(r.ok)
        self.assertIn("problem_restatement", [v.rule for v in r.violations])

    def test_placeholder_baseline_rejected(self) -> None:
        claim = dict(_good_claim())
        claim["mandatory_baselines"] = [
            "current_best: TBD (Professor will design)",
            "naive: TBD",
            "random_or_null: TBD",
        ]
        r = validate_claim_contract(
            new_claim=claim,
            problem_statement="something",
            market_context=_good_market_ctx(),
            config=CFG_ALL,
        )
        self.assertFalse(r.ok)
        rules = [v.rule for v in r.violations]
        self.assertTrue(
            "placeholder_baseline" in rules or "baseline_too_vague" in rules
        )

    def test_no_measurable_success_rejected(self) -> None:
        claim = dict(_good_claim())
        claim["success_criteria"] = ["the method works well", "robust to noise"]
        r = validate_claim_contract(
            new_claim=claim,
            problem_statement="x",
            market_context=_good_market_ctx(),
            config=CFG_ALL,
        )
        self.assertFalse(r.ok)
        self.assertIn(
            "success_no_measurable_threshold",
            [v.rule for v in r.violations],
        )

    def test_unhittable_disproof_rejected(self) -> None:
        claim = dict(_good_claim())
        claim["disproof_conditions"] = [
            "the methodology turns out to be philosophically untenable"
        ]
        r = validate_claim_contract(
            new_claim=claim,
            problem_statement="x",
            market_context=_good_market_ctx(),
            config=CFG_ALL,
        )
        self.assertFalse(r.ok)
        self.assertIn("disproof_unhittable", [v.rule for v in r.violations])

    def test_baselines_ignoring_market_rejected(self) -> None:
        claim = dict(_good_claim())
        claim["mandatory_baselines"] = [
            "current_best: some unrelated generic methodology with enough chars to pass length",
            "naive: a completely different approach unrelated to the market output entirely",
            "random_or_null: yet another approach not grounded in dossier or papers at all",
        ]
        r = validate_claim_contract(
            new_claim=claim,
            problem_statement="x",
            market_context=_good_market_ctx(),
            config=CFG_ALL,
        )
        self.assertFalse(r.ok)
        self.assertIn(
            "baselines_ignore_market_output",
            [v.rule for v in r.violations],
        )


class GradStudentValidatorTests(unittest.TestCase):
    def test_pure_agreement_rejected(self) -> None:
        r = validate_grad_student_review(
            commentary="Looks great, advisor. I will proceed.",
            concerns=[],
            config=CFG_ALL,
        )
        self.assertFalse(r.ok)
        self.assertIn("grad_student_no_challenge", [v.rule for v in r.violations])

    def test_korean_pushback_accepted(self) -> None:
        r = validate_grad_student_review(
            commentary=(
                "교수님, 잠깐 짚어둘 게 있어요. synthetic data가 너무 "
                "이상화되어 있어서 OOD에서는 다를 수 있어요."
            ),
            concerns=[],
            config=CFG_ALL,
        )
        self.assertTrue(r.ok)

    def test_english_pushback_accepted(self) -> None:
        r = validate_grad_student_review(
            commentary=(
                "Wait — the synthetic setup looks too idealized. I'm "
                "concerned that the generator and evaluator share latent "
                "structure."
            ),
            concerns=[],
            config=CFG_ALL,
        )
        self.assertTrue(r.ok)

    def test_explicit_concerns_count_as_challenge(self) -> None:
        r = validate_grad_student_review(
            commentary="OK.",
            concerns=[{"summary": "x", "evidence": "y"}],
            config=CFG_ALL,
        )
        self.assertTrue(r.ok)


class FollowUpStrengthTests(unittest.TestCase):
    def test_strong_follow_ups_accepted(self) -> None:
        r = validate_follow_up_strength(
            parent_claim="The methodology beats baselines on AUROC.",
            follow_ups=[
                {
                    "type": "mechanism",
                    "successor_claim": "The methodology's improvement comes from the MI-audit component, not the permutation step.",
                    "rationale": "isolate mechanism",
                },
                {
                    "type": "necessity",
                    "successor_claim": "Same-budget alternatives without the MI audit do not match the AUROC threshold.",
                    "rationale": "necessity",
                },
            ],
            config=CFG_ALL,
        )
        self.assertTrue(r.ok)

    def test_all_restating_follow_ups_rejected(self) -> None:
        r = validate_follow_up_strength(
            parent_claim="The methodology beats baselines on AUROC.",
            follow_ups=[
                {
                    "type": "mechanism",
                    "successor_claim": "The methodology beats baselines on AUROC.",
                },
                {
                    "type": "necessity",
                    "successor_claim": "The methodology beats baselines on AUROC.",
                },
            ],
            config=CFG_ALL,
        )
        self.assertFalse(r.ok)
        self.assertIn("follow_ups_all_weaken", [v.rule for v in r.violations])


if __name__ == "__main__":
    unittest.main()
