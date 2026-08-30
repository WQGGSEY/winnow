"""Pure policy for evidence-driven, strong-result-only research search.

The active MCP server owns persistence and transport parsing. This module owns
the domain identities and decisions that must not depend on MCP wording:

- one content-addressed problem-level goal,
- causal strategy identity,
- measured observation identity,
- executable experiment identity,
- evidence-linked frontier priority, and
- the three scientific search dispositions.

The functions return plain JSON-compatible dictionaries so the first active-path
slice can live inside the existing ``search_state.json`` transaction. A later
persistence migration can wrap the same policy in richer domain classes without
changing its invariants.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping


class AdaptiveSearchError(ValueError):
    """Raised when adaptive search input would violate a scientific invariant."""


_VERIFICATION_AXES = {
    "boundary",
    "capability",
    "constraint",
    "mechanism",
    "necessity",
    "operational",
    "taste",
    "validity",
}

_CAPABILITY_ID = re.compile(
    r"^(?:local_runner|cpu|accelerator:[a-z0-9_.-]+|module:[a-z0-9_.-]+|"
    r"data:[a-z0-9_.-]+|oracle:[a-z0-9_.-]+)$"
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _normalized_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _strings(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _without_placeholders(values: object) -> list[str]:
    placeholders = ("tbd", "to be defined", "professor must define", "various")
    return [
        value
        for value in _strings(values)
        if not any(marker in value.lower() for marker in placeholders)
    ]


def build_research_goal(
    *,
    thread: Mapping[str, Any],
    grilling: Mapping[str, Any],
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the immutable problem-level goal from pre-generation artifacts.

    Connector reductions and child nodes are intentionally absent. They are
    strategy proposals and therefore cannot author the bar they will be judged
    against.
    """

    extracted = grilling.get("extracted") or {}
    intent = envelope.get("operator_intent") or {}
    question = str(
        thread.get("user_goal")
        or grilling.get("user_goal")
        or extracted.get("claim_under_test")
        or ""
    ).strip()
    if not question:
        raise AdaptiveSearchError(
            "cannot freeze research goal without a pre-generation user goal"
        )

    round_requirements = [
        str(round_record.get("user_response") or "").strip()
        for round_record in grilling.get("rounds") or []
        if isinstance(round_record, Mapping)
        and str(round_record.get("user_response") or "").strip()
    ]
    operator_requirements = list(
        dict.fromkeys(
            [
                question,
                *round_requirements,
                *_strings(extracted.get("taste_constraints")),
            ]
        )
    )
    success_criteria = list(
        dict.fromkeys(
            [
                *_without_placeholders(extracted.get("success_criteria")),
                *operator_requirements,
            ]
        )
    )
    disproof_conditions = _without_placeholders(
        extracted.get("disproof_conditions")
    )
    if not disproof_conditions:
        disproof_conditions = [
            requirement
            for requirement in operator_requirements
            if any(
                marker in requirement.lower()
                for marker in ("fail", "refut", "disproof", "실패")
            )
        ]
    mandatory_baselines = _without_placeholders(
        extracted.get("mandatory_baselines")
    )
    if not mandatory_baselines:
        mandatory_baselines = [
            requirement
            for requirement in operator_requirements
            if "baseline" in requirement.lower()
        ]
    bar = {
        "claim_under_test": str(extracted.get("claim_under_test") or question).strip(),
        "mandatory_baselines": mandatory_baselines,
        "success_criteria": success_criteria,
        "disproof_conditions": disproof_conditions or [f"The operator goal fails: {question}"],
        "operator_requirements": operator_requirements,
        "target_scope": str(
            intent.get("target_deploy_grade_scope") or "directional"
        ),
        "data_source_anchor": intent.get("data_source_anchor"),
        "data_source_snapshot_id": intent.get("data_source_snapshot_id"),
        "external_falsifier": envelope.get("external_falsifier") or {},
    }
    bar_digest = hashlib.sha256(_canonical_json(bar).encode("utf-8")).hexdigest()
    identity = {"question": question, "bar_digest": bar_digest}
    return {
        "id": _content_id("goal", identity),
        "question": question,
        "bar": bar,
        "bar_digest": f"sha256:{bar_digest}",
        "source": "pre_generation",
        "strong_completion_blocked": False,
    }


def initialize_adaptive_state(goal: Mapping[str, Any]) -> dict[str, Any]:
    """Create the versioned adaptive envelope for an existing search state."""

    return {
        "version": 1,
        "revision": 0,
        "goal": dict(goal),
        "strategies": [],
        "observations": [],
        "experiments": [],
        "duplicate_rejections": [],
        "command_receipts": {},
        "disposition": "continue",
        "pause": None,
        "strong_result_receipt": None,
    }


def capabilities_from_envelope(envelope: Mapping[str, Any]) -> set[str]:
    """Derive dispatchable capabilities from the operator-owned envelope."""

    capabilities = {"local_runner"}
    for source in envelope.get("data_sources_available") or []:
        if not isinstance(source, Mapping):
            continue
        source_id = str(source.get("id") or "").strip()
        if source_id:
            capabilities.add(f"data:{source_id}")
        kind = str(source.get("kind") or "").strip()
        if kind:
            capabilities.add(kind)
    for oracle in envelope.get("llm_oracles_available") or []:
        if not isinstance(oracle, Mapping):
            continue
        kind = str(oracle.get("kind") or "").strip()
        if kind:
            capabilities.add(f"oracle:{kind}")
    compute = envelope.get("compute_budget") or {}
    if compute:
        capabilities.add("cpu")
    capabilities.update(
        str(value).strip()
        for value in envelope.get("runtime_capabilities") or []
        if _CAPABILITY_ID.fullmatch(str(value).strip())
    )
    return capabilities


def normalize_required_capabilities(values: object) -> set[str]:
    """Translate legacy prose requirements into stable resource identifiers.

    New strategy proposals should emit canonical IDs. Existing search states
    predate that contract, so this bridge recognizes only explicit resource
    words and leaves unknown prose unsatisfied instead of guessing.
    """

    normalized: set[str] = set()
    for value in _strings(values):
        if _CAPABILITY_ID.fullmatch(value):
            normalized.add(value)
            continue
        lower = _normalized_text(value)
        recognized: set[str] = set()
        if "localrunner" in lower or "local runner" in lower:
            recognized.add("local_runner")
        if any(token in lower for token in ("cuda", "gpu", "nvidia")):
            recognized.add("accelerator:cuda")
        if "torch" in lower or "pytorch" in lower:
            recognized.add("module:torch")
        adapter_match = re.search(r"([a-z0-9_.-]+)\s+adapter\b", lower)
        if adapter_match:
            recognized.add(f"data:{adapter_match.group(1)}")
        if any(
            token in lower
            for token in (
                "implementation",
                "construction",
                "width scaling",
                "architecture",
                "permutation",
            )
        ):
            recognized.add("local_runner")
        normalized.update(recognized or {value})
    return normalized


def strategy_fingerprint(*, mechanism: str, intervention: str) -> str:
    """Identify a strategy by causal mechanism and executable intervention."""

    normalized = {
        "mechanism": _normalized_text(mechanism),
        "intervention": _normalized_text(intervention),
    }
    if not normalized["mechanism"] or not normalized["intervention"]:
        raise AdaptiveSearchError(
            "strategy requires both a causal mechanism and an intervention"
        )
    return _content_id("strategy", normalized)


def experiment_fingerprint(plan: Mapping[str, Any]) -> str:
    """Identify executable work while ignoring node labels and prose wording."""

    source_files = []
    for source in plan.get("source_files") or []:
        if not isinstance(source, Mapping):
            continue
        source_files.append(
            {
                "path": str(source.get("path") or ""),
                "content": str(source.get("content") or ""),
            }
        )
    reproducibility = plan.get("reproducibility") or {}
    recipe = {
        "task_class": plan.get("task_class"),
        "entrypoint": plan.get("entrypoint") or {},
        "source_files": sorted(source_files, key=lambda value: value["path"]),
        "inputs": plan.get("inputs") or {},
        "expected_outputs": plan.get("expected_outputs") or {},
        "baseline_evidence_requirements": plan.get(
            "baseline_evidence_requirements"
        )
        or [],
        "resources": plan.get("resources") or {},
        # code_snapshot is a node-local path. Source content is already part of
        # the recipe, so including that path would let a renamed clone evade
        # duplicate detection.
        "reproducibility": {
            "seed": reproducibility.get("seed"),
            "data_snapshot": reproducibility.get("data_snapshot"),
        },
    }
    return _content_id("experiment", recipe)


def make_negative_observation(
    *,
    node_id: str,
    worker_report: Mapping[str, Any],
    final_verdict: str,
) -> dict[str, Any]:
    """Reduce a negative result to measured evidence plus an auditable declaration."""

    measured = {
        "worker_status": worker_report.get("status"),
        "claim_verdict_candidate": worker_report.get("claim_verdict_candidate"),
        "metrics": worker_report.get("metrics") or {},
        "baselines": worker_report.get("baselines") or {},
        "baseline_evidence_status": worker_report.get("baseline_evidence_status")
        or {},
        "disproof_conditions_hit": worker_report.get("disproof_conditions_hit")
        or [],
        "input_evidence": worker_report.get("input_evidence") or {},
    }
    evidence_digest = hashlib.sha256(
        _canonical_json(measured).encode("utf-8")
    ).hexdigest()
    identity = {"evidence_digest": evidence_digest}
    return {
        "id": _content_id("observation", identity),
        "node_id": node_id,
        "polarity": "negative",
        "evidence_digest": f"sha256:{evidence_digest}",
        "measured": measured,
        "declared_failure_mode": str(final_verdict).strip(),
    }


def _require_text(candidate: Mapping[str, Any], field: str) -> str:
    value = str(candidate.get(field) or "").strip()
    if not value:
        raise AdaptiveSearchError(f"strategy candidate requires {field}")
    return value


def _priority_for_candidate(
    *,
    goal: Mapping[str, Any],
    observation_id: str,
    tests_bar_gaps: list[str],
    predicted_outcomes: list[str],
    required_capabilities: list[str],
    available_capabilities: set[str],
    estimated_cost: float,
    mechanism_novelty: float,
    depth: int,
    max_depth: int,
) -> dict[str, Any]:
    criteria = _strings((goal.get("bar") or {}).get("success_criteria"))
    closure = len(set(tests_bar_gaps)) / max(1, len(criteria))
    information_gain = 1.0 if len(set(predicted_outcomes)) >= 2 else 0.0
    capability_fit = float(
        normalize_required_capabilities(required_capabilities)
        <= available_capabilities
    )
    depth_cost = max(0, depth) / max(1, max_depth)
    normalized_cost = min(1.0, max(0.0, estimated_cost) * 0.7 + depth_cost * 0.3)
    components = {
        "bar_gap_closure": round(min(1.0, closure), 6),
        "information_gain": information_gain,
        "mechanism_novelty": round(min(1.0, max(0.0, mechanism_novelty)), 6),
        "capability_fit": capability_fit,
        "normalized_cost": round(normalized_cost, 6),
        "evidence_basis": [observation_id],
    }
    components["score"] = round(
        4 * components["bar_gap_closure"]
        + 3 * components["information_gain"]
        + 2 * components["mechanism_novelty"]
        + components["capability_fit"]
        - 2 * components["normalized_cost"],
        6,
    )
    return components


def _causal_novelty(
    mechanism: str,
    intervention: str,
    references: Iterable[tuple[str, str]],
) -> float:
    candidate_tokens = set(
        re.findall(r"[a-z0-9가-힣]+", _normalized_text(f"{mechanism} {intervention}"))
    )
    similarities: list[float] = []
    for prior_mechanism, prior_intervention in references:
        prior_tokens = set(
            re.findall(
                r"[a-z0-9가-힣]+",
                _normalized_text(f"{prior_mechanism} {prior_intervention}"),
            )
        )
        union = candidate_tokens | prior_tokens
        if union:
            similarities.append(len(candidate_tokens & prior_tokens) / len(union))
    return 1.0 - max(similarities, default=0.0)


def prepare_strategy_expansion(
    *,
    goal: Mapping[str, Any],
    observation: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    known_strategy_ids: set[str],
    available_capabilities: set[str],
    depth: int,
    max_depth: int,
    known_strategies: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Validate and content-address at least two causal successor strategies."""

    candidate_list = list(candidates)
    if len(candidate_list) < 2:
        raise AdaptiveSearchError(
            "negative evidence requires at least two distinct causal strategies"
        )
    goal_id = str(goal.get("id") or "")
    observation_id = str(observation.get("id") or "")
    if not goal_id or not observation_id:
        raise AdaptiveSearchError("goal and observation must be content-addressed")
    valid_gaps = set(_strings((goal.get("bar") or {}).get("success_criteria")))
    prepared: list[dict[str, Any]] = []
    seen = set(known_strategy_ids)
    causal_references = [
        (
            str(strategy.get("mechanism") or ""),
            str(strategy.get("intervention") or ""),
        )
        for strategy in known_strategies
        if isinstance(strategy, Mapping)
    ]

    for candidate in candidate_list:
        family = _require_text(candidate, "strategy_family")
        if _normalized_text(family) in _VERIFICATION_AXES:
            raise AdaptiveSearchError(
                f"{family!r} is a verification axis, not a distinct causal strategy"
            )
        mechanism = _require_text(candidate, "mechanism")
        intervention = _require_text(candidate, "intervention")
        strategy_id = strategy_fingerprint(
            mechanism=mechanism,
            intervention=intervention,
        )
        if strategy_id in seen:
            raise AdaptiveSearchError(
                "negative expansion strategies must be pairwise distinct and new"
            )
        seen.add(strategy_id)

        information_target = _require_text(candidate, "information_target")
        predicted_outcomes = _strings(candidate.get("predicted_outcomes"))
        if len(set(predicted_outcomes)) < 2:
            raise AdaptiveSearchError(
                "diagnostic strategy requires at least two distinct predicted outcomes"
            )
        tests_bar_gaps = _strings(candidate.get("tests_bar_gaps"))
        if not tests_bar_gaps or any(gap not in valid_gaps for gap in tests_bar_gaps):
            raise AdaptiveSearchError(
                "tests_bar_gaps must reference the frozen goal success criteria exactly"
            )
        required_capabilities = _strings(candidate.get("required_capabilities"))
        try:
            estimated_cost = float(candidate.get("estimated_cost", 1.0))
        except (TypeError, ValueError) as exc:
            raise AdaptiveSearchError("estimated_cost must be numeric") from exc
        if not 0 <= estimated_cost <= 1:
            raise AdaptiveSearchError("estimated_cost must be between 0 and 1")
        mechanism_novelty = _causal_novelty(
            mechanism,
            intervention,
            causal_references,
        )

        prepared.append(
            {
                "id": strategy_id,
                "goal_id": goal_id,
                "family": family,
                "mechanism": mechanism,
                "intervention": intervention,
                "information_target": information_target,
                "predicted_outcomes": predicted_outcomes,
                "tests_bar_gaps": tests_bar_gaps,
                "required_capabilities": required_capabilities,
                "estimated_cost": estimated_cost,
                "derived_from_observation_id": observation_id,
                "status": "candidate",
                "priority": _priority_for_candidate(
                    goal=goal,
                    observation_id=observation_id,
                    tests_bar_gaps=tests_bar_gaps,
                    predicted_outcomes=predicted_outcomes,
                    required_capabilities=required_capabilities,
                    available_capabilities=available_capabilities,
                    estimated_cost=estimated_cost,
                    mechanism_novelty=mechanism_novelty,
                    depth=depth,
                    max_depth=max_depth,
                ),
            }
        )
        causal_references.append((mechanism, intervention))
    return prepared


def derive_search_disposition(
    *,
    has_queued_work: bool,
    pause: Mapping[str, Any] | None,
    verified_strong_receipt: Mapping[str, Any] | None,
) -> str:
    """Derive scientific status without treating exhaustion as completion."""

    if verified_strong_receipt is not None:
        if verified_strong_receipt.get("verified") is not True:
            raise AdaptiveSearchError(
                "goal achievement requires a verified strong-result receipt"
            )
        return "goal_achieved"
    if has_queued_work:
        return "continue"
    if pause is None:
        raise AdaptiveSearchError(
            "empty frontier requires a resumable pause when no strong result exists"
        )
    return "paused_needs_expansion"
