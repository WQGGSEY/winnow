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


# --- Anti-laziness deterministic rules (P0c) ----------------------------- #


_CAPABILITY_METRIC_MARKERS = re.compile(
    r"\b(?:AUC|AUROC|F1|F-?score|accuracy|precision|recall|MAP|nDCG|ROC|MRR|BLEU)\b",
    re.IGNORECASE,
)

_DECISION_RULE_MARKERS = re.compile(
    # A claim that names a decision rule mentions a threshold-or-action language.
    # We accept several common shapes: "threshold T", "decision rule", "deploy when",
    # "reject if", "false-positive cost", "expected utility", "false-positive rate ≤",
    # "false-negative rate ≤", "Bayes-optimal", "decision-theoretic", "cost-weighted".
    r"\b(?:decision[\s_-]?rule|threshold|deploy[\s_-]?when|reject[\s_-]?if|"
    r"accept[\s_-]?if|false[\s_-]?positive[\s_-]?cost|false[\s_-]?negative[\s_-]?cost|"
    r"FP[\s_-]?cost|FN[\s_-]?cost|expected[\s_-]?utility|cost[\s_-]?weighted|"
    r"bayes[\s_-]?optimal|decision[\s_-]?theoretic|operating[\s_-]?point)\b",
    re.IGNORECASE,
)


def validate_baseline_provenance(
    *,
    baseline_refs: list[dict[str, Any]],
    market_dossier: dict[str, Any] | None,
    config: dict[str, Any] | None = None,
) -> ValidationResult:
    """Persona check that current_best_known baselines have real paper
    provenance, not self-made competitors.

    Catches the "AlgoXpert" anti-pattern: invent a baseline, beat it,
    claim progress. current_best_known must trace back to a market-
    research-discovered candidate whose dossier entry carries a paper
    citation / arXiv id / repo link.
    """
    cfg = config or {}
    violations: list[PersonaViolation] = []
    if not cfg.get("reject_self_made_baseline", True):
        return ValidationResult(ok=True, violations=[])
    if not market_dossier:
        return ValidationResult(ok=True, violations=[])

    candidates = {c.get("id"): c for c in (market_dossier.get("candidates_index") or [])}
    for ref in baseline_refs or []:
        roles = set(ref.get("roles") or [])
        if "current_best_known" not in roles:
            continue
        cand_ids = ref.get("candidates") or []
        ok_provenance = False
        for cid in cand_ids:
            cand = candidates.get(cid)
            if not cand:
                continue
            provenance = (
                cand.get("paper_citation")
                or cand.get("arxiv_id")
                or cand.get("doi")
                or cand.get("repo_url")
                or cand.get("filename")
            )
            if provenance and str(provenance).strip():
                ok_provenance = True
                break
        if not ok_provenance:
            violations.append(
                PersonaViolation(
                    rule="self_made_baseline",
                    message=(
                        f"baseline_ref dossier={ref.get('baseline_dossier_id')} "
                        f"carries the 'current_best_known' role but no candidate "
                        f"in its set has paper_citation / arxiv_id / doi / "
                        f"repo_url / filename provenance. Self-made baselines "
                        f"(invented for this thread) cannot satisfy "
                        f"current_best_known — the role exists specifically to "
                        f"force comparison against published prior work."
                    ),
                    suggested_fix=(
                        "Re-open the market_research dossier and pick a "
                        "candidate whose paper / arxiv / repo is downloaded "
                        "into reference_papers/. Use THAT as current_best_known. "
                        "Move the self-made comparator to a different role "
                        "(naive or random_or_null) or drop it."
                    ),
                )
            )
            break
    return ValidationResult(ok=not violations, violations=violations)


def validate_synthetic_data_bridging(
    *,
    dataset_manifest: dict[str, Any] | None,
    config: dict[str, Any] | None = None,
) -> ValidationResult:
    """Persona check that synthetic data sources carry an explicit
    synthetic-to-real bridging argument.

    Catches the "synthetic-only forever" anti-pattern: prove a claim on
    a toy generator and call it deployable. If the dataset_manifest
    declares synthetic data, it must explain how the synthetic regime
    constrains conclusions about the user's real environment.
    """
    cfg = config or {}
    violations: list[PersonaViolation] = []
    if not cfg.get("reject_synthetic_only_without_bridging", True):
        return ValidationResult(ok=True, violations=[])
    if not dataset_manifest:
        return ValidationResult(ok=True, violations=[])

    source = (
        dataset_manifest.get("data_source")
        or dataset_manifest.get("source")
        or ""
    )
    if "synthetic" not in str(source).lower():
        return ValidationResult(ok=True, violations=[])

    bridging = (
        dataset_manifest.get("synthetic_to_real_bridging_argument")
        or dataset_manifest.get("bridging_argument")
        or ""
    )
    if len(str(bridging).strip()) >= 80:
        return ValidationResult(ok=True, violations=[])

    violations.append(
        PersonaViolation(
            rule="synthetic_data_without_bridging",
            message=(
                "dataset_manifest declares a synthetic data source but the "
                "synthetic_to_real_bridging_argument field is missing or too "
                "short (<80 chars). Synthetic results cannot generalize to the "
                "user's real environment without an explicit bridging argument "
                "spelling out which structural properties (SNR range, asset "
                "count, time horizon, generator class) the synthetic regime "
                "shares with the user's real data, and which it does not."
            ),
            suggested_fix=(
                "Add a synthetic_to_real_bridging_argument string of >=80 "
                "chars naming: (1) which features of the user's real data "
                "the synthetic generator reproduces (cite an estimate of "
                "real-data SNR / regime from the market dossier or external "
                "source), (2) which features it does NOT (caveat), and "
                "(3) what conclusion is supported in the synthetic regime "
                "vs what would require real-data validation."
            ),
        )
    )
    return ValidationResult(ok=not violations, violations=violations)


def validate_camera_ready_directives(
    *,
    directives: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> ValidationResult:
    """Persona check that AC camera-ready directives include at least one
    new-measurement requirement, not just paper-text disclaimers.

    Catches the "AC waves through with disclaimer-only directives" anti-
    pattern: under-measured papers passed publish-revise because every
    directive was 'add a sentence to the abstract' rather than 'do the
    SNR sweep before camera-ready'.
    """
    cfg = config or {}
    violations: list[PersonaViolation] = []
    if not cfg.get("reject_disclaimer_only_directives", True):
        return ValidationResult(ok=True, violations=[])
    if not directives:
        return ValidationResult(ok=True, violations=[])

    if any(bool(d.get("requires_new_measurement")) for d in directives):
        return ValidationResult(ok=True, violations=[])

    violations.append(
        PersonaViolation(
            rule="disclaimer_only_directives",
            message=(
                "All your camera_ready_directives are paper-text additions "
                "(disclaimer, abstract sentence, limitations note). At least "
                "one directive must set requires_new_measurement=true and "
                "demand an additional experiment / measurement before camera-"
                "ready, otherwise rebuttal-surfaced concerns get buried as "
                "limitations rather than addressed."
            ),
            suggested_fix=(
                "Either (a) reject the current direction so advance_research "
                "can generate a blind replacement, OR (b) add a directive with requires_new_"
                "measurement=true naming the specific experiment (e.g. SNR "
                "sweep over [X, Y, Z] at n=N seeds=K) that must precede "
                "camera-ready."
            ),
        )
    )
    return ValidationResult(ok=not violations, violations=violations)


def validate_decision_rule_for_capability_claim(
    *,
    claim_contract: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> ValidationResult:
    """Persona check that capability claims (AUC / F1 / etc.) include a
    deployable decision rule, not just a ranking metric headline.

    Catches the "AUC headline only" anti-pattern: ranking metrics tell
    the user nothing about which threshold to deploy at, what FP/FN costs
    are, or how to act tomorrow. A real deployable claim includes a
    decision rule or operating-point statement.
    """
    cfg = config or {}
    violations: list[PersonaViolation] = []
    if not cfg.get("reject_decision_rule_missing_claim", True):
        return ValidationResult(ok=True, violations=[])

    claim_text = str(claim_contract.get("claim_under_test") or "")
    success = " ".join(str(s) for s in (claim_contract.get("success_criteria") or []))
    full_text = claim_text + " " + success

    # Only fire when the claim explicitly leans on a capability metric.
    if not _CAPABILITY_METRIC_MARKERS.search(full_text):
        return ValidationResult(ok=True, violations=[])

    has_decision_rule = bool(
        claim_contract.get("decision_rule")
    ) or bool(_DECISION_RULE_MARKERS.search(full_text))

    if has_decision_rule:
        return ValidationResult(ok=True, violations=[])

    violations.append(
        PersonaViolation(
            rule="capability_claim_without_decision_rule",
            message=(
                "Your claim_under_test leans on a ranking / capability metric "
                "(AUC, F1, accuracy, etc.) but does not specify a decision "
                "rule. A ranking metric alone tells the user 'which method "
                "ranks better' but not 'which threshold to deploy at, what "
                "false-positive / false-negative costs to expect, or how to "
                "act on a single new sample tomorrow'. Without that, even a "
                "perfect AUC=1.0 result is not deployable."
            ),
            suggested_fix=(
                "Add a claim_contract.decision_rule field (or include "
                "decision-rule language in the claim text itself): name the "
                "operating point (e.g. 'deploy if PSC score >= threshold T "
                "where T is calibrated to false-positive rate <= 0.05 on the "
                "training universe'), and state the FP/FN cost framing the "
                "user should expect."
            ),
        )
    )
    return ValidationResult(ok=not violations, violations=violations)


# --- PR7: feasibility envelope enforcement -------------------------------- #


def validate_claim_fits_envelope(
    *,
    claim_contract: dict[str, Any],
    envelope: dict[str, Any] | None,
    registered_adapter_ids: set[str] | None = None,
    config: dict[str, Any] | None = None,
) -> ValidationResult:
    """PR7: Reject a claim whose declared deploy_grade_scope cannot be met
    by the feasibility envelope. This is the *generator-for-goodness* gate
    — strong+safe by construction rather than filter-bad-after-the-fact.

    Rules:
      - claim.deploy_grade_scope is required (must be one of
        deployment/feasibility/directional).
      - 'deployment' requires (a) envelope.data_sources_available has a
        real_adapter, (b) that adapter is in registered_adapter_ids, AND
        (c) the claim's data_source_anchor names that adapter id.
      - 'feasibility' allows synthetic data sources but the claim's
        data_source_anchor must start with 'synthetic:' (or be a
        registered adapter id — feasibility on real data is fine too).
      - 'directional' allows anything but downgrades implicit expectation:
        decision_rule no longer required, no real-adapter requirement,
        synthetic OK without bridging.
    """
    cfg = config or {}
    violations: list[PersonaViolation] = []
    if not cfg.get("require_feasibility_envelope", True):
        return ValidationResult(ok=True, violations=[])

    # Backward-compat: if no envelope has been submitted and the caller
    # hasn't set deploy_grade_scope, the legacy claim shape is allowed
    # through. The strong+safe-by-construction gate fires the moment the
    # envelope file exists OR the claim explicitly sets a scope.
    scope = (claim_contract or {}).get("deploy_grade_scope")
    if scope is None and not envelope:
        return ValidationResult(ok=True, violations=[])

    if scope not in {"deployment", "feasibility", "directional"}:
        # Envelope exists OR scope was attempted but invalid — enforce.
        violations.append(
            PersonaViolation(
                rule="missing_deploy_grade_scope",
                message=(
                    "claim_contract.deploy_grade_scope is required when a "
                    "feasibility envelope has been submitted, and must be "
                    "one of {deployment, feasibility, directional}."
                ),
                suggested_fix=(
                    "Add deploy_grade_scope: 'deployment' if the user can "
                    "deploy this tomorrow on real data; 'feasibility' for a "
                    "synthetic-regime proof-of-concept with explicit bridging; "
                    "'directional' for a methodology-hint paper that does NOT "
                    "claim deploy-grade utility."
                ),
            )
        )
        return ValidationResult(ok=False, violations=violations)

    if scope == "directional":
        # Directional papers are allowed to skip the real-adapter
        # requirement, but they cannot LATER be upgraded to 'deployment'
        # without re-design. We let it through.
        return ValidationResult(ok=True, violations=[])

    if not envelope:
        violations.append(
            PersonaViolation(
                rule="missing_feasibility_envelope",
                message=(
                    "deploy_grade_scope is set but no FeasibilityEnvelope "
                    "has been submitted for this thread. The envelope must "
                    "precede the claim — call submit_feasibility_envelope "
                    "first."
                ),
                suggested_fix=(
                    "Call submit_feasibility_envelope(thread_id=..., envelope=...) "
                    "declaring data_sources_available, llm_oracles_available, "
                    "compute_budget, baseline_provenance_available, and "
                    "operator_intent. Then revise this claim with a scope "
                    "that fits the envelope."
                ),
            )
        )
        return ValidationResult(ok=False, violations=violations)

    data_sources = envelope.get("data_sources_available") or []
    real_adapter_ids_in_env = {
        s["id"] for s in data_sources
        if s.get("kind") == "real_adapter" and s.get("id")
    }
    anchor = (claim_contract.get("data_source_anchor") or "").strip()

    if scope == "deployment":
        # All three conditions must hold.
        if not real_adapter_ids_in_env:
            violations.append(
                PersonaViolation(
                    rule="deployment_scope_without_real_adapter_in_envelope",
                    message=(
                        "deploy_grade_scope='deployment' but the envelope "
                        "declares no real_adapter data source. Synthetic-"
                        "only evidence cannot back a deployment-grade claim."
                    ),
                    suggested_fix=(
                        "Either (a) downgrade deploy_grade_scope to "
                        "'feasibility' (synthetic + bridging argument is "
                        "acceptable there), or (b) re-run "
                        "submit_feasibility_envelope after registering a "
                        "real-data adapter in settings.json.data_adapters."
                    ),
                )
            )
        if not anchor or anchor not in real_adapter_ids_in_env:
            violations.append(
                PersonaViolation(
                    rule="deployment_anchor_not_in_envelope",
                    message=(
                        f"deploy_grade_scope='deployment' but "
                        f"claim_contract.data_source_anchor={anchor!r} is "
                        f"not one of the envelope's real_adapter ids "
                        f"({sorted(real_adapter_ids_in_env)})."
                    ),
                    suggested_fix=(
                        "Set claim_contract.data_source_anchor to one of the "
                        "real_adapter ids declared in the envelope."
                    ),
                )
            )
        if registered_adapter_ids is not None and anchor and anchor not in registered_adapter_ids:
            violations.append(
                PersonaViolation(
                    rule="deployment_anchor_not_registered",
                    message=(
                        f"data_source_anchor={anchor!r} is not registered "
                        f"under settings.json.data_adapters.registered "
                        f"({sorted(registered_adapter_ids)})."
                    ),
                    suggested_fix=(
                        "Add the adapter to settings.json.data_adapters.registered "
                        "with id + module + provenance, or pick an already-"
                        "registered adapter id."
                    ),
                )
            )
        return ValidationResult(ok=not violations, violations=violations)

    # scope == 'feasibility'
    if not anchor:
        violations.append(
            PersonaViolation(
                rule="feasibility_scope_without_data_anchor",
                message=(
                    "deploy_grade_scope='feasibility' requires "
                    "data_source_anchor to be set (either 'synthetic:<label>' "
                    "or a real adapter id)."
                ),
                suggested_fix=(
                    "Set data_source_anchor='synthetic:<generator_label>' if "
                    "using synthetic data, or a registered adapter_id if "
                    "using real data."
                ),
            )
        )
    elif anchor.startswith("synthetic:"):
        # OK — but the dataset_manifest's bridging argument will be checked
        # by validate_synthetic_data_bridging separately.
        pass
    elif anchor not in real_adapter_ids_in_env:
        violations.append(
            PersonaViolation(
                rule="feasibility_anchor_not_in_envelope",
                message=(
                    f"data_source_anchor={anchor!r} doesn't match any "
                    f"envelope source. Synthetic anchors must start with "
                    f"'synthetic:'; real anchors must match a declared "
                    f"real_adapter id ({sorted(real_adapter_ids_in_env)})."
                ),
                suggested_fix=(
                    "Pick an anchor declared in the envelope, or update the "
                    "envelope first via submit_feasibility_envelope."
                ),
            )
        )
    return ValidationResult(ok=not violations, violations=violations)
