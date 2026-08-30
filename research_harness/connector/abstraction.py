"""[[abstraction (de-domained)]] — the single vague skeleton the front-end works from.

One LLM call (P-VISIBLE — abstraction-generation must see P to strip its
domain) turns the problem P into ONE deliberately-vague, domain-stripped
structural skeleton. Then the [[firewall]]'s **de-domaining scan** runs: a
*behavioral* check (we read the produced text and look for P's domain
vocabulary — never the model's self-report of whether it de-domained) that
triggers a bounded regenerate when domain terms leak.

The scan is intentionally NOT fatal: the firewall's real guarantee is
*structural* — downstream P-blind steps are simply never handed P — so a
residual lexical leak in the abstraction text only mildly biases readings and
is backstopped by the gate. We regenerate to reduce it, then proceed with the
best abstraction and record any residual leak honestly.
"""

from __future__ import annotations

import re
from typing import Any

from research_harness.connector.codex_call import (
    CommandRunner,
    call_agent_json,
)

# Domain-neutral / structural words that appear in problem statements but are
# NOT domain vocabulary — excluded from the leak scan so we don't regenerate
# forever chasing generic research language. Conservative by design: a missed
# domain word is backstopped by the structural firewall; a false positive only
# wastes one regenerate.
_STRUCTURAL_STOPWORDS: frozenset[str] = frozenset(
    {
        "it", "is", "possible", "solve", "solved", "solving", "the", "a", "an",
        "and", "or", "of", "to", "in", "on", "for", "with", "without", "from",
        "into", "between", "under", "over", "than", "that", "this", "these",
        "those", "system", "systems", "method", "methods", "approach",
        "approaches", "model", "models", "modeling", "improve", "improves",
        "improving", "improvement", "outperform", "outperforms", "better",
        "best", "using", "use", "used", "based", "given", "data", "dataset",
        "datasets", "problem", "problems", "task", "tasks", "more", "less",
        "high", "low", "higher", "lower", "performance", "result", "results",
        "across", "within", "via", "such", "can", "be", "are", "as", "at",
        "by", "we", "our", "their", "its", "while", "when", "where", "which",
        "what", "how", "new", "novel", "general", "robust", "robustness",
        "accuracy", "quality", "effective", "efficient", "scalable",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'\-]*")


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def extract_domain_terms(extracted: dict[str, Any]) -> list[str]:
    """Independently derive the domain vocabulary to scan the abstraction for.

    Behavioral, not declared: drawn from P's own text (the grilling
    ``domain`` slug + the content nouns in ``claim_under_test``), NOT from
    anything the abstraction-LLM reports about itself. Generic structural
    words are dropped so the scan targets genuine domain terms.
    """
    terms: set[str] = set()
    domain = str(extracted.get("domain") or "")
    if domain and domain != "unspecified_domain":
        for tok in domain.replace("-", "_").split("_"):
            tok = tok.strip().lower()
            if len(tok) >= 3 and tok not in _STRUCTURAL_STOPWORDS:
                terms.add(tok)
    for tok in _tokenize(str(extracted.get("claim_under_test") or "")):
        if len(tok) >= 4 and tok not in _STRUCTURAL_STOPWORDS:
            terms.add(tok)
    return sorted(terms)


def scan_for_leak(abstraction_text: str, domain_terms: list[str]) -> list[str]:
    """Return the domain terms that leaked into the abstraction (word-boundary).

    Word-boundary match so ``art`` does not falsely hit ``start``.
    """
    if not abstraction_text or not domain_terms:
        return []
    text_lower = abstraction_text.lower()
    leaked: list[str] = []
    for term in domain_terms:
        if re.search(r"\b" + re.escape(term) + r"\b", text_lower):
            leaked.append(term)
    return leaked


_SYSTEM_PROMPT = (
    "You de-domain a research problem into a single VAGUE structural skeleton. "
    "You receive a problem P stated in one domain's vocabulary. Produce ONE "
    "abstraction that: (1) STRIPS all domain-specific vocabulary — named "
    "entities, materials, instruments, proper terms of the field — keeping only "
    "domain-neutral relational/structural language; (2) PRESERVES P's structural "
    "skeleton — the relationships, constraints, what-acts-on-what, the shape of "
    "the question — so a faithful reduction back onto P stays possible; (3) is "
    "deliberately VAGUE and under-specified, so the skeleton admits many "
    "different field readings — do NOT resolve it toward any one field. Output "
    "exactly one JSON object and nothing else: "
    '{"abstraction": "<the de-domained skeleton, 2-5 sentences>", '
    '"domain_terms_stripped": ["<term>", ...]}. No prose, no code fences.'
)


def _build_user_prompt(extracted: dict[str, Any], leaked_terms: list[str]) -> str:
    lines = [
        "## Problem P (stated in its home domain)",
        str(extracted.get("claim_under_test") or "").strip(),
        "",
        f"Home domain tag: {extracted.get('domain') or 'unspecified'}",
    ]
    facets = extracted.get("goal_facets") or []
    if facets:
        lines.append("Goal facets: " + ", ".join(str(f) for f in facets))
    if leaked_terms:
        lines.extend(
            [
                "",
                "## Rewrite required",
                "Your previous abstraction still contained these DOMAIN terms, "
                "which must be removed: " + ", ".join(leaked_terms) + ".",
                "Replace each with a domain-neutral structural phrase. Keep the "
                "skeleton; remove the vocabulary.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Your turn",
                "Output exactly one JSON object as specified.",
            ]
        )
    return "\n".join(lines)


def generate_abstraction(
    grilling_session: dict[str, Any],
    *,
    model: str,
    codex_path: str,
    runner: CommandRunner,
    max_regen: int = 2,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Generate the single de-domained abstraction, scanning for + regenerating
    on domain-vocabulary leaks (bounded by ``max_regen`` retries).

    Returns a dict (NOT written to disk here — the connector orchestrator
    assembles the session artifact): ``abstraction``, the ``scanned_domain_terms``
    the behavioral scan used, ``residual_leaked_terms`` after the final attempt,
    ``firewall_clean`` (residual empty), ``regen_attempts``, and aggregate
    ``usage``.
    """
    extracted = grilling_session["extracted"]
    domain_terms = extract_domain_terms(extracted)

    leaked: list[str] = []
    abstraction_text = ""
    attempts = 0
    total_usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }

    for attempt in range(max_regen + 1):
        parsed, usage = call_agent_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_build_user_prompt(extracted, leaked),
            model=model,
            codex_path=codex_path,
            runner=runner,
            timeout_seconds=timeout_seconds,
            label="abstraction",
        )
        attempts += 1
        for key, value in usage.as_dict().items():
            total_usage[key] += value
        abstraction_text = str(parsed.get("abstraction") or "").strip()
        if not abstraction_text:
            raise ValueError("abstraction step returned empty 'abstraction' text")
        leaked = scan_for_leak(abstraction_text, domain_terms)
        if not leaked:
            break

    return {
        "abstraction": abstraction_text,
        "scanned_domain_terms": domain_terms,
        "residual_leaked_terms": leaked,
        "firewall_clean": not leaked,
        "regen_attempts": attempts,
        "usage": total_usage,
    }
