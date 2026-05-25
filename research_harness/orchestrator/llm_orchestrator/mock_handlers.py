"""Deterministic reasoning handlers used by MockLLMClient.

CRITICAL NOTE ON MOCK vs REAL REASONING:
    These handlers are STUBS. They do not "think." They use keyword matching
    + fixed templates to produce plausible-looking dialog so the codepath and
    the four-stage tree search can be exercised end-to-end without spending
    API credits.

    Real reasoning only happens when settings.runtime.llm_orchestrator.backend
    is set to "anthropic" AND the billing_ack / execution_ack environment
    variables are present. In that case Professor.py and GradStudent.py
    send their full persona prompts + the node + worker_report + critic
    reviews to anthropic, and the model actually evaluates whether (for
    example) the synthetic-data setup begs the question — producing fresh,
    context-specific challenges each time.

    Treat dialog produced by these mocks as a *placeholder transcript*, not
    as evidence that the harness has caught a real methodological flaw.
    Only the anthropic backend can do that.
"""

from __future__ import annotations

import json
import re
from typing import Any

from research_harness.orchestrator.llm_orchestrator.llm_client import (
    LLMResponse,
    MockLLMClient,
)


def register_default_mock_handlers(client: MockLLMClient) -> None:
    client.register("professor.reduce", _professor_reduce_handler)
    client.register("professor.respond", _professor_respond_handler)
    client.register("professor.readiness", _professor_readiness_handler)
    client.register("professor.revise_after_reject", _professor_revise_handler)
    client.register("professor.problem_to_claim", _professor_problem_to_claim_handler)
    client.register("professor.draft_roadmap", _professor_draft_roadmap_handler)
    client.register("professor.revise_roadmap", _professor_revise_roadmap_handler)
    client.register("professor.design_experiment", _professor_design_experiment_handler)
    client.register("professor.prioritize_claims", _professor_prioritize_claims_handler)
    client.register("grad_student.review", _grad_student_review_handler)
    client.register("grad_student.commentary", _grad_student_commentary_handler)


# --------------------------------------------------------------------------- #
# Professor handlers                                                           #
# --------------------------------------------------------------------------- #


def _professor_reduce_handler(messages, system, json_schema_hint):
    prompt = messages[-1]["content"]
    deterministic = _extract_block(prompt, "Deterministic baseline decision (for reference)")
    parsed_baseline = _parse_json_block(deterministic) or {}
    final_verdict = parsed_baseline.get("final_verdict", "inconclusive")
    next_transition = parsed_baseline.get("next_transition", "needs_child_branch")
    suggestions_in = parsed_baseline.get("child_branch_suggestions") or []
    claim_line = _first_line_after(prompt, "Claim under test:")
    worker_status = _first_line_after(prompt, "Status:") or "n/a"
    verdict_candidate = _first_line_after(prompt, "Claim verdict candidate:") or "inconclusive"
    overall = _first_line_after(prompt, "Baseline evidence overall:") or "n/a"
    disproof_line = _first_line_after(prompt, "Disproof conditions hit:") or "[]"
    node_type = _node_type_from_prompt(prompt)

    follow_up_children: list[dict[str, Any]] = []
    cleaned_suggestions: list[dict[str, Any]] = []

    # Case (4) — claim is false: prune the branch.
    disproof_hit = "[]" not in disproof_line and disproof_line.strip()
    contradicted = (
        verdict_candidate == "contradicted"
        or disproof_hit
        or "failed" in overall
    )
    # Case (3) — claim is supported: promote AND open follow-up successor claims.
    supported = "passed" in overall and not contradicted

    if contradicted:
        next_transition = "pruned"
        final_verdict = "contradicted"
        professor_response = (
            f"This branch is contradicted by the run — worker reported "
            f"`{verdict_candidate}` with baseline status `{overall}`. Pruning. "
            f"I'll remember the lesson and avoid this design in successor claims."
        )
    elif supported:
        next_transition = "promoted"
        final_verdict = "supported_with_scope_narrowing"
        follow_up_children = _make_follow_up_claims(claim_line, node_type)
        cohort_size = len(follow_up_children)
        professor_response = (
            f"Good work — `{claim_line[:120]}…` holds up under the baseline "
            f"triad. Promoting this node. I'm spawning {cohort_size} successor "
            f"claims for the next cohort to chase."
        )
    else:
        cleaned_suggestions = _suggestions_with_focus(suggestions_in, claim_line)
        focus_list = ", ".join(f"{s['type']}" for s in cleaned_suggestions)
        professor_response = (
            f"Worker run status: {worker_status}. Before I can promote this, "
            f"please address: {focus_list}. I'll spawn the children — keep your "
            f"scope on the parent claim, do not introduce new ones."
        )

    research_status = (
        "supported_with_scope_narrowing"
        if next_transition == "promoted"
        else (
            "pruned_contradicted"
            if next_transition == "pruned"
            else parsed_baseline.get("research_status", "interpretable_negative_result")
        )
    )
    payload = {
        "reduction": {
            "final_verdict": final_verdict,
            "research_status": research_status,
            "next_transition": next_transition,
            "child_branch_suggestions": cleaned_suggestions,
            "accepted_lesson_candidates": parsed_baseline.get(
                "accepted_lesson_candidates", []
            ),
        },
        "response_to_grad_student": professor_response,
        "follow_up_children": follow_up_children,
    }
    text = json.dumps(payload, ensure_ascii=False)
    return LLMResponse(text=text, structured=payload)


def _suggestions_with_focus(
    suggestions_in: list[dict[str, Any]],
    claim_line: str,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for s in suggestions_in[:2]:
        if not isinstance(s, dict):
            continue
        cleaned.append(
            {
                "type": s.get("type", "validity"),
                "reason": s.get("reason", "Address open objection."),
                "source": "professor_decision",
                "claim_focus": _claim_focus_for(s, claim_line),
            }
        )
    if not cleaned:
        cleaned.append(
            {
                "type": "validity",
                "reason": "Tighten the experimental design before re-promotion.",
                "source": "professor_decision",
                "claim_focus": f"Re-test the validity axis of: {claim_line[:200]}",
            }
        )
    return cleaned


# Successor-claim templates by parent node_type. The Professor uses these
# to grow the tree once the parent claim is supported. Each successor is a
# *new* claim that depends on the parent's success, not a re-test of it.
_FOLLOW_UP_TEMPLATES: dict[str, list[tuple[str, str, str]]] = {
    "capability": [
        ("mechanism", "Identify which component of the design causally drives the supported capability claim, i.e. {claim}", "supported capability → ask why"),
        ("necessity", "Show the supported capability does not collapse when the strongest same-budget baseline is matched on the metric of interest", "supported capability → ask if it is necessary"),
        ("boundary", "Map the regime boundary at which the supported capability breaks down", "supported capability → find its limits"),
        ("constraint", "Test how the supported capability degrades under realistic operational constraints (latency, memory, distribution shift)", "supported capability → stress under ops"),
    ],
    "validity": [
        ("capability", "Use the now-validated measurement methodology to test a primary capability claim built on top of: {claim}", "validated method → flip to capability"),
        ("necessity", "Compare the validated method against the simplest published alternative on the same data", "validated method → necessity vs alternatives"),
        ("boundary", "Identify the regimes where the validated method's assumptions still hold", "validated method → scope its assumptions"),
        ("mechanism", "Explain why the validity check rejected confounds the naive heuristic missed", "validated method → mechanism diagnostic"),
    ],
    "necessity": [
        ("mechanism", "Identify which structural choice in the necessary method is non-substitutable", "necessity supported → why"),
        ("boundary", "Find the smallest regime where necessity still holds", "necessity supported → minimum sufficient regime"),
        ("constraint", "Verify necessity survives operational constraints (compute / latency)", "necessity supported → operational test"),
    ],
    "mechanism": [
        ("boundary", "Find the boundary where the proposed mechanism stops explaining behavior", "mechanism supported → find its edge"),
        ("constraint", "Stress-test the mechanism under operational constraints", "mechanism supported → ops test"),
        ("necessity", "Show the mechanism is required even when given a strong substitute", "mechanism supported → necessity"),
    ],
    "boundary": [
        ("constraint", "Re-test the boundary under operational constraints", "boundary supported → ops"),
        ("mechanism", "Explain what causes the boundary effect", "boundary supported → mechanism"),
    ],
    "constraint": [
        ("boundary", "Find the operational regime where the constraint stops binding", "constraint supported → boundary"),
    ],
    "operational": [
        ("validity", "Validate that the operational setup does not introduce confounds into the main claim", "operational supported → validity"),
    ],
    "taste": [],
}


def _make_follow_up_claims(claim_line: str, parent_type: str) -> list[dict[str, Any]]:
    """Generate 1-4 successor claims for a promoted parent.

    Each successor is a new claim the next grad-student cohort can test.
    The harness will materialize them as child nodes typed by the
    parent's logical follow-up axis (mechanism/necessity/boundary/constraint).
    """
    templates = _FOLLOW_UP_TEMPLATES.get(parent_type) or _FOLLOW_UP_TEMPLATES["capability"]
    successors: list[dict[str, Any]] = []
    parent_claim_short = claim_line[:200] if claim_line else "(parent claim)"
    for index, (ftype, template, rationale) in enumerate(templates, start=1):
        successors.append(
            {
                "type": ftype,
                "successor_claim": template.format(claim=parent_claim_short),
                "rationale": rationale,
            }
        )
    return successors


def _node_type_from_prompt(prompt: str) -> str:
    """Pull node type out of '=== Node n_xxx_yyy (capability, stage=...) ==='."""
    import re as _re

    m = _re.search(r"=== Node [^\(]+\(([^,]+),", prompt)
    if m:
        return m.group(1).strip()
    return "capability"


def _professor_readiness_handler(messages, system, json_schema_hint):
    """Decide submit / keep_working without falling for laziness.

    Submit only when the claim tree has covered at least the three load-bearing
    successor axes (mechanism, necessity, boundary). If any of those is missing
    AND there are queued nodes that could cover it, keep working. If nothing is
    queued and we have ≥1 supportable axis, submit honestly.
    """
    prompt = messages[-1]["content"]
    coverage_line = _first_line_after(prompt, "Promoted claim types so far:") or "[]"
    queued_line = _first_line_after(prompt, "Promoted count:") or ""
    cycle_line = _first_line_after(prompt, "Cycle:") or "0"
    # Parse queued count from the same line.
    queued_count = 0
    if "Queued:" in queued_line:
        try:
            queued_count = int(queued_line.split("Queued:")[1].split("|")[0].strip())
        except ValueError:
            queued_count = 0
    cycle_index = 0
    try:
        cycle_index = int(cycle_line.strip())
    except ValueError:
        cycle_index = 0
    coverage = [
        c.strip().strip("'\"")
        for c in coverage_line.strip("[]").split(",")
        if c.strip().strip("'\"")
    ]
    critical_axes = {"mechanism", "necessity", "boundary"}
    missing = sorted(critical_axes - set(coverage))
    # Submit policy:
    #  - if all 3 critical axes covered, submit immediately.
    #  - if nothing queued AND at least one critical axis covered, submit.
    #  - else keep working — but if we've been going many cycles, ship what we have.
    submit = False
    if not missing:
        submit = True
        message = (
            f"Tree covers mechanism, necessity, and boundary. "
            f"Strong + honest. Submitting."
        )
    elif queued_count == 0 and len(set(coverage) & critical_axes) >= 1:
        submit = True
        message = (
            f"No more candidates queued; we have {coverage} covered. "
            f"Submitting at the strongest defensible scope rather than "
            f"forcing a weaker rewrite."
        )
    elif cycle_index >= 6:
        submit = True
        message = (
            f"Burned {cycle_index} cycles. Submitting with {coverage} covered; "
            f"missing axes ({missing}) become limitations in the paper, not "
            f"a reason to dilute the claim."
        )
    else:
        message = (
            f"Not yet — missing critical axes: {missing}. Rejecting the lazy "
            f"option of narrowing the claim to dodge these; we run another cycle."
        )
    payload = {
        "submit": submit,
        "advisor_message": message,
        "missing_axes": missing,
        "lazy_alternative_rejected": (
            "Refuse to narrow root claim to dodge missing axes."
        ),
    }
    text = json.dumps(payload, ensure_ascii=False)
    return LLMResponse(text=text, structured=payload)


def _professor_revise_handler(messages, system, json_schema_hint):
    prompt = messages[-1]["content"]
    original_claim = _first_line_after(prompt, "Original root claim:") or "(unknown)"
    blocking_block = _extract_block(prompt, "Blocking reasons:")
    blocking_lines = [
        line.strip(" -")
        for line in blocking_block.splitlines()
        if line.strip(" -")
    ]
    first_blocker = blocking_lines[0] if blocking_lines else "the AC's core concern"
    new_claim = (
        f"A scoped but ambitious version of: \"{original_claim[:160]}\" that "
        f"directly addresses {first_blocker[:120]} via an additional "
        f"controlled experiment — without weakening the substantive predictive "
        f"capability the AC questioned."
    )
    rationale = (
        "Refusing the lazy option of weakening the headline claim. "
        "The AC's blocker is methodological, not substantive; we address it "
        "via tighter experimental scope, not by demoting the contribution."
    )
    payload = {"new_claim": new_claim, "rationale": rationale}
    return LLMResponse(
        text=json.dumps(payload, ensure_ascii=False),
        structured=payload,
    )


def _professor_problem_to_claim_handler(messages, system, json_schema_hint):
    """Convert a problem statement into a strong claim contract.

    The mock refuses the lazy option (claim = problem restated) and instead
    constructs a contract whose claim asserts *capability of solving* the
    problem, with a generic but concrete baseline triad and falsifiable
    disproof conditions tied to the problem's keywords.
    """
    prompt = messages[-1]["content"]
    problem = _first_line_after(prompt, "Problem statement:") or "(unstated)"
    domain = _first_line_after(prompt, "Domain:") or "general"
    facets_line = _first_line_after(prompt, "Goal facets:") or "[]"
    short_problem = problem[:200]
    claim = (
        f"There exists a methodology in the {domain} domain that produces a "
        f"calibrated, falsifiable signal distinguishing the problem stated "
        f"(\"{short_problem}\") from chance-level confounds, and beats the "
        f"strongest published heuristic on the same evaluation."
    )
    baselines = [
        f"current_best: the strongest published heuristic for {domain}",
        f"naive: the simplest single-statistic threshold practitioners already use in {domain}",
        f"random_or_null: a permutation / shuffled-label null distribution computed on the same evaluation data",
    ]
    success = [
        f"In controlled synthetic ground-truth cases the methodology achieves AUROC >= 0.80 on the binary task implied by the problem",
        f"The methodology's signal is statistically lower for negative ground-truth cases than positive cases at 95% CI in >=90% of Monte Carlo trials",
        f"Per-component ablations show each layer of the methodology contributes positively (no redundant component)",
    ]
    disproof = [
        f"AUROC of the methodology fails to exceed 0.55 in any synthetic ground-truth setup — indistinguishable from chance",
        f"The methodology's headline signal is statistically identical (overlapping 95% CIs) for positive vs random cases",
        f"Any individual component of the methodology contradicts the combined signal — i.e. the layers are incoherent rather than complementary",
    ]
    rationale = (
        f"Refused the lazy option of restating the problem as the claim. "
        f"This claim is HONEST (it asserts a capability we can in principle "
        f"verify with synthetic ground-truth) and STRONG (it commits to "
        f"specific numerical thresholds that competitive prior work would "
        f"have to beat). Goal facets present in the problem: {facets_line}."
    )
    payload = {
        "claim_under_test": claim,
        "mandatory_baselines": baselines,
        "success_criteria": success,
        "disproof_conditions": disproof,
        "rationale": rationale,
    }
    return LLMResponse(text=json.dumps(payload, ensure_ascii=False), structured=payload)


def _professor_draft_roadmap_handler(messages, system, json_schema_hint):
    """Default 5-milestone arc. Honest+strong: covers validity → capability
    → mechanism → necessity → boundary in that order.
    """
    prompt = messages[-1]["content"]
    root_claim = _first_line_after(prompt, "Root claim under test:") or "(unstated)"
    short = root_claim[:140]
    milestones = [
        {
            "name": "establish_validity",
            "description": f"Confirm the measurement methodology underlying \"{short}\" is sound.",
            "expected_node_types": ["validity"],
            "rationale": "Validate methodology before believing any capability number.",
        },
        {
            "name": "demonstrate_capability",
            "description": "Show the methodology beats the baseline triad on the headline metric.",
            "expected_node_types": ["capability"],
            "rationale": "Capability claim is the core contribution.",
        },
        {
            "name": "isolate_mechanism",
            "description": "Identify the load-bearing component of the methodology.",
            "expected_node_types": ["mechanism"],
            "rationale": "Mechanism turns capability into a publishable result.",
        },
        {
            "name": "test_necessity",
            "description": "Show same-budget alternatives do not match.",
            "expected_node_types": ["necessity"],
            "rationale": "Necessity defends against ad-hoc-substitution rebuttals.",
        },
        {
            "name": "map_boundary",
            "description": "Find regimes where the claim breaks; characterize honest scope.",
            "expected_node_types": ["boundary", "constraint"],
            "rationale": "Boundary + operational stress test gives the paper its honest scope.",
        },
    ]
    payload = {"milestones": milestones}
    return LLMResponse(text=json.dumps(payload, ensure_ascii=False), structured=payload)


def _professor_revise_roadmap_handler(messages, system, json_schema_hint):
    """Default behaviour: do not delete milestones, but insert one extra "refit"
    milestone after the milestone that just failed (if any).
    """
    prompt = messages[-1]["content"]
    trigger = _first_line_after(prompt, "Reason for revision:") or "(unspecified)"
    payload = {
        "edits": [
            {
                "op": "insert",
                "after": None,
                "milestone": {
                    "name": f"refit_after_{trigger[:30].replace(' ', '_')}",
                    "description": f"One extra validity pass triggered by: {trigger}",
                    "expected_node_types": ["validity"],
                    "rationale": "Run an extra check rather than weaken the claim.",
                },
            }
        ],
        "summary": (
            f"Roadmap revised because of: {trigger}. Inserted one extra "
            f"validity-axis milestone rather than weakening the headline claim."
        ),
    }
    return LLMResponse(text=json.dumps(payload, ensure_ascii=False), structured=payload)


def _professor_prioritize_claims_handler(messages, system, json_schema_hint):
    """Deterministic priority ordering: type weight first, then depth/id.

    Weight by claim axis: validity > capability > necessity > mechanism >
    boundary > constraint > operational > taste. Roughly mirrors which
    claim type reduces overall research uncertainty earliest.
    """
    prompt = messages[-1]["content"]
    candidates_block = _extract_block(prompt, "Candidates:")
    parsed = _parse_json_block(candidates_block) or []
    weights = {
        "validity": 0,
        "capability": 1,
        "necessity": 2,
        "mechanism": 3,
        "boundary": 4,
        "constraint": 5,
        "operational": 6,
        "taste": 7,
    }
    ordered = sorted(
        parsed if isinstance(parsed, list) else [],
        key=lambda c: (weights.get(c.get("type", ""), 9), c.get("id", "")),
    )
    payload = {
        "ordered_ids": [c["id"] for c in ordered if isinstance(c, dict) and c.get("id")],
        "rationale": (
            "Front-loaded validity/capability claims so we resolve methodology "
            "questions before chasing mechanism/boundary; remaining types "
            "sorted by type weight."
        ),
    }
    return LLMResponse(text=json.dumps(payload, ensure_ascii=False), structured=payload)


def _professor_design_experiment_handler(messages, system, json_schema_hint):
    """Deterministic mock that:
      - If the shared lib is empty (root), emit the full reusable skeleton
        (_lib/data.py, _lib/baselines/*, _lib/eval/*) plus a thin
        experiment.py that imports them.
      - If the shared lib already has modules, emit ONLY the thin
        experiment.py — reuse everything else.

    Real-LLM mode replaces this with an actual reasoning step; this mock
    just guarantees the codepath is exercised end-to-end without API spend.
    """
    prompt = messages[-1]["content"]
    shared_lib_block = _extract_block(prompt, "Shared lib already available:")
    has_shared = "(empty" not in (shared_lib_block or "(empty")
    claim = _first_line_after(prompt, "Claim under test:") or "(claim)"
    short_claim = claim[:140].replace('"', '\\"')

    source_files: list[dict[str, str]] = []
    if not has_shared:
        # Bootstrap the reusable lib for the whole thread.
        source_files.append({
            "path": "_lib/__init__.py",
            "purpose": "package marker for the thread-wide reusable lib",
            "content": '"""Thread-wide reusable lib generated by Professor."""\n',
        })
        source_files.append({
            "path": "_lib/data.py",
            "purpose": "reusable synthetic data generator",
            "content": _LIB_DATA_PY,
        })
        source_files.append({
            "path": "_lib/baselines/__init__.py",
            "purpose": "baseline triad",
            "content": (
                '"""Baseline triad: current_best, naive, random_or_null."""\n'
                'from . import current_best, naive, random_or_null  # noqa: F401\n'
            ),
        })
        source_files.append({
            "path": "_lib/baselines/current_best.py",
            "purpose": "strongest known baseline",
            "content": _LIB_CURRENT_BEST_PY,
        })
        source_files.append({
            "path": "_lib/baselines/naive.py",
            "purpose": "naive baseline",
            "content": _LIB_NAIVE_PY,
        })
        source_files.append({
            "path": "_lib/baselines/random_or_null.py",
            "purpose": "random / permutation null baseline",
            "content": _LIB_RANDOM_PY,
        })
        source_files.append({
            "path": "_lib/eval/__init__.py",
            "purpose": "eval helpers",
            "content": 'from .metrics import compute_headline_metric  # noqa: F401\n',
        })
        source_files.append({
            "path": "_lib/eval/metrics.py",
            "purpose": "headline metric computation",
            "content": _LIB_METRICS_PY,
        })
    # Thin experiment.py — always emitted per-node; imports the shared lib.
    source_files.append({
        "path": "src/experiment.py",
        "purpose": f"thin orchestrator for claim: {short_claim}",
        "content": _experiment_py(short_claim),
    })

    payload = {
        "task_class": "eval",
        "objective": f"Test the claim: {short_claim}",
        "entrypoint": {"command": ["python"], "args": ["src/experiment.py"]},
        "resources": {"timeout_sec": 60, "gpu": None, "cpu": 1, "memory_gb": 1},
        "expected_outputs": {
            "metrics_files": ["artifacts/metrics.json"],
            "logs": ["artifacts/run.log"],
            "artifact_dirs": ["artifacts/"],
        },
        "baseline_evidence_requirements": [
            {
                "role": "current_best_known",
                "metric_key": "headline_metric",
                "baseline_key": "current_best_known",
                "operator": "greater_than",
                "margin": 0,
                "required": True,
            },
            {
                "role": "naive",
                "metric_key": "headline_metric",
                "baseline_key": "naive",
                "operator": "greater_than",
                "margin": 0,
                "required": True,
            },
            {
                "role": "random_or_null",
                "metric_key": "headline_metric",
                "baseline_key": "random_or_null",
                "operator": "greater_than",
                "margin": 0,
                "required": True,
            },
        ],
        "source_files": source_files,
    }
    return LLMResponse(text=json.dumps(payload, ensure_ascii=False), structured=payload)


# Reusable Python skeleton modules emitted on first design call.
# These are intentionally small + deterministic + numeric. Real-LLM mode
# replaces them with model-authored code that responds to the actual claim.
_LIB_DATA_PY = '''"""Reusable synthetic data generator for the thread."""
import hashlib


def generate(seed: int = 0, n: int = 200) -> list[float]:
    """Return n pseudo-random floats keyed by seed (deterministic)."""
    out: list[float] = []
    for i in range(n):
        digest = hashlib.sha256(f"{seed}:{i}".encode()).digest()
        # Map first 8 bytes to a float in [-1, 1].
        v = int.from_bytes(digest[:8], "big") / (2 ** 64)
        out.append(v * 2 - 1)
    return out
'''

_LIB_CURRENT_BEST_PY = '''"""current_best_known baseline: pretend strongest published heuristic."""
from .. import data


def score(seed: int = 0) -> float:
    series = data.generate(seed=seed, n=200)
    pos = sum(1 for x in series if x > 0)
    return pos / len(series) * 0.85  # slightly worse than the proposed method
'''

_LIB_NAIVE_PY = '''"""naive baseline: simplest single-statistic threshold."""
from .. import data


def score(seed: int = 0) -> float:
    series = data.generate(seed=seed, n=200)
    return abs(sum(series) / len(series))  # mean magnitude, low signal
'''

_LIB_RANDOM_PY = '''"""random_or_null baseline: permutation null distribution."""
import hashlib


def score(seed: int = 0) -> float:
    digest = hashlib.sha256(f"random:{seed}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / (2 ** 32) * 0.5  # chance-level
'''

_LIB_METRICS_PY = '''"""Reusable metric computation."""
from .. import data


def compute_headline_metric(seed: int = 0) -> float:
    series = data.generate(seed=seed, n=200)
    pos = sum(1 for x in series if x > 0)
    # Proposed method: slightly above current_best by construction.
    return pos / len(series) * 0.92
'''


def _experiment_py(claim_short: str) -> str:
    return f'''"""Experiment for claim:
    {claim_short}

Imports everything reusable from the thread's _lib/. Only this file is
claim-specific; the lib is shared across the entire research thread.
"""
import json
import sys
from pathlib import Path

# The harness materializes _lib/ at the workspace root (sibling of src/).
# Add the workspace to sys.path so `from _lib import ...` resolves when the
# runner invokes `python src/experiment.py` with cwd=workspace.
_WORKSPACE = Path(__file__).resolve().parent.parent
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from _lib import data, eval as _eval
from _lib.baselines import current_best, naive, random_or_null


def main() -> None:
    artifacts = Path("artifacts")
    artifacts.mkdir(exist_ok=True)
    seed = 0
    headline = _eval.compute_headline_metric(seed=seed)
    payload = {{
        "metrics": {{
            "headline_metric": headline,
        }},
        "baselines": {{
            "current_best_known": current_best.score(seed=seed),
            "naive": naive.score(seed=seed),
            "random_or_null": random_or_null.score(seed=seed),
        }},
        "claim_verdict_candidate": "supported",
        "disproof_conditions_hit": [],
        "unexpected_observations": [],
    }}
    (artifacts / "metrics.json").write_text(json.dumps(payload, indent=2) + "\\n")
    print("experiment finished; headline_metric={{:.4f}}".format(headline))


if __name__ == "__main__":
    main()
'''


def _professor_respond_handler(messages, system, json_schema_hint):
    prompt = messages[-1]["content"]
    concern = _extract_block(prompt, "Concern from grad student:")
    short_concern = (concern.splitlines()[0] if concern else "")[:140]
    text = (
        f"Acknowledged. \"{short_concern}\" — I'll prepare the missing materials "
        "(template scaffold and data spec) and resubmit this node. Please rerun "
        "once you see the updated brief; do not change the claim or baselines."
    )
    return LLMResponse(text=text)


# --------------------------------------------------------------------------- #
# Grad student handlers                                                        #
# --------------------------------------------------------------------------- #


def _grad_student_review_handler(messages, system, json_schema_hint):
    """Skeptical grad student. Doesn't blindly accept the template — looks for
    common synthetic-data / setup smells and raises concerns BEFORE running.
    """
    prompt = messages[-1]["content"]
    has_template = "Domain template available: yes" in prompt
    template_summary = _first_line_after(prompt, "Template summary:") or ""
    claim_line = _first_line_after(prompt, "Claim under test:") or ""
    if not has_template:
        payload = {
            "commentary": (
                f"교수님, 이 claim을 돌릴 템플릿이 없는데요. "
                f"\"{claim_line[:100]}…\" 못 돌립니다. 만들어주셔야 할 것 같아요."
            ),
            "concerns": [
                {
                    "summary": "Domain template missing",
                    "evidence": (
                        "experiment_plan_templates/<domain>/ has no plan.json+src/ "
                        "pair; deterministic runner cannot materialize the experiment."
                    ),
                    "blocked": True,
                    "suggested_branch_type": "operational",
                }
            ],
        }
        return LLMResponse(text=json.dumps(payload, ensure_ascii=False), structured=payload)

    # Template is present. Look at the claim for falsification-worthy smells.
    # The grad student raises ONE non-blocking concern when the setup smells
    # too idealized. Concerns surface in the dialog and are forwarded to the
    # Professor for response, but do NOT block the run (so the harness still
    # makes forward progress).
    concerns: list[dict[str, Any]] = []
    claim_lower = claim_line.lower()
    setup_smells: list[tuple[str, str]] = []
    if any(token in claim_lower for token in ("synthetic", "ground-truth", "ground_truth")):
        setup_smells.append(
            (
                "Synthetic ground-truth setup",
                "교수님, 이거 synthetic ground-truth 의존도가 너무 높아 보여요. "
                "생성기와 평가기가 같은 latent 구조를 공유하면 결국 self-fulfilling "
                "result가 나올 수 있는데, 진짜 OOD 데이터에서는 어떻게 검증할지 "
                "design에 들어있나요? 이거 한 번 봐주셔야 할 것 같습니다.",
            )
        )
    if any(token in claim_lower for token in ("auroc", "auc")) and "0.8" in claim_lower:
        setup_smells.append(
            (
                "AUROC threshold may be too easy",
                "AUROC >= 0.80 라는 임계값이 그렇게 strong한지 의문이에요. "
                "기존 rolling IC heuristic도 toy benchmark에서는 쉽게 넘는 수치인데, "
                "이걸로 진짜 capability를 주장하기엔 약하지 않을까요?",
            )
        )
    if "shuffled" in claim_lower or "permut" in claim_lower:
        setup_smells.append(
            (
                "Permutation-only null may not be strong enough",
                "permutation null 한 가지만 비교 baseline으로 잡는 건 너무 약한 것 "
                "같아요. 실제 noise alpha는 단순 셔플이 아니라 systematic structure가 "
                "있을 텐데, null을 더 ambitious하게 잡아야 할 것 같습니다.",
            )
        )
    # Always raise at least *one* concern when the template is generic — a sharp
    # grad student doesn't just say "looks good." Default concern is a
    # measurement-fairness check.
    if not setup_smells:
        setup_smells.append(
            (
                "Default measurement-fairness check",
                "잠깐만요, 한 가지만 짚고 갈게요. 현재 setup에서 baseline triad가 "
                "method와 정확히 같은 features와 normalization을 받는지가 design "
                "에서 분명하지 않아요. 이게 비대칭이면 capability 결과가 "
                "confounded될 수 있습니다.",
            )
        )

    summary, evidence = setup_smells[0]
    concerns.append(
        {
            "summary": summary,
            "evidence": evidence,
            "blocked": False,  # surface to advisor but do not halt the run
            "suggested_branch_type": "validity",
        }
    )
    payload = {
        "commentary": (
            f"교수님, 템플릿 `{template_summary[:60]}`은 baselines 커버는 합니다. "
            f"그런데 짚어두고 싶은 게 있어요 — {summary}. 일단은 진행하면서 "
            f"결과 보고할게요."
        ),
        "concerns": concerns,
    }
    return LLMResponse(text=json.dumps(payload, ensure_ascii=False), structured=payload)


def _grad_student_commentary_handler(messages, system, json_schema_hint):
    """Post-run commentary with skeptical edge.

    A sharp grad student doesn't say "Run finished cleanly, all good." If the
    numbers look too clean (passed AND headline >> baselines), they say so —
    flagging that the advisor should check whether the result is real or
    an artifact of the setup.
    """
    prompt = messages[-1]["content"]
    claim_line = _first_line_after(prompt, "Claim:") or ""
    verdict = _first_line_after(prompt, "Claim verdict candidate:") or "n/a"
    baseline_overall = _first_line_after(prompt, "Baseline status:") or "n/a"
    metrics_line = _first_line_after(prompt, "Metrics:") or ""
    suspicious = _looks_too_clean(metrics_line)
    if "passed" in baseline_overall:
        if suspicious:
            text = (
                f"교수님, 결과는 supported로 깨끗하게 떨어졌습니다 — Sharpe "
                f"수치가 baseline 대비 너무 크게 우월해요. 솔직히 의심됩니다. "
                f"이거 synthetic data setup에서 generator/evaluator가 같은 "
                f"latent을 공유해서 self-fulfilling result일 가능성이 있는 것 "
                f"같아요. promote 전에 한 번 더 검증하시는 게 좋을 것 같습니다. "
                f"(metrics: {metrics_line[:120]})"
            )
        else:
            text = (
                f"Run finished for \"{claim_line[:80]}…\". Worker verdict "
                f"`{verdict}`, baseline triad passed ({baseline_overall}). "
                f"Headline: {metrics_line[:120]}. 표면적으로는 깨끗합니다."
            )
    elif "not_evaluable" in baseline_overall:
        text = (
            f"교수님, baseline triad가 not_evaluable로 떨어졌어요 "
            f"({baseline_overall}). Worker는 `{verdict}` 라고 합니다. "
            f"이거 metric 키가 baseline 키랑 매핑이 잘 안 되는 거 아닐까요? "
            f"한 번 봐주세요."
        )
    else:
        text = (
            f"교수님, 이 노드 baseline check 실패했습니다 ({baseline_overall}). "
            f"Worker verdict `{verdict}`. validity child가 필요해 보입니다, "
            f"단순 재실행으로는 안 될 것 같아요."
        )
    return LLMResponse(text=text)


def _looks_too_clean(metrics_line: str) -> bool:
    """Heuristic for 'this result is too good to be true'.

    Triggers when the headline metric in the prompt is an order of magnitude
    above what a typical baseline would be — a strong signal that the
    synthetic setup is rigged. The grad student uses this to challenge the
    advisor instead of cheering.
    """
    import re as _re

    nums = [float(x) for x in _re.findall(r"[-+]?\d+\.\d+|[-+]?\d+", metrics_line)]
    # If the largest reported metric is >5 in absolute value, treat as suspicious.
    # (Sharpe > 5 in real life is essentially never; on toy synthetic data
    # everyone hits 10+. The grad student should flag this.)
    return any(abs(n) > 5 for n in nums)


# --------------------------------------------------------------------------- #
# Small text helpers                                                           #
# --------------------------------------------------------------------------- #


def _extract_block(prompt: str, header: str) -> str:
    """Pull text between a header line and the next blank line / section header."""
    idx = prompt.find(header)
    if idx < 0:
        return ""
    rest = prompt[idx + len(header):]
    # Cut at next double-newline or next '=== ' marker.
    end = re.search(r"\n\n|\n===", rest)
    return rest[: end.start()] if end else rest


def _first_line_after(prompt: str, header: str) -> str:
    idx = prompt.find(header)
    if idx < 0:
        return ""
    rest = prompt[idx + len(header):].lstrip()
    return rest.split("\n", 1)[0].strip()


def _parse_json_block(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _claim_focus_for(suggestion: dict[str, Any], claim_line: str) -> str:
    t = suggestion.get("type", "validity")
    reason = suggestion.get("reason", "")
    return (
        f"Drill into the {t} axis of the parent claim. Specifically: {reason[:160]}"
        if reason
        else f"Drill into the {t} axis of: {claim_line[:160]}"
    )
