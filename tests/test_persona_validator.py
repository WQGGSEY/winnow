"""Unit tests for the MCP-boundary persona validator.

Each rule is tested in isolation: a violating input should fail, a clean
input should pass, and the rejection message must name the failing rule so
Claude Code can correct the next attempt.
"""

from __future__ import annotations

import unittest

from research_harness.orchestrator.llm_orchestrator.persona_validator import (
    validate_claim_contract,
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


class AntiLazinessBaselineProvenanceTests(unittest.TestCase):
    def test_self_made_baseline_rejected(self):
        from research_harness.orchestrator.llm_orchestrator.persona_validator import (
            validate_baseline_provenance,
        )
        baseline_refs = [
            {"baseline_dossier_id": "bd_x", "roles": ["current_best_known"],
             "candidates": ["c_made_up"]}
        ]
        dossier = {"candidates_index": [{"id": "c_made_up", "label": "self-defined"}]}
        r = validate_baseline_provenance(baseline_refs=baseline_refs, market_dossier=dossier)
        self.assertFalse(r.ok)
        self.assertIn("self_made_baseline", [v.rule for v in r.violations])

    def test_paper_cited_baseline_passes(self):
        from research_harness.orchestrator.llm_orchestrator.persona_validator import (
            validate_baseline_provenance,
        )
        baseline_refs = [
            {"baseline_dossier_id": "bd_x", "roles": ["current_best_known"],
             "candidates": ["c_real"]}
        ]
        dossier = {"candidates_index": [
            {"id": "c_real", "label": "Smith 2024", "arxiv_id": "2401.12345"}
        ]}
        r = validate_baseline_provenance(baseline_refs=baseline_refs, market_dossier=dossier)
        self.assertTrue(r.ok, r.reject_message())


class AntiLazinessSyntheticBridgingTests(unittest.TestCase):
    def test_synthetic_without_bridging_rejected(self):
        from research_harness.orchestrator.llm_orchestrator.persona_validator import (
            validate_synthetic_data_bridging,
        )
        r = validate_synthetic_data_bridging(dataset_manifest={"data_source": "synthetic"})
        self.assertFalse(r.ok)
        self.assertIn(
            "synthetic_data_without_bridging", [v.rule for v in r.violations]
        )

    def test_synthetic_with_bridging_passes(self):
        from research_harness.orchestrator.llm_orchestrator.persona_validator import (
            validate_synthetic_data_bridging,
        )
        manifest = {
            "data_source": "synthetic",
            "synthetic_to_real_bridging_argument": (
                "MLP-(16,8) generator over N=30 assets, T=252 days. Real WorldQuant "
                "SNR estimated 0.05-0.15 (Lopez de Prado 2018 ch.11); span covers that. "
                "Real validation still required for deploy."
            ),
        }
        r = validate_synthetic_data_bridging(dataset_manifest=manifest)
        self.assertTrue(r.ok, r.reject_message())

    def test_real_data_skips_check(self):
        from research_harness.orchestrator.llm_orchestrator.persona_validator import (
            validate_synthetic_data_bridging,
        )
        r = validate_synthetic_data_bridging(
            dataset_manifest={"data_source": "real_panel.parquet"}
        )
        self.assertTrue(r.ok, r.reject_message())


class AntiLazinessDirectivesTests(unittest.TestCase):
    def test_disclaimer_only_directives_rejected(self):
        from research_harness.orchestrator.llm_orchestrator.persona_validator import (
            validate_camera_ready_directives,
        )
        directives = [
            {"directive": "Add disclaimer.", "requires_new_measurement": False},
            {"directive": "Note seed.", "requires_new_measurement": False},
        ]
        r = validate_camera_ready_directives(directives=directives)
        self.assertFalse(r.ok)
        self.assertIn("disclaimer_only_directives", [v.rule for v in r.violations])

    def test_at_least_one_measurement_directive_passes(self):
        from research_harness.orchestrator.llm_orchestrator.persona_validator import (
            validate_camera_ready_directives,
        )
        directives = [
            {"directive": "Add disclaimer.", "requires_new_measurement": False},
            {"directive": "Run SNR sweep [0.05, 0.15, 0.30] n=50 seeds=3.",
             "requires_new_measurement": True},
        ]
        r = validate_camera_ready_directives(directives=directives)
        self.assertTrue(r.ok, r.reject_message())


class AntiLazinessDecisionRuleTests(unittest.TestCase):
    def test_auc_only_claim_rejected(self):
        from research_harness.orchestrator.llm_orchestrator.persona_validator import (
            validate_decision_rule_for_capability_claim,
        )
        claim = {
            "claim_under_test": "Method achieves ROC AUC >= 0.80 on held-out.",
            "success_criteria": ["AUC >= 0.80"],
        }
        r = validate_decision_rule_for_capability_claim(claim_contract=claim)
        self.assertFalse(r.ok)
        self.assertIn(
            "capability_claim_without_decision_rule", [v.rule for v in r.violations]
        )

    def test_auc_with_decision_rule_passes(self):
        from research_harness.orchestrator.llm_orchestrator.persona_validator import (
            validate_decision_rule_for_capability_claim,
        )
        claim = {
            "claim_under_test": (
                "Method achieves AUC >= 0.80 AND, at operating-point threshold T "
                "calibrated to false-positive rate <= 0.05, supports a "
                "deploy-when decision rule."
            ),
            "success_criteria": ["AUC >= 0.80", "FP-rate <= 0.05"],
        }
        r = validate_decision_rule_for_capability_claim(claim_contract=claim)
        self.assertTrue(r.ok, r.reject_message())

    def test_non_capability_claim_skipped(self):
        from research_harness.orchestrator.llm_orchestrator.persona_validator import (
            validate_decision_rule_for_capability_claim,
        )
        claim = {
            "claim_under_test": "Method respects in-sample-only taste constraint.",
            "success_criteria": ["procedural compliance audit passes"],
        }
        r = validate_decision_rule_for_capability_claim(claim_contract=claim)
        self.assertTrue(r.ok, r.reject_message())


if __name__ == "__main__":
    unittest.main()
