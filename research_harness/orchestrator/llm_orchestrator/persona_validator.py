"""Deterministic persona-enforcement layer.

Sits at the MCP server boundary. When Claude Code's reasoning produces a
claim contract / decision / commentary, we run it through this validator
BEFORE accepting it. Violations cause the tool call to be REJECTED with an
explicit reason; Claude Code retries.

The point is to enforce the operator's stated persona:
  - Professor: honest + strong, no lazy / safe / weak claims, no one-off code
  - GradStudent: skeptical, will push back, not blindly compliant

Each rule is conservative (high precision over recall). The goal is to catch
common drift patterns deterministically; subtle drift falls to the LLM-based
critic pack downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class PersonaViolation:
    rule: str
    message: str
    suggested_fix: str


@dataclass
class ValidationResult:
    ok: bool
    violations: list[PersonaViolation]

    def reject_message(self) -> str:
        """Format violations into a single retry instruction for Claude Code."""
        if not self.violations:
            return ""
        lines = [
            "REJECTED by persona enforcement layer. Your previous attempt "
            "violated the operator's contract on one or more axes. Address ALL "
            "of the following and retry:",
        ]
        for v in self.violations:
            lines.append(f"  - [{v.rule}] {v.message}")
            lines.append(f"      Fix: {v.suggested_fix}")
        lines.append(
            "Do not work around these checks by adding superficial text — the "
            "checks are deterministic. Produce a substantively stronger answer."
        )
        return "\n".join(lines)


_PLACEHOLDER_TOKENS = (
    "tbd",
    "todo",
    "to be determined",
    "professor will design",
    "professor must define",
    "placeholder",
    "various",
    "any reasonable",
    "something like",
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _token_overlap(a: str, b: str) -> float:
    """Token overlap. Splits on any non-letter/digit boundary, keeping
    unicode word characters so Korean / CJK input is tokenized roughly
    syllable-block by syllable-block instead of being collapsed to empty."""
    # `\w` in unicode mode matches Hangul, Hiragana, Hanzi, etc.
    aw = set(re.findall(r"\w+", a.lower(), flags=re.UNICODE))
    bw = set(re.findall(r"\w+", b.lower(), flags=re.UNICODE))
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(len(aw), len(bw))


def validate_claim_contract(
    *,
    new_claim: dict[str, Any],
    problem_statement: str | None = None,
    market_context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> ValidationResult:
    """Persona check on a Professor-designed claim_contract.

    Catches the most common Professor drift patterns: lazy restatement of
    the problem as the claim, placeholder baselines, fuzzy success
    criteria, unhittable disproof.
    """
    cfg = config or {}
    violations: list[PersonaViolation] = []
    claim = str(new_claim.get("claim_under_test") or "").strip()
    baselines = list(new_claim.get("mandatory_baselines") or [])
    success = list(new_claim.get("success_criteria") or [])
    disproof = list(new_claim.get("disproof_conditions") or [])

    # 1. Claim cannot be a restatement of the problem.
    if (
        cfg.get("reject_problem_restatement", True)
        and problem_statement
        and claim
    ):
        overlap = _token_overlap(claim, problem_statement)
        if overlap > 0.7 and len(claim) <= len(problem_statement) * 1.5:
            violations.append(
                PersonaViolation(
                    rule="problem_restatement",
                    message=(
                        f"Your claim_under_test is essentially a restatement "
                        f"of the problem (token overlap {overlap:.0%}). The "
                        f"Professor's job is to DESIGN a testable claim, not "
                        f"echo the problem back."
                    ),
                    suggested_fix=(
                        "Commit to a specific methodology + a specific "
                        "numerical threshold the claim must beat. The claim "
                        "should be defensible in front of an area chair as "
                        "a non-trivial contribution."
                    ),
                )
            )

    # 2. Baselines must NOT contain placeholder tokens and must be reasonably
    #    specific.
    if cfg.get("reject_placeholder_baselines", True):
        min_chars = int(cfg.get("min_baseline_role_specificity_chars", 40))
        for b in baselines:
            b_norm = _normalize(str(b))
            if any(token in b_norm for token in _PLACEHOLDER_TOKENS):
                violations.append(
                    PersonaViolation(
                        rule="placeholder_baseline",
                        message=f"Baseline contains placeholder language: \"{str(b)[:120]}\"",
                        suggested_fix=(
                            "Name a concrete published heuristic / algorithm "
                            "for each baseline role. 'TBD' is a contract "
                            "violation."
                        ),
                    )
                )
                break
            if len(str(b)) < min_chars:
                violations.append(
                    PersonaViolation(
                        rule="baseline_too_vague",
                        message=(
                            f"Baseline is too short to be specific "
                            f"(<{min_chars} chars): \"{str(b)[:120]}\""
                        ),
                        suggested_fix=(
                            "Spell out the baseline algorithm + its data "
                            "preprocessing in one sentence so a grad student "
                            "could implement it without asking."
                        ),
                    )
                )
                break

    # 3. Success criteria must reference at least one measurable threshold.
    if cfg.get("reject_missing_measurable_success", True) and success:
        has_threshold = any(
            re.search(r"\d+(\.\d+)?\s*(%|percent)|>=|<=|>|<|threshold", str(s), re.I)
            for s in success
        )
        if not has_threshold:
            violations.append(
                PersonaViolation(
                    rule="success_no_measurable_threshold",
                    message=(
                        "None of your success_criteria contains a measurable "
                        "threshold (>=, <=, percentage, etc.). 'works well' / "
                        "'shows improvement' are not testable."
                    ),
                    suggested_fix=(
                        "Pick at least one criterion with an explicit number "
                        "the experiment must clear (e.g. 'AUROC >= 0.80', "
                        "'p < 0.01 vs permutation null')."
                    ),
                )
            )

    # 4. Baselines must be grounded in market output (when available).
    #    The market step downloaded reference papers + wrote a
    #    baseline_dossier_candidate. If the Professor's baselines don't
    #    reference any of that material, the design is essentially
    #    free-styled — a persona violation.
    if (
        cfg.get("require_market_grounding", True)
        and market_context
        and baselines
    ):
        dossier_text = (market_context.get("baseline_dossier_yaml") or "").lower()
        analysis_text = (market_context.get("baseline_analysis_md") or "").lower()
        paper_filenames = [
            str(p.get("filename", "")).lower()
            for p in (market_context.get("reference_papers") or [])
            if isinstance(p, dict)
        ]
        if dossier_text or analysis_text or paper_filenames:
            grounded_count = 0
            # Role labels + filler — would falsely "ground" any baseline
            # that just labels itself "current_best:" etc.
            ROLE_STOP = {
                "current", "best", "known", "naive", "random",
                "null", "baseline", "baselines", "with", "from", "this",
                "that", "they", "them", "have", "been", "would", "should",
                "could", "must", "into", "than", "then", "when", "where",
                "which", "what", "well", "more", "some", "such", "very",
                "approach", "approaches", "method", "methods", "methodology",
                "generic", "different", "various", "unrelated", "completely",
                "yet", "another", "not", "the", "and", "for", "but", "are",
                "ground", "grounded", "papers", "paper", "dossier", "output",
                "entirely", "enough", "chars", "pass", "length",
            }
            for b in baselines:
                b_norm = _normalize(str(b))
                raw_b_tokens = set(re.findall(r"[a-z0-9]{4,}", b_norm))
                b_tokens = raw_b_tokens - ROLE_STOP
                if not b_tokens:
                    continue
                dossier_tokens = (
                    set(re.findall(r"[a-z0-9]{4,}", dossier_text)) - ROLE_STOP
                )
                analysis_tokens = (
                    set(re.findall(r"[a-z0-9]{4,}", analysis_text)) - ROLE_STOP
                )
                shared_dossier = len(b_tokens & dossier_tokens)
                shared_analysis = len(b_tokens & analysis_tokens)
                cites_paper = any(
                    fname.replace(".pdf", "") in b_norm
                    or any(part in b_norm for part in fname.split("_") if len(part) > 4)
                    for fname in paper_filenames
                )
                if shared_dossier >= 2 or shared_analysis >= 2 or cites_paper:
                    grounded_count += 1
            if grounded_count == 0:
                violations.append(
                    PersonaViolation(
                        rule="baselines_ignore_market_output",
                        message=(
                            "Your baselines don't appear to reference the "
                            "market step's output (baseline_dossier_candidate, "
                            "baseline_analysis, or reference_papers). The "
                            "market step downloaded real prior work "
                            "specifically so the Professor could ground the "
                            "baseline triad in it."
                        ),
                        suggested_fix=(
                            "Re-read the market output from "
                            "`get_research_state`. Copy the concrete "
                            "algorithm names + their published metric "
                            "thresholds verbatim from baseline_dossier_"
                            "candidate. Cite at least one reference paper by "
                            "filename / arxiv id in the current_best_known "
                            "baseline."
                        ),
                    )
                )

    # 5. Disproof conditions must be specific and reachable.
    if cfg.get("reject_unhittable_disproof", True) and disproof:
        has_reachable = any(
            re.search(r"\d+(\.\d+)?|fails?|does not|cannot", str(d), re.I)
            for d in disproof
        )
        if not has_reachable:
            violations.append(
                PersonaViolation(
                    rule="disproof_unhittable",
                    message=(
                        "Your disproof_conditions are too abstract to "
                        "actually trigger. A disproof condition that never "
                        "fires is not a falsification — it's protection."
                    ),
                    suggested_fix=(
                        "Pick at least one condition that explicitly names an "
                        "outcome (numeric or categorical) that would refute "
                        "the claim if observed."
                    ),
                )
            )

    return ValidationResult(ok=not violations, violations=violations)


def validate_grad_student_review(
    *,
    commentary: str,
    concerns: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
) -> ValidationResult:
    """Persona check on a GradStudent's pre-run review or post-run commentary.

    Catches the "blindly compliant grad student" drift: pure agreement, no
    challenge, no skepticism.
    """
    cfg = config or {}
    violations: list[PersonaViolation] = []
    if not cfg.get("require_grad_student_challenge", True):
        return ValidationResult(ok=True, violations=[])

    text = (commentary or "").strip()
    concerns_list = concerns or []
    if not text:
        return ValidationResult(ok=True, violations=[])

    challenge_markers = [
        # Korean
        "잠깐",
        "의문",
        "의심",
        "걱정",
        "이상화",
        "이상해",
        "확인해",
        "정말 그런",
        "하지만",
        # English
        "wait",
        "concern",
        "suspect",
        "doubt",
        "however",
        "but ",
        "isn't this",
        "may not",
        "questionable",
        "issue:",
        "problem:",
        "could be wrong",
    ]
    has_challenge = any(m in text.lower() for m in challenge_markers) or bool(concerns_list)
    if not has_challenge:
        violations.append(
            PersonaViolation(
                rule="grad_student_no_challenge",
                message=(
                    "Your commentary is pure agreement with the advisor's "
                    "design. A sharp grad student raises at least one "
                    "concrete concern or asks a clarifying question."
                ),
                suggested_fix=(
                    "Re-read the setup with skepticism. Is the synthetic "
                    "data idealized? Is the baseline triad fair? Is success "
                    "measurable against the actual claim? Flag at least one "
                    "thing before you proceed."
                ),
            )
        )
    return ValidationResult(ok=not violations, violations=violations)


def validate_follow_up_strength(
    *,
    parent_claim: str,
    follow_ups: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> ValidationResult:
    """Persona check on Professor-proposed follow-up successor claims.

    Catches the "scope-narrowing only" drift: every follow-up just weakens
    the parent claim rather than opening a new axis.
    """
    cfg = config or {}
    violations: list[PersonaViolation] = []
    if not follow_ups:
        return ValidationResult(ok=True, violations=[])
    weakening = []
    for f in follow_ups:
        succ = str(f.get("successor_claim") or "")
        if not succ:
            continue
        # Catch "scoped version of <parent>" style restatements that don't
        # add evidence — token overlap with parent + same length range.
        overlap = _token_overlap(succ, parent_claim)
        if overlap > 0.85 and len(succ) < len(parent_claim) * 1.2:
            weakening.append(succ[:120])
    if len(weakening) == len(follow_ups) and len(follow_ups) >= 2:
        violations.append(
            PersonaViolation(
                rule="follow_ups_all_weaken",
                message=(
                    "All of your follow-up claims are near-restatements of "
                    "the parent (high token overlap, similar length). The "
                    "advisor is supposed to open NEW axes (mechanism, "
                    "necessity, boundary), not chain weakenings."
                ),
                suggested_fix=(
                    "Each follow-up should explore a different axis of the "
                    "parent's success. Use the claim-type roadmap: at least "
                    "one mechanism, one necessity, one boundary."
                ),
            )
        )
    return ValidationResult(ok=not violations, violations=violations)
