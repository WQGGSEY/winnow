"""[[reduction]] — the mandatory P-AWARE construction-worker.

P RE-ENTERS here (the [[firewall]] lifts): given the far-domain reading, its
field↔abstraction correspondence, the P-blind method research, and P itself,
this step **translates** the far method onto P — building a P-claim_contract:
a correspondence table mapping the method's parts to P's actual observables,
``claim_under_test`` in P's own terms, plus baselines / success / disproof.

FAITHFUL, not creative: it translates and must PRESERVE the far-ness (no
flattening into a generic P approach — the creativity already happened in the
reading). It is the **implicit filter**: a reading whose correspondence breaks
once P's specifics return dies here (``reducible=false`` or a malformed
contract). Honesty (same pattern as prune-1): the step may force a hollow
mapping — its reliability is NOT propped up by more LLM judging; the resulting
P-claim goes to the executed gate, where a hollow mapping fails validity /
mechanism / necessity. The professor stays the judge.

Whether a reading "reduced" is DERIVED from whether a well-formed claim_contract
was actually constructed (behavioral), not asserted.
"""

from __future__ import annotations

from typing import Any

from research_harness.connector.claude_call import CommandRunner, call_claude_json

_SYSTEM_PROMPT = (
    "You are a construction worker translating a far-domain method onto a "
    "specific problem P. You are given: P (in its own domain); a reading of P's "
    "abstract structural skeleton through a FAR field, with the field's method "
    "and a correspondence skeleton; and prior work on that far method. Build a "
    "claim_contract FOR P by faithfully translating the far method onto P:\n"
    "(1) correspondence_table: map each part of the far method to P's actual "
    "observable / quantity / mechanism;\n"
    "(2) claim_under_test: stated in P's OWN terms — what, on P, this far method "
    "claims;\n"
    "(3) mandatory_baselines: exactly three, framed for P — a current_best_known, "
    "a naive, and a random_or_null baseline;\n"
    "(4) success_criteria: concrete, on P;\n"
    "(5) disproof_conditions: reachable conditions that would refute it on P.\n"
    "PRESERVE the far-ness — do NOT flatten the method into a generic P approach. "
    "If the far method genuinely cannot be mapped onto P, set reducible=false and "
    "explain. Output exactly one JSON object and nothing else: "
    '{"reducible": <bool>, "correspondence_table": [{"method_part": "...", '
    '"p_observable": "..."}, ...], "claim_under_test": "...", '
    '"mandatory_baselines": ["...", "...", "..."], "success_criteria": ["..."], '
    '"disproof_conditions": ["..."], "far_ness_note": "<how the far-ness is '
    'preserved, one or two lines>"}. No prose, no code fences.'
)


def _build_user_prompt(
    extracted: dict[str, Any],
    reading: dict[str, Any],
    prune1_result: dict[str, Any],
    method_research: dict[str, Any] | None,
) -> str:
    lines = [
        "## Problem P (its home domain)",
        str(extracted.get("claim_under_test") or "").strip(),
        f"Home domain tag: {extracted.get('domain') or 'unspecified'}",
        "",
        "## Far reading to translate onto P",
        f"assigned field: {reading.get('field', {}).get('name')} "
        f"({reading.get('field', {}).get('code')})",
        f"reading: {reading.get('reading', '')}",
        f"field_method: {reading.get('field_method', '')}",
        f"emergent_claim: {reading.get('emergent_claim', '')}",
        "",
        "## Field↔skeleton correspondence (from triage)",
    ]
    for pair in prune1_result.get("correspondence") or []:
        if isinstance(pair, dict):
            lines.append(
                f"- {pair.get('abstraction_element', '')} ↔ "
                f"{pair.get('reading_counterpart', '')}"
            )
    if method_research and method_research.get("papers"):
        lines.append("")
        lines.append("## Prior work on the far method (P-blind search)")
        for paper in method_research["papers"][:6]:
            lines.append(f"- {paper.get('title', '')} ({paper.get('url', '')})")
    elif method_research is not None:
        lines.append("")
        lines.append(
            "## Prior work on the far method: none found "
            "(the method may be niche — translate from first principles)."
        )
    lines.extend(["", "## Your turn", "Output exactly one JSON object as specified."])
    return "\n".join(lines)


def _nonempty_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def reduce_to_claim(
    grilling_session: dict[str, Any],
    reading: dict[str, Any],
    prune1_result: dict[str, Any],
    *,
    model: str,
    max_budget: str,
    claude_path: str,
    runner: CommandRunner,
    method_research: dict[str, Any] | None = None,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Translate ``reading`` onto P into a P-claim_contract. P-aware.

    ``reduced`` is derived behaviorally: the model declared it reducible AND a
    well-formed claim_contract (non-empty claim + ≥1 each of baselines /
    success / disproof) was actually constructed. A malformed or declined
    reduction produces ``claim_contract=None`` and the reading dies here.
    """
    extracted = grilling_session["extracted"]
    parsed, usage = call_claude_json(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(extracted, reading, prune1_result, method_research),
        model=model,
        max_budget=max_budget,
        claude_path=claude_path,
        runner=runner,
        timeout_seconds=timeout_seconds,
        label=f"reduction[{reading.get('field', {}).get('code')}]",
    )

    claim = str(parsed.get("claim_under_test") or "").strip()
    baselines = _nonempty_str_list(parsed.get("mandatory_baselines"))
    success = _nonempty_str_list(parsed.get("success_criteria"))
    disproof = _nonempty_str_list(parsed.get("disproof_conditions"))
    well_formed = bool(claim) and bool(baselines) and bool(success) and bool(disproof)
    reduced = bool(parsed.get("reducible")) and well_formed

    claim_contract: dict[str, Any] | None = None
    if reduced:
        claim_contract = {
            "claim_under_test": claim,
            "mandatory_baselines": baselines,
            "success_criteria": success,
            "disproof_conditions": disproof,
        }

    return {
        "field": reading.get("field"),
        "reduced": reduced,
        "claim_contract": claim_contract,
        "correspondence_table": parsed.get("correspondence_table")
        if isinstance(parsed.get("correspondence_table"), list)
        else [],
        "far_ness_note": str(parsed.get("far_ness_note") or "").strip(),
        # Recorded for inspection; the verdict above is the behavioral one.
        "llm_reducible_flag": bool(parsed.get("reducible")),
        "well_formed": well_formed,
        "usage": usage,
    }
