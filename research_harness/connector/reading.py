"""[[reading (field-forced)]] — realize the diversity the vague abstraction permits.

One LLM call, **P-BLIND** (this function never receives P → the [[firewall]] is
enforced structurally), forced to read the single vague abstraction through ONE
assigned field. The field is chosen *externally* by uniform random sampling
(see :mod:`field_sampler`) and passed in — the model never selects it and never
picks "the best" field (both collapse diversity). The model only does the
conditional work: read the skeleton through the field it was handed.
"""

from __future__ import annotations

from typing import Any

from research_harness.connector.claude_call import CommandRunner, call_claude_json

_SYSTEM_PROMPT = (
    "You read a single vague structural skeleton through the lens of ONE "
    "assigned field. You are given (1) an abstract, deliberately-vague "
    "structural skeleton and (2) an assigned field. Read the skeleton AS IF it "
    "were a structure or phenomenon studied in that field: what in the assigned "
    "field has this shape? Name the field's natural method or tool for working "
    "with such a structure, and state the research claim that method would make "
    "about it. Do NOT water it down toward generic language — commit to the "
    "field's specific machinery. Output exactly one JSON object and nothing "
    'else: {"reading": "<how the skeleton reads in this field, 2-4 sentences>", '
    '"field_method": "<the field\'s specific method or tool>", '
    '"emergent_claim": "<the claim that method would make about the structure>"}. '
    "No prose, no code fences."
)


def _build_user_prompt(abstraction_text: str, field: dict[str, Any]) -> str:
    return "\n".join(
        [
            "## Assigned field",
            f"{field.get('name')} ({field.get('code')})",
            "",
            "## Structural skeleton",
            abstraction_text.strip(),
            "",
            "## Your turn",
            "Output exactly one JSON object as specified.",
        ]
    )


def generate_reading(
    abstraction_text: str,
    field: dict[str, Any],
    *,
    model: str,
    max_budget: str,
    claude_path: str,
    runner: CommandRunner,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Force-read the abstraction through ``field``. P-blind by signature.

    Returns ``{field, reading, field_method, emergent_claim, usage}``.
    """
    parsed, usage = call_claude_json(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(abstraction_text, field),
        model=model,
        max_budget=max_budget,
        claude_path=claude_path,
        runner=runner,
        timeout_seconds=timeout_seconds,
        label=f"reading[{field.get('code')}]",
    )
    reading = str(parsed.get("reading") or "").strip()
    if not reading:
        raise ValueError(f"reading step for field {field.get('code')} returned empty 'reading'")
    return {
        "field": {"code": field.get("code"), "name": field.get("name"), "archive": field.get("archive")},
        "reading": reading,
        "field_method": str(parsed.get("field_method") or "").strip(),
        "emergent_claim": str(parsed.get("emergent_claim") or "").strip(),
        "usage": usage,
    }
