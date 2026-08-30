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

from research_harness.connector.codex_call import CommandRunner, call_agent_json

_SYSTEM_PROMPT = (
    "You read a single vague structural skeleton through the lens of ONE "
    "assigned field. You are given (1) an abstract, deliberately-vague "
    "structural skeleton and (2) an assigned field. Read the skeleton AS IF it "
    "were a SYSTEM or PHENOMENON studied in that field: what PROCESS or MECHANISM "
    "studied in this field would GENERATE, DRIVE, or EXPLAIN a structure of this "
    "shape — the causal 'why the system behaves that way'? Name that mechanism "
    "concretely (a generative causal process the field actually studies), and "
    "state the concrete, observable BEHAVIOR that mechanism predicts such a "
    "system would exhibit. This is NOT a request for a method, tool, test, "
    "estimator, metric, or analysis technique — do NOT answer with how to measure, "
    "detect, or validate anything; answer ONLY with the underlying mechanism that "
    "produces the structure and what it makes the system DO. Commit to the field's "
    "specific causal machinery; do NOT water it down toward generic language. "
    "Output exactly one JSON object and nothing else: "
    '{"reading": "<how the skeleton reads as a phenomenon in this field, 2-4 sentences>", '
    '"field_mechanism": "<the field\'s specific generative process/mechanism>", '
    '"predicted_behavior": "<the concrete behavior that mechanism predicts the system exhibits>"}. '
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
    codex_path: str,
    runner: CommandRunner,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Force-read the abstraction through ``field``. P-blind by signature.

    Returns ``{field, reading, field_mechanism, predicted_behavior, usage}``.
    """
    parsed, usage = call_agent_json(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(abstraction_text, field),
        model=model,
        codex_path=codex_path,
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
        "field_mechanism": str(parsed.get("field_mechanism") or "").strip(),
        "predicted_behavior": str(parsed.get("predicted_behavior") or "").strip(),
        "usage": usage.as_dict(),
    }
