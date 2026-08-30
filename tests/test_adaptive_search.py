from __future__ import annotations

from copy import deepcopy

import pytest

from research_harness.orchestrator.adaptive_search import (
    AdaptiveSearchError,
    build_research_goal,
    capabilities_from_envelope,
    derive_search_disposition,
    experiment_fingerprint,
    make_negative_observation,
    normalize_required_capabilities,
    prepare_strategy_expansion,
)


def _goal() -> dict:
    return build_research_goal(
        thread={"user_goal": "Beat the word baseline on the held-out taxonomy."},
        grilling={
            "user_goal": "Beat the word baseline on the held-out taxonomy.",
            "extracted": {
                "claim_under_test": "A character model beats the word baseline.",
                "mandatory_baselines": ["word baseline"],
                "success_criteria": ["accuracy improves", "macro-F1 improves"],
                "disproof_conditions": ["either metric does not improve"],
                "taste_constraints": ["CPU-only", "fixed split"],
            },
        },
        envelope={
            "operator_intent": {
                "target_deploy_grade_scope": "deployment",
                "data_source_anchor": "taxonomy",
                "data_source_snapshot_id": "as_" + "1" * 64,
            },
            "external_falsifier": {"kind": "real_holdout"},
        },
    )


def test_runtime_capabilities_normalize_legacy_resource_prose() -> None:
    available = capabilities_from_envelope(
        {
            "compute_budget": {"max_runner_seconds_per_node": 60},
            "runtime_capabilities": [
                "local_runner",
                "accelerator:cuda",
                "module:torch",
                "data:fashion_mnist_dev_gpu",
            ],
        }
    )

    required = normalize_required_capabilities(
        [
            "CUDA-enabled PyTorch",
            "fixed spatial permutation implementation",
            "fashion_mnist_dev_gpu adapter",
        ]
    )

    assert required <= available
    assert normalize_required_capabilities(["data:private_holdout"]) == {
        "data:private_holdout"
    }


def _candidate(
    family: str,
    mechanism: str,
    intervention: str,
    gap: str,
) -> dict:
    return {
        "type": "mechanism",
        "successor_claim": f"Test {family} against the frozen bar.",
        "rationale": f"The failed observation points to {mechanism}.",
        "strategy_family": family,
        "mechanism": mechanism,
        "intervention": intervention,
        "information_target": f"Whether {mechanism} explains the failure.",
        "predicted_outcomes": [
            "If present, the intervention improves the failed comparison.",
            "If absent, the failed comparison remains unchanged.",
        ],
        "tests_bar_gaps": [gap],
        "required_capabilities": ["data:taxonomy"],
        "estimated_cost": 0.25,
    }


def test_research_goal_is_content_addressed_and_problem_level() -> None:
    goal = _goal()
    same = _goal()

    assert goal == same
    assert goal["id"].startswith("goal_")
    assert goal["bar_digest"].startswith("sha256:")
    assert goal["bar"]["target_scope"] == "deployment"
    assert goal["bar"]["data_source_snapshot_id"] == "as_" + "1" * 64


def test_negative_expansion_requires_two_causal_strategies_on_same_bar() -> None:
    goal = _goal()
    observation = make_negative_observation(
        node_id="n_parent",
        worker_report={
            "claim_verdict_candidate": "contradicted",
            "metrics": {"accuracy": 0.4},
            "baselines": {"word": 0.4},
            "input_evidence": {"snapshot_id": "as_" + "1" * 64},
        },
        final_verdict="accuracy tied the word baseline",
    )
    prepared = prepare_strategy_expansion(
        goal=goal,
        observation=observation,
        candidates=[
            _candidate(
                "boundary-aware features",
                "word boundaries erase discriminative fragments",
                "add boundary-crossing character features",
                "accuracy improves",
            ),
            _candidate(
                "archive-conditioned smoothing",
                "rare archives are over-smoothed",
                "condition smoothing on archive support",
                "macro-F1 improves",
            ),
        ],
        known_strategy_ids=set(),
        available_capabilities={"data:taxonomy"},
        depth=2,
        max_depth=5,
    )

    assert len(prepared) == 2
    assert len({item["id"] for item in prepared}) == 2
    assert {item["goal_id"] for item in prepared} == {goal["id"]}
    assert all(item["derived_from_observation_id"] == observation["id"] for item in prepared)
    assert all(item["priority"]["evidence_basis"] == [observation["id"]] for item in prepared)


def test_negative_expansion_rejects_verification_axis_clones() -> None:
    goal = _goal()
    observation = make_negative_observation(
        node_id="n_parent", worker_report={}, final_verdict="negative"
    )
    duplicate = _candidate(
        "validity",
        "the same causal mechanism",
        "the same executable intervention",
        "accuracy improves",
    )

    with pytest.raises(AdaptiveSearchError, match="distinct"):
        prepare_strategy_expansion(
            goal=goal,
            observation=observation,
            candidates=[duplicate, deepcopy(duplicate)],
            known_strategy_ids=set(),
            available_capabilities={"data:taxonomy"},
            depth=1,
            max_depth=5,
        )


def test_priority_measures_causal_novelty_against_known_strategies() -> None:
    goal = _goal()
    observation = make_negative_observation(
        node_id="n_parent", worker_report={}, final_verdict="negative"
    )
    prepared = prepare_strategy_expansion(
        goal=goal,
        observation=observation,
        candidates=[
            _candidate(
                "similar smoothing",
                "rare archives are over smoothed",
                "condition smoothing on archive support",
                "accuracy improves",
            ),
            _candidate(
                "boundary interactions",
                "token boundaries erase cross-fragment interactions",
                "add boundary crossing interaction features",
                "macro-F1 improves",
            ),
        ],
        known_strategy_ids={"strategy_existing"},
        known_strategies=[
            {
                "mechanism": "rare archives are over-smoothed",
                "intervention": "condition smoothing on archive support",
            }
        ],
        available_capabilities={"data:taxonomy"},
        depth=1,
        max_depth=5,
    )

    assert (
        prepared[0]["priority"]["mechanism_novelty"]
        < prepared[1]["priority"]["mechanism_novelty"]
    )


def test_experiment_fingerprint_ignores_node_labels_but_tracks_recipe() -> None:
    plan = {
        "node_id": "n_first",
        "plan_id": "plan_first",
        "objective": "First wording",
        "entrypoint": {"command": ["python3"], "args": ["src/experiment.py"]},
        "source_files": [{"path": "src/experiment.py", "content": "print(1)", "purpose": "x"}],
        "inputs": {"snapshot_id": "as_" + "1" * 64},
        "baseline_evidence_requirements": [{"metric_key": "m", "baseline_key": "b"}],
        "resources": {"timeout_sec": 10},
        "reproducibility": {
            "seed": 7,
            "code_snapshot": "professor_templates/n_first",
            "data_snapshot": "snapshot-a",
        },
    }
    renamed = deepcopy(plan)
    renamed.update(node_id="n_second", plan_id="plan_second", objective="Second wording")
    renamed["reproducibility"]["code_snapshot"] = "professor_templates/n_second"

    assert experiment_fingerprint(plan) == experiment_fingerprint(renamed)

    changed = deepcopy(renamed)
    changed["source_files"][0]["content"] = "print(2)"
    assert experiment_fingerprint(plan) != experiment_fingerprint(changed)


def test_observation_identity_collapses_evidence_equivalent_nodes() -> None:
    report = {
        "claim_verdict_candidate": "contradicted",
        "metrics": {"accuracy": 0.4},
        "baselines": {"word": 0.4},
        "input_evidence": {"snapshot_id": "as_" + "1" * 64},
    }

    first = make_negative_observation(
        node_id="n_first", worker_report=report, final_verdict="tied"
    )
    second = make_negative_observation(
        node_id="n_second", worker_report=report, final_verdict="same tie"
    )

    assert first["id"] == second["id"]
    assert first["node_id"] != second["node_id"]


def test_only_verified_strong_receipt_can_complete() -> None:
    assert derive_search_disposition(
        has_queued_work=False,
        pause={"reason": "needs_strategy_expansion"},
        verified_strong_receipt=None,
    ) == "paused_needs_expansion"
    assert derive_search_disposition(
        has_queued_work=True,
        pause=None,
        verified_strong_receipt=None,
    ) == "continue"
    assert derive_search_disposition(
        has_queued_work=False,
        pause=None,
        verified_strong_receipt={"goal_id": _goal()["id"], "verified": True},
    ) == "goal_achieved"

    with pytest.raises(AdaptiveSearchError, match="verified strong-result receipt"):
        derive_search_disposition(
            has_queued_work=False,
            pause=None,
            verified_strong_receipt={"goal_id": _goal()["id"], "verified": False},
        )
