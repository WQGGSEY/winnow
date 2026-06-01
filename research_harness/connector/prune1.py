"""[[prune-1]] — cheap, P-BLIND, lenient coherence triage.

An INDEPENDENT LLM call (separate context from the reading that produced the
candidate) must **construct the correspondence skeleton** between the assigned
field's reading and the abstraction. The verdict is DERIVED from that
construction (a demonstration) — NOT from any self-reported confidence, and NOT
even from the model's own ``constructible`` flag, both of which are *declared*
values the harness refuses to trust. ``passed`` iff the model actually built a
non-trivial correspondence.

Binary + lenient: any genuine partial mapping passes; only blatant nonsense is
cut. Deliberately unreliable-tolerant — the model may hallucinate a skeleton
for nonsense, but its errors are biased to *false-pass* (the reading slips to
the gate, costing only compute) over *false-cut* (which would lose a creative
reading). Reliability is NOT stacked here with more judges; it lives in the
executed production gate.
"""

from __future__ import annotations

from typing import Any

from research_harness.connector.claude_call import CommandRunner, call_claude_json

# A constructed correspondence with at least this many non-trivial element↔
# counterpart pairs counts as a demonstration → pass. Lenient on purpose.
MIN_CORRESPONDENCE_PAIRS = 2

_SYSTEM_PROMPT = (
    "You check whether a candidate reading actually corresponds to a structural "
    "skeleton — cheap triage, NOT deep judgement. You are given (1) an abstract "
    "structural skeleton and (2) a candidate reading of it through some field. "
    "CONSTRUCT the correspondence: map each salient element of the skeleton to "
    "its counterpart in the reading. Be LENIENT — a partial but genuine mapping "
    "counts; declare it non-constructible only if the reading is blatant "
    "nonsense with respect to the skeleton. Do NOT output a confidence number. "
    "Output exactly one JSON object and nothing else: "
    '{"correspondence": [{"abstraction_element": "...", "reading_counterpart": "..."}, ...], '
    '"constructible": <true|false>, "note": "<one line>"}. No prose, no code fences.'
)


def _build_user_prompt(abstraction_text: str, reading: dict[str, Any]) -> str:
    return "\n".join(
        [
            "## Structural skeleton",
            abstraction_text.strip(),
            "",
            "## Candidate reading",
            f"reading: {reading.get('reading', '')}",
            f"field_mechanism: {reading.get('field_mechanism', '')}",
            f"predicted_behavior: {reading.get('predicted_behavior', '')}",
            "",
            "## Your turn",
            "Construct the correspondence as one JSON object.",
        ]
    )


def _count_pairs(correspondence: Any) -> int:
    if not isinstance(correspondence, list):
        return 0
    count = 0
    for pair in correspondence:
        if not isinstance(pair, dict):
            continue
        a = str(pair.get("abstraction_element") or "").strip()
        b = str(pair.get("reading_counterpart") or "").strip()
        if a and b:
            count += 1
    return count


def prune1_check(
    abstraction_text: str,
    reading: dict[str, Any],
    *,
    model: str,
    max_budget: str,
    claude_path: str,
    runner: CommandRunner,
    min_pairs: int = MIN_CORRESPONDENCE_PAIRS,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Construct the field↔abstraction correspondence and DERIVE pass/cut from it.

    P-blind by signature. ``passed`` is derived from the number of non-trivial
    constructed pairs (behavioral demonstration), never from the model's
    ``constructible`` self-report.
    """
    field_code = reading.get("field", {}).get("code")
    parsed, usage = call_claude_json(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(abstraction_text, reading),
        model=model,
        max_budget=max_budget,
        claude_path=claude_path,
        runner=runner,
        timeout_seconds=timeout_seconds,
        label=f"prune1[{field_code}]",
    )
    correspondence = parsed.get("correspondence")
    num_pairs = _count_pairs(correspondence)
    passed = num_pairs >= min_pairs
    return {
        "field": reading.get("field"),
        "passed": passed,
        "num_pairs": num_pairs,
        "correspondence": correspondence if isinstance(correspondence, list) else [],
        # The model's own flag is recorded for inspection ONLY — it does not
        # drive the verdict (a declared value the harness does not trust).
        "llm_constructible_flag": bool(parsed.get("constructible")),
        "note": str(parsed.get("note") or "").strip(),
        "usage": usage,
    }
