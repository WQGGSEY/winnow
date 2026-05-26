"""PR7 tests: feasibility envelope + claim-fits-envelope validator."""

from __future__ import annotations

import unittest

from research_harness.orchestrator.llm_orchestrator.persona_validator import (
    validate_claim_fits_envelope,
)
from research_harness.schemas.validator import (
    SchemaValidationError,
    validate_named_schema,
)


def _valid_envelope() -> dict:
    return {
        "thread_id": "t1",
        "data_sources_available": [
            {"kind": "real_adapter", "id": "wq_snapshot", "scope_note": "S&P 500"},
            {"kind": "synthetic", "id": "synth_panel_v1"},
        ],
        "llm_oracles_available": [
            {"kind": "subscription_claude_code", "model": "claude-opus-4-7"},
        ],
        "compute_budget": {
            "max_runner_seconds_per_node": 900,
            "max_concurrent_nodes": 2,
            "max_total_node_hours": 4.0,
        },
        "baseline_provenance_available": [
            {"candidate_id": "c1", "provenance": "arxiv:2401.12345", "role_hint": "current_best_known"},
        ],
        "operator_intent": {
            "target_deploy_grade_scope": "deployment",
            "acceptable_alternative_scopes": ["feasibility", "directional"],
        },
    }


class FeasibilityEnvelopeSchemaTests(unittest.TestCase):
    def test_valid_envelope_passes(self):
        validate_named_schema("feasibility_envelope", _valid_envelope())

    def test_missing_operator_intent_rejected(self):
        env = _valid_envelope()
        del env["operator_intent"]
        with self.assertRaisesRegex(SchemaValidationError, "operator_intent"):
            validate_named_schema("feasibility_envelope", env)

    def test_invalid_target_scope_rejected(self):
        env = _valid_envelope()
        env["operator_intent"]["target_deploy_grade_scope"] = "production_grade"
        with self.assertRaisesRegex(SchemaValidationError, "enum"):
            validate_named_schema("feasibility_envelope", env)


class ClaimFitsEnvelopeTests(unittest.TestCase):
    def test_no_envelope_no_scope_passes_backward_compat(self):
        r = validate_claim_fits_envelope(
            claim_contract={"claim_under_test": "x"},
            envelope=None,
        )
        self.assertTrue(r.ok, r.reject_message())

    def test_scope_without_envelope_rejected(self):
        r = validate_claim_fits_envelope(
            claim_contract={"claim_under_test": "x", "deploy_grade_scope": "deployment"},
            envelope=None,
        )
        self.assertFalse(r.ok)
        self.assertIn("missing_feasibility_envelope", [v.rule for v in r.violations])

    def test_envelope_without_scope_rejected(self):
        r = validate_claim_fits_envelope(
            claim_contract={"claim_under_test": "x"},
            envelope=_valid_envelope(),
        )
        self.assertFalse(r.ok)
        self.assertIn("missing_deploy_grade_scope", [v.rule for v in r.violations])

    def test_directional_scope_passes_with_envelope(self):
        r = validate_claim_fits_envelope(
            claim_contract={"claim_under_test": "x", "deploy_grade_scope": "directional"},
            envelope=_valid_envelope(),
        )
        self.assertTrue(r.ok, r.reject_message())

    def test_deployment_scope_with_synthetic_only_envelope_rejected(self):
        env = _valid_envelope()
        env["data_sources_available"] = [{"kind": "synthetic", "id": "s1"}]
        r = validate_claim_fits_envelope(
            claim_contract={
                "claim_under_test": "x",
                "deploy_grade_scope": "deployment",
                "data_source_anchor": "s1",
            },
            envelope=env,
            registered_adapter_ids=set(),
        )
        self.assertFalse(r.ok)
        rules = [v.rule for v in r.violations]
        self.assertIn("deployment_scope_without_real_adapter_in_envelope", rules)

    def test_deployment_scope_with_registered_adapter_passes(self):
        r = validate_claim_fits_envelope(
            claim_contract={
                "claim_under_test": "x",
                "deploy_grade_scope": "deployment",
                "data_source_anchor": "wq_snapshot",
            },
            envelope=_valid_envelope(),
            registered_adapter_ids={"wq_snapshot"},
        )
        self.assertTrue(r.ok, r.reject_message())

    def test_deployment_scope_with_unregistered_adapter_rejected(self):
        r = validate_claim_fits_envelope(
            claim_contract={
                "claim_under_test": "x",
                "deploy_grade_scope": "deployment",
                "data_source_anchor": "wq_snapshot",
            },
            envelope=_valid_envelope(),
            registered_adapter_ids=set(),  # nothing registered
        )
        self.assertFalse(r.ok)
        self.assertIn(
            "deployment_anchor_not_registered", [v.rule for v in r.violations]
        )

    def test_feasibility_scope_with_synthetic_anchor_passes(self):
        r = validate_claim_fits_envelope(
            claim_contract={
                "claim_under_test": "x",
                "deploy_grade_scope": "feasibility",
                "data_source_anchor": "synthetic:mlp_16_8",
            },
            envelope=_valid_envelope(),
        )
        self.assertTrue(r.ok, r.reject_message())

    def test_feasibility_scope_without_anchor_rejected(self):
        r = validate_claim_fits_envelope(
            claim_contract={
                "claim_under_test": "x",
                "deploy_grade_scope": "feasibility",
            },
            envelope=_valid_envelope(),
        )
        self.assertFalse(r.ok)
        self.assertIn(
            "feasibility_scope_without_data_anchor", [v.rule for v in r.violations]
        )


if __name__ == "__main__":
    unittest.main()
