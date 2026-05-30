"""MCP server — Claude Code interactive drives reasoning through these tools.

This module exposes the research_harness state as a Model-Context-Protocol
server. The operator runs the frontend to set up grilling + market, then
opens Claude Code interactive in a terminal, points it at this MCP server,
and issues a single command (`/research start <thread_id>`). Claude Code
itself becomes the Professor + GradStudent — reading state via these tools
and committing decisions back.

Persona is enforced at the boundary: every tool call goes through
persona_validator before being persisted. Violations are returned as
explicit retry instructions so Claude Code rewrites and retries.

Tools exposed
-------------
- get_research_state(thread_id)          : full current snapshot
- get_next_admissible_node(thread_id)    : next claim to work on
- design_initial_claim_contract(...)     : convert problem → strong claim
- design_experiment_template(...)        : write per-claim experiment code
- submit_grad_student_review(...)        : pre-run skeptical review
- submit_grad_student_commentary(...)    : post-run skeptical commentary
- submit_professor_decision(...)         : verdict + follow-up children
- decide_publication_readiness(...)      : submit-or-keep-working
- finalize_paper(...)                    : trigger rebuttal + AC + publish

The MCP server is intentionally THIN — it does not run experiments or
LLMs, it only reads/writes the harness's existing schemas and applies the
persona validator. The work loop (cycle through stages, dispatch to
worker, etc.) is replaced by Claude Code's own multi-step reasoning.

Implementation note: this file uses a minimal stdio JSON-RPC subset of the
MCP spec — that way no external `mcp` package is required. Claude Code's
MCP client follows the same protocol shape that
`claude.ai/mcp` documents.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from research_harness.config import load_settings
from research_harness.orchestrator.llm_orchestrator.persona_validator import (
    validate_baseline_provenance,
    validate_camera_ready_directives,
    validate_claim_contract,
    validate_claim_fits_envelope,
    validate_decision_rule_for_capability_claim,
    validate_follow_up_strength,
    validate_grad_student_review,
    validate_revision_after_reject,
    validate_synthetic_data_bridging,
)


# --- MCP tool definitions ------------------------------------------------- #


PROFESSOR_CONTRACT = (
    "You are the Professor (\"교수님\"). When you call any tool below, you are "
    "acting as the senior advisor in our claim-first research harness. "
    "Persona is enforced deterministically — calls that produce lazy, safe, "
    "weak, or problem-restating claims will be REJECTED with an explicit "
    "reason and you'll be asked to retry. "
    "Hard rules:\n"
    "  • Never restate the problem as the claim.\n"
    "  • Never use placeholder baselines (\"TBD\", \"various\", etc.).\n"
    "  • Success criteria MUST commit to a specific numeric threshold.\n"
    "  • Disproof conditions MUST be reachable (not 'should be impossible').\n"
    "  • When promoting a node, the follow-up children must open NEW axes "
    "(mechanism / necessity / boundary), not chain weakenings of the parent.\n"
    "  • When designing experiment code, REUSE the thread's existing _lib/ "
    "modules; only add new shared modules when truly missing."
)


GRAD_STUDENT_CONTRACT = (
    "You are the GradStudent (\"대학원생\"). When submitting reviews or "
    "commentary, you do NOT just agree with the advisor. You read the setup "
    "with skepticism — synthetic-data idealization, unfair baselines, "
    "unmeasurable success, unhittable disproof — and flag at least one "
    "concrete concern before proceeding. Pure agreement is a persona "
    "violation and will be REJECTED."
)


PRACTITIONER_REVIEWER_CONTRACT = (
    "You are an EXTREME PRACTITIONER reviewer wearing one specific critic "
    "hat (validity, reproducibility, mechanism, taste, etc. — the critic_id "
    "tells you which). The persona contract is enforced by schema: every "
    "review you submit MUST populate the five practitioner fields or it is "
    "rejected:\n"
    "  • so_what — 2-4 sentences in operator language. What does this "
    "evidence actually teach us about the world?\n"
    "  • next_actions — concrete things to do in 1-4 weeks. Each item: "
    "{action, owner_role, eta_weeks, prerequisite_evidence}. \"Consider X\" "
    "and \"investigate Y\" are NOT acceptable.\n"
    "  • practitioner_take — would YOU commit resources on this evidence "
    "today? If yes, what guard rails? If no, what cheapest experiment "
    "flips your call?\n"
    "  • evidence_anchors — specific pointers into worker_report / "
    "baselines / dialog entries that justify your scores. No anchors → "
    "the review is opinion, rejected.\n"
    "  • direct_methodology_for_user — does this evidence give the "
    "ORIGINAL user (intake submitter) a direct methodology they can apply? "
    "{verdict: provides|partial|absent, methodology_summary, gap_to_close}. "
    "A technically-strong claim that doesn't help the user act must "
    "LOWER scores — proving a claim ≠ helping the user.\n"
    "Read critics/PRACTITIONER_PERSONA.md and the per-critic body before "
    "writing. prepare_rebuttal_packet surfaces both, plus the original "
    "user intake so you can judge methodology fit."
)


AC_CONTRACT = (
    "You are the Area Chair (AC). You have read every rebuttal-stage critic "
    "review and the full rebuttal packet. Your job is NOT to compute a "
    "threshold mean — that is rejected as fake. You synthesize the rebuttal "
    "conversation and tell the Professor what the camera-ready manuscript "
    "must look like. Even an 'accept' MUST emit camera_ready_directives — "
    "rebuttal-surfaced insights that the Professor will fold into the "
    "camera-ready. An accept with zero directives is rejected: if the "
    "evidence is so clean that no rebuttal added anything, the AC must "
    "still name the boundary condition or scope sharpening that the "
    "rebuttal made explicit. Every directive must cite origin_critic_ids "
    "and target a specific paper section. "
    "You must ALSO synthesize a methodology_assessment in rebuttal_synthesis: "
    "every reviewer scored direct_methodology_for_user against the ORIGINAL "
    "user intake. Aggregate those verdicts and decide whether the body of "
    "evidence as a whole hands the original problem-asker a usable methodology. "
    "If aggregate_verdict ≠ 'provides', at least one camera_ready_directive "
    "MUST address the methodology gap — a paper that proves the reshaped "
    "claim but leaves the user stuck does not earn camera-ready."
)


TOOL_DEFINITIONS = [
    {
        "name": "get_research_state",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Returns the current research state for a thread: thread.json, "
            "grilling extraction, market brief, search_state (if production "
            "started), roadmap, readiness history, and per-node dialogs. "
            "Always call this BEFORE any decision so you have current context."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string"}},
        },
    },
    {
        "name": "get_next_admissible_node",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Returns the SINGLE next node the lab should work on. Selection "
            "policy (deterministic, the harness owns this — do NOT pick a "
            "different node):\n"
            "  1. Compute the active research stage from coverage. Stages: "
            "scope_pinning (admits validity/taste/operational) → "
            "baseline_evidence (capability) → mechanism_or_necessity "
            "(mechanism/necessity) → boundary_ablation (boundary/constraint). "
            "Advance when the current stage has at least one promoted node "
            "OR has no remaining queued admissible nodes.\n"
            "  2. From the queued frontier, keep only nodes whose type the "
            "active stage admits.\n"
            "  3. Sort by type weight (validity=0, capability=1, "
            "necessity=2, mechanism=3, boundary=4, constraint=5, "
            "operational=6, taste=7), then depth asc, then id.\n"
            "  4. Return the top-1.\n"
            "After processing the returned node (review → run → professor "
            "decision), call this again to get the next. Token-efficient: "
            "ONE node at a time, no parallel chains."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string"}},
        },
    },
    {
        "name": "resume_production_state",
        "description": (
            "Recovery tool. Call this if the previous Claude Code session "
            "died mid-run (e.g. process killed during execute_node_experiment). "
            "Demotes any node stuck in 'running' WITHOUT a persisted "
            "worker_report back to 'ready', so the selector re-queues it. "
            "Nodes that ARE persisted (completed_worker_report / critic_reviewed "
            "/ orchestrator_reduced) are left alone — get_next_admissible_node "
            "already picks them up and tells you which tool to call next."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string"}},
        },
    },
    {
        "name": "submit_feasibility_envelope",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "PR7: BEFORE designing any claim, the Professor declares the "
            "FeasibilityEnvelope — what the harness actually has at this "
            "thread's disposal: registered real-data adapters, available "
            "LLM oracles (subscription / live API / proxy), compute budget "
            "(seconds per node / concurrent nodes / total hours), "
            "paper-cited baselines from the market dossier, and the "
            "operator's target deploy_grade_scope. This anchors every "
            "subsequent claim: 'deployment' scope is blocked when no real "
            "adapter is registered, 'live LLM' oracle is blocked when no "
            "billing_ack is set, etc. The envelope is persisted to "
            "production/feasibility_envelope.json and read by every "
            "downstream validator."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "envelope"],
            "properties": {
                "thread_id": {"type": "string"},
                "envelope": {
                    "type": "object",
                    "description": "FeasibilityEnvelope object — see feasibility_envelope.schema.json.",
                },
            },
        },
    },
    {
        "name": "compute_falsifier_result",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "ADR 0006: run the envelope's external_falsifier predicate over "
            "held-out evidence. The HARNESS computes the pass/fail — you cannot "
            "assert achieved=true directly. A passing result against the "
            "registered holdout is the precondition the user-goal attestation "
            "gate checks. For cross_generator_transfer supply evidence="
            "{ranking_a:[...], ranking_b:[...]} where ranking_b is the HELD-OUT "
            "generator B over the SAME pipeline order (the harness computes "
            "Spearman rho). For real_holdout supply evidence={observed:<number "
            "measured on the real adapter>}. Without a registered falsifier "
            "(kind!=none) this is refused and the thread stays at "
            "unverified_screen."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "evidence"],
            "properties": {
                "thread_id": {"type": "string"},
                "evidence": {
                    "type": "object",
                    "description": (
                        "Kind-specific. cross_generator_transfer: {ranking_a, "
                        "ranking_b, pipeline_labels?}. real_holdout: {observed, "
                        "adapter_provenance?}. ADR 0008: cross_generator_transfer "
                        "is proposer-authored — it is a kill-capable SCREEN, not a "
                        "strength-certifier. Only real_holdout reaches transfer_valid."
                    ),
                },
            },
        },
    },
    {
        "name": "pin_frozen_question",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "ADR 0008 Axis 1: pin the FORMAL QUESTION the construct-adversary is "
            "funded against, BEFORE the construction loop. Authorship separation — "
            "the question is frozen from grilling (a pre-construction artifact) with "
            "a provenance hash and is IMMUTABLE; the construction cannot restate or "
            "modify the referent it is judged against. Required before "
            "submit_construct_adversary_report. Earning construct_valid (close to "
            "the QUESTION) is air-gapped-possible; reality-closeness (transfer_valid) "
            "is NOT — it needs a real referent the operator holds."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "question"],
            "properties": {
                "thread_id": {"type": "string"},
                "question": {
                    "type": "object",
                    "description": "FrozenQuestion — see frozen_question.schema.json. source_provenance is stamped by the harness from the grilling artifact.",
                },
            },
        },
    },
    {
        "name": "submit_construct_adversary_report",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "ADR 0008 Axis 1: submit the funded construct-adversary's ENUMERATED "
            "search of the construction's pass_but_wrong region against the frozen "
            "question. The adversary is authored to BREAK (referent = frozen "
            "question, not the proposer's reasoning) and has no accept authority. "
            "construct_valid is earned ONLY by a funded FAILURE: budget spent, "
            ">= min distinct pass_but_wrong worlds tested, and NO world where the "
            "measurement passes but the frozen answer is NO. The harness RE-DERIVES "
            "the verdict — a self-reported 'survived' enumerating a breaking world is "
            "overruled, and budget-0 is auto-invalid."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "report"],
            "properties": {
                "thread_id": {"type": "string"},
                "report": {
                    "type": "object",
                    "description": "ConstructAdversaryReport — see construct_adversary_report.schema.json.",
                },
            },
        },
    },
    {
        "name": "design_initial_claim_contract",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Convert the funding-agency problem statement into a strong + "
            "honest claim_contract. PRECONDITION: you MUST have called "
            "`get_research_state` first and read the returned "
            "`baseline_dossier_candidate_yaml`, `baseline_analysis_md`, and "
            "`reference_papers` list. The three mandatory_baselines you "
            "submit must be GROUNDED in that material: copy the concrete "
            "algorithm names + their published metric thresholds verbatim "
            "from the dossier candidate, and cite at least one paper by "
            "name (filename, title, or arxiv id) in the current_best_known "
            "baseline. Submissions that ignore the market output get "
            "REJECTED. Required fields: claim_under_test (must NOT be a "
            "restatement of the problem), mandatory_baselines (3 concrete "
            "strings — current_best_known / naive / random_or_null, each "
            "grounded in the market output), success_criteria (>=1 with a "
            "numeric threshold), disproof_conditions (>=1 reachable)."
        ),
        "inputSchema": {
            "type": "object",
            "required": [
                "thread_id",
                "claim_under_test",
                "mandatory_baselines",
                "success_criteria",
                "disproof_conditions",
                "deploy_grade_scope",
            ],
            "properties": {
                "thread_id": {"type": "string"},
                "claim_under_test": {"type": "string"},
                "mandatory_baselines": {"type": "array", "items": {"type": "string"}},
                "success_criteria": {"type": "array", "items": {"type": "string"}},
                "disproof_conditions": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
                "deploy_grade_scope": {
                    "type": "string",
                    "enum": ["deployment", "feasibility", "directional"],
                    "description": (
                        "PR7: required when a feasibility_envelope is on disk. "
                        "'deployment'=real_adapter-backed end-to-end claim; "
                        "'feasibility'=synthetic-regime proof-of-concept with "
                        "explicit bridging; 'directional'=methodology-hint, "
                        "no deploy-grade utility claim."
                    ),
                },
                "data_source_anchor": {
                    "type": "string",
                    "description": (
                        "PR7: anchor id from the feasibility_envelope — either a "
                        "registered real_adapter id or 'synthetic:<label>'."
                    ),
                },
            },
        },
    },
    {
        "name": "design_experiment_template",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Write the experiment code for the current node. Submit a "
            "plan_metadata dict (task_class, objective, entrypoint, resources, "
            "expected_outputs, baseline_evidence_requirements, source_files). "
            "source_files paths starting with `_lib/` are shared across the "
            "whole thread (do this once at the root); other paths land under "
            "the per-node dir. REUSE existing shared modules whenever they "
            "suffice — call get_research_state to see what's already there. "
            "The harness writes the files to disk; this tool does not run "
            "the experiment (that's execute_node_experiment)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "node_id", "plan_metadata"],
            "properties": {
                "thread_id": {"type": "string"},
                "node_id": {"type": "string"},
                "plan_metadata": {"type": "object"},
            },
        },
    },
    {
        "name": "execute_node_experiment",
        "description": (
            "Run the experiment for the current node. The harness builds "
            "the experiment_plan, materializes the source tree, runs "
            "LocalRunner against it, and returns the worker_report. No LLM "
            "call. Transitions node from 'ready'→'running'→"
            "'completed_worker_report' on success."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "node_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "node_id": {"type": "string"},
            },
        },
    },
    {
        "name": "run_critic_reviews",
        "description": (
            "Run the deterministic critic pack (always/by_node_type/by_domain/"
            "by_stage) on the node's worker_report. Returns the list of "
            "critic_review dicts. Transitions node to 'critic_reviewed'."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "node_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "node_id": {"type": "string"},
            },
        },
    },
    {
        "name": "submit_grad_student_review",
        "description": (
            f"{GRAD_STUDENT_CONTRACT}\n\n"
            "Pre-run review of a worker_task. Submit your commentary plus a "
            "list of concerns. Pure-agreement commentary (no challenge) "
            "will be REJECTED. Concerns should name what specifically looks "
            "idealized / unfair / unmeasurable."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "node_id", "commentary"],
            "properties": {
                "thread_id": {"type": "string"},
                "node_id": {"type": "string"},
                "commentary": {"type": "string"},
                "concerns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["summary", "evidence"],
                        "properties": {
                            "summary": {"type": "string"},
                            "evidence": {"type": "string"},
                            "blocked": {"type": "boolean"},
                        },
                    },
                },
            },
        },
    },
    {
        "name": "submit_professor_decision",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Promote / branch / prune a node after seeing the worker report "
            "and critic reviews. When promoting, you must also propose 2-4 "
            "follow_up_children that explore NEW axes — mechanism / "
            "necessity / boundary / constraint — not just rewordings of the "
            "parent. Restatements will be REJECTED."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "node_id", "next_transition"],
            "properties": {
                "thread_id": {"type": "string"},
                "node_id": {"type": "string"},
                "next_transition": {
                    "type": "string",
                    "enum": ["promoted", "needs_child_branch", "pruned"],
                },
                "final_verdict": {"type": "string"},
                "response_to_grad_student": {"type": "string"},
                "follow_up_children": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["type", "successor_claim"],
                        "properties": {
                            "type": {"type": "string"},
                            "successor_claim": {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    {
        "name": "revise_root_after_reject",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "The AC rejected the paper. Propose a NEW root claim — stronger, "
            "not safer — that addresses the AC's blocking reasons. Same "
            "persona enforcement as design_initial_claim_contract: no "
            "restatement of the AC's complaint, no placeholder baselines, "
            "must reference the market output."
        ),
        "inputSchema": {
            "type": "object",
            "required": [
                "thread_id",
                "new_claim_under_test",
                "mandatory_baselines",
                "success_criteria",
                "disproof_conditions",
                "deploy_grade_scope",
            ],
            "properties": {
                "thread_id": {"type": "string"},
                "new_claim_under_test": {"type": "string"},
                "mandatory_baselines": {"type": "array", "items": {"type": "string"}},
                "success_criteria": {"type": "array", "items": {"type": "string"}},
                "disproof_conditions": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
                "deploy_grade_scope": {
                    "type": "string",
                    "enum": ["deployment", "feasibility", "directional"],
                    "description": (
                        "PR7: required when a feasibility_envelope is on disk. "
                        "Same semantics as design_initial_claim_contract."
                    ),
                },
                "data_source_anchor": {
                    "type": "string",
                    "description": (
                        "PR7: anchor id from envelope — real_adapter id or "
                        "'synthetic:<label>'."
                    ),
                },
                "evidence_breadth": {
                    "type": "object",
                    "description": (
                        "Optional. Breadth of the revised claim's evidence "
                        "(used by validate_revision_after_reject to detect "
                        "narrowing-without-breadth)."
                    ),
                },
                "previous_evidence_breadth": {
                    "type": "object",
                    "description": "Optional. Breadth of the prior rejected claim's evidence.",
                },
            },
        },
    },
    {
        "name": "decide_publication_readiness",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Decide whether the claim tree is mature enough to submit to "
            "the area chair. NOT based on iterations — based on coverage of "
            "the critical successor axes (mechanism, necessity, boundary). "
            "Premature submissions and 'submit because we're tired' are "
            "violations. If keep_working, name the missing axis."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "submit"],
            "properties": {
                "thread_id": {"type": "string"},
                "submit": {"type": "boolean"},
                "advisor_message": {"type": "string"},
                "missing_axes": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "prepare_rebuttal_packet",
        "description": (
            "Build the rebuttal packet markdown deterministically and return "
            "(1) the packet text, (2) the routed critic list with each "
            "critic's full body (so you can role-play each one in turn), "
            "(3) the practitioner persona contract text, and (4) the worker "
            "report + baselines for the promoted node. After this call, "
            "loop through the returned critic_list and call "
            "submit_rebuttal_critic_review once per critic_id. The packet "
            "itself contains NO synthesized critic scores — those come from "
            "you via submit_rebuttal_critic_review."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "promoted_node_id": {
                    "type": "string",
                    "description": "Optional. Defaults to the first promoted node closest to root.",
                },
            },
        },
    },
    {
        "name": "submit_rebuttal_critic_review",
        "description": (
            f"{PRACTITIONER_REVIEWER_CONTRACT}\n\n"
            "Submit ONE critic's review of the promoted node's rebuttal "
            "packet. Idempotent: re-submitting with the same critic_id "
            "overwrites the previous review. Schema validation runs before "
            "the file is persisted; missing so_what / next_actions / "
            "practitioner_take / evidence_anchors fields cause rejection."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "review"],
            "properties": {
                "thread_id": {"type": "string"},
                "review": {
                    "type": "object",
                    "description": "Full CriticReview object — see critic_review.schema.json.",
                },
            },
        },
    },
    {
        "name": "submit_orchestrator_reduction",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "After all rebuttal-stage critics have submitted, the Professor "
            "submits a single orchestrator-level reduction for the promoted "
            "node. This is the final per-node verdict that the AC will read. "
            "Synthesize the rebuttal critics' practitioner takes; do NOT "
            "average scores. Field final_verdict must be one of: supported, "
            "supported_with_scope_narrowing, contradicted, "
            "confounded_or_not_evaluable, taste_rejected_local_branch, "
            "blocked_by_operational_issue."
        ),
        "inputSchema": {
            "type": "object",
            "required": [
                "thread_id", "node_id", "final_verdict",
                "research_status", "score_summary", "synthesis_message",
            ],
            "properties": {
                "thread_id": {"type": "string"},
                "node_id": {"type": "string"},
                "final_verdict": {"type": "string"},
                "research_status": {"type": "string"},
                "score_summary": {
                    "type": "object",
                    "properties": {
                        "validity": {"type": "integer"},
                        "necessity": {"type": "integer"},
                        "reproducibility": {"type": "integer"},
                        "taste_alignment": {"type": "integer"},
                    },
                },
                "synthesis_message": {"type": "string", "minLength": 60},
                "blocking_objections": {"type": "array", "items": {"type": "string"}},
                "accepted_lesson_candidates": {"type": "array", "items": {"type": "string"}},
                "next_transition": {"type": "string"},
            },
        },
    },
    {
        "name": "submit_ac_decision",
        "description": (
            f"{AC_CONTRACT}\n\n"
            "Submit the final Area-Chair decision after reading all rebuttal "
            "critic reviews + the orchestrator reduction. camera_ready_"
            "directives is REQUIRED to be non-empty even on accept — every "
            "rebuttal exchange produces at least one camera-ready insight "
            "worth folding in. If you cannot name even one, you have not "
            "read the rebuttal carefully enough."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "ac_decision"],
            "properties": {
                "thread_id": {"type": "string"},
                "ac_decision": {
                    "type": "object",
                    "description": "Full ACDecision object — see ac_decision.schema.json.",
                },
            },
        },
    },
    {
        "name": "submit_camera_ready_revision",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "The AC has decided (accept or revise) and emitted "
            "camera_ready_directives. The Professor responds: address every "
            "directive (accepted_fully / accepted_with_modification / "
            "escalated_back_to_ac), state the revised scope, AND state the "
            "single good mental_model_statement this research gives the "
            "reader. A research without a mental model is a list of "
            "results, not a paper — empty / vague mental_model_statement "
            "is rejected."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "camera_ready_revision"],
            "properties": {
                "thread_id": {"type": "string"},
                "camera_ready_revision": {
                    "type": "object",
                    "description": "Full CameraReadyRevision object — see camera_ready_revision.schema.json.",
                },
            },
        },
    },
    {
        "name": "prepare_paper_writing_context",
        "description": (
            "Return the full context the paper writer needs: state bundle, "
            "rebuttal reviews, AC decision, camera-ready revision, mental "
            "model statement, plus the data keys available for figures "
            "(metric keys, LOCO cells, baselines, ablation rows). Reading "
            "this, you will draft the paper_outline (title + mental_model + "
            "section_outline + figure_specs + table_specs) and submit it "
            "via submit_paper_outline."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string"}},
        },
    },
    {
        "name": "submit_paper_outline",
        "description": (
            "Submit the paper's title, mental_model_statement, and section "
            "outline. The mental_model_statement is REQUIRED and must be "
            "concrete — 'this paper gives the reader a mental model of "
            "<X>' such that a non-expert can grasp <X> in 30 seconds. "
            "After this, call register_paper_figure for each figure_spec "
            "and submit_paper_section for each section_id in order."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "outline"],
            "properties": {
                "thread_id": {"type": "string"},
                "outline": {"type": "object", "description": "PaperOutline object."},
            },
        },
    },
    {
        "name": "submit_paper_section",
        "description": (
            "Submit one section's prose (HTML fragment). Must reference at "
            "least one figure/table from the outline OR provide evidence "
            "anchors. Each section must include mental_model_link — one "
            "sentence connecting back to the paper's mental model — so no "
            "section drifts into a results dump."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "section"],
            "properties": {
                "thread_id": {"type": "string"},
                "section": {"type": "object", "description": "PaperSection object."},
            },
        },
    },
    {
        "name": "register_paper_figure",
        "description": (
            "Ask the server to render a figure using matplotlib. The "
            "available figure_type values are: loco_heatmap, baseline_bars, "
            "ablation_drops, lift_ci_forest, score_radar, "
            "claim_tree_status, metric_table. The server pulls data from "
            "the worker_report / rebuttal_synthesis via data_spec keys, "
            "draws the figure, and writes it to "
            "production/publication/figures/{figure_id}.png. After this "
            "you can embed it in any submit_paper_section via "
            "<img src='figures/{figure_id}.png'>."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "figure_request"],
            "properties": {
                "thread_id": {"type": "string"},
                "figure_request": {"type": "object", "description": "PaperFigureRequest object."},
            },
        },
    },
    {
        "name": "submit_professor_user_goal_attestation",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Dual-gate publication: AC accept is NECESSARY but NOT SUFFICIENT. "
            "The Professor must also attest that the ORIGINAL user intake "
            "problem (not the reshaped academic claim) is addressable with the "
            "evidence produced. If achieved=false, render_final_paper is "
            "blocked and the system enters honest_failure exit unless "
            "additional research is triggered. Schema enforces: at least 2 "
            "evidence anchors back to the intake, >=80-char concrete "
            "user-action statement, and required_additional_research listing "
            "when achieved=false."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "attestation"],
            "properties": {
                "thread_id": {"type": "string"},
                "attestation": {
                    "type": "object",
                    "description": "UserGoalAttestation object — see user_goal_attestation.schema.json.",
                },
            },
        },
    },
    {
        "name": "propose_alternative_root_directions",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "When the AC has emitted decision=reject_and_diversify (or when the "
            "operator wants fan-out before a single-shot revise), the Professor "
            "proposes N>=3 alternative root claim angles covering distinct axes "
            "(operational_root, taste_root, mechanism_root, "
            "inverted_validity_root, different_method_root, "
            "boundary_first_root, necessity_root). The system records the "
            "alternatives; select_alternative_root picks one to bootstrap into "
            "a fresh production attempt. This replaces linear "
            "revise_root_after_reject when the rejected direction is "
            "structurally hopeless rather than just under-evidenced."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "proposal"],
            "properties": {
                "thread_id": {"type": "string"},
                "proposal": {
                    "type": "object",
                    "description": "AlternativeRootProposal — see alternative_root_proposal.schema.json. Must include >=3 distinct angle values.",
                },
            },
        },
    },
    {
        "name": "select_alternative_root",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "After propose_alternative_root_directions, select ONE alternative "
            "by index to bootstrap into a fresh production attempt. The "
            "selected claim becomes the new root via the same archive-and-"
            "rebuild path as revise_root_after_reject, but the operator log "
            "preserves the full N-alternative slate so future audits can see "
            "the diversification step happened."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "selected_index", "selection_rationale"],
            "properties": {
                "thread_id": {"type": "string"},
                "selected_index": {"type": "integer", "minimum": 0},
                "selection_rationale": {"type": "string"},
            },
        },
    },
    {
        "name": "seed_alternative_root_formulation",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Multi-root tournament: pull a named formulation from "
            "grilling_session.extracted.alternative_claim_formulations and "
            "add it as a second/third root_node (parent=null) to the live "
            "search_state. The current root is NOT touched — both run in "
            "parallel and the strongest survives at publication time. "
            "Use when the current root has been pruned (Rail 5 fires "
            "must_revise_root) AND grilling produced an alternative the "
            "operator hasn't tried yet — avoids hand-rolling a new claim "
            "via revise_root_after_reject."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "formulation_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "formulation_id": {
                    "type": "string",
                    "description": "Matches grilling_session.extracted.alternative_claim_formulations[*].formulation_id.",
                },
                "seed_drafts": {
                    "type": "boolean",
                    "description": "If true (default), also seed typed sibling drafts under the new root via seed_drafts_from_root.",
                },
            },
        },
    },
    {
        "name": "seed_forest_from_connector",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "ADR 0012 connector->production handoff. Reads the connector_session's "
            "kept claim_contracts (the diverse far-framings of P that cleared "
            "prune-1 + reduction) and seeds a MULTI-ROOT forest search_state: each "
            "claim becomes a coexisting root (parent=null), each draft-seeded. "
            "Replaces design_initial_claim_contract for the multi-root path; the "
            "existing per-node gate then runs every tree, and select_strongest_survivor "
            "picks the single output at the end. Refuses if production is already "
            "seeded; 0 claims -> no_claims (render honest-failure)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string"}},
        },
    },
    {
        "name": "select_strongest_survivor",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "ADR 0012 forest terminal: of the connector forest's gate-survivor roots "
            "(those with a promoted terminal node), select the SINGLE strongest-earned "
            "one by verdict_strength (ADR 0008 ladder) then investigation_depth. Reads "
            "each survivor root's per-root attestation; returns 'incomplete' if any "
            "survivor still lacks its per-root terminal (run it first), 'honest_failure' "
            "if there are no survivors, else 'ok' with the winner to render. There is "
            "ONE output — other survivors are search by-products. (Single-root threads "
            "use the legacy terminal, not this tool.)"
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string"}},
        },
    },
    {
        "name": "snapshot_root_terminal",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "ADR 0012 forest (snapshot-and-reset): after a survivor root's full "
            "terminal has run flat under production/rebuttal/, snapshot those "
            "artifacts to production/rebuttal/<root_id>/ and reset the flat dir so "
            "the next root's terminal starts clean. Call once per survivor root "
            "right after its terminal completes; when all survivors are snapshotted, "
            "call select_strongest_survivor. Single-root threads never call this."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "root_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "root_id": {
                    "type": "string",
                    "description": "The forest root (parent=null node id) whose just-completed flat terminal to snapshot.",
                },
            },
        },
    },
    {
        "name": "enqueue_operator_prompt",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Hands-free escalation channel. When the auto-resolver refuses to "
            "chain (safety budget hit, identical-fingerprint loop, suggestion "
            "confidence below the auto-dispatch bar), the rail surfaces a "
            "prompt to the operator via this tool. The frontend renders "
            "pending prompts in the production phase view; the operator's "
            "free-text response lands back via get_pending_operator_response. "
            "Fire-and-forget — does not block; caller polls separately."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "kind", "prompt"],
            "properties": {
                "thread_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["auto_resolver_escalation", "decision_request", "context_request"],
                },
                "prompt": {"type": "string", "minLength": 1},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of suggested operator responses (rendered as quick-pick chips).",
                },
                "source_rail": {"type": "string"},
                "event_id": {
                    "type": "string",
                    "description": "Stable id for idempotent retry. Auto-generated when omitted.",
                },
            },
        },
    },
    {
        "name": "get_pending_operator_response",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Polled by the Claude Code subprocess to consume operator "
            "responses to prompts previously enqueued via "
            "enqueue_operator_prompt. Returns the oldest responded prompt "
            "(or the one matching event_id if supplied) and marks it as "
            "consumed in the queue log. Returns status=='empty' when "
            "nothing is ready — caller should wait and try again."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "event_id": {
                    "type": "string",
                    "description": "Consume this specific event_id; otherwise the oldest responded prompt is taken.",
                },
            },
        },
    },
    {
        "name": "render_honest_failure_paper",
        "description": (
            "Terminal exit when no path to a positive deployable result exists. "
            "Renders honest_failure.html under production/publication/ instead "
            "of paper.html, documenting: the user's original intake, all "
            "attempts made, the final attestation showing achieved=false, and "
            "the concrete experiments that would change the answer. Used when "
            "max_reject_cycles is exhausted or when the operator (or Claude "
            "Code) decides the direction is structurally hopeless and further "
            "fan-out would not help. This is a HONEST outcome, not a failure "
            "of the harness — better than publishing a misleading paper."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string"}},
        },
    },
    {
        "name": "render_final_paper",
        "description": (
            "Assemble all submitted sections + registered figures + tables + "
            "the mental_model_statement into the final Sakana-v2-style ICML "
            "two-column HTML paper at production/publication/paper.html, "
            "regenerate interactive_summary.html and slides_summary.html "
            "alongside it, and write production_run_summary.json so the "
            "frontend production panel populates. DUAL-GATE: requires both "
            "(a) AC decision in {accept, revise, revise_with_new_measurements} "
            "and (b) submit_professor_user_goal_attestation with achieved=true. "
            "If achieved=false or attestation missing, render is blocked and "
            "the operator is told to either trigger fan-out via "
            "propose_alternative_root_directions, run more measurements, or "
            "call render_honest_failure_paper for the final honest-failure exit."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string"}},
        },
    },
]


# --- Tool handlers -------------------------------------------------------- #


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _persona_cfg(settings: dict[str, Any]) -> dict[str, Any]:
    return (
        settings.get("runtime", {})
        .get("llm_orchestrator", {})
        .get("mcp", {})
        .get("persona_enforcement", {})
    )


def _thread_dir(thread_id: str) -> Path:
    return _repo_root() / "runs" / "threads" / thread_id


def _read_json(p: Path) -> dict[str, Any] | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


_TOOL_ENVELOPE_TAILS = re.compile(
    # Trailing fragments left over when Claude Code's tool-call XML envelope
    # bleeds into a string parameter (closing </commentary>, </invoke>,
    # </function_calls>, or a stray <parameter name="...">JSON</parameter>
    # block appended after the real text). We strip from the FIRST tail
    # match onward so the persisted text ends where the prose ends.
    r"""(?ix)
    (
        </\s*commentary\s*>
      | </\s*response_to_grad_student\s*>
      | </\s*[a-z][a-z0-9_:-]*\s*>\s*(?=\s*<\s*(?:/?\s*invoke|/?\s*function_calls|parameter\b))
      | <\s*parameter\s+name\s*=\s*"
      | <\s*/?\s*(?:invoke|function_calls|antml:[a-z_]+)\b
    )
    .*\Z
    """,
    re.DOTALL,
)


def _categorize_failure(final_verdict: str, transition: str) -> str:
    """Map a Professor's verdict + transition to one of the memory/failures
    category folders. Conservative: unknown verdicts fall to negative_result."""
    v = final_verdict.lower()
    if "contradicted" in v or "disproof" in v:
        return "negative_result"
    if "confounded" in v or "leakage" in v or "baseline" in v:
        return "confounded_result"
    if "blocked_by_operational" in v or "operational" in v:
        return "invalid_experiment"
    if "taste_rejected" in v:
        return "taste_rejection"
    if transition == "pruned" and "implementation" in v:
        return "implementation_failure"
    return "negative_result"


def _write_failure_record(
    *,
    tid: str,
    node_id: str,
    final_verdict: str,
    transition: str,
    response_to_grad_student: str,
    node: dict[str, Any],
    worker_report: dict[str, Any],
) -> None:
    """Write a one-page failure record under memory/failures/<category>/.

    Idempotent: re-running on the same node overwrites the file. The
    failures/index.yaml is updated so retrieve_failure_summaries can
    surface this record on the next thread that shares query_tags.
    """
    import hashlib
    import yaml

    repo = _repo_root()
    category = _categorize_failure(final_verdict, transition)
    failures_root = repo / "memory" / "failures"
    cat_dir = failures_root / category
    cat_dir.mkdir(parents=True, exist_ok=True)

    suffix = hashlib.md5(
        f"{tid}|{node_id}|{transition}|{final_verdict}".encode("utf-8")
    ).hexdigest()[:10]
    filename = f"{node_id}__{category}__{suffix}.md"
    fpath = cat_dir / filename

    cleaned_response = _strip_tool_envelope_leak(response_to_grad_student or "").strip()
    claim = (node.get("claim_contract", {}) or {}).get("claim_under_test", "")
    tags = sorted(set((node.get("failure_retrieval", {}) or {}).get("query_tags", [])))
    metrics_summary = ", ".join(
        f"{k}={round(v, 4) if isinstance(v, (int, float)) else v}"
        for k, v in (worker_report.get("metrics") or {}).items()
        if not str(k).startswith("_")
    )
    body = (
        "---\n"
        f"node_id: {node_id}\n"
        f"thread_id: {tid}\n"
        f"category: {category}\n"
        f"transition: {transition}\n"
        f"final_verdict: {final_verdict}\n"
        f"tags: {tags}\n"
        "source: mcp_server\n"
        "---\n\n"
        f"# Failure: {node_id}\n\n"
        f"**Claim under test:**\n> {claim}\n\n"
        f"**Professor verdict ({transition}):** {final_verdict}\n\n"
        f"**Headline metrics:** {metrics_summary or '(none recorded)'}\n\n"
        f"**Lesson (from Professor's response):**\n\n{cleaned_response or '(no natural-language reply recorded)'}\n"
    )
    fpath.write_text(body, encoding="utf-8")

    # Update index.yaml — add this file under the appropriate category if
    # not already listed. The index drives retrieve_failure_summaries.
    index_path = failures_root / "index.yaml"
    index = yaml.safe_load(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"categories": {}}
    categories = index.setdefault("categories", {})
    cat_block = categories.setdefault(category, {"description": "", "files": []})
    files = cat_block.setdefault("files", [])
    rel_path = f"{category}/{filename}"
    if rel_path not in files:
        files.append(rel_path)
    # parse_simple_yaml in config.py expects: list dashes indented +2 past
    # the key, descriptions quoted, no line wrapping. pyyaml.safe_dump
    # outputs a different (still-valid) style that the custom parser
    # cannot read back. Emit manually with the expected shape.
    index_path.write_text(_format_failure_index(index), encoding="utf-8")


def _format_failure_index(index: dict[str, Any]) -> str:
    """Emit memory/failures/index.yaml in the shape parse_simple_yaml
    accepts: 2-space indent throughout, list dashes at +2 past their key,
    description strings double-quoted on a single line."""
    out: list[str] = ["categories:"]
    for cat_name, cat_block in (index.get("categories") or {}).items():
        out.append(f"  {cat_name}:")
        desc = (cat_block or {}).get("description") or ""
        # quote-safe: escape backslash + double-quote then wrap.
        safe_desc = str(desc).replace("\\", "\\\\").replace("\"", "\\\"")
        out.append(f"    description: \"{safe_desc}\"")
        files_list = (cat_block or {}).get("files") or []
        if not files_list:
            out.append("    files: []")
        else:
            out.append("    files:")
            for f in files_list:
                safe_f = str(f).replace("\\", "\\\\").replace("\"", "\\\"")
                out.append(f"      - \"{safe_f}\"")
    return "\n".join(out) + "\n"


def _strip_tool_envelope_leak(text: Any) -> str:
    """Remove trailing Claude Code tool-call XML envelope leakage from a
    string before persisting it to dialog.json. No-op for non-strings or
    clean strings. Idempotent.
    """
    if not isinstance(text, str):
        return text  # type: ignore[return-value]
    stripped = _TOOL_ENVELOPE_TAILS.sub("", text).rstrip()
    return stripped


def handle_get_research_state(args: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    tid = args["thread_id"]
    d = _thread_dir(tid)
    thread = _read_json(d / "thread.json") or {}
    mcp_cfg = (
        settings.get("runtime", {})
        .get("llm_orchestrator", {})
        .get("mcp", {})
    )
    model_preference = (
        thread.get("mcp_model")
        or mcp_cfg.get("default_model")
        or "claude-sonnet-4-6"
    )
    # Surface the full market output, not just market_research_brief.json:
    #   - baseline_dossier_candidate.yaml  ← the precise baseline spec the
    #     Professor must use to build claim_contract.mandatory_baselines
    #   - baseline_analysis.md            ← prose analysis of the candidates
    #   - reference_papers/               ← list of downloaded papers so the
    #     Professor can cite specific prior work in success_criteria
    market_dir = d / "market"
    baseline_dossier_yaml = None
    bd_path = market_dir / "baseline_dossier_candidate.yaml"
    if bd_path.exists():
        try:
            baseline_dossier_yaml = bd_path.read_text(encoding="utf-8")
        except OSError:
            baseline_dossier_yaml = None
    baseline_analysis_md = None
    ba_path = market_dir / "baseline_analysis.md"
    if ba_path.exists():
        try:
            baseline_analysis_md = ba_path.read_text(encoding="utf-8")
        except OSError:
            baseline_analysis_md = None
    reference_papers: list[dict[str, Any]] = []
    rp_dir = market_dir / "reference_papers"
    if rp_dir.is_dir():
        for p in sorted(rp_dir.iterdir()):
            if p.is_file() and p.suffix.lower() == ".pdf":
                reference_papers.append(
                    {"filename": p.name, "size_bytes": p.stat().st_size}
                )
    return {
        "thread": thread,
        "operator_model_preference": model_preference,
        "_model_note": (
            "The operator selected this model in the frontend for this thread. "
            "If you are running with a different model, switch to it before "
            "continuing — per-thread model is part of the operator's contract."
        ),
        "grilling": _read_json(d / "grilling" / "grilling_session.json"),
        "market_brief": _read_json(market_dir / "market_research_brief.json"),
        "baseline_dossier_candidate_yaml": baseline_dossier_yaml,
        "baseline_analysis_md": baseline_analysis_md,
        "reference_papers": reference_papers,
        "_market_usage_contract": (
            "When designing claim_contract.mandatory_baselines, you MUST "
            "ground each of the three roles (current_best_known / naive / "
            "random_or_null) in the baseline_dossier_candidate above — copy "
            "the concrete algorithm names + their published metric thresholds "
            "verbatim. Citing a paper by name from reference_papers is "
            "required for the current_best_known role. Ignoring market "
            "output is a persona violation; the validator will reject your "
            "design and you'll be asked to retry."
        ),
        "search_state": _read_json(d / "production" / "tree" / "search_state.json"),
        "tree_summary": _read_json(d / "production" / "tree" / "tree_search_summary.json"),
        "intake_to_claim": _read_json(d / "production" / "intake_to_claim_dialog.json"),
    }


# Claim-type weights — lower = run earlier. Same policy that
# ParallelAgent.prioritize_claims used; relocated to the MCP boundary so
# Claude Code follows the same order it would have followed under the
# automated loop.
_TYPE_WEIGHTS = {
    "validity": 0,
    "capability": 1,
    "necessity": 2,
    "mechanism": 3,
    "boundary": 4,
    "constraint": 5,
    "operational": 6,
    "taste": 7,
}

# 4-stage claim-typed search (same as AgentManager.DEFAULT_STAGES_SPEC).
_STAGES: list[dict[str, Any]] = [
    {"name": "scope_pinning", "admits": {"validity", "taste", "operational"}},
    {"name": "baseline_evidence", "admits": {"capability"}},
    {"name": "mechanism_or_necessity", "admits": {"mechanism", "necessity"}},
    {"name": "boundary_ablation", "admits": {"boundary", "constraint"}},
]


def _active_stage(search_state: dict[str, Any]) -> dict[str, Any]:
    """Pick the leftmost stage that still has admissible queued work, OR
    that has no promoted node of its admitted types yet."""
    nodes_by_id = {n["id"]: n for n in search_state.get("nodes", [])}
    promoted_types: set[str] = set()
    for nid in search_state.get("promoted_node_ids", []):
        node = nodes_by_id.get(nid)
        if node:
            promoted_types.add(node.get("type", ""))
    queued = [
        item for item in search_state.get("frontier", [])
        if item.get("status") == "queued"
    ]
    queued_types = {
        nodes_by_id.get(item["node_id"], {}).get("type", "")
        for item in queued
    }
    for stage in _STAGES:
        admits = stage["admits"]
        has_pending = bool(admits & queued_types)
        has_promoted = bool(admits & promoted_types)
        if has_pending and not has_promoted:
            return stage
        if has_pending and has_promoted:
            # Stage is in-progress; keep advancing children here unless an
            # earlier stage has new work (handled by the leftmost-first loop).
            return stage
    # All stages have either no queued work or are fully covered.
    return _STAGES[-1]


_RESUME_NEXT_TOOL = {
    "running": "execute_node_experiment",
    "completed_worker_report": "run_critic_reviews",
    "critic_reviewed": "submit_professor_decision",
    "orchestrator_reduced": "submit_professor_decision",
}


def handle_get_next_admissible_node(args: dict[str, Any]) -> dict[str, Any]:
    """Selector with resume support.

    Priority order:
      1. Any in-progress node (running / completed_worker_report /
         critic_reviewed / orchestrator_reduced) — these are partially-
         processed nodes that a previous Claude Code session left stranded.
         The response names the next MCP tool to call so the caller can
         pick up exactly where the prior session stopped.
      2. New queued admissible nodes per the 4-stage claim-typed policy.
    """
    tid = args["thread_id"]
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path)
    if not state or not state.get("nodes"):
        return {
            "status": "no_state",
            "reason": (
                "search_state.json doesn't exist yet. Call "
                "design_initial_claim_contract first — it bootstraps the "
                "root node + search_state."
            ),
        }
    nodes_by_id = {n["id"]: n for n in state["nodes"]}

    # --- Resume path: pick up where the previous session stopped. -----
    incomplete = []
    for n in state["nodes"]:
        if n.get("status") in _RESUME_NEXT_TOOL:
            incomplete.append(n)
    if incomplete:
        # Deepest-first so leaf-y mid-state nodes don't starve.
        incomplete.sort(
            key=lambda n: (
                # type weight tiebreaker (validity first)
                _TYPE_WEIGHTS.get(n.get("type", ""), 99),
                n.get("id", ""),
            )
        )
        node = incomplete[0]
        nid = node["id"]
        next_tool = _RESUME_NEXT_TOOL[node["status"]]
        return {
            "status": "resume",
            "node_id": nid,
            "node_type": node.get("type"),
            "current_status": node["status"],
            "next_tool_to_call": next_tool,
            "claim_under_test": node.get("claim_contract", {}).get(
                "claim_under_test", ""
            ),
            "_selection_policy": (
                f"RESUME: node {nid} was left in state '{node['status']}' "
                f"by an earlier session. Call {next_tool} next to continue."
            ),
        }

    # --- Fresh path: pick by stage + type weight. ---------------------
    stage = _active_stage(state)
    admits: set[str] = stage["admits"]
    candidates: list[tuple[float, int, str, dict[str, Any]]] = []
    for item in state.get("frontier", []):
        if item.get("status") != "queued":
            continue
        node = nodes_by_id.get(item["node_id"])
        if not node:
            continue
        if node.get("type") not in admits:
            continue
        weight = _TYPE_WEIGHTS.get(node.get("type", ""), 99)
        candidates.append(
            (weight, int(item.get("depth", 0)), item["node_id"], node)
        )
    if not candidates:
        # Rail 5: surface MUST_REVISE_ROOT when the deepest promoted node's
        # successor branches have collapsed (>=3 negative, 0 promoted children).
        # Without this, operators silently fall through to decide_publication_readiness
        # while the underlying root claim is dead in the water — exactly the
        # pattern that let thread_e5b277f9 ship with 3/3 negative successors.
        revise_signal = _detect_must_revise_root_signal(state)
        if revise_signal:
            # Surface any unseeded alternative_claim_formulations from
            # grilling — operator can call seed_alternative_root_formulation
            # to drop one in without doing fresh research.
            grilling = _read_json(_thread_dir(tid) / "grilling" / "grilling_session.json") or {}
            formulations = (grilling.get("extracted") or {}).get("alternative_claim_formulations") or []
            existing_root_ids = {n["id"] for n in state.get("nodes", []) if n.get("parent") is None}
            unseeded = [
                {
                    "formulation_id": f.get("formulation_id"),
                    "scope_kind": f.get("scope_kind"),
                    "scope_note": f.get("scope_note"),
                    "ranked_priority": f.get("ranked_priority"),
                }
                for f in formulations
                if isinstance(f, dict)
                and not any(rid.endswith(f"_root_{f.get('formulation_id')}") for rid in existing_root_ids)
            ]
            next_choices = ["revise_root_after_reject", "propose_alternative_root_directions"]
            if unseeded:
                next_choices.insert(0, "seed_alternative_root_formulation")
            # Hands-free: when an unseeded formulation exists, attach an
            # auto_action_suggestion so the auto-resolver can dispatch
            # seed_alternative_root_formulation without operator decision.
            # Pick the lowest ranked_priority (1 = try first).
            auto_action: dict[str, Any] | None = None
            if unseeded:
                ranked = sorted(
                    unseeded,
                    key=lambda f: (f.get("ranked_priority") or 99, f.get("formulation_id") or ""),
                )
                top = ranked[0]
                auto_action = {
                    "tool": "seed_alternative_root_formulation",
                    "args": {"thread_id": tid, "formulation_id": top["formulation_id"]},
                    "source_rail": "rail_5_must_revise_root",
                    "rationale": (
                        f"Promoted node {revise_signal['promoted_node_id']} collapsed "
                        f"({len(revise_signal['negative_children'])} pruned, 0 promoted children); "
                        f"unseeded formulation {top['formulation_id']!r} "
                        f"({top.get('scope_kind')}) is the next-priority alternative."
                    ),
                    "confidence": "high",
                }
            response = {
                "status": "must_revise_root",
                "active_stage": stage["name"],
                "promoted_node_id": revise_signal["promoted_node_id"],
                "negative_children": revise_signal["negative_children"],
                "alternative_root_candidates": revise_signal["alternative_root_candidates"],
                "unseeded_alternative_formulations": unseeded,
                "reason": (
                    f"Promoted node {revise_signal['promoted_node_id']} has "
                    f"{len(revise_signal['negative_children'])} negative direct successor(s) "
                    f"and 0 promoted children. The current root claim cannot be saved by more children — "
                    + (
                        f"call seed_alternative_root_formulation with one of {[f['formulation_id'] for f in unseeded]} "
                        f"to spin up a pre-vetted alternative root in parallel, or "
                        if unseeded else ""
                    )
                    + "call revise_root_after_reject (single-shot) / propose_alternative_root_directions (fan-out)."
                ),
                "next_tool_choices": next_choices,
            }
            if auto_action is not None:
                response["auto_action_suggestion"] = auto_action
                response = _maybe_auto_dispatch(tid, response)
            return response
        return {
            "status": "no_admissible_node",
            "active_stage": stage["name"],
            "reason": (
                f"Stage {stage['name']} has no queued nodes of admitted types "
                f"{sorted(admits)}. Either the stage is done or all "
                f"admissible nodes have been processed — call "
                f"decide_publication_readiness."
            ),
        }
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    _, depth, nid, node = candidates[0]
    return {
        "status": "ok",
        "active_stage": stage["name"],
        "node_id": nid,
        "node_type": node.get("type"),
        "depth": depth,
        "parent": node.get("parent"),
        "claim_under_test": node.get("claim_contract", {}).get(
            "claim_under_test", ""
        ),
        "mandatory_baselines": node.get("claim_contract", {}).get(
            "mandatory_baselines", []
        ),
        "success_criteria": node.get("claim_contract", {}).get(
            "success_criteria", []
        ),
        "disproof_conditions": node.get("claim_contract", {}).get(
            "disproof_conditions", []
        ),
        "_selection_policy": (
            f"Picked {nid} via deterministic policy: stage={stage['name']}, "
            f"type weight (validity<capability<necessity<…), depth asc, id "
            f"asc. {len(candidates) - 1} other admissible candidate(s) "
            f"queued behind it."
        ),
    }


def handle_resume_production_state(args: dict[str, Any]) -> dict[str, Any]:
    """Operator-invoked recovery: if a node has been stuck in `running` for
    a long time (Claude Code session died before execute_node_experiment
    could complete), demote it to `ready` so the selector re-queues it.
    Other mid-states (completed_worker_report / critic_reviewed /
    orchestrator_reduced) are kept as-is — they're not stuck, they're
    waiting for the next tool which the selector now points at."""
    from research_harness.orchestrator.search_state import (
        validate_search_state,
    )

    tid = args["thread_id"]
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path)
    if not state:
        return {"status": "no_state"}
    validate_search_state(state)
    demoted: list[str] = []
    for node in state["nodes"]:
        if node.get("status") != "running":
            continue
        node_dir = (
            _thread_dir(tid) / "production" / "tree" / "nodes" / node["id"]
        )
        # If no worker_report on disk, the previous session never got past
        # LocalRunner — safe to demote.
        if not (node_dir / "worker_report.json").exists():
            node["status"] = "ready"
            for item in state["frontier"]:
                if item["node_id"] == node["id"]:
                    item["status"] = "queued"
                    break
            state["transitions"].append({
                "node_id": node["id"],
                "from_status": "running",
                "to_status": "ready",
                "event": "resume_demote",
                "reason": "previous session died before worker_report; re-queued",
                "created_child_ids": [],
            })
            demoted.append(node["id"])
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"status": "ok", "demoted_node_ids": demoted}


def handle_submit_feasibility_envelope(args: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """PR7: persist the operator+Professor feasibility envelope. Subsequent
    design_initial_claim_contract and revise_root_after_reject calls read
    this file and validate claim scope against it."""
    from research_harness.schemas.validator import validate_named_schema
    tid = args["thread_id"]
    env = args["envelope"]
    try:
        validate_named_schema("feasibility_envelope", env)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    # Cross-check: 'deployment' target requires at least one real_adapter
    # data source. (We catch this at envelope-submission time too, not just
    # at claim-design time, so the operator sees the constraint early.)
    target = (env.get("operator_intent") or {}).get("target_deploy_grade_scope")
    real_sources = [s for s in env.get("data_sources_available", []) if s.get("kind") == "real_adapter"]
    registered = {
        a.get("id")
        for a in (settings.get("data_adapters", {}) or {}).get("registered", []) or []
    }
    if target == "deployment":
        if not real_sources:
            return {
                "status": "rejected",
                "reason": (
                    "operator_intent.target_deploy_grade_scope='deployment' "
                    "but data_sources_available has no kind='real_adapter'. "
                    "Either register a real-data adapter under "
                    "settings.json.data_adapters.registered and reference its "
                    "id here, or downgrade target to 'feasibility' / 'directional'."
                ),
            }
        unregistered = [s["id"] for s in real_sources if s["id"] not in registered]
        if unregistered:
            return {
                "status": "rejected",
                "reason": (
                    f"real_adapter ids not in settings.json.data_adapters.registered: "
                    f"{unregistered}. Add them or remove from the envelope."
                ),
            }

    # ADR 0006: stamp the harness-owned max_attestable_status from the
    # registered external_falsifier. This is NOT operator-writable — any
    # operator-supplied value is overwritten by the derivation.
    env["max_attestable_status"] = _derive_max_attestable_status(env)

    env_path = _thread_dir(tid) / "production" / "feasibility_envelope.json"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        json.dumps(env, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "envelope_path": str(env_path),
        "registered_real_adapters": sorted(registered),
        "target_scope": target,
        "max_attestable_status": env["max_attestable_status"],
        "next_step": (
            "Now call design_initial_claim_contract. The claim's "
            "deploy_grade_scope MUST fit this envelope (deployment requires "
            "real_adapter; feasibility allows synthetic with bridging; "
            "directional is methodology-hint only). "
            + (
                "max_attestable_status=goal_achieved: a passing falsifier_result "
                "(compute_falsifier_result) is required for achieved=true."
                if env["max_attestable_status"] == "goal_achieved"
                else "max_attestable_status=unverified_screen: achieved=true is "
                "refused until an external_falsifier (real_holdout / "
                "cross_generator_transfer) is registered in this envelope."
            )
        ),
    }


def handle_compute_falsifier_result(args: dict[str, Any]) -> dict[str, Any]:
    """ADR 0006: run the envelope's external_falsifier predicate over held-out
    evidence and persist the harness-produced FalsifierResult. This is the
    ONLY producer of a passing falsifier_result — the worker cannot stamp
    passed=true; it falls out of the deterministic predicate the harness
    owns. A passing result against the registered holdout is the precondition
    the attestation gate checks for achieved=true."""
    from research_harness.falsifier import FalsifierError, compute_falsifier_result
    from research_harness.schemas.validator import validate_named_schema
    from research_harness.config import load_settings as _ls

    tid = args["thread_id"]
    evidence = args.get("evidence") or {}

    envelope = _read_json(_thread_dir(tid) / "production" / "feasibility_envelope.json")
    if not envelope:
        return {
            "status": "rejected",
            "reason": "feasibility_envelope.json missing — submit it (with an external_falsifier) before computing a falsifier result.",
        }
    falsifier = envelope.get("external_falsifier") or {}
    if falsifier.get("kind") in (None, "none"):
        return {
            "status": "rejected",
            "reason": (
                "envelope.external_falsifier.kind is 'none' — register a "
                "real_holdout or cross_generator_transfer falsifier via "
                "submit_feasibility_envelope first. Without one the thread is "
                "capped at unverified_screen and achieved=true is refused."
            ),
        }

    guard_thresholds = _falsifier_guard_thresholds(_ls(_repo_root()))

    # ADR 0006 rev.2: for the air-gapped weak falsifier, the harness MEASURES
    # that generator B is structurally distinct from A by running both on a
    # harness-fixed probe — never trusting a worker-supplied "distinct" label.
    # A non-distinct B yields a below-threshold distance → uninformative,
    # closing the thread_c8919361 hole (a passing rho on a non-distinct B).
    measured_distance: float | None = None
    measured_known: float | None = None
    probe: dict[str, Any] = {}
    if falsifier.get("kind") == "cross_generator_transfer":
        from research_harness import falsifier_probe
        probe = falsifier_probe.measure_behavioral_distance(
            evidence.get("generator_a"),
            evidence.get("generator_b"),
            repo_root=_repo_root(),
            thread_id=tid,
        )
        measured_distance = probe.get("distance")
        measured_known = probe.get("known_baseline_transfer")

    try:
        result = compute_falsifier_result(
            thread_id=tid,
            falsifier=falsifier,
            evidence=evidence,
            measured_behavioral_distance=measured_distance,
            measured_known_baseline_transfer=measured_known,
            guard_thresholds=guard_thresholds,
        )
        validate_named_schema("falsifier_result", result)
    except FalsifierError as exc:
        return {"status": "rejected", "reason": f"falsifier predicate not evaluable: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    # ADR 0009 (B3): record which axis the patchwork-insufficiency probe (the
    # null floor) gates, read from the IMMUTABLE frozen question — never a
    # per-node judgment, so a node cannot relabel itself to switch the probe off
    # its own claim. method_is_subject (e.g. this screen) routes the probe to the
    # generator/screen DESIGN axis; method_is_solution gates the claim falsifier.
    from research_harness.falsifier import patchwork_probe_applies_to_claim
    role = _frozen_subject_role(tid)
    result.setdefault("guards", {})["patchwork_probe_axis"] = {
        "subject_role": role or "method_is_solution",
        "subject_role_source": "frozen_question" if role else "default",
        "applies_to": "claim_falsifier" if patchwork_probe_applies_to_claim(role) else "screen_design_axis",
    }

    out_path = _rebuttal_dir(tid) / "falsifier_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verdict = result.get("verdict", "passed" if result["passed"] else "failed")
    if verdict == "passed":
        next_step = (
            "Falsifier PASSED (predicate met AND guards ok). The dual gate's "
            "falsifier precondition is satisfied — submit_professor_user_goal_"
            "attestation may set achieved=true only if a real referent unlocks "
            "transfer_valid (cross_generator_transfer remains a screen, not a "
            "strength-certifier)."
        )
    elif verdict == "degenerate":
        next_step = (
            "Falsifier DEGENERATE — the supplied rankings do not discriminate "
            "the pipelines (near-constant / heavily tied), so rho carries no "
            "transferable signal. rho MUST NOT be reported as transfer evidence. "
            "Supply rankings that actually separate the pipelines."
        )
    elif verdict == "uninformative":
        reason = probe.get("reason") or (
            "generator B is not measurably distinct from A on the harness probe "
            "(behavioral distance below threshold), or the predicate sits at the "
            "permutation-null noise band"
        )
        next_step = (
            f"Falsifier UNINFORMATIVE — {reason}. rho MUST NOT be reported as "
            "transfer evidence. Supply harness-loadable generator_a / generator_b "
            "synthetic recipes for a structurally DISTINCT held-out B (same "
            "family, different structure), or raise the predicate above the null "
            "floor."
        )
    else:  # failed
        next_step = (
            f"Falsifier FAILED (observed {result['observed']:.4g} vs "
            f"{result['predicate']['op']} {result['predicate']['threshold']}). "
            "achieved=true remains refused. Either improve the pipeline so the "
            "held-out ranking is preserved, or attest honestly with "
            "attested_status=unverified_screen / not_achieved."
        )
    return {
        "status": "ok",
        "passed": result["passed"],
        "verdict": verdict,
        "observed": result["observed"],
        "guards": result.get("guards", {}),
        "result_path": str(out_path),
        "next_step": next_step,
    }


def handle_pin_frozen_question(args: dict[str, Any]) -> dict[str, Any]:
    """ADR 0008 Axis 1: pin the formal question the construct-adversary will be
    funded against. Authorship separation — the question is frozen from a
    PRE-construction artifact (grilling) with a provenance hash and is
    IMMUTABLE, so the construction loop cannot restate or modify the referent
    it is judged against."""
    import hashlib
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    question = args["question"]

    grilling_path = _thread_dir(tid) / "grilling" / "grilling_session.json"
    if question.get("source_artifact") == "grilling_session" and not grilling_path.exists():
        return {
            "status": "rejected",
            "reason": "source_artifact=grilling_session but no grilling_session.json exists to freeze from.",
        }
    # Stamp provenance from the pre-construction source so a later construction
    # cannot silently re-author the question.
    if question.get("source_artifact") == "grilling_session":
        digest = hashlib.sha256(grilling_path.read_bytes()).hexdigest()[:16]
        question["source_provenance"] = f"grilling_sha256:{digest}"

    try:
        validate_named_schema("frozen_question", question)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    out_path = _thread_dir(tid) / "production" / "frozen_question.json"
    if out_path.exists():
        existing = _read_json(out_path) or {}
        if existing.get("formal_statement") != question.get("formal_statement"):
            return {
                "status": "rejected",
                "reason": (
                    "a frozen_question is already pinned and is IMMUTABLE "
                    f"(question_id={existing.get('question_id')!r}). The construction "
                    "may not re-author the question it is judged against. Remove the "
                    "file manually only if the operator is re-pinning before construction."
                ),
            }
        return {"status": "ok", "question_id": existing.get("question_id"), "note": "already pinned (idempotent)"}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(question, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "question_id": question["question_id"],
        "frozen_question_path": str(out_path),
        "next_step": (
            "Question frozen. To earn construct_valid (Axis 1), the construction "
            "must declare its pass_but_wrong region and a construct-adversary must "
            "search it and FAIL to break it (submit_construct_adversary_report). "
            "Reality-closeness (transfer_valid) stays unreachable without a real referent."
        ),
    }


def handle_submit_construct_adversary_report(args: dict[str, Any]) -> dict[str, Any]:
    """ADR 0008 Axis 1: record the funded construct-adversary's enumerated
    search. The harness RE-DERIVES the verdict (construct_adversary.py) from the
    search — a self-reported 'survived' that enumerated a breaking world is
    overruled. construct_valid is earned only by a funded FAILURE."""
    from research_harness.config import load_settings as _ls
    from research_harness.construct_adversary import evaluate_construct_adversary_report
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    report = args["report"]

    fq = _frozen_question(tid)
    if not fq:
        return {
            "status": "rejected",
            "reason": "no frozen_question pinned — call pin_frozen_question first. The adversary must attack the FROZEN (proposer-un-authored) question.",
        }
    try:
        validate_named_schema("construct_adversary_report", report)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}
    if report.get("question_id") != fq.get("question_id"):
        return {
            "status": "rejected",
            "reason": (
                f"report.question_id ({report.get('question_id')!r}) does not match the "
                f"pinned frozen_question.question_id ({fq.get('question_id')!r}). The "
                "adversary must attack the frozen question, not a restated one."
            ),
        }

    minb, minw = _construct_adversary_thresholds(_ls(_repo_root()))
    verdict = evaluate_construct_adversary_report(report, min_budget=minb, min_distinct_worlds=minw)
    stamped = dict(report)
    stamped["harness_verdict"] = verdict["verdict"]
    stamped["harness_reasons"] = verdict["reasons"]

    out_path = _rebuttal_dir(tid) / "construct_adversary_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(stamped, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if verdict["verdict"] == "survived":
        nxt = (
            "construct_valid EARNED (funded adversary failed to break the frozen "
            "question). This is Axis 1 — necessary, NOT sufficient. It says nothing "
            "about reality: transfer_valid stays unreachable without a real referent."
        )
    elif verdict["verdict"] == "broken":
        nxt = (
            "construct-INVALID: the adversary found a pass-but-wrong instance. The "
            "construction passes its measurement where the frozen answer is NO — fix "
            "the construction (close the pass_but_wrong world), do not narrow the claim."
        )
    else:
        nxt = (
            "INVALID (not a funded failure): " + "; ".join(verdict["reasons"])
            + ". A certification that did not actually search proves nothing — fund the "
            "adversary and enumerate distinct pass_but_wrong worlds."
        )
    return {
        "status": "ok",
        "harness_verdict": verdict["verdict"],
        "distinct_worlds": verdict["distinct_worlds"],
        "result_path": str(out_path),
        "next_step": nxt,
    }


def handle_design_initial_claim_contract(
    args: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    tid = args["thread_id"]
    grilling = _read_json(
        _thread_dir(tid) / "grilling" / "grilling_session.json"
    )
    problem_statement = (
        (grilling or {}).get("extracted", {}).get("claim_under_test") or ""
    )
    # Load the market context so the validator can check that the Professor
    # actually grounded the baselines in the downloaded reference papers /
    # dossier candidate.
    market_dir = _thread_dir(tid) / "market"
    market_context = {
        "baseline_dossier_yaml": (
            (market_dir / "baseline_dossier_candidate.yaml").read_text(encoding="utf-8")
            if (market_dir / "baseline_dossier_candidate.yaml").exists()
            else ""
        ),
        "baseline_analysis_md": (
            (market_dir / "baseline_analysis.md").read_text(encoding="utf-8")
            if (market_dir / "baseline_analysis.md").exists()
            else ""
        ),
        "reference_papers": [
            {"filename": p.name}
            for p in sorted((market_dir / "reference_papers").glob("*.pdf"))
        ] if (market_dir / "reference_papers").is_dir() else [],
    }
    new_claim = {
        "claim_under_test": args["claim_under_test"],
        "mandatory_baselines": args["mandatory_baselines"],
        "success_criteria": args["success_criteria"],
        "disproof_conditions": args["disproof_conditions"],
    }
    persona_cfg = _persona_cfg(settings)
    result = validate_claim_contract(
        new_claim=new_claim,
        problem_statement=problem_statement,
        market_context=market_context,
        config=persona_cfg,
    )
    if not result.ok:
        return {
            "status": "rejected",
            "reason": result.reject_message(),
        }
    # Anti-laziness check: capability-metric claims need a decision rule.
    rule_check = validate_decision_rule_for_capability_claim(
        claim_contract=new_claim,
        config=persona_cfg,
    )
    if not rule_check.ok:
        return {"status": "rejected", "reason": rule_check.reject_message()}
    # PR7: enforce feasibility envelope. Claim's deploy_grade_scope must
    # fit the envelope declared upfront via submit_feasibility_envelope.
    envelope = _read_json(
        _thread_dir(tid) / "production" / "feasibility_envelope.json"
    )
    registered = {
        a.get("id")
        for a in (settings.get("data_adapters", {}) or {}).get("registered", []) or []
    }
    env_check = validate_claim_fits_envelope(
        claim_contract={**new_claim,
                         "deploy_grade_scope": args.get("deploy_grade_scope"),
                         "data_source_anchor": args.get("data_source_anchor")},
        envelope=envelope,
        registered_adapter_ids=registered,
        config=persona_cfg,
    )
    if not env_check.ok:
        return {"status": "rejected", "reason": env_check.reject_message()}
    # Promote the scope/anchor onto the persisted contract.
    new_claim["deploy_grade_scope"] = args.get("deploy_grade_scope")
    if args.get("data_source_anchor"):
        new_claim["data_source_anchor"] = args["data_source_anchor"]
    # Persist intake-to-claim hand-off.
    handoff_path = _thread_dir(tid) / "production" / "intake_to_claim_dialog.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        json.dumps(
            {
                "original_contract": {"claim_under_test": problem_statement},
                "new_contract": new_claim,
                "dialog": [
                    {
                        "speaker": "professor",
                        "intent": "problem_to_claim",
                        "text": args.get("rationale", ""),
                        "metadata": {"source": "mcp_server"},
                    }
                ],
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    # Bootstrap search_state so subsequent MCP tools have something to read.
    # Without this Claude Code calls get_next_admissible_node and gets
    # `no_state`, leaving the production panel stuck on "not yet run".
    from research_harness.orchestrator.root_node_from_grilling import (
        attach_market_research_dossier,
        build_root_node_from_grilling,
        has_placeholder_baseline,
    )
    from research_harness.orchestrator.search_state import (
        initialize_search_state,
        search_policy_from_config,
    )
    from research_harness.orchestrator.treesearch.drafts import seed_drafts_from_root

    repo = _repo_root()
    market_brief = _read_json(
        _thread_dir(tid) / "market" / "market_research_brief.json"
    ) or {}
    baseline_dossier_id = (
        market_brief.get("baseline_dossier_id") or "bd_pending_market_research"
    )
    candidate_ids: list[str] = []
    raw_candidates = market_brief.get("baseline_dossier_candidates_index")
    if isinstance(raw_candidates, list):
        candidate_ids = [
            c["id"] for c in raw_candidates
            if isinstance(c, dict) and c.get("id")
        ]
    root_node = build_root_node_from_grilling(
        grilling or {},
        baseline_dossier_id=baseline_dossier_id,
        candidate_ids=candidate_ids,
    )
    # Overlay the Professor's strong+honest contract on top of grilling's
    # placeholder contract.
    root_node["claim_contract"] = {**root_node["claim_contract"], **new_claim}
    if has_placeholder_baseline(root_node):
        root_node = attach_market_research_dossier(
            root_node,
            baseline_dossier_id=baseline_dossier_id,
            candidate_ids=candidate_ids,
            baseline_analysis_md_path=market_brief.get("baseline_analysis_md_path"),
        )
    # --- Rail 2: baseline dossier substance preflight. -------------------
    # Block production entry when the dossier is a deterministic dump
    # (no operator-reviewed substance). thread_e5b277f9 entered production
    # with selected.one_paragraph_reason="Top-ranked search result" and
    # naive/null candidates = "TBD"; that should never have been allowed.
    bd_path = _thread_dir(tid) / "market" / "baseline_dossier_candidate.yaml"
    dump_signals = _detect_deterministic_dump_dossier(bd_path)
    if dump_signals:
        return {
            "status": "rejected",
            "reason": (
                "baseline_dossier_candidate.yaml is a deterministic dump — operator "
                "must review the market_research candidates and resolve the placeholder "
                "before production can start. Signals: " + "; ".join(dump_signals)
            ),
        }
    policy = search_policy_from_config(repo)
    state = initialize_search_state(
        search_id=f"s_{tid}", root_node=root_node, policy=policy
    )
    state["status"] = "running"
    draft_ids = seed_drafts_from_root(
        state,
        num_drafts=int(policy["num_drafts"]),
        max_depth=int(policy["max_depth"]),
    )
    tree_dir = _thread_dir(tid) / "production" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    (tree_dir / "search_state.json").write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "status": "accepted",
        "new_contract": new_claim,
        "root_node_id": root_node["id"],
        "draft_node_ids": draft_ids,
        "search_state_initialized": True,
        "next_step": (
            "Call get_next_admissible_node to receive the next node to "
            "process. The selector will pick by claim-type weight (validity "
            "first)."
        ),
    }


def handle_submit_grad_student_review(
    args: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    result = validate_grad_student_review(
        commentary=args.get("commentary", ""),
        concerns=args.get("concerns") or [],
        config=_persona_cfg(settings),
    )
    if not result.ok:
        return {"status": "rejected", "reason": result.reject_message()}
    # Persist into the node's dialog log so the frontend graph picks it up.
    tid = args["thread_id"]
    node_id = args["node_id"]
    dialog_path = (
        _thread_dir(tid) / "production" / "tree" / "nodes" / node_id / "dialog.json"
    )
    dialog_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_json(dialog_path) or {"node_id": node_id, "entries": []}
    existing["entries"].append(
        {
            "speaker": "grad_student",
            "intent": "task_review",
            "text": _strip_tool_envelope_leak(args.get("commentary", "")),
            "metadata": {
                "source": "mcp_server",
                "concerns_count": len(args.get("concerns") or []),
            },
        }
    )
    for c in args.get("concerns") or []:
        existing["entries"].append(
            {
                "speaker": "grad_student",
                "intent": "concern",
                "text": _strip_tool_envelope_leak(
                    f"{c.get('summary', '')} — {c.get('evidence', '')}"
                ),
                "metadata": {"blocked": bool(c.get("blocked"))},
            }
        )
    dialog_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"status": "accepted"}


def handle_design_experiment_template(args: dict[str, Any]) -> dict[str, Any]:
    """Persist Professor-authored experiment code (source_files dict) to the
    thread's professor_templates/<node_id>/ + _lib/ on disk."""
    from research_harness.orchestrator.experiment_plan import (
        PLAN_METADATA_FILENAME,
        SRC_DIRNAME,
        _professor_template_root,
    )

    tid = args["thread_id"]
    node_id = args["node_id"]
    plan_meta = args["plan_metadata"]
    if not isinstance(plan_meta, dict):
        return {"status": "rejected", "reason": "plan_metadata must be an object"}
    source_files = plan_meta.get("source_files") or []
    if not isinstance(source_files, list):
        return {"status": "rejected", "reason": "plan_metadata.source_files must be a list"}

    tree_dir = _thread_dir(tid) / "production" / "tree"
    template_root = _professor_template_root(tree_dir)
    node_dir = template_root / node_id
    node_dir.mkdir(parents=True, exist_ok=True)
    src_dir = node_dir / SRC_DIRNAME
    src_dir.mkdir(parents=True, exist_ok=True)

    plan_only = {k: v for k, v in plan_meta.items() if k != "source_files"}
    (node_dir / PLAN_METADATA_FILENAME).write_text(
        json.dumps(plan_only, indent=2) + "\n", encoding="utf-8"
    )
    files_written: list[str] = []
    for sf in source_files:
        if not isinstance(sf, dict):
            continue
        rel = str(sf.get("path") or "").strip()
        content = str(sf.get("content") or "")
        if not rel or not content:
            continue
        if rel.startswith("_lib/"):
            target = template_root / rel
        else:
            target = node_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and rel.startswith("_lib/"):
            # Idempotent: never clobber existing shared lib.
            continue
        target.write_text(content, encoding="utf-8")
        files_written.append(rel)
    return {
        "status": "accepted",
        "files_written": files_written,
        "node_template_dir": str(node_dir),
        "_lib_dir": str(template_root / "_lib"),
    }


def handle_execute_node_experiment(args: dict[str, Any]) -> dict[str, Any]:
    """Build experiment_plan from the Professor's template, materialize the
    workspace, run LocalRunner, persist worker_report, and transition the
    node from 'ready'→'running'→'completed_worker_report'."""
    from research_harness.config import load_settings as _ls
    from research_harness.orchestrator.experiment_plan import (
        build_experiment_plan_for_node,
        build_job_manifest_from_experiment_plan,
        validate_experiment_plan,
    )
    from research_harness.orchestrator.search_state import (
        transition_node,
        validate_search_state,
    )
    from research_harness.orchestrator.tree_search import _attach_runner_outputs
    from research_harness.runner.evidence import (
        build_worker_report_from_runner_evidence,
    )
    from research_harness.runner.local_runner import LocalRunner
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    node_id = args["node_id"]
    repo = _repo_root()
    settings_local = _ls(repo)
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path)
    if not state:
        return {"status": "rejected", "reason": "search_state.json missing"}
    validate_search_state(state)
    node = next((n for n in state["nodes"] if n["id"] == node_id), None)
    if not node:
        return {"status": "rejected", "reason": f"node {node_id} not in search_state"}

    run_dir = state_path.parent
    node_run_dir = run_dir / "nodes" / node_id
    node_run_dir.mkdir(parents=True, exist_ok=True)

    if node["status"] == "ready":
        transition_node(
            state, node_id, "running",
            event="mcp_dispatch",
            reason="MCP server execute_node_experiment",
        )

    plan, template_used = build_experiment_plan_for_node(
        repo, node, run_dir, settings=settings_local
    )
    validate_experiment_plan(node, plan, run_dir)
    (node_run_dir / "experiment_plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = build_job_manifest_from_experiment_plan(node, plan, run_dir)
    validate_named_schema("job_manifest", manifest)
    (node_run_dir / "job_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    runner = LocalRunner(run_dir, settings=settings_local)
    runner_result = runner.execute(manifest)
    validate_named_schema("runner_result", runner_result)
    evidence = build_worker_report_from_runner_evidence(
        node, manifest, runner_result, run_dir
    )
    runner_result_path = Path(runner_result["workspace"]) / "runner_result.json"
    _attach_runner_outputs(
        node, run_dir,
        experiment_plan_path=node_run_dir / "experiment_plan.json",
        job_manifest_path=node_run_dir / "job_manifest.json",
        runner_result_path=runner_result_path,
        runner_result=runner_result,
        source_files=evidence.source_files,
        metrics_evidence_paths=evidence.metrics_evidence_paths,
    )
    node["outputs"]["template_used"] = template_used
    worker_report = evidence.worker_report
    validate_named_schema("worker_report", worker_report)
    (node_run_dir / "worker_report.json").write_text(
        json.dumps(worker_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    transition_node(
        state, node_id, "completed_worker_report",
        event="worker_report",
        reason=f"worker status: {worker_report['status']}",
    )
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "status": "ok",
        "worker_status": worker_report["status"],
        "claim_verdict_candidate": worker_report.get("claim_verdict_candidate"),
        "metrics": worker_report.get("metrics", {}),
        "baselines": worker_report.get("baselines", {}),
        "baseline_evidence_overall": (
            worker_report.get("baseline_evidence_status") or {}
        ).get("overall"),
        "template_used": template_used,
    }


def handle_run_critic_reviews(args: dict[str, Any]) -> dict[str, Any]:
    """Deterministic critic pack — selects critics by folder routing and
    runs them. Transitions node to 'critic_reviewed'."""
    from research_harness.critics.governance import select_critics
    from research_harness.critics.review_runner import run_critic_reviews as _run
    from research_harness.orchestrator.search_state import (
        transition_node,
        validate_search_state,
    )
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    node_id = args["node_id"]
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path)
    if not state:
        return {"status": "rejected", "reason": "search_state.json missing"}
    validate_search_state(state)
    node = next((n for n in state["nodes"] if n["id"] == node_id), None)
    if not node:
        return {"status": "rejected", "reason": f"node {node_id} not in search_state"}
    worker_report = _read_json(
        _thread_dir(tid) / "production" / "tree" / "nodes" / node_id / "worker_report.json"
    )
    if not worker_report:
        return {"status": "rejected", "reason": "run execute_node_experiment first"}
    routing = select_critics(_repo_root(), node)
    reviews = _run(node, worker_report, routing)
    for review in reviews:
        validate_named_schema("critic_review", review)
    (_thread_dir(tid) / "production" / "tree" / "nodes" / node_id /
     "critic_reviews.json").write_text(
        json.dumps(reviews, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    transition_node(
        state, node_id, "critic_reviewed",
        event="critic_reviews",
        reason=f"{len(reviews)} critics reviewed",
    )
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    blocking = [r for r in reviews if r.get("blocking")]
    return {
        "status": "ok",
        "review_count": len(reviews),
        "blocking_count": len(blocking),
        "reviews": reviews,
    }


def handle_revise_root_after_reject(
    args: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    """AC rejected the paper. Accept a new claim contract and re-bootstrap
    production from a fresh root node."""
    tid = args["thread_id"]
    ac_path = _thread_dir(tid) / "production" / "rebuttal" / "ac_decision.json"
    prev_ac = _read_json(ac_path)
    # 'reject_and_diversify' routes through select_alternative_root, which
    # delegates to this handler; accept both terminal-reject AC decisions.
    if not prev_ac or prev_ac.get("decision") not in {"reject", "reject_and_diversify"}:
        return {
            "status": "rejected",
            "reason": (
                "revise_root_after_reject is only valid after an AC reject/reject_and_diversify. "
                "Current ac_decision.json status: "
                f"{(prev_ac or {}).get('decision', 'missing')}"
            ),
        }
    new_claim = {
        "claim_under_test": args["new_claim_under_test"],
        "mandatory_baselines": args["mandatory_baselines"],
        "success_criteria": args["success_criteria"],
        "disproof_conditions": args["disproof_conditions"],
    }
    market_dir = _thread_dir(tid) / "market"
    market_context = {
        "baseline_dossier_yaml": (
            (market_dir / "baseline_dossier_candidate.yaml").read_text(encoding="utf-8")
            if (market_dir / "baseline_dossier_candidate.yaml").exists() else ""
        ),
        "baseline_analysis_md": (
            (market_dir / "baseline_analysis.md").read_text(encoding="utf-8")
            if (market_dir / "baseline_analysis.md").exists() else ""
        ),
        "reference_papers": [
            {"filename": p.name}
            for p in sorted((market_dir / "reference_papers").glob("*.pdf"))
        ] if (market_dir / "reference_papers").is_dir() else [],
    }
    grilling = _read_json(_thread_dir(tid) / "grilling" / "grilling_session.json")
    problem_statement = (
        (grilling or {}).get("extracted", {}).get("claim_under_test") or ""
    )
    persona_cfg = _persona_cfg(settings)
    result = validate_claim_contract(
        new_claim=new_claim,
        problem_statement=problem_statement,
        market_context=market_context,
        config=persona_cfg,
    )
    if not result.ok:
        return {"status": "rejected", "reason": result.reject_message()}
    # Anti-laziness: forbid narrowing the success threshold without
    # proportionally widening evidence breadth. Pull the old claim from
    # intake_to_claim_dialog.json (still on disk before we archive it below).
    old_handoff = _read_json(
        _thread_dir(tid) / "production" / "intake_to_claim_dialog.json"
    ) or {}
    old_claim = old_handoff.get("new_contract")
    if old_claim:
        narrow_check = validate_revision_after_reject(
            new_claim=new_claim,
            old_claim=old_claim,
            new_evidence_breadth=args.get("evidence_breadth"),
            old_evidence_breadth=args.get("previous_evidence_breadth"),
            config=persona_cfg,
        )
        if not narrow_check.ok:
            return {"status": "rejected", "reason": narrow_check.reject_message()}
    # Capability claims must include a decision rule.
    rule_check = validate_decision_rule_for_capability_claim(
        claim_contract=new_claim,
        config=persona_cfg,
    )
    if not rule_check.ok:
        return {"status": "rejected", "reason": rule_check.reject_message()}
    # PR7: re-check feasibility envelope on the revised claim.
    envelope = _read_json(
        _thread_dir(tid) / "production" / "feasibility_envelope.json"
    )
    registered = {
        a.get("id")
        for a in (settings.get("data_adapters", {}) or {}).get("registered", []) or []
    }
    env_check = validate_claim_fits_envelope(
        claim_contract={**new_claim,
                         "deploy_grade_scope": args.get("deploy_grade_scope"),
                         "data_source_anchor": args.get("data_source_anchor")},
        envelope=envelope,
        registered_adapter_ids=registered,
        config=persona_cfg,
    )
    if not env_check.ok:
        return {"status": "rejected", "reason": env_check.reject_message()}
    if args.get("deploy_grade_scope"):
        new_claim["deploy_grade_scope"] = args["deploy_grade_scope"]
    if args.get("data_source_anchor"):
        new_claim["data_source_anchor"] = args["data_source_anchor"]
    # Archive the rejected attempt and write the revised claim as a fresh
    # intake_to_claim handoff so the operator can re-run production from scratch.
    import time
    attempt_root = _thread_dir(tid) / f"production.attempt_reject_{int(time.time())}"
    prod_dir = _thread_dir(tid) / "production"
    if prod_dir.exists():
        prod_dir.rename(attempt_root)
    prod_dir.mkdir(parents=True, exist_ok=True)
    (prod_dir / "intake_to_claim_dialog.json").write_text(
        json.dumps({
            "original_contract": {"claim_under_test": problem_statement},
            "new_contract": new_claim,
            "dialog": [{
                "speaker": "professor",
                "intent": "revision_after_ac_reject",
                "text": args.get("rationale", "AC rejected previous paper; honest+strong revision."),
                "metadata": {
                    "previous_ac_decision": prev_ac,
                    "archived_attempt": str(attempt_root),
                },
            }],
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "status": "accepted",
        "new_contract": new_claim,
        "archived_attempt": str(attempt_root),
    }


def handle_submit_professor_decision(
    args: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    """ApplyProfessor's promote/branch/prune to the actual search_state."""
    from research_harness.orchestrator.search_state import (
        add_child_nodes,
        transition_node,
        validate_search_state,
    )
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    node_id = args["node_id"]
    transition = args["next_transition"]
    follow_ups = args.get("follow_up_children") or []
    # Enforce the deterministic selector: the operator's policy is "process
    # the leftmost-stage, lowest-type-weight admissible node next". If
    # Claude Code submits a decision for some OTHER node, reject and point
    # at the correct one. (Sidesteps the model picking out-of-order nodes.)
    state = _read_json(
        _thread_dir(tid) / "production" / "tree" / "search_state.json"
    )
    if state:
        expected = handle_get_next_admissible_node({"thread_id": tid})
        if expected.get("status") == "ok" and expected.get("node_id") != node_id:
            return {
                "status": "rejected",
                "reason": (
                    f"You submitted a decision for node {node_id!r}, but the "
                    f"deterministic selector points at {expected['node_id']!r} "
                    f"next (stage={expected.get('active_stage')}, type="
                    f"{expected.get('node_type')}). Call "
                    f"get_next_admissible_node, process THAT node, then "
                    f"retry the submission with its id."
                ),
            }
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path)
    if not state:
        return {"status": "rejected", "reason": "search_state.json missing"}
    validate_search_state(state)
    node = next((n for n in state["nodes"] if n["id"] == node_id), None)
    if not node:
        return {"status": "rejected", "reason": f"node {node_id} not in search_state"}
    parent_claim = node["claim_contract"]["claim_under_test"]
    if transition == "promoted":
        check = validate_follow_up_strength(
            parent_claim=parent_claim, follow_ups=follow_ups,
            config=_persona_cfg(settings),
        )
        if not check.ok:
            return {"status": "rejected", "reason": check.reject_message()}

    # Record the reduction JSON for audit / frontend.
    decision_path = (
        _thread_dir(tid) / "production" / "tree" / "nodes" / node_id /
        "mcp_professor_decision.json"
    )
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(
        json.dumps(args, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # Also append the Professor's natural-language reply + follow-up briefs
    # into dialog.json so the frontend graph view / inspector shows real
    # two-way conversation, not a one-sided grad-student monolog.
    dialog_path = decision_path.parent / "dialog.json"
    existing = _read_json(dialog_path) or {"node_id": node_id, "entries": []}
    response_text = _strip_tool_envelope_leak(
        (args.get("response_to_grad_student") or "").strip()
    )
    final_verdict = args.get("final_verdict") or ""
    if response_text:
        existing["entries"].append({
            "speaker": "professor",
            "intent": "verdict",
            "text": response_text,
            "metadata": {
                "final_verdict": final_verdict,
                "next_transition": transition,
                "follow_up_count": len(follow_ups),
                "source": "mcp_server",
            },
        })
    elif final_verdict or transition:
        # Even when Claude Code didn't supply a natural-language reply, log
        # the bare decision so the dialog log isn't visibly silent.
        existing["entries"].append({
            "speaker": "professor",
            "intent": "verdict",
            "text": (
                f"Decision: {transition} (final_verdict: {final_verdict or 'n/a'})"
            ),
            "metadata": {
                "final_verdict": final_verdict,
                "next_transition": transition,
                "source": "mcp_server",
                "synthetic_text": True,
            },
        })
    for f in follow_ups:
        successor = _strip_tool_envelope_leak(
            (f.get("successor_claim") or "").strip()
        )
        if not successor:
            continue
        ftype = f.get("type") or "?"
        rationale = _strip_tool_envelope_leak(
            (f.get("rationale") or "").strip()
        )
        existing["entries"].append({
            "speaker": "professor",
            "intent": "follow_up_brief",
            "text": (
                f"Next cohort: please test the {ftype} successor — "
                f"\"{successor}\". Rationale: {rationale}"
            ),
            "metadata": {
                "type": ftype,
                "successor_claim": successor,
                "source": "mcp_server",
            },
        })
    dialog_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # Reach orchestrator_reduced. Allowed transitions require we already be
    # at critic_reviewed; if the operator skipped run_critic_reviews, force
    # a no-blocking review on the fly via the deterministic reviewer.
    if node["status"] == "critic_reviewed":
        transition_node(
            state, node_id, "orchestrator_reduced",
            event="mcp_professor_decision",
            reason=args.get("final_verdict") or "mcp decision",
        )
    elif node["status"] == "completed_worker_report":
        # Run critics on the fly so the transition chain is valid.
        from research_harness.critics.governance import select_critics
        from research_harness.critics.review_runner import run_critic_reviews as _run
        worker_report = _read_json(
            decision_path.parent / "worker_report.json"
        ) or {}
        routing = select_critics(_repo_root(), node)
        reviews = _run(node, worker_report, routing)
        for r in reviews:
            validate_named_schema("critic_review", r)
        (decision_path.parent / "critic_reviews.json").write_text(
            json.dumps(reviews, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        transition_node(
            state, node_id, "critic_reviewed",
            event="critic_reviews",
            reason="auto-run from mcp_submit_decision",
        )
        transition_node(
            state, node_id, "orchestrator_reduced",
            event="mcp_professor_decision",
            reason=args.get("final_verdict") or "mcp decision",
        )

    # Final transition.
    created_child_ids: list[str] = []
    dropped_followups: list[dict[str, Any]] = []
    if transition == "promoted":
        transition_node(
            state, node_id, "promoted",
            event="mcp_promotion", reason="mcp accepted promotion",
        )
        if follow_ups:
            from research_harness.orchestrator.treesearch.parallel_agent import (
                _build_follow_up_children,
            )
            from research_harness.orchestrator.search_state import (
                search_policy_from_config,
            )
            frontier_item = next(
                (it for it in state["frontier"] if it["node_id"] == node_id), None
            )
            parent_depth = int(frontier_item["depth"]) if frontier_item else 0
            # state.max_depth is the canonical authority; if state is missing
            # it (legacy thread), fall back to the config rather than a
            # hardcoded magic number — operators who tune harness.yaml
            # should see the same cap in fresh and resumed threads.
            effective_max_depth = int(
                state.get("max_depth")
                or search_policy_from_config(_repo_root()).get("max_depth")
            )
            children = _build_follow_up_children(
                node, follow_ups,
                parent_depth=parent_depth,
                max_depth=effective_max_depth,
                dropped_followups=dropped_followups,
            )
            created_child_ids = [c["id"] for c in children]
            if children:
                add_child_nodes(
                    state, node_id, children,
                    reason="mcp professor follow-ups",
                )
    elif transition == "needs_child_branch":
        transition_node(
            state, node_id, "needs_child_branch",
            event="mcp_branch", reason="mcp accepted child branch",
        )
    elif transition == "pruned":
        transition_node(
            state, node_id, "pruned",
            event="mcp_prune", reason="mcp accepted prune",
        )

    # PR4: Failure memory auto-generation. When a node ends in a state the
    # rest of the harness considers a learnable failure (pruned, with an
    # informative final_verdict), write a one-page failure record so future
    # threads can retrieve it. Categories follow the existing memory/failures/
    # taxonomy.
    final_verdict = args.get("final_verdict") or ""
    if transition in {"pruned", "needs_child_branch"} and final_verdict:
        try:
            _write_failure_record(
                tid=tid,
                node_id=node_id,
                final_verdict=final_verdict,
                transition=transition,
                response_to_grad_student=args.get("response_to_grad_student") or "",
                node=node,
                worker_report=_read_json(
                    _thread_dir(tid) / "production" / "tree" / "nodes" / node_id / "worker_report.json"
                ) or {},
            )
        except Exception:  # noqa: BLE001
            # Non-fatal — failure memory is a secondary benefit, not a
            # blocker for the primary search state mutation.
            pass

    state["status"] = (
        "completed"
        if not any(it["status"] == "queued" for it in state["frontier"])
        else "running"
    )
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    response: dict[str, Any] = {
        "status": "accepted",
        "applied_transition": transition,
        "created_child_ids": created_child_ids,
        "search_state_status": state["status"],
    }
    if dropped_followups:
        # Surface why some Professor follow-ups did NOT become tree nodes —
        # depth_limit_reached, empty_successor_claim, etc. This makes
        # the previously-silent drop visible to Claude Code and to the
        # operator inspecting the response.
        response["dropped_followups"] = dropped_followups
        response["dropped_followups_summary"] = (
            f"{len(dropped_followups)} follow-up(s) not materialized — see dropped_followups[].reason. "
            "If reason starts with 'depth_limit_reached', adjust configs/harness.yaml `search.max_depth` "
            "and resume; the cap is configurable, not a code constant."
        )
    return response


def handle_decide_publication_readiness(args: dict[str, Any]) -> dict[str, Any]:
    tid = args["thread_id"]
    readiness_path = (
        _thread_dir(tid) / "production" / "tree" / "mcp_readiness.json"
    )
    readiness_path.parent.mkdir(parents=True, exist_ok=True)
    readiness_path.write_text(
        json.dumps(args, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"status": "recorded"}


# --- LLM-driven rebuttal + paper writer (Phase C / D) -------------------- #


def _rebuttal_dir(tid: str) -> Path:
    return _thread_dir(tid) / "production" / "rebuttal"


# ADR 0012 forest: the terminal's flat artifacts under production/rebuttal/.
# An explicit allowlist (not a name heuristic) so snapshot/reset/restore never
# touch the per-root snapshot subdirs themselves.
_TERMINAL_ARTIFACTS: tuple[str, ...] = (
    "falsifier_result.json",
    "construct_adversary_report.json",
    "rebuttal_packet.md",
    "rebuttal_routing.json",
    "orchestrator_reduction.json",
    "ac_decision.json",
    "camera_ready_revision.json",
    "user_goal_attestation.json",
)
_TERMINAL_ARTIFACT_DIRS: tuple[str, ...] = ("rebuttal_reviews",)


def _copy_terminal_artifacts(src: Path, dst: Path) -> list[str]:
    """Copy the flat terminal artifacts present in ``src`` into ``dst``."""
    import shutil

    dst.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in _TERMINAL_ARTIFACTS:
        f = src / name
        if f.is_file():
            shutil.copy2(f, dst / name)
            copied.append(name)
    for name in _TERMINAL_ARTIFACT_DIRS:
        d = src / name
        if d.is_dir():
            shutil.copytree(d, dst / name, dirs_exist_ok=True)
            copied.append(name + "/")
    return copied


def _clear_terminal_artifacts(rebuttal: Path) -> None:
    """Remove the flat terminal artifacts so the next root starts clean.

    Only the allowlisted artifacts are removed — per-root snapshot subdirs and
    anything else under rebuttal/ are left intact.
    """
    import shutil

    for name in _TERMINAL_ARTIFACTS:
        f = rebuttal / name
        if f.is_file():
            f.unlink()
    for name in _TERMINAL_ARTIFACT_DIRS:
        d = rebuttal / name
        if d.is_dir():
            shutil.rmtree(d)


def _publication_dir(tid: str) -> Path:
    return _thread_dir(tid) / "production" / "publication"


def _figures_dir(tid: str) -> Path:
    return _publication_dir(tid) / "figures"


def _resolve_promoted_node(tid: str, override_id: str | None = None) -> dict[str, Any]:
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path)
    if not state:
        raise ValueError("search_state.json missing — production not initialized")
    promoted = state.get("promoted_node_ids") or []
    if not promoted:
        raise ValueError("no promoted nodes yet — rebuttal requires a promoted root")
    target = override_id or next(
        (pid for pid in promoted
         if next((n for n in state["nodes"] if n["id"] == pid), {}).get("parent") is None),
        promoted[0],
    )
    node = next((n for n in state["nodes"] if n["id"] == target), None)
    if not node:
        raise ValueError(f"promoted node {target} missing from search_state")
    return {"state": state, "node": node, "promoted_id": target}


def handle_prepare_rebuttal_packet(args: dict[str, Any]) -> dict[str, Any]:
    """Build packet + critic list + practitioner persona text. NO LLM here."""
    from research_harness.critics.governance import select_critics
    from research_harness.publishing.rebuttal import build_rebuttal_packet
    from research_harness.config import load_settings as _ls

    tid = args["thread_id"]
    repo = _repo_root()
    ctx = _resolve_promoted_node(tid, args.get("promoted_node_id"))
    node = ctx["node"]
    promoted_id = ctx["promoted_id"]

    node_dir = _thread_dir(tid) / "production" / "tree" / "nodes" / promoted_id
    worker_report = _read_json(node_dir / "worker_report.json") or {}
    if not worker_report:
        return {"status": "rejected", "reason": "worker_report missing for promoted node"}
    promotion_critic_reviews = _read_json(node_dir / "critic_reviews.json") or []

    rebuttal_node = dict(node)
    rebuttal_node["stage"] = "rebuttal"
    routing = select_critics(repo, rebuttal_node, _ls(repo))

    rebuttal_dir = _rebuttal_dir(tid)
    rebuttal_dir.mkdir(parents=True, exist_ok=True)

    packet_state = {
        "node": node,
        "worker_report": worker_report,
        "critic_reviews": promotion_critic_reviews,
        "critic_routing": routing,
        "orchestrator_reduction": {
            "node_id": promoted_id,
            "final_verdict": "pending_llm_reduction",
            "research_status": "pending_llm_reduction",
            "next_transition": "pending",
            "score_summary": {},
            "blocking_objections": [],
            "accepted_lesson_candidates": [],
            "failure_branch_prior": {
                "source": "failure_memory", "query_tags": [],
                "selected_failure_files": [], "risk_controls": [], "branch_suggestions": [],
            },
            "child_branch_suggestions": [],
        },
    }
    packet_path = rebuttal_dir / "rebuttal_packet.md"
    build_rebuttal_packet(packet_state, packet_path)

    # Persist routing so subsequent submissions can validate critic_id is in scope.
    (rebuttal_dir / "rebuttal_routing.json").write_text(
        json.dumps(routing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Load each critic's body so the LLM can role-play one at a time.
    critic_list: list[dict[str, Any]] = []
    for crit in routing["applied_critics"]:
        crit_path = (repo / crit["path"]) if not Path(crit["path"]).is_absolute() else Path(crit["path"])
        if crit_path.exists():
            critic_body = crit_path.read_text(encoding="utf-8")
        else:
            critic_body = ""
        critic_list.append({
            "critic_id": crit["critic_id"],
            "route": crit["route"],
            "path": crit["path"],
            "body": critic_body,
        })

    persona_path = repo / "critics" / "PRACTITIONER_PERSONA.md"
    practitioner_persona = persona_path.read_text(encoding="utf-8") if persona_path.exists() else ""

    # Surface the ORIGINAL user problem (intake) and the Professor's reshaped
    # claim. Critics need both to judge direct_methodology_for_user: the
    # paper may have proved the reshaped claim but still leave the original
    # user without an actionable methodology.
    intake_dialog = _read_json(
        _thread_dir(tid) / "production" / "intake_to_claim_dialog.json"
    ) or {}
    original_user_problem = (
        intake_dialog.get("original_contract", {}).get("claim_under_test")
        or intake_dialog.get("original_user_problem")
        or ""
    )
    reshaped_claim = (
        intake_dialog.get("new_contract", {}).get("claim_under_test")
        or node.get("claim_contract", {}).get("claim_under_test")
        or ""
    )

    # PR4: inject relevant prior-thread failures + active lessons so the
    # rebuttal critics start with the harness's cumulative memory, not a
    # cold start. retrieve_failure_summaries is safe — empty list when no
    # tags match.
    prior_failures: list[dict[str, Any]] = []
    try:
        from research_harness.memory.failure_retrieval import (
            retrieve_failure_summaries,
        )
        tags = (node.get("failure_retrieval", {}) or {}).get("query_tags", [])
        summaries = retrieve_failure_summaries(
            repo,
            query_tags=tags,
            selected_fail_files=[],
            top_k=5,
        )
        prior_failures = [
            {
                "file": s.file,
                "category": s.category,
                "tags": s.tags,
                "lesson": s.lesson,
                "reason": s.reason,
                "score": s.score,
            }
            for s in summaries
        ]
    except Exception:  # noqa: BLE001
        prior_failures = []

    active_lessons: list[dict[str, Any]] = []
    try:
        from research_harness.config import load_lessons
        active_lessons = (load_lessons(repo) or {}).get("active_lessons", []) or []
    except Exception:  # noqa: BLE001
        active_lessons = []

    return {
        "status": "ok",
        "thread_id": tid,
        "promoted_node_id": promoted_id,
        "rebuttal_packet_path": str(packet_path),
        "rebuttal_packet_markdown": packet_path.read_text(encoding="utf-8"),
        "critic_list": critic_list,
        "practitioner_persona": practitioner_persona,
        "worker_report": worker_report,
        "node": node,
        "original_user_problem": original_user_problem,
        "reshaped_claim_under_test": reshaped_claim,
        "prior_failures": prior_failures,
        "active_lessons": active_lessons,
        "methodology_fit_reminder": (
            "When writing direct_methodology_for_user, judge fit to "
            "original_user_problem (not reshaped_claim_under_test). The "
            "Professor's claim may be proved while the user is still stuck."
        ),
        "next_step": (
            "Loop through critic_list and call submit_rebuttal_critic_review "
            "for each critic_id. After all submitted, call "
            "submit_orchestrator_reduction, then submit_ac_decision, then "
            "submit_camera_ready_revision."
        ),
    }


def handle_submit_rebuttal_critic_review(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    review = args["review"]
    routing = _read_json(_rebuttal_dir(tid) / "rebuttal_routing.json")
    if not routing:
        return {"status": "rejected", "reason": "prepare_rebuttal_packet must run first"}
    allowed_ids = {c["critic_id"] for c in routing["applied_critics"]}
    critic_id = review.get("critic_id")
    if critic_id not in allowed_ids:
        return {
            "status": "rejected",
            "reason": f"critic_id {critic_id!r} is not in this thread's routing. allowed: {sorted(allowed_ids)}",
        }
    try:
        validate_named_schema("critic_review", review)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    reviews_dir = _rebuttal_dir(tid) / "rebuttal_reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / f"{critic_id}.json").write_text(
        json.dumps(review, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    submitted = sorted(p.stem for p in reviews_dir.glob("*.json"))
    pending = sorted(allowed_ids - set(submitted))
    return {
        "status": "ok",
        "submitted_critic_id": critic_id,
        "submitted_so_far": submitted,
        "pending_critic_ids": pending,
        "next_step": (
            "Continue with the next critic_id" if pending else
            "All rebuttal critics submitted — call submit_orchestrator_reduction next."
        ),
    }


def handle_submit_orchestrator_reduction(args: dict[str, Any]) -> dict[str, Any]:
    tid = args["thread_id"]
    node_id = args["node_id"]
    ctx = _resolve_promoted_node(tid, node_id)
    if ctx["promoted_id"] != node_id:
        return {"status": "rejected", "reason": f"node_id {node_id} is not the promoted node"}

    # Require all routed critics to have submitted before reduction.
    routing = _read_json(_rebuttal_dir(tid) / "rebuttal_routing.json") or {}
    required = {c["critic_id"] for c in routing.get("applied_critics", [])}
    reviews_dir = _rebuttal_dir(tid) / "rebuttal_reviews"
    submitted = {p.stem for p in reviews_dir.glob("*.json")} if reviews_dir.exists() else set()
    missing = sorted(required - submitted)
    if missing:
        return {
            "status": "rejected",
            "reason": f"orchestrator_reduction requires every routed critic to submit first. missing: {missing}",
        }

    final_verdict = args.get("final_verdict") or ""
    allowed_verdicts = {
        "supported", "supported_with_scope_narrowing", "contradicted",
        "confounded_or_not_evaluable", "taste_rejected_local_branch",
        "blocked_by_operational_issue",
    }
    if final_verdict not in allowed_verdicts:
        return {"status": "rejected", "reason": f"final_verdict must be one of {sorted(allowed_verdicts)}"}
    if len((args.get("synthesis_message") or "").strip()) < 60:
        return {"status": "rejected", "reason": "synthesis_message must be >= 60 chars (operator-language synthesis, not a label)"}

    # --- ADR 0007 lever 0: adversarial-dominant aggregation. -------------
    # A grounded critic kill (contradicted / blocking) cannot be reconciled
    # into a positive reduction. It freezes the verdict until DEFEATED on
    # merits in blocking_objections (critic_id + defeated=true + substantive
    # rebuttal). This recovers the decorrelation the persona pack already
    # paid for — RSTF's critics DID kill, the reduction just averaged them in.
    from research_harness.config import load_settings as _ls
    if (
        _adversarial_dominance_enabled(_ls(_repo_root()))
        and final_verdict in {"supported", "supported_with_scope_narrowing"}
    ):
        kills = _collect_rebuttal_kills(tid)
        undefeated = _undefeated_kills(kills, args.get("blocking_objections"))
        if undefeated:
            ids = ", ".join(k["critic_id"] for k in undefeated)
            return {
                "status": "rejected",
                "reason": (
                    f"adversarial-dominance: critic(s) [{ids}] returned a kill "
                    "(contradicted / blocking) that is not defeated. You cannot "
                    f"reduce to {final_verdict!r} by reconciliation. Either (a) "
                    "defeat each kill on merits — add a blocking_objections entry "
                    "{critic_id, defeated:true, defeat_rebuttal:<engage the "
                    "objection's grounds, not relabel>}, or (b) set final_verdict "
                    "to contradicted / confounded_or_not_evaluable. Narrowing the "
                    "claim does NOT defeat a kill."
                ),
                "undefeated_kills": [k["critic_id"] for k in undefeated],
            }

    reduction = {
        "node_id": node_id,
        "final_verdict": final_verdict,
        "research_status": args.get("research_status", final_verdict),
        "next_transition": args.get("next_transition", "promoted"),
        "score_summary": args.get("score_summary", {}),
        "synthesis_message": args["synthesis_message"],
        "blocking_objections": args.get("blocking_objections", []),
        "accepted_lesson_candidates": args.get("accepted_lesson_candidates", []),
        "failure_branch_prior": {
            "source": "rebuttal_synthesis", "query_tags": [],
            "selected_failure_files": [], "risk_controls": [], "branch_suggestions": [],
        },
        "child_branch_suggestions": [],
    }
    (_rebuttal_dir(tid) / "orchestrator_reduction.json").write_text(
        json.dumps(reduction, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": "ok", "next_step": "Call submit_ac_decision next."}


# --- Strictness rails (see docs/adr/0005 + post-thread_e5b277f9 review) -- #

_NEGATIVE_SUCCESSOR_VERDICTS = {
    "contradicted",
    "confounded_or_not_evaluable",
    "blocked_by_operational_issue",
}
_BLOCKING_SUCCESSOR_VERDICTS = {
    "confounded_or_not_evaluable",
    "blocked_by_operational_issue",
}


def _aggregate_successor_verdicts(tid: str, promoted_node_id: str) -> dict[str, Any]:
    """Count direct-children final_verdicts of the promoted node.

    Used by submit_ac_decision to block accept when the promoted root's
    children largely returned negative — which is exactly the failure mode
    thread_e5b277f9 hit (3/3 successors negative yet AC=accept).
    """
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path) or {}
    children = [
        n for n in state.get("nodes", [])
        if n.get("parent") == promoted_node_id
    ]
    nodes_dir = _thread_dir(tid) / "production" / "tree" / "nodes"
    negative: list[tuple[str, str]] = []
    blocking: list[tuple[str, str]] = []
    evaluated = 0
    for child in children:
        decision_path = nodes_dir / child["id"] / "mcp_professor_decision.json"
        decision = _read_json(decision_path)
        if not decision:
            continue
        evaluated += 1
        fv = str(decision.get("final_verdict") or "")
        if fv in _NEGATIVE_SUCCESSOR_VERDICTS:
            negative.append((child["id"], fv))
        if fv in _BLOCKING_SUCCESSOR_VERDICTS:
            blocking.append((child["id"], fv))
    return {
        "total_children": len(children),
        "evaluated": evaluated,
        "negative_count": len(negative),
        "negative_children": negative,
        "blocking_children": blocking,
    }


_DETERMINISTIC_DUMP_REASON_PATTERNS = (
    "top-ranked search result",
    "tbd",
    "professor will design",
    "operator should review",
    "deterministic dump",
)
_DETERMINISTIC_DUMP_RISK_TAGS = {
    "operator_should_review",
    "operator_review_required",
}


def _detect_deterministic_dump_dossier(bd_path: Path) -> list[str]:
    """Return non-empty signal list if baseline_dossier_candidate.yaml looks
    like a deterministic dump (no operator-reviewed substance). Used both by
    AC confidence down-clamp and by future market preflight."""
    if not bd_path.exists():
        return []
    try:
        import yaml as _yaml
        dossier = _yaml.safe_load(bd_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    if not isinstance(dossier, dict):
        return []
    signals: list[str] = []
    selected = dossier.get("selected") or {}
    if isinstance(selected, dict):
        reason = str(selected.get("one_paragraph_reason") or "").lower()
        if any(pat in reason for pat in _DETERMINISTIC_DUMP_REASON_PATTERNS):
            signals.append(f"selected.one_paragraph_reason matches placeholder pattern: {reason[:80]!r}")
        risk_tags = {str(t).lower() for t in (selected.get("risk_tags") or [])}
        if risk_tags & _DETERMINISTIC_DUMP_RISK_TAGS:
            signals.append(f"selected.risk_tags contains operator-review marker: {sorted(risk_tags & _DETERMINISTIC_DUMP_RISK_TAGS)}")
    for cand in dossier.get("candidates_index") or []:
        if not isinstance(cand, dict):
            continue
        method = str(cand.get("method") or "")
        decision = str(cand.get("decision") or "")
        if decision in {"selected_as_naive", "selected_as_random_or_null"} and method.lower().lstrip().startswith(("naive: tbd", "random_or_null: tbd")):
            signals.append(f"candidate {cand.get('id')} ({decision}) is unresolved placeholder: {method!r}")
    return signals


def _derive_max_attestable_status(envelope: dict[str, Any] | None) -> str:
    """ADR 0006: harness-owned ceiling on goal achievement, derived from the
    envelope's external_falsifier. Thin re-export of falsifier.derive_max_
    attestable_status so call sites in this module stay terse."""
    from research_harness.falsifier import derive_max_attestable_status
    return derive_max_attestable_status(envelope)


def _falsification_gate_enabled(settings: dict[str, Any]) -> bool:
    """ADR 0006: the gate defaults ON. Disabling is a loosening, allowed only
    via runtime.llm_orchestrator.mcp.persona_enforcement.falsification_gate."""
    fg = _persona_cfg(settings).get("falsification_gate")
    if isinstance(fg, dict):
        return fg.get("enabled", True) is not False
    return True


def _falsifier_result_blocks_achievement(
    fr: dict[str, Any] | None, falsifier: dict[str, Any]
) -> str | None:
    """ADR 0006: returns a rejection reason if the on-disk falsifier_result
    does not legitimately support achieved=true, else None. The result must
    exist, be harness-produced, have passed, and be computed against the
    SAME holdout + kind the envelope registered (so a result computed against
    some other, easier source can't be substituted)."""
    if not fr:
        return (
            "ADR 0006: achieved=true requires a passing falsifier_result.json, "
            "but none exists. Run compute_falsifier_result against the "
            "envelope's registered holdout first."
        )
    from research_harness.falsifier import PRODUCED_BY
    if fr.get("produced_by") != PRODUCED_BY:
        return (
            "falsifier_result.produced_by is not the harness falsifier module — "
            "the result must be harness-computed, not hand-written."
        )
    if not fr.get("passed"):
        pred = fr.get("predicate") or {}
        return (
            f"falsifier_result.passed is false (observed {fr.get('observed')} "
            f"vs {pred.get('op')} {pred.get('threshold')}). The held-out check "
            "did not clear the predicate — achieved=true stays refused."
        )
    if fr.get("holdout_source_id") != falsifier.get("holdout_source_id"):
        return (
            f"falsifier_result.holdout_source_id "
            f"({fr.get('holdout_source_id')!r}) does not match the envelope's "
            f"registered holdout ({falsifier.get('holdout_source_id')!r}). The "
            "result must be computed against the REGISTERED holdout."
        )
    if fr.get("kind") != falsifier.get("kind"):
        return (
            f"falsifier_result.kind ({fr.get('kind')!r}) does not match the "
            f"envelope's external_falsifier.kind ({falsifier.get('kind')!r})."
        )
    return None


# --- ADR 0008: verdict-strength type system + referent ledger ----------- #


def _frozen_question(tid: str) -> dict[str, Any] | None:
    return _read_json(_thread_dir(tid) / "production" / "frozen_question.json")


def _frozen_subject_role(tid: str) -> str | None:
    """ADR 0009 (B3): the patchwork-probe axis, read from the IMMUTABLE frozen
    question (never a per-node judgment). None when unpinned → callers default
    conservatively to method_is_solution."""
    return (_frozen_question(tid) or {}).get("subject_role")


def _construct_adversary_thresholds(settings: dict[str, Any]) -> tuple[int, int]:
    from research_harness.construct_adversary import (
        DEFAULT_MIN_BUDGET, DEFAULT_MIN_DISTINCT_WORLDS,
    )
    cfg = _persona_cfg(settings).get("construct_adversary")
    minb, minw = DEFAULT_MIN_BUDGET, DEFAULT_MIN_DISTINCT_WORLDS
    if isinstance(cfg, dict):
        if isinstance(cfg.get("min_budget"), int) and cfg["min_budget"] >= 0:
            minb = cfg["min_budget"]
        if isinstance(cfg.get("min_distinct_worlds"), int) and cfg["min_distinct_worlds"] >= 1:
            minw = cfg["min_distinct_worlds"]
    return minb, minw


def _falsifier_guard_thresholds(settings: dict[str, Any]) -> dict[str, Any]:
    """ADR 0006 rev.2: cross_generator_transfer guard thresholds, overridable
    via persona_enforcement.falsifier_guards. The guard itself is intrinsic to
    deriving an honest verdict (not a disableable rail) — only the thresholds
    are tunable."""
    from research_harness.falsifier import (
        DEFAULT_MAX_TIE_FRACTION,
        DEFAULT_MIN_BEHAVIORAL_DISTANCE,
        DEFAULT_MIN_RANK_VARIANCE,
        DEFAULT_NULL_FLOOR_K,
    )
    out = {
        "min_behavioral_distance": DEFAULT_MIN_BEHAVIORAL_DISTANCE,
        "min_rank_variance": DEFAULT_MIN_RANK_VARIANCE,
        "max_tie_fraction": DEFAULT_MAX_TIE_FRACTION,
        "null_floor_k": DEFAULT_NULL_FLOOR_K,
    }
    cfg = _persona_cfg(settings).get("falsifier_guards")
    if isinstance(cfg, dict):
        for key in out:
            val = cfg.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
                out[key] = float(val)
    return out


def _real_referent_verified(tid: str) -> bool:
    """ADR 0008 Axis 2: a REAL referent is verified iff the envelope registered
    a real_holdout falsifier AND a passing harness-computed falsifier_result
    exists against it. cross_generator_transfer is proposer-authored and does
    NOT count — it cannot unlock reality-closeness."""
    envelope = _read_json(_thread_dir(tid) / "production" / "feasibility_envelope.json") or {}
    falsifier = envelope.get("external_falsifier") or {}
    if falsifier.get("kind") != "real_holdout":
        return False
    fr = _read_json(_rebuttal_dir(tid) / "falsifier_result.json")
    return _falsifier_result_blocks_achievement(fr, falsifier) is None


def _construct_referent_verified(tid: str, settings: dict[str, Any]) -> bool:
    """ADR 0008 Axis 1: a CONSTRUCT referent is verified iff a funded
    construct-adversary survived against the pinned frozen question. The
    harness RE-DERIVES the verdict from the adversary's enumerated search."""
    fq = _frozen_question(tid)
    if not fq:
        return False
    report = _read_json(_rebuttal_dir(tid) / "construct_adversary_report.json")
    if not report:
        return False
    if report.get("question_id") != fq.get("question_id"):
        return False  # adversary must attack the frozen (un-authored) question
    from research_harness.construct_adversary import evaluate_construct_adversary_report
    minb, minw = _construct_adversary_thresholds(settings)
    out = evaluate_construct_adversary_report(
        report, min_budget=minb, min_distinct_worlds=minw
    )
    return out.get("verdict") == "survived"


def _thread_referent_ledger(tid: str, settings: dict[str, Any]) -> dict[str, Any]:
    """ADR 0008: harness-derived strength ceiling. The proposer authors none of
    these signals — they are computed from executed, harness-checked evidence."""
    from research_harness import verdict_strength as _V
    return _V.referent_ledger(
        real_referent_verified=_real_referent_verified(tid),
        construct_referent_verified=_construct_referent_verified(tid, settings),
    )


_METHODOLOGY_ORDER = {"absent": 0, "partial": 1, "provides": 2}
_METHODOLOGY_INV = {0: "absent", 1: "partial", 2: "provides"}


def _clamp_methodology_assessment(
    tid: str, settings: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    """ADR 0008 rev.2 (A3): the methodology verdict inherits the WEAKER of the
    executed verification paths. The AC authors aggregate_verdict; the harness
    CAPS it from the executed evidence so a clean construct-adversary pass cannot
    launder 'provides' over a falsifier that was run but did not pass
    (uninformative / degenerate / failed). The cap only LOWERS, and only when a
    path was actually executed and came back weak — a thread with no falsifier /
    no adversary keeps its authored verdict (those are bounded elsewhere by
    verdict_strength / attested_status). Returns decision unchanged or with a
    clamped aggregate_verdict + clamp_reason."""
    synthesis = decision.get("rebuttal_synthesis")
    if not isinstance(synthesis, dict):
        return decision
    methodology = synthesis.get("methodology_assessment")
    if not isinstance(methodology, dict):
        return decision
    authored = methodology.get("aggregate_verdict")
    if authored not in _METHODOLOGY_ORDER:
        return decision

    cap = 2  # 'provides' unless a path forbids it
    reasons: list[str] = []

    # INV-reality-cap (T2-10): 'provides' asserts a methodology usable for the
    # user's REAL decision — which is unconstructable air-gapped. It is reachable
    # ONLY with a passing real referent, STRUCTURALLY — even if no falsifier ran
    # at all. This is a ceiling on the reachable verdict (mirror verdict_strength),
    # not a downclamp-on-observed-weakness: absent a real referent, 'provides' is
    # simply not in the reachable vocabulary.
    if not _real_referent_verified(tid):
        cap = min(cap, 1)
        reasons.append(
            "no real referent verified (no passing real_holdout falsifier) — 'provides' "
            "is unconstructable air-gapped; ceiling is 'partial' (INV-reality-cap)"
        )

    # Falsifier path: a run-but-not-passed result forbids 'provides'. (A1 typed
    # the verdict — uninformative/degenerate/failed all mean measurement gave no
    # usable transfer evidence; rho must not be reported as transfer support.)
    fr = _read_json(_rebuttal_dir(tid) / "falsifier_result.json")
    if fr and not fr.get("passed"):
        cap = min(cap, 1)
        reasons.append(
            f"falsifier verdict={fr.get('verdict', 'not-passed')!r} — the measurement "
            "path did not pass, so the methodology cannot be 'provides' (unverified by "
            "measurement; rho is not transfer evidence)"
        )

    # Construct-adversary path: a broken construction is construct-invalid
    # (methodology absent); an invalid certification caps at partial.
    fq = _frozen_question(tid)
    report = _read_json(_rebuttal_dir(tid) / "construct_adversary_report.json")
    if fq and report and report.get("question_id") == fq.get("question_id"):
        from research_harness.construct_adversary import evaluate_construct_adversary_report
        minb, minw = _construct_adversary_thresholds(settings)
        adv = evaluate_construct_adversary_report(report, min_budget=minb, min_distinct_worlds=minw)
        if adv.get("verdict") == "broken":
            cap = min(cap, 0)
            reasons.append(
                "construct-adversary verdict=broken — the construction is construct-invalid; "
                "methodology is absent"
            )
        elif adv.get("verdict") == "invalid":
            cap = min(cap, 1)
            reasons.append(
                "construct-adversary verdict=invalid — certification unacceptable; "
                "methodology capped at partial"
            )

    if _METHODOLOGY_ORDER[authored] <= cap:
        return decision  # authored already at/below the executed-evidence ceiling

    decision = dict(decision)
    synthesis = dict(decision["rebuttal_synthesis"])
    methodology = dict(synthesis["methodology_assessment"])
    methodology["aggregate_verdict"] = _METHODOLOGY_INV[cap]
    methodology["clamp_reason"] = (
        f"harness clamped aggregate_verdict {authored!r} -> "
        f"{_METHODOLOGY_INV[cap]!r}: " + "; ".join(reasons)
    )
    synthesis["methodology_assessment"] = methodology
    decision["rebuttal_synthesis"] = synthesis
    return decision


_SCOPE_RANK = {"directional": 1, "feasibility": 2, "deployment": 3}


def _claim_deploy_scope(tid: str) -> str | None:
    """Best-effort read of the attested claim's deploy_grade_scope from the
    search_state — promoted node's claim_contract first, then the root's."""
    state = _read_json(_thread_dir(tid) / "production" / "tree" / "search_state.json")
    if not state:
        return None
    nodes = state.get("nodes") or []
    promoted = [n for n in nodes if n.get("status") == "promoted"]
    roots = [n for n in nodes if n.get("parent") in (None, "")]
    for bucket in (promoted, roots, nodes):
        for n in bucket:
            scope = (n.get("claim_contract") or {}).get("deploy_grade_scope")
            if scope in _SCOPE_RANK:
                return scope
    return None


def _compute_scope_attainment(
    tid: str, envelope: dict[str, Any] | None
) -> dict[str, Any]:
    """ADR 0006 incentive integrity: record narrowing relative to the frozen
    seed so a narrowed win is visibly below a full-scope attempt. Enum-only
    keys are omitted when unknown (the schema forbids nulls there)."""
    seed = ((envelope or {}).get("operator_intent") or {}).get("target_deploy_grade_scope")
    attested = _claim_deploy_scope(tid)
    out: dict[str, Any] = {}
    if seed in _SCOPE_RANK:
        out["seed_target_scope"] = seed
    if attested in _SCOPE_RANK:
        out["attested_scope"] = attested
    if seed in _SCOPE_RANK and attested in _SCOPE_RANK:
        out["narrowed"] = _SCOPE_RANK[attested] < _SCOPE_RANK[seed]
    return out


# --- ADR 0007: depth gate (premature-termination is the enemy) ---------- #

_DEFAULT_MIN_DISTINCT_ATTEMPTS = 2
# Nodes that actually ran and produced evidence (vs. proposed/ready/blocked).
_RAN_STATUSES = {
    "completed_worker_report", "critic_reviewed", "orchestrator_reduced",
    "promoted", "needs_child_branch", "pruned",
}


def _premature_termination_gate_enabled(settings: dict[str, Any]) -> bool:
    """ADR 0007: depth gate on the lazy door. Default ON; disabling is a
    loosening (reverts to ADR 0006's unconditional honest-failure exit)."""
    g = _persona_cfg(settings).get("premature_termination_gate")
    if isinstance(g, dict):
        return g.get("enabled", True) is not False
    return True


def _min_distinct_attempts(settings: dict[str, Any]) -> int:
    g = _persona_cfg(settings).get("premature_termination_gate")
    if isinstance(g, dict):
        v = g.get("min_distinct_attempts")
        if isinstance(v, int) and v >= 1:
            return v
    return _DEFAULT_MIN_DISTINCT_ATTEMPTS


def _adversarial_dominance_enabled(settings: dict[str, Any]) -> bool:
    """ADR 0007 lever 0: a grounded critic kill freezes accept. Default ON."""
    g = _persona_cfg(settings).get("adversarial_dominance")
    if isinstance(g, dict):
        return g.get("enabled", True) is not False
    return True


def _node_is_narrowing(node: dict[str, Any], root_scope: str | None) -> bool:
    """ADR 0007: a scope-narrowing relabel is NOT a distinct depth attempt.
    It repackages the same failure smaller. Detected by feasibility_narrowed
    scope_kind, a deploy_grade_scope weaker than the root's, or a
    feasibility-formulation node id."""
    cc = node.get("claim_contract") or {}
    if cc.get("scope_kind") == "feasibility_narrowed":
        return True
    sc = cc.get("deploy_grade_scope")
    if (
        sc in _SCOPE_RANK and root_scope in _SCOPE_RANK
        and _SCOPE_RANK[sc] < _SCOPE_RANK[root_scope]
    ):
        return True
    if "acf_feasibility" in str(node.get("id") or ""):
        return True
    return False


def _node_final_verdict(tid: str, node_id: str) -> str:
    dec = _read_json(
        _thread_dir(tid) / "production" / "tree" / "nodes" / node_id
        / "mcp_professor_decision.json"
    )
    return str((dec or {}).get("final_verdict") or "")


def _investigation_depth(tid: str) -> dict[str, Any]:
    """ADR 0007: measure earned depth, the reward axis. distinct_attempts
    counts genuine attempts (ran nodes that are NOT scope-narrowing relabels)
    plus archived prior root attempts; narrowing pivots are excluded and
    surfaced separately as the visible penalty. killed_hypotheses counts
    load-bearing negatives (contradicted) — a deep negative closes hypothesis
    space."""
    state = _read_json(_thread_dir(tid) / "production" / "tree" / "search_state.json") or {}
    nodes = state.get("nodes") or []
    root_scope: str | None = None
    for r in nodes:
        if r.get("parent") in (None, ""):
            sc = (r.get("claim_contract") or {}).get("deploy_grade_scope")
            if sc in _SCOPE_RANK:
                root_scope = sc
                break
    ran = [n for n in nodes if n.get("status") in _RAN_STATUSES]
    narrowing = [n for n in ran if _node_is_narrowing(n, root_scope)]
    narrowing_ids = {id(n) for n in narrowing}
    distinct = [n for n in ran if id(n) not in narrowing_ids]
    killed = [
        n for n in distinct
        if _node_final_verdict(tid, n.get("id", "")) == "contradicted"
    ]
    archived = [
        p.name for p in _thread_dir(tid).glob("production.attempt_*") if p.is_dir()
    ]
    return {
        "distinct_attempts": len(distinct) + len(archived),
        "tree_distinct_attempts": len(distinct),
        "narrowing_pivots": len(narrowing),
        "killed_hypotheses": len(killed),
        "archived_attempts": len(archived),
    }


def _subtree_node_ids(state: dict[str, Any], root_id: str) -> set[str]:
    """All node ids in root_id's subtree (root + descendants via parent links)."""
    nodes = state.get("nodes") or []
    children: dict[Any, list[str]] = {}
    for n in nodes:
        children.setdefault(n.get("parent"), []).append(n.get("id"))
    out: set[str] = set()
    stack = [root_id]
    while stack:
        nid = stack.pop()
        if nid in out:
            continue
        out.add(nid)
        stack.extend(c for c in children.get(nid, []) if c)
    return out


def _investigation_depth_for_root(tid: str, root_id: str) -> dict[str, Any]:
    """ADR 0012 forest tie-break: per-root subtree depth. Mirrors
    _investigation_depth but restricted to root_id's subtree; archived prior
    attempts are thread-level and do not apply per-root (excluded)."""
    state = _read_json(_thread_dir(tid) / "production" / "tree" / "search_state.json") or {}
    all_nodes = state.get("nodes") or []
    subtree = _subtree_node_ids(state, root_id)
    nodes = [n for n in all_nodes if n.get("id") in subtree]
    rnode = next((n for n in all_nodes if n.get("id") == root_id), None)
    sc = ((rnode or {}).get("claim_contract") or {}).get("deploy_grade_scope")
    root_scope = sc if sc in _SCOPE_RANK else None
    ran = [n for n in nodes if n.get("status") in _RAN_STATUSES]
    narrowing = [n for n in ran if _node_is_narrowing(n, root_scope)]
    narrowing_ids = {id(n) for n in narrowing}
    distinct = [n for n in ran if id(n) not in narrowing_ids]
    killed = [
        n for n in distinct
        if _node_final_verdict(tid, n.get("id", "")) == "contradicted"
    ]
    return {
        "distinct_attempts": len(distinct),
        "tree_distinct_attempts": len(distinct),
        "narrowing_pivots": len(narrowing),
        "killed_hypotheses": len(killed),
        "archived_attempts": 0,
    }


def _forest_survivor_roots(tid: str) -> list[dict[str, Any]]:
    """ADR 0012: forest roots (parent=None) with a promoted node in their
    subtree. Returns [{root_id, promoted_node_id}] — the root itself if
    promoted, else the first promoted descendant (search_state order)."""
    state = _read_json(_thread_dir(tid) / "production" / "tree" / "search_state.json") or {}
    nodes = state.get("nodes") or []
    promoted_list = state.get("promoted_node_ids") or []
    promoted_set = set(promoted_list)
    roots = [n.get("id") for n in nodes if n.get("parent") in (None, "")]
    survivors: list[dict[str, Any]] = []
    for rid in roots:
        sub = _subtree_node_ids(state, rid)
        prom_in = [pid for pid in promoted_list if pid in sub]
        if not prom_in:
            continue
        pnode = rid if rid in promoted_set else prom_in[0]
        survivors.append({"root_id": rid, "promoted_node_id": pnode})
    return survivors


# --- ADR 0007 lever 0: adversarial-dominant aggregation ----------------- #

_MIN_DEFEAT_REBUTTAL_CHARS = 40


def _collect_rebuttal_kills(tid: str) -> list[dict[str, Any]]:
    """A 'kill' is a rebuttal critic that returned verdict_candidate=
    contradicted OR blocking=true. The harness's persona diversity already
    produces these; lever 0 stops them from being reconciled away."""
    reviews_dir = _rebuttal_dir(tid) / "rebuttal_reviews"
    kills: list[dict[str, Any]] = []
    if not reviews_dir.exists():
        return kills
    for p in sorted(reviews_dir.glob("*.json")):
        r = _read_json(p) or {}
        if r.get("verdict_candidate") == "contradicted" or r.get("blocking") is True:
            kills.append({
                "critic_id": r.get("critic_id") or p.stem,
                "verdict_candidate": r.get("verdict_candidate"),
                "blocking": bool(r.get("blocking")),
                "objections": r.get("objections") or [],
            })
    return kills


def _undefeated_kills(
    kills: list[dict[str, Any]], blocking_objections: list[Any] | None
) -> list[dict[str, Any]]:
    """A kill is DEFEATED on merits only when blocking_objections names its
    critic_id with defeated=true AND a substantive defeat_rebuttal. Relabeling
    (scope-narrowing, re-wording) is not defeat — it leaves the kill live."""
    defeated: set[str] = set()
    for bo in blocking_objections or []:
        if not isinstance(bo, dict):
            continue
        cid = bo.get("critic_id")
        rebuttal = str(bo.get("defeat_rebuttal") or "").strip()
        if cid and bo.get("defeated") is True and len(rebuttal) >= _MIN_DEFEAT_REBUTTAL_CHARS:
            defeated.add(cid)
    return [k for k in kills if k["critic_id"] not in defeated]


def _has_real_data_adapter(tid: str) -> bool | None:
    """True if envelope declares at least one real_adapter data source.
    None if envelope is missing (signal not informative)."""
    envelope = _read_json(_thread_dir(tid) / "production" / "feasibility_envelope.json")
    if not envelope:
        return None
    sources = envelope.get("data_sources_available") or []
    for src in sources:
        if isinstance(src, dict) and src.get("kind") == "real_adapter":
            return True
    return False


def _check_user_goal_anchor_bindings(
    tid: str, worker_report: dict[str, Any]
) -> dict[str, list[Any]]:
    """Rail 3: verify grilling-time user_goal_anchors against worker_report.metrics.

    Returns {
        "unbound": [anchor_text, ...],            # must_be_measured AND bound_metric_key is None
        "unmeasured": [(anchor_text, key), ...],  # bound key absent/null in metrics
    }
    Unbound drives confidence down-clamp (soft signal). Unmeasured drives a
    hard accept-block — operator committed to a measurement that the
    experiment never produced.
    """
    grilling = _read_json(_thread_dir(tid) / "grilling" / "grilling_session.json") or {}
    anchors = (grilling.get("extracted") or {}).get("user_goal_anchors") or []
    if not anchors:
        return {"unbound": [], "unmeasured": []}
    metrics = (worker_report or {}).get("metrics") or {}
    unbound: list[str] = []
    unmeasured: list[tuple[str, str]] = []
    for anchor in anchors:
        if not isinstance(anchor, dict) or not anchor.get("must_be_measured"):
            continue
        bound = anchor.get("bound_metric_key")
        text = str(anchor.get("anchor_text") or "")
        if not bound:
            unbound.append(text)
            continue
        if bound not in metrics or metrics.get(bound) is None:
            unmeasured.append((text, str(bound)))
    return {"unbound": unbound, "unmeasured": unmeasured}


def _compute_ac_downclamp_signals(
    tid: str,
    successor_agg: dict[str, Any],
    anchor_check: dict[str, list[Any]] | None = None,
) -> list[str]:
    """Collect signals that justify auto-clamping AC confidence to 'low'."""
    reasons: list[str] = []
    if successor_agg["evaluated"] > 0:
        ratio = successor_agg["negative_count"] / successor_agg["evaluated"]
        if ratio >= 0.5:
            reasons.append(
                f"successor_negative_ratio={successor_agg['negative_count']}/{successor_agg['evaluated']}>=0.5"
            )
    real_adapter = _has_real_data_adapter(tid)
    if real_adapter is False:
        reasons.append("feasibility_envelope.data_sources_available has no real_adapter (synthetic-only)")
    bd_path = _thread_dir(tid) / "market" / "baseline_dossier_candidate.yaml"
    dump_signals = _detect_deterministic_dump_dossier(bd_path)
    if dump_signals:
        reasons.append("baseline_dossier_substance=deterministic_dump: " + "; ".join(dump_signals))
    if anchor_check and anchor_check.get("unbound"):
        truncated = anchor_check["unbound"][:5]
        suffix = "" if len(anchor_check["unbound"]) <= 5 else f" (+{len(anchor_check['unbound']) - 5} more)"
        reasons.append(
            "user_goal_anchors_unbound: " + " | ".join(str(t) for t in truncated) + suffix
        )
    return reasons


def _detect_must_revise_root_signal(state: dict[str, Any]) -> dict[str, Any] | None:
    """Rail 5: detect when the deepest promoted node has collapsed branches.

    Triggers when a promoted node has >=3 negative direct children (status pruned
    AND final_verdict in NEGATIVE set, OR status pruned with no decision-file
    counted via status alone) and 0 promoted children. Returns dict with the
    promoted_node_id + alternative root candidates already in the tree, or None.
    """
    nodes = state.get("nodes") or []
    if not nodes:
        return None
    by_id = {n["id"]: n for n in nodes}

    # Find promoted leaves (promoted nodes with no promoted children).
    promoted_ids = {n["id"] for n in nodes if n.get("status") == "promoted"}
    if not promoted_ids:
        return None
    promoted_with_promoted_child: set[str] = set()
    for n in nodes:
        if n.get("status") == "promoted" and n.get("parent") in promoted_ids:
            promoted_with_promoted_child.add(n["parent"])
    promoted_leaves = promoted_ids - promoted_with_promoted_child

    for leaf_id in promoted_leaves:
        children = [n for n in nodes if n.get("parent") == leaf_id]
        if not children:
            continue
        negative_children = [
            n["id"] for n in children
            if n.get("status") == "pruned"
        ]
        promoted_children = [n["id"] for n in children if n.get("status") == "promoted"]
        if promoted_children or len(negative_children) < 3:
            continue
        # Surface dangling alternative roots: sibling drafts of leaf_id whose
        # status is queued/pruned but were never tried — operator may want to
        # pick them up via revise_root_after_reject.
        parent_id = by_id[leaf_id].get("parent")
        siblings = [
            n["id"] for n in nodes
            if n.get("parent") == parent_id and n["id"] != leaf_id
        ] if parent_id else []
        return {
            "promoted_node_id": leaf_id,
            "negative_children": negative_children,
            "alternative_root_candidates": siblings,
        }
    return None


# Map of tool_name → handler used by the inline auto-dispatcher. Kept narrow
# on purpose: only rails whose auto-action is genuinely "fire and continue"
# should be listed here. Adding a new entry implies operator-grade trust that
# the action is non-destructive and idempotent within chain_safety_check.
def _auto_dispatchable_handlers() -> dict[str, Any]:
    return {
        "seed_alternative_root_formulation": handle_seed_alternative_root_formulation,
    }


def _escalate_to_operator(
    tid: str,
    suggestion: Any,
    *,
    refusal_reason: str,
) -> dict[str, Any]:
    """Enqueue an operator prompt summarizing a refused auto-action so the
    frontend can render it and let the operator decide what to do next.

    Returns a small dict the caller can attach to the response under
    ``operator_prompt`` so the LLM driving the session knows where to poll.
    """
    from research_harness.orchestrator.operator_prompts import enqueue_prompt

    prompt_text = (
        f"Auto-resolver refused to dispatch {suggestion.tool!r} "
        f"(source: {suggestion.source_rail}). Reason: {refusal_reason}. "
        f"Rationale that triggered the suggestion: {suggestion.rationale}"
    )
    # Stable event_id so retries don't pile up duplicate prompts for the
    # same suggestion+refusal combination.
    event_id = f"opr_esc_{suggestion.source_rail}_{abs(hash(refusal_reason)) & 0xFFFFFFFF:08x}"
    try:
        record = enqueue_prompt(
            _thread_dir(tid),
            kind="auto_resolver_escalation",
            prompt=prompt_text,
            options=[
                "dispatch_anyway",
                "abandon_and_revise_root",
                "render_honest_failure_paper",
            ],
            source_rail=suggestion.source_rail,
            event_id=event_id,
        )
    except ValueError:
        # Idempotent: if a prompt for this exact (source_rail, refusal)
        # already exists, surface the same event_id without duplicating.
        return {"event_id": event_id, "status": "already_enqueued"}
    return {
        "event_id": record["event_id"],
        "status": "enqueued",
        "next_step": (
            "Poll get_pending_operator_response with this event_id to consume "
            "the operator's reply once it lands."
        ),
    }


def handle_enqueue_operator_prompt(args: dict[str, Any]) -> dict[str, Any]:
    """MCP-side wrapper around operator_prompts.enqueue_prompt."""
    from research_harness.orchestrator.operator_prompts import enqueue_prompt

    tid = args["thread_id"]
    try:
        record = enqueue_prompt(
            _thread_dir(tid),
            kind=args["kind"],
            prompt=args["prompt"],
            options=args.get("options"),
            source_rail=args.get("source_rail"),
            event_id=args.get("event_id"),
        )
    except ValueError as exc:
        return {"status": "rejected", "reason": str(exc)}
    return {
        "status": "ok",
        "event_id": record["event_id"],
        "next_step": (
            "Poll get_pending_operator_response to consume the operator's "
            "reply once it lands."
        ),
    }


def handle_get_pending_operator_response(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.orchestrator.operator_prompts import take_pending_response

    tid = args["thread_id"]
    record = take_pending_response(_thread_dir(tid), event_id=args.get("event_id"))
    if record is None:
        return {"status": "empty"}
    return {
        "status": "ok",
        "event_id": record["event_id"],
        "kind": record["kind"],
        "prompt": record["prompt"],
        "response": record["response"],
        "source_rail": record.get("source_rail"),
        "created_at": record["created_at"],
        "responded_at": record.get("responded_at"),
    }


def _maybe_auto_dispatch(tid: str, response: dict[str, Any]) -> dict[str, Any]:
    """If response carries an auto_action_suggestion that passes safety, run it.

    Mutates the response by appending an ``auto_resolved`` field describing
    what was dispatched (or refused, with the safety reason). The original
    detection fields (status, reason, etc.) are preserved so the audit trail
    of why the auto-action fired is never lost.
    """
    from research_harness.orchestrator.auto_resolver import (
        chain_safety_check, pick_auto_action, record_auto_action,
    )

    suggestion = pick_auto_action(response)
    if suggestion is None:
        return response
    handlers = _auto_dispatchable_handlers()
    handler = handlers.get(suggestion.tool)
    if handler is None:
        response["auto_resolved"] = {
            "dispatched": False,
            "reason": f"tool {suggestion.tool!r} not in auto-dispatchable allowlist",
        }
        record_auto_action(
            _thread_dir(tid), suggestion,
            outcome="refused_by_safety_check",
            refusal_reason="not in auto-dispatchable allowlist",
        )
        response["operator_prompt"] = _escalate_to_operator(
            tid, suggestion,
            refusal_reason=f"tool {suggestion.tool!r} not in auto-dispatchable allowlist",
        )
        return response
    verdict = chain_safety_check(_thread_dir(tid), suggestion)
    if not verdict.ok:
        response["auto_resolved"] = {
            "dispatched": False,
            "reason": verdict.reason,
            "history_length": verdict.history_length,
        }
        record_auto_action(
            _thread_dir(tid), suggestion,
            outcome="refused_by_safety_check",
            refusal_reason=verdict.reason,
        )
        response["operator_prompt"] = _escalate_to_operator(
            tid, suggestion, refusal_reason=verdict.reason,
        )
        return response
    try:
        chained = handler(suggestion.args)
    except Exception as exc:  # noqa: BLE001
        response["auto_resolved"] = {
            "dispatched": True,
            "tool": suggestion.tool,
            "error": f"{type(exc).__name__}: {exc}",
        }
        record_auto_action(
            _thread_dir(tid), suggestion,
            outcome="dispatch_error",
            refusal_reason=str(exc),
        )
        response["operator_prompt"] = _escalate_to_operator(
            tid, suggestion, refusal_reason=f"dispatch error: {exc}",
        )
        return response
    response["auto_resolved"] = {
        "dispatched": True,
        "tool": suggestion.tool,
        "args": suggestion.args,
        "rationale": suggestion.rationale,
        "source_rail": suggestion.source_rail,
        "result": chained,
    }
    record_auto_action(
        _thread_dir(tid), suggestion,
        outcome="ok" if chained.get("status") == "ok" else "rejected",
        dispatch_result=chained,
    )
    return response


def handle_seed_alternative_root_formulation(args: dict[str, Any]) -> dict[str, Any]:
    """Multi-root: add a second/third root from grilling.alternative_claim_formulations.

    The current root is untouched — both run in parallel via the same
    search_state. seed_drafts_from_root by default fires for the new root only
    (it's idempotent against the existing root's already-populated children).
    """
    from research_harness.orchestrator.root_node_from_grilling import (
        build_root_node_from_grilling,
    )
    from research_harness.orchestrator.search_state import (
        search_policy_from_config,
    )
    from research_harness.orchestrator.treesearch.drafts import seed_drafts_from_root
    from research_harness.orchestrator.validation import validate_node_invariants
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    formulation_id = args["formulation_id"]
    seed_drafts_flag = args.get("seed_drafts", True)

    grilling = _read_json(_thread_dir(tid) / "grilling" / "grilling_session.json")
    if not grilling:
        return {"status": "rejected", "reason": "grilling_session.json missing — run grilling first"}
    formulations = (grilling.get("extracted") or {}).get("alternative_claim_formulations") or []
    chosen = next(
        (f for f in formulations if isinstance(f, dict) and f.get("formulation_id") == formulation_id),
        None,
    )
    if not chosen:
        available = [f.get("formulation_id") for f in formulations if isinstance(f, dict)]
        return {
            "status": "rejected",
            "reason": f"formulation_id {formulation_id!r} not found in grilling.alternative_claim_formulations. Available: {available}",
        }

    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path)
    if not state or not state.get("nodes"):
        return {
            "status": "rejected",
            "reason": "search_state.json missing — design_initial_claim_contract must run first to seed the primary root",
        }

    market_brief = _read_json(_thread_dir(tid) / "market" / "market_research_brief.json") or {}
    baseline_dossier_id = market_brief.get("baseline_dossier_id") or "bd_pending_market_research"
    candidate_ids: list[str] = []
    raw_candidates = market_brief.get("baseline_dossier_candidates_index")
    if isinstance(raw_candidates, list):
        candidate_ids = [c["id"] for c in raw_candidates if isinstance(c, dict) and c.get("id")]

    # Build a new root node with the formulation's claim overriding grilling's.
    new_root = build_root_node_from_grilling(
        grilling,
        baseline_dossier_id=baseline_dossier_id,
        candidate_ids=candidate_ids,
        node_id_suffix=f"root_{formulation_id}",
    )
    new_root["claim_contract"]["claim_under_test"] = chosen["claim_under_test"]
    new_root["lineage"]["introduced_assumptions"].append(
        f"alternative_claim_formulation: {formulation_id} (scope_kind={chosen.get('scope_kind')})"
    )
    validate_node_invariants(new_root)
    validate_named_schema("node", new_root)

    if any(n["id"] == new_root["id"] for n in state["nodes"]):
        return {
            "status": "rejected",
            "reason": f"root with id {new_root['id']} already exists in search_state — formulation has already been seeded",
        }

    state["nodes"].append(new_root)
    state["frontier"].append({
        "node_id": new_root["id"], "parent": None, "depth": 0, "priority": 1.0,
        "stage": new_root.get("stage", "promotion"), "status": "queued",
        "reason": f"alternative root from formulation {formulation_id}",
    })
    state["transitions"].append({
        "node_id": new_root["id"], "from_status": "ready", "to_status": "ready",
        "event": "seed_alternative_root", "reason": f"formulation_id={formulation_id}",
        "created_child_ids": [],
    })

    draft_ids: list[str] = []
    if seed_drafts_flag:
        policy = search_policy_from_config(_repo_root())
        draft_ids = seed_drafts_from_root(
            state,
            num_drafts=int(policy["num_drafts"]),
            max_depth=int(policy["max_depth"]),
            root_id=new_root["id"],
        )

    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "status": "ok",
        "new_root_id": new_root["id"],
        "formulation_id": formulation_id,
        "scope_kind": chosen.get("scope_kind"),
        "seeded_draft_ids": draft_ids,
        "next_step": (
            "Call get_next_admissible_node — the new root + drafts are now in the frontier. "
            "Both the original root tree and this one will be expanded in parallel."
        ),
    }


def handle_seed_forest_from_connector(args: dict[str, Any]) -> dict[str, Any]:
    """ADR 0012 connector->production handoff.

    Reads the connector_session's kept claim_contracts and seeds a MULTI-ROOT
    forest search_state (each claim = a coexisting parent=null root, draft-seeded
    via the existing scaffold). Replaces design_initial_claim_contract for the
    multi-root path. Refuses if production is already seeded; 0 claims ->
    no_claims (the production phase then renders honest-failure).
    """
    from research_harness.connector.forest import build_forest_search_state
    from research_harness.orchestrator.search_state import search_policy_from_config

    tid = args["thread_id"]
    grilling = _read_json(_thread_dir(tid) / "grilling" / "grilling_session.json")
    if not grilling:
        return {"status": "rejected", "reason": "grilling_session.json missing — run grilling first"}
    connector = _read_json(_thread_dir(tid) / "connector" / "connector_session.json")
    if not connector:
        return {
            "status": "rejected",
            "reason": "connector_session.json missing — run the connector phase first",
        }

    conn_status = connector.get("status")
    claims = connector.get("claims") or []
    if conn_status in {"blocked_by_gate", "blocked_by_execution_ack", "aborted", "in_progress"}:
        return {"status": "rejected", "reason": f"connector did not complete (status={conn_status!r})"}
    if not claims:
        return {
            "status": "no_claims",
            "reason": "connector produced 0 claims (completed_no_claims) — no forest to seed",
            "next_step": "render_honest_failure_paper: the connector found no reducible far-framing.",
        }

    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    if state_path.exists():
        return {
            "status": "rejected",
            "reason": "production already seeded (search_state.json exists) — refusing to clobber",
        }

    policy = search_policy_from_config(_repo_root())
    state = build_forest_search_state(grilling, claims, search_id=f"s_{tid}", policy=policy)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    root_ids = [n["id"] for n in state["nodes"] if n.get("parent") is None]
    return {
        "status": "ok",
        "num_roots": len(root_ids),
        "root_ids": root_ids,
        "search_state_path": str(state_path),
        "next_step": (
            "Call get_next_admissible_node — the forest's N roots + their drafts are in "
            "the frontier. The existing per-node gate runs every tree; at the end, "
            "select_strongest_survivor picks the single earned output (per-survivor terminal)."
        ),
    }


def handle_snapshot_root_terminal(args: dict[str, Any]) -> dict[str, Any]:
    """ADR 0012 forest (snapshot-and-reset): after a survivor root's full terminal
    has run flat under production/rebuttal/, snapshot those artifacts into
    production/rebuttal/<root_id>/ and RESET the flat dir so the next root's
    terminal starts clean. select_strongest_survivor then reads the per-root
    snapshots. Single-root threads never call this (they keep the flat path)."""
    tid = args["thread_id"]
    root_id = args["root_id"]
    rebuttal = _rebuttal_dir(tid)
    dest = rebuttal / root_id
    copied = _copy_terminal_artifacts(rebuttal, dest)
    if not copied:
        return {
            "status": "rejected",
            "reason": (
                "no flat terminal artifacts under production/rebuttal/ to snapshot — "
                "run this root's terminal first (compute_falsifier_result -> "
                "submit_construct_adversary_report -> ... -> "
                "submit_professor_user_goal_attestation)."
            ),
        }
    _clear_terminal_artifacts(rebuttal)
    return {
        "status": "ok",
        "root_id": root_id,
        "snapshot_dir": str(dest),
        "snapshotted": copied,
        "reset": True,
        "next_step": (
            "Run the next survivor root's terminal (flat) + snapshot_root_terminal "
            "again. When every survivor is snapshotted, call select_strongest_survivor."
        ),
    }


def handle_select_strongest_survivor(args: dict[str, Any]) -> dict[str, Any]:
    """ADR 0012 forest terminal: pick the single strongest-earned survivor.

    Reads each survivor root's per-root attestation (verdict_strength) + per-root
    investigation_depth, ranks via connector.select.select_strongest, records
    forest_selection.json, and returns the winner (the production then renders
    the winner's paper). 0 survivors -> honest_failure. If a survivor lacks its
    per-root attestation, returns incomplete (run that root's terminal first).
    The single-root legacy path is untouched — this is only for the connector
    forest (>1 coexisting root).
    """
    from research_harness.connector.select import select_strongest

    tid = args["thread_id"]
    survivors = _forest_survivor_roots(tid)
    if not survivors:
        return {
            "status": "honest_failure",
            "reason": "no forest survivor reached a promoted terminal",
            "next_step": "render_honest_failure_paper — no claim earned standing.",
        }

    candidates: list[dict[str, Any]] = []
    pending: list[str] = []
    for s in survivors:
        rid = s["root_id"]
        att = _read_json(_rebuttal_dir(tid) / rid / "user_goal_attestation.json")
        if not att:
            pending.append(rid)
            continue
        depth = _investigation_depth_for_root(tid, rid)
        candidates.append({
            "node_id": rid,
            "promoted_node_id": s["promoted_node_id"],
            "verdict_strength": att.get("verdict_strength"),
            "distinct_attempts": depth["distinct_attempts"],
        })

    if pending:
        return {
            "status": "incomplete",
            "pending_roots": pending,
            "reason": "these survivor roots have no per-root attestation yet",
            "next_step": (
                "Run the per-survivor terminal (prepare_rebuttal_packet -> ... -> "
                "submit_professor_user_goal_attestation with promoted_node_id=<root>) "
                "for each pending root, then call select_strongest_survivor again."
            ),
        }

    result = select_strongest(candidates)
    winner = result["winner"]
    selection = {
        "selected_root_id": winner["node_id"] if winner else None,
        "selected_promoted_node_id": winner["promoted_node_id"] if winner else None,
        "verdict_strength": winner["verdict_strength"] if winner else None,
        "ranking": result["ranking"],
        "num_survivors": len(survivors),
    }
    sel_path = _thread_dir(tid) / "production" / "tree" / "forest_selection.json"
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    sel_path.write_text(json.dumps(selection, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Restore the winner's snapshotted terminal to the flat rebuttal/ dir so
    # render_final_paper (which reads flat, unchanged) renders the winner. The
    # winner's snapshot subdir is left intact for audit.
    if winner:
        rebuttal = _rebuttal_dir(tid)
        _clear_terminal_artifacts(rebuttal)
        _copy_terminal_artifacts(rebuttal / winner["node_id"], rebuttal)
    return {
        "status": "ok",
        "selected_root_id": selection["selected_root_id"],
        "selected_promoted_node_id": selection["selected_promoted_node_id"],
        "verdict_strength": selection["verdict_strength"],
        "ranking": selection["ranking"],
        "next_step": (
            "render_final_paper for the winner (promoted_node_id="
            f"{selection['selected_promoted_node_id']}). The other survivors are "
            "search by-products — there is ONE output."
        ),
    }


def handle_submit_ac_decision(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    decision = args["ac_decision"]

    # Require orchestrator_reduction to exist.
    reduction = _read_json(_rebuttal_dir(tid) / "orchestrator_reduction.json")
    if not reduction:
        return {"status": "rejected", "reason": "submit_orchestrator_reduction must run before submit_ac_decision"}

    try:
        validate_named_schema("ac_decision", decision)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    # Extra guard: even on accept, camera_ready_directives must be non-empty.
    if not decision.get("camera_ready_directives"):
        return {
            "status": "rejected",
            "reason": "camera_ready_directives must be non-empty even on accept. "
                      "If the rebuttal truly added nothing, name the scope-sharpening it made explicit.",
        }

    # --- ADR 0007 lever 0: adversarial-dominant aggregation. -------------
    # A grounded critic kill that the orchestrator_reduction did not defeat on
    # merits blocks accept outright — the AC cannot accept over a live kill.
    from research_harness.config import load_settings as _ls_ac
    if decision.get("decision") == "accept" and _adversarial_dominance_enabled(_ls_ac(_repo_root())):
        kills = _collect_rebuttal_kills(tid)
        undefeated = _undefeated_kills(kills, reduction.get("blocking_objections"))
        if undefeated:
            ids = ", ".join(k["critic_id"] for k in undefeated)
            return {
                "status": "rejected",
                "reason": (
                    f"accept blocked by adversarial-dominance: critic(s) [{ids}] "
                    "returned an undefeated kill (contradicted / blocking). Defeat "
                    "each on merits in the orchestrator_reduction's "
                    "blocking_objections (critic_id + defeated=true + substantive "
                    "defeat_rebuttal) before accepting, or downgrade the decision. "
                    "Reconciling the kill into the synthesis is exactly the "
                    "diversity-throwing-away this gate prevents."
                ),
                "undefeated_kills": [k["critic_id"] for k in undefeated],
            }

    # --- Rail 1: successor-verdict aggregator + hard accept gate. --------
    # Block accept when the promoted root's direct children largely failed.
    successor_agg = {"total_children": 0, "evaluated": 0, "negative_count": 0,
                     "negative_children": [], "blocking_children": []}
    try:
        promoted_ctx = _resolve_promoted_node(tid)
        successor_agg = _aggregate_successor_verdicts(tid, promoted_ctx["promoted_id"])
    except (OSError, ValueError):
        # Tree state missing — fall through; schema gate already ran.
        pass
    if decision.get("decision") == "accept" and successor_agg["evaluated"] >= 2:
        ratio = successor_agg["negative_count"] / successor_agg["evaluated"]
        if ratio >= 2 / 3:
            # Hands-free: if an unseeded alternative_claim_formulation is
            # available, attach an auto_action_suggestion so the auto-resolver
            # can pivot to it without operator selection.
            response: dict[str, Any] = {
                "status": "rejected",
                "reason": (
                    f"accept blocked by successor-verdict rail: "
                    f"{successor_agg['negative_count']}/{successor_agg['evaluated']} direct children of the promoted root "
                    f"returned negative verdicts {successor_agg['negative_children']}. "
                    f"Either downgrade to revise_with_new_measurements / reject_and_diversify, "
                    f"or call revise_root_after_reject to swap the root before re-attempting accept."
                ),
            }
            grilling = _read_json(_thread_dir(tid) / "grilling" / "grilling_session.json") or {}
            formulations = (grilling.get("extracted") or {}).get("alternative_claim_formulations") or []
            try:
                state_for_ids = _read_json(
                    _thread_dir(tid) / "production" / "tree" / "search_state.json"
                ) or {}
                existing_root_ids = {n["id"] for n in state_for_ids.get("nodes", []) if n.get("parent") is None}
            except (OSError, ValueError):
                existing_root_ids = set()
            unseeded = [
                f for f in formulations
                if isinstance(f, dict)
                and not any(rid.endswith(f"_root_{f.get('formulation_id')}") for rid in existing_root_ids)
            ]
            if unseeded:
                ranked = sorted(
                    unseeded,
                    key=lambda f: (f.get("ranked_priority") or 99, f.get("formulation_id") or ""),
                )
                top = ranked[0]
                response["auto_action_suggestion"] = {
                    "tool": "seed_alternative_root_formulation",
                    "args": {"thread_id": tid, "formulation_id": top["formulation_id"]},
                    "source_rail": "rail_1_accept_block_two_thirds_negative",
                    "rationale": (
                        f"accept blocked because {successor_agg['negative_count']}/"
                        f"{successor_agg['evaluated']} successors negative; unseeded "
                        f"formulation {top['formulation_id']!r} ({top.get('scope_kind')}) "
                        f"is next-priority alternative — pivot instead of revising the dead root."
                    ),
                    "confidence": "high",
                }
                response = _maybe_auto_dispatch(tid, response)
            return response
        if successor_agg["blocking_children"]:
            return {
                "status": "rejected",
                "reason": (
                    f"accept blocked: successor(s) {successor_agg['blocking_children']} returned blocking verdicts "
                    f"(confounded_or_not_evaluable / blocked_by_operational_issue). "
                    f"Resolve or branch around the operational issue before accepting."
                ),
            }

    # --- Rail 3: user_goal anchor binding check. -------------------------
    # If the operator explicitly bound an anchor to a metric key, that key
    # MUST exist with a non-null value in the promoted node's worker_report
    # — otherwise the accept is silently overclaiming on the user's
    # original intent (the thread_e5b277f9 failure mode).
    anchor_check = {"unbound": [], "unmeasured": []}
    try:
        if successor_agg["total_children"] >= 0:  # i.e. we resolved a promoted node
            promoted_ctx = _resolve_promoted_node(tid)
            wr_path = (
                _thread_dir(tid) / "production" / "tree" / "nodes"
                / promoted_ctx["promoted_id"] / "worker_report.json"
            )
            worker_report = _read_json(wr_path) or {}
            anchor_check = _check_user_goal_anchor_bindings(tid, worker_report)
    except (OSError, ValueError):
        pass
    if decision.get("decision") == "accept" and anchor_check["unmeasured"]:
        return {
            "status": "rejected",
            "reason": (
                "accept blocked by user_goal anchor-binding rail: "
                + "; ".join(
                    f"anchor {a!r} bound to metric_key {k!r} but worker_report.metrics has no measured value"
                    for a, k in anchor_check["unmeasured"]
                )
                + ". Either run the missing measurement or unbind the anchor (must_be_measured=false) "
                + "with explicit scope-narrowing in a camera-ready directive."
            ),
        }

    # Anti-laziness: at least one directive must demand a new measurement
    # (not just paper-text disclaimers) when AC chooses revise. Accept can
    # be all-disclaimer in principle but in practice the rebuttal usually
    # surfaces something measurable.
    if decision.get("decision") == "revise":
        try:
            from research_harness.config import load_settings as _ls
            cfg = _persona_cfg(_ls(_repo_root()))
        except (OSError, ValueError):
            cfg = {}
        dir_check = validate_camera_ready_directives(
            directives=decision.get("camera_ready_directives") or [],
            config=cfg,
        )
        if not dir_check.ok:
            return {"status": "rejected", "reason": dir_check.reject_message()}

    # --- Rail 4: confidence down-clamp based on uncertainty signals. -----
    downclamp_reasons = _compute_ac_downclamp_signals(tid, successor_agg, anchor_check)
    if downclamp_reasons:
        decision = dict(decision)
        decision["confidence"] = "low"
        decision["confidence_downclamp_reasons"] = downclamp_reasons

    # --- ADR 0008 rev.2 (A3): methodology inherits the weaker verification path.
    from research_harness.config import load_settings as _ls_clamp
    decision = _clamp_methodology_assessment(tid, _ls_clamp(_repo_root()), decision)

    (_rebuttal_dir(tid) / "ac_decision.json").write_text(
        json.dumps(decision, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    next_step = (
        "Call submit_camera_ready_revision next — the Professor must address every directive."
        if decision["decision"] in {"accept", "revise"}
        else "AC rejected. Call revise_root_after_reject to propose a stronger claim."
    )
    return {"status": "ok", "decision": decision["decision"], "next_step": next_step}


def handle_submit_camera_ready_revision(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    revision = args["camera_ready_revision"]
    ac = _read_json(_rebuttal_dir(tid) / "ac_decision.json")
    if not ac:
        return {"status": "rejected", "reason": "submit_ac_decision must run before submit_camera_ready_revision"}
    if ac.get("decision") not in {"accept", "revise"}:
        return {"status": "rejected", "reason": f"camera-ready is not applicable when AC decision is {ac.get('decision')!r}"}

    try:
        validate_named_schema("camera_ready_revision", revision)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    # Every directive must be addressed by index.
    directives = ac.get("camera_ready_directives", [])
    addressed = {r.get("directive_index") for r in revision.get("responses_to_directives", [])}
    missing = sorted(set(range(len(directives))) - addressed)
    if missing:
        return {
            "status": "rejected",
            "reason": f"every camera_ready_directive must be addressed. missing indices: {missing}",
        }

    (_rebuttal_dir(tid) / "camera_ready_revision.json").write_text(
        json.dumps(revision, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "next_step": "Call prepare_paper_writing_context, then submit_paper_outline → register_paper_figure → submit_paper_section → render_final_paper.",
    }


# --- Paper writer tools (Phase D) ---------------------------------------- #


def _paper_dir(tid: str) -> Path:
    return _publication_dir(tid) / "_drafts"


def handle_prepare_paper_writing_context(args: dict[str, Any]) -> dict[str, Any]:
    tid = args["thread_id"]
    ctx = _resolve_promoted_node(tid)
    rebuttal_dir = _rebuttal_dir(tid)
    reviews_dir = rebuttal_dir / "rebuttal_reviews"
    reviews = []
    if reviews_dir.exists():
        for p in sorted(reviews_dir.glob("*.json")):
            reviews.append(_read_json(p))
    ac = _read_json(rebuttal_dir / "ac_decision.json")
    revision = _read_json(rebuttal_dir / "camera_ready_revision.json")
    reduction = _read_json(rebuttal_dir / "orchestrator_reduction.json")
    if not (ac and revision and reduction and reviews):
        return {
            "status": "rejected",
            "reason": "paper writing requires AC decision, camera-ready revision, orchestrator reduction, "
                      "and at least one rebuttal review — finish the rebuttal flow first.",
        }
    node_dir = _thread_dir(tid) / "production" / "tree" / "nodes" / ctx["promoted_id"]
    worker_report = _read_json(node_dir / "worker_report.json") or {}

    # Surface keys available for figure data_spec lookups.
    metric_keys = sorted((worker_report.get("metrics") or {}).keys())
    baseline_keys = sorted((worker_report.get("baselines") or {}).keys())

    # PR4: surface cumulative memory (prior failures + active lessons) so
    # the paper writer can cite them in related work / limitations.
    repo = _repo_root()
    active_lessons: list[dict[str, Any]] = []
    try:
        from research_harness.config import load_lessons
        active_lessons = (load_lessons(repo) or {}).get("active_lessons", []) or []
    except Exception:  # noqa: BLE001
        active_lessons = []

    return {
        "status": "ok",
        "thread_id": tid,
        "promoted_node_id": ctx["promoted_id"],
        "node": ctx["node"],
        "worker_report": worker_report,
        "rebuttal_reviews": reviews,
        "orchestrator_reduction": reduction,
        "ac_decision": ac,
        "camera_ready_revision": revision,
        "mental_model_statement": revision.get("mental_model_statement"),
        "active_lessons": active_lessons,
        "available_metric_keys": metric_keys,
        "available_baseline_keys": baseline_keys,
        "supported_figure_types": [
            "loco_heatmap", "baseline_bars", "ablation_drops",
            "lift_ci_forest", "score_radar", "claim_tree_status", "metric_table",
        ],
    }


def handle_submit_paper_outline(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.schemas.validator import validate_named_schema
    tid = args["thread_id"]
    outline = args["outline"]
    try:
        validate_named_schema("paper_outline", outline)
        for spec in outline.get("figure_specs", []):
            validate_named_schema("paper_figure_request", spec)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    paper_dir = _paper_dir(tid)
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "outline.json").write_text(
        json.dumps(outline, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "section_ids_expected": [s["section_id"] for s in outline["section_outline"]],
        "figure_ids_expected": [f["figure_id"] for f in outline["figure_specs"]],
        "next_step": "Call register_paper_figure for each figure_spec, then submit_paper_section per section.",
    }


def handle_register_paper_figure(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.schemas.validator import validate_named_schema
    from research_harness.publishing.figures import render_figure, FigureRenderError

    tid = args["thread_id"]
    req = args["figure_request"]
    try:
        validate_named_schema("paper_figure_request", req)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    outline = _read_json(_paper_dir(tid) / "outline.json") or {}
    expected_ids = {f["figure_id"] for f in outline.get("figure_specs", [])}
    if expected_ids and req["figure_id"] not in expected_ids:
        return {
            "status": "rejected",
            "reason": f"figure_id {req['figure_id']!r} not in submitted outline. expected: {sorted(expected_ids)}",
        }

    ctx = _resolve_promoted_node(tid)
    node_dir = _thread_dir(tid) / "production" / "tree" / "nodes" / ctx["promoted_id"]
    worker_report = _read_json(node_dir / "worker_report.json") or {}
    ac = _read_json(_rebuttal_dir(tid) / "ac_decision.json") or {}

    figures_dir = _figures_dir(tid)
    figures_dir.mkdir(parents=True, exist_ok=True)
    try:
        artifact_path = render_figure(
            figure_id=req["figure_id"],
            figure_type=req["figure_type"],
            caption=req["caption"],
            data_spec=req.get("data_spec", {}),
            worker_report=worker_report,
            ac_decision=ac,
            output_dir=figures_dir,
        )
    except FigureRenderError as exc:
        return {"status": "rejected", "reason": f"figure render failed: {exc}"}

    # Record registration.
    registry_path = _paper_dir(tid) / "figures.json"
    registry = _read_json(registry_path) or {}
    registry[req["figure_id"]] = {**req, "artifact_path": str(artifact_path)}
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return {
        "status": "ok",
        "figure_id": req["figure_id"],
        "artifact_path": str(artifact_path),
        "embed_html": f"<figure><img src='figures/{req['figure_id']}.png' alt='{req.get('alt_text') or req['caption']}'><figcaption>{req['caption']}</figcaption></figure>",
    }


def handle_submit_paper_section(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.schemas.validator import validate_named_schema
    tid = args["thread_id"]
    section = args["section"]
    try:
        validate_named_schema("paper_section", section)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    outline = _read_json(_paper_dir(tid) / "outline.json")
    if not outline:
        return {"status": "rejected", "reason": "submit_paper_outline must run before submit_paper_section"}
    expected = {s["section_id"] for s in outline["section_outline"]}
    if section["section_id"] not in expected:
        return {
            "status": "rejected",
            "reason": f"section_id {section['section_id']!r} not in outline. expected: {sorted(expected)}",
        }
    # Method / experiments / discussion sections require evidence anchors.
    if section["section_id"] in {"method", "experiments", "discussion"} and not section.get("evidence_anchors"):
        return {
            "status": "rejected",
            "reason": f"section {section['section_id']!r} requires non-empty evidence_anchors",
        }

    sections_dir = _paper_dir(tid) / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    (sections_dir / f"{section['section_id']}.json").write_text(
        json.dumps(section, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    submitted = sorted(p.stem for p in sections_dir.glob("*.json"))
    pending = sorted(expected - set(submitted))
    return {
        "status": "ok",
        "submitted_so_far": submitted,
        "pending_section_ids": pending,
        "next_step": "Continue with next section" if pending else "All sections submitted — call render_final_paper.",
    }


def _attemptable_in_envelope(
    envelope: dict[str, Any], item: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]]]:
    """ADR 0007 rev.2 (A2): DETERMINISTIC attemptability — every declared
    required_resource must be present in the feasibility_envelope. Absent/empty
    required_resources → conservatively attemptable (True): an item that names
    no out-of-envelope dependency cannot be silently treated as un-doable. The
    boolean is harness-DERIVED from envelope membership (reuses the
    declared-resource-∈-envelope pattern from persona_validator); the LLM never
    authors it. Returns (attemptable, absent_resources)."""
    reqs = item.get("required_resources") or []
    if not reqs:
        return True, []
    ds_ids = {
        s.get("id") for s in (envelope.get("data_sources_available") or [])
        if isinstance(s, dict)
    }
    oracle_kinds = {
        o.get("kind") for o in (envelope.get("llm_oracles_available") or [])
        if isinstance(o, dict)
    } - {"none"}
    absent: list[dict[str, Any]] = []
    for r in reqs:
        if not isinstance(r, dict):
            continue
        kind, ref = r.get("kind"), r.get("ref")
        if kind == "data_source":
            present = ref in ds_ids
        elif kind == "llm_oracle":
            present = ref in oracle_kinds
        else:
            present = True  # unknown kind cannot make an item un-attemptable
        if not present:
            absent.append(r)
    return (len(absent) == 0), absent


def handle_submit_professor_user_goal_attestation(args: dict[str, Any]) -> dict[str, Any]:
    """Professor's final attestation that the original intake problem is
    addressable. Second half of the dual publication gate."""
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    attestation = args["attestation"]
    try:
        validate_named_schema("user_goal_attestation", attestation)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    # Cross-check: achieved=true is inconsistent with empty paper writer
    # state (Professor cannot attest goal achieved when no paper exists yet).
    if attestation.get("achieved") and attestation.get("required_additional_research"):
        return {
            "status": "rejected",
            "reason": "achieved=true but required_additional_research is non-empty. If research is still required, set achieved=false; the dual-gate will then route to honest-failure or follow-up flow.",
        }
    if not attestation.get("achieved") and not attestation.get("required_additional_research"):
        return {
            "status": "rejected",
            "reason": "achieved=false requires required_additional_research to be non-empty. Name at least one concrete experiment that would flip the attestation to true.",
        }

    # Cross-check with AC: if AC ruled methodology_assessment.aggregate_verdict
    # != 'provides', Professor cannot unilaterally attest goal achieved.
    ac = _read_json(_rebuttal_dir(tid) / "ac_decision.json") or {}
    methodology = (ac.get("rebuttal_synthesis") or {}).get("methodology_assessment", {}) or {}
    if attestation.get("achieved") and methodology and methodology.get("aggregate_verdict") != "provides":
        return {
            "status": "rejected",
            "reason": (
                "AC's methodology_assessment.aggregate_verdict is "
                f"{methodology.get('aggregate_verdict')!r}, not 'provides'. "
                "Professor cannot attest user goal achieved over an AC-recognized "
                "methodology gap. Either (a) flip achieved=false and list the "
                "required_additional_research that closes the AC's remaining_gap, "
                "or (b) go back to submit_ac_decision and revise the AC synthesis "
                "if the gap was over-stated."
            ),
        }

    # --- ADR 0008: verdict-strength type system. ----------------------------
    # closeness is proven only by a funded adversary FAILING against a referent
    # the proposer did not author. The harness DERIVES verdict strength from the
    # referent ledger; the LLM cannot author it. A verdict above the available
    # referent is unconstructable:
    #   transfer_valid (reality-close)   <- real referent  [UNSAYABLE air-gapped]
    #   construct_valid (question-close) <- funded construct-adversary survived
    #   internally_valid                 <- internal referent only (honest floor)
    from research_harness.config import load_settings as _ls
    from research_harness import verdict_strength as _V
    settings = _ls(_repo_root())
    envelope = _read_json(_thread_dir(tid) / "production" / "feasibility_envelope.json")
    ledger = _thread_referent_ledger(tid, settings)
    strength = ledger["max_reachable_verdict"]

    if attestation.get("achieved") and _falsification_gate_enabled(settings):
        # achieved=true asserts the result is usable for the user's REAL
        # decision — reality-closeness — which is transfer_valid. Air-gapped
        # that verdict is unconstructable.
        if strength != _V.REALITY_VERDICT:
            return {
                "status": "rejected",
                "reason": (
                    "ADR 0008: achieved=true asserts the result is usable for the "
                    "user's REAL decision (reality-close = transfer_valid), but that "
                    f"verdict is UNCONSTRUCTABLE here — max reachable strength is "
                    f"{strength!r}. No air-gapped adversary can certify reality-"
                    "closeness (it has no contact with reality). Unlock transfer_valid "
                    "ONLY by registering a real referent: a real_holdout "
                    "external_falsifier + a passing compute_falsifier_result (the "
                    "operator's intranet scalar). Otherwise attest achieved=false — "
                    f"the harness stamps the honest verdict ({strength}). "
                    "construct_valid (a funded construct-adversary that FAILED to break "
                    "the frozen question) is the strongest air-gapped terminal: "
                    "necessary, NOT sufficient."
                ),
                "referent_ledger": ledger,
            }

    # Harness-stamp verdict strength + first-class status. The LLM authors none
    # of these — they are derived from executed, harness-checked evidence.
    attestation["verdict_strength"] = strength
    attestation["referent_ledger"] = ledger
    falsifier_kind = ((envelope or {}).get("external_falsifier") or {}).get("kind")
    if attestation.get("achieved"):
        attestation["attested_status"] = "goal_achieved"            # transfer_valid
    elif falsifier_kind == "real_holdout" and not ledger["has_real_referent"]:
        # A real referent is registered but its falsifier hasn't passed yet —
        # the run is mid-way to transfer_valid; keep going (retry), don't terminate.
        attestation["attested_status"] = "not_achieved"
    elif strength == "construct_valid":
        attestation["attested_status"] = "construct_valid_screen"   # strongest air-gapped terminal
    else:
        attestation["attested_status"] = "unverified_screen"        # honest floor terminal
    attestation["scope_attainment"] = _compute_scope_attainment(tid, envelope)

    # ADR 0007 rev.2 (A2): harness-stamp attemptability per required-research
    # item (the LLM cannot author the boolean — we overwrite any submitted
    # value). honest-failure later refuses to render while any
    # attemptable-in-envelope item is still undone.
    attemptability_audit: list[dict[str, Any]] = []
    for item in attestation.get("required_additional_research") or []:
        if not isinstance(item, dict):
            continue
        ok, absent = _attemptable_in_envelope(envelope or {}, item)
        item["attemptable_in_envelope"] = ok
        if not ok:
            attemptability_audit.append(
                {"experiment": item.get("experiment"), "declared_out_of_envelope": absent}
            )
    if attemptability_audit:
        # Operator-visible: items the run declared un-attemptable. The harness
        # cannot verify air-gapped that the experiment TRULY needs the named
        # absent resource — surfaced for audit, not silently honored.
        attestation["attemptability_audit"] = attemptability_audit

    out_path = _rebuttal_dir(tid) / "user_goal_attestation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(attestation, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    status = attestation["attested_status"]
    if status == "goal_achieved":
        next_step = (
            "transfer_valid: a real referent's falsifier passed. Dual gate "
            "cleared — proceed to prepare_paper_writing_context / "
            "submit_paper_outline / register_paper_figure / submit_paper_section "
            "/ render_final_paper."
        )
    elif status == "construct_valid_screen":
        next_step = (
            "attested_status=construct_valid_screen (Axis 1): a funded "
            "construct-adversary FAILED to break the frozen question — the "
            "strongest air-gapped terminal. NECESSARY, NOT SUFFICIENT: it says "
            "nothing about reality (transfer_valid stays unsayable without a real "
            "referent). Publish the screen honestly via render_honest_failure_paper; "
            "the manuscript must state narrowing ≠ closing."
        )
    elif status == "unverified_screen":
        next_step = (
            "attested_status=unverified_screen (internally_valid floor): no funded "
            "adversary has survived against the frozen question yet. Before "
            "terminating, EARN construct_valid — pin_frozen_question (if not pinned) "
            "then submit_construct_adversary_report with a real funded search. If "
            "genuinely exhausted, render_honest_failure_paper (depth gate applies)."
        )
    else:  # not_achieved
        next_step = (
            "attested_status=not_achieved. A real referent IS registered but its "
            "falsifier has not passed. Run/repair compute_falsifier_result against "
            "the real holdout and re-attest; or if the direction is hopeless, "
            "propose_alternative_root_directions / render_honest_failure_paper."
        )
    return {
        "status": "ok",
        "achieved": attestation.get("achieved"),
        "attested_status": status,
        "verdict_strength": strength,
        "referent_ledger": ledger,
        "next_step": next_step,
    }


def handle_propose_alternative_root_directions(args: dict[str, Any]) -> dict[str, Any]:
    """Professor proposes N>=3 alternative root claim angles after AC
    reject_and_diversify (or operator request). Replaces single-shot
    revise_root_after_reject when the direction is structurally hopeless."""
    from research_harness.schemas.validator import validate_named_schema

    tid = args["thread_id"]
    proposal = args["proposal"]
    try:
        validate_named_schema("alternative_root_proposal", proposal)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}

    # Diversification check: N>=3 alternatives across >=3 distinct angles.
    alternatives = proposal["alternatives"]
    angles = {a.get("angle") for a in alternatives}
    if len(angles) < 3:
        return {
            "status": "rejected",
            "reason": (
                f"propose_alternative_root_directions requires >=3 DISTINCT "
                f"angles, but you submitted only {len(angles)}: {sorted(angles)}. "
                "Repeating the same angle with different wording is not "
                "diversification — pick from at least 3 of "
                "{operational_root, taste_root, mechanism_root, "
                "inverted_validity_root, different_method_root, "
                "boundary_first_root, necessity_root}."
            ),
        }

    proposal_path = _thread_dir(tid) / "production" / "alternative_root_proposal.json"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(
        json.dumps(proposal, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Track cycle count to enforce termination later.
    cycles_path = _thread_dir(tid) / "production" / "fanout_cycles.json"
    cycles = _read_json(cycles_path) or {"count": 0, "history": []}
    cycles["count"] = int(cycles.get("count", 0)) + 1
    cycles["history"].append({"angle_set": sorted(angles)})
    cycles_path.write_text(
        json.dumps(cycles, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return {
        "status": "ok",
        "alternatives_count": len(alternatives),
        "distinct_angles": sorted(angles),
        "cycle_count": cycles["count"],
        "next_step": (
            "Call select_alternative_root with selected_index in "
            f"[0..{len(alternatives) - 1}] to pick one alternative; the "
            "selected claim becomes the new root via the same archive-and-"
            "rebuild path as revise_root_after_reject."
        ),
    }


def handle_select_alternative_root(args: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Pick one of the N alternatives proposed earlier and bootstrap it as
    the new root."""
    tid = args["thread_id"]
    idx = int(args["selected_index"])
    proposal = _read_json(_thread_dir(tid) / "production" / "alternative_root_proposal.json")
    if not proposal:
        return {
            "status": "rejected",
            "reason": "propose_alternative_root_directions must run before select_alternative_root",
        }
    alternatives = proposal.get("alternatives") or []
    if idx < 0 or idx >= len(alternatives):
        return {"status": "rejected", "reason": f"selected_index {idx} out of range [0,{len(alternatives)})"}

    chosen = alternatives[idx]
    # Bootstrap as revise_root_after_reject with the chosen alternative's fields.
    revise_args = {
        "thread_id": tid,
        "new_claim_under_test": chosen["claim_under_test"],
        "mandatory_baselines": chosen["mandatory_baselines"],
        "success_criteria": chosen["success_criteria"],
        "disproof_conditions": chosen["disproof_conditions"],
        "rationale": (
            f"[Fan-out selected angle={chosen['angle']}] {chosen.get('rationale','')}\n\n"
            f"Selection rationale: {args.get('selection_rationale','')}\n\n"
            f"Why this angle: {chosen.get('why_this_angle','')}"
        ),
    }
    return handle_revise_root_after_reject(revise_args, settings)


def handle_render_honest_failure_paper(args: dict[str, Any]) -> dict[str, Any]:
    """Honest-failure exit: when N alternative roots all failed or
    user_goal_attestation.achieved=false with no path to flip it, produce
    an honest-failure summary instead of a paper. The output is also an
    HTML artifact but explicitly framed as 'we tried X/Y/Z; none worked
    because A/B/C; here is what would change our answer'."""
    tid = args["thread_id"]
    repo = _repo_root()
    pdir = _thread_dir(tid) / "production"
    pdir.mkdir(parents=True, exist_ok=True)

    # Gather all evidence the system has accumulated.
    intake = _read_json(pdir / "intake_to_claim_dialog.json") or {}
    user_problem = intake.get("original_contract", {}).get("claim_under_test", "")
    attestation = _read_json(_rebuttal_dir(tid) / "user_goal_attestation.json") or {}
    fanout = _read_json(pdir / "fanout_cycles.json") or {}
    proposal = _read_json(pdir / "alternative_root_proposal.json") or {}
    archived_attempts = sorted(
        p.name for p in _thread_dir(tid).glob("production.attempt_*") if p.is_dir()
    )

    # --- ADR 0007: depth gate on the lazy door. --------------------------
    # A weak/negative terminal is the MIRROR of the fake-strength terminal:
    # both are premature termination. It renders only when the give-up was
    # EARNED — (a) a load-bearing mechanism is stated and (b) distinct
    # genuine attempts (narrowing relabels excluded) are exhausted.
    # Otherwise: refuse and send the system back to dig.
    from research_harness.config import load_settings as _ls
    settings = _ls(repo)
    if _premature_termination_gate_enabled(settings):
        depth = _investigation_depth(tid)
        min_attempts = _min_distinct_attempts(settings)
        reduction = _read_json(_rebuttal_dir(tid) / "orchestrator_reduction.json") or {}
        mechanism = attestation.get("load_bearing_mechanism") or ""
        has_mechanism = (
            len(str(mechanism).strip()) >= 80
            or bool(reduction.get("blocking_objections"))
        )
        if depth["distinct_attempts"] < min_attempts or not has_mechanism:
            problems = []
            if depth["distinct_attempts"] < min_attempts:
                problems.append(
                    f"only {depth['distinct_attempts']} distinct genuine attempt(s) "
                    f"(need >= {min_attempts}; narrowing_pivots="
                    f"{depth['narrowing_pivots']} do NOT count — scope-narrowing is "
                    "relabeling, not depth)"
                )
            if not has_mechanism:
                problems.append(
                    "no load-bearing mechanism stated (set attestation."
                    "load_bearing_mechanism: WHY this fails / what must be true to "
                    "succeed, >=80 chars, or record blocking_objections in the "
                    "orchestrator_reduction)"
                )
            return {
                "status": "rejected",
                "reason": (
                    "ADR 0007 depth gate — premature termination refused: "
                    + "; ".join(problems)
                    + ". A shallow give-up scores as low as fake strength. Generate "
                    "and TRY the next diagnostic hypothesis (why did it fail / what "
                    "would have to be true / adjacent hypothesis) until it cracks or "
                    "genuine attempts are exhausted — then re-render."
                ),
                "investigation_depth": depth,
            }

        # ADR 0007 rev.2 (A2): attemptable-in-envelope gate. A give-up is not
        # honest while work the harness CAN do remains. Block + route to fan-out.
        # attemptable_in_envelope is harness-stamped at attestation time; only an
        # item the harness confirmed needs an out-of-envelope resource is exempt.
        attemptable_undone = [
            r for r in (attestation.get("required_additional_research") or [])
            if isinstance(r, dict) and r.get("attemptable_in_envelope") is True
        ]
        if attemptable_undone:
            return {
                "status": "rejected",
                "reason": (
                    "ADR 0007 rev.2 attemptable gate — honest-failure refused: "
                    f"{len(attemptable_undone)} required-research item(s) are "
                    "attemptable IN-ENVELOPE and not yet done: "
                    + "; ".join((r.get("experiment") or "")[:80] for r in attemptable_undone)
                    + ". A give-up is not honest while in-envelope work remains. Fan "
                    "out and run them (propose_alternative_root_directions), or — if "
                    "an item genuinely needs an out-of-envelope resource (real_adapter "
                    "/ live network / operator) — declare it in required_resources so "
                    "the harness reclassifies it as not attemptable."
                ),
                "attemptable_required_research": [
                    r.get("experiment") for r in attemptable_undone
                ],
                "auto_action_suggestion": {
                    "tool": "propose_alternative_root_directions",
                    "source_rail": "attemptable_required_research_blocks_honest_failure",
                    "rationale": (
                        "honest-failure blocked because in-envelope required research "
                        "remains undone; fan out to attempt it before terminating."
                    ),
                    "confidence": "high",
                },
            }

    import html as _h
    pub_dir = pdir / "publication"
    pub_dir.mkdir(parents=True, exist_ok=True)
    # ADR 0010 (T2-18): construct_valid_screen is a BOUNDED RESULT — a funded
    # construct-adversary FAILED to break the frozen question, so the construction
    # is construct-valid (close to the QUESTION). That is a bounded POSITIVE, not a
    # failure; only unverified_screen / not_achieved render as honest-failure. The
    # operator-stamped attested_status routes this; a node cannot relabel itself.
    bounded = attestation.get("attested_status") == "construct_valid_screen"
    outcome = "bounded_result" if bounded else "honest_failure"
    out_path = pub_dir / (f"{outcome}.html")
    title = "Bounded Result" if bounded else "Honest Failure"
    accent = "#2f7d4f" if bounded else "#c25450"
    lead = (
        "A funded construct-adversary FAILED to break this thread's frozen question: "
        "the construction is construct-valid (close to the QUESTION). This is a "
        "BOUNDED POSITIVE, not a failure — but it is NOT reality-close (transfer_valid "
        "is unsayable air-gapped), so narrowing ≠ closing. The user's problem, the "
        "screen, and what a real referent would add are below."
        if bounded else
        "This thread did not produce a deployable result. Rather than publish a "
        "weak paper to hide that, the harness terminates with this honest "
        "failure report. The user's original problem, every attempt that was "
        "made, and what would change the answer are below."
    )
    body_html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title} — {_h.escape(tid)}</title>"
        "<style>body{font-family:Georgia,serif;max-width:900px;margin:2em auto;padding:0 2em;line-height:1.55;}"
        f"h1{{color:{accent};}}h2{{border-bottom:1px solid #ccc;padding-bottom:4px;}}"
        f".callout{{background:#f4f6f4;border-left:3px solid {accent};padding:10px 14px;margin:1em 0;}}"
        "code{background:#f4f4f4;padding:1px 4px;}"
        "</style></head><body>"
        f"<h1>{title} Report</h1>"
        f"<p><strong>Thread:</strong> <code>{_h.escape(tid)}</code></p>"
        "<div class='callout'>" + lead + "</div>"
        f"<h2>Original user intake</h2><p>{_h.escape(user_problem)}</p>"
        f"<h2>Final Professor attestation</h2>"
        f"<p>achieved = <strong>{attestation.get('achieved')}</strong></p>"
        f"<p>{_h.escape(attestation.get('what_user_can_do_with_this_paper',''))}</p>"
        f"<h2>What would change our answer</h2>"
        + "<ul>"
        + "".join(
            f"<li><code>{_h.escape(r.get('axis',''))}</code>: "
            f"{_h.escape(r.get('experiment',''))} "
            f"<em>({_h.escape(r.get('rationale',''))})</em></li>"
            for r in (attestation.get("required_additional_research") or [])
        )
        + "</ul>"
        f"<h2>Diversification attempts</h2>"
        f"<p>Fan-out cycles: {fanout.get('count', 0)}</p>"
        f"<p>Archived production attempts: {len(archived_attempts)}</p>"
        + "<ul>"
        + "".join(f"<li><code>{_h.escape(a)}</code></li>" for a in archived_attempts)
        + "</ul>"
        f"<h2>Last alternative root slate</h2>"
        + "<ol>"
        + "".join(
            f"<li><strong>{_h.escape(a.get('angle',''))}</strong>: "
            f"{_h.escape(a.get('claim_under_test',''))[:240]}</li>"
            for a in (proposal.get("alternatives") or [])
        )
        + "</ol>"
        "<h2>Honest limits of this search</h2>"
        "<div class='callout'>"
        "This harness is a full-auto single model. It can reach up to "
        "<em>unbridged-recombination</em> novelty (recombining known frames in a way "
        "no single frame gave); it <strong>cannot</strong> reach "
        "<em>absent-concept</em> novelty (a concept outside its and the taxonomy's "
        "manifold), and it cannot tell which of the two a given failure is. The "
        "far-framing domain taxonomy is operator-curated, so its coverage is the "
        "operator's concept coverage — a domain that could have bridged to the answer "
        "but was never listed is unreachable. Where a transfer screen was run, "
        "behavioral distance shows generator B was measurably distinct from A but "
        "cannot certify the divergence lay on the question's axis (reality-closeness "
        "needs a real referent the operator holds). These bounds are stated, not hidden."
        "</div>"
        "</body></html>"
    )
    out_path.write_text(body_html, encoding="utf-8")

    summary = {
        "type": "production_run_summary",
        "outcome": outcome,
        "thread_id": tid,
        "user_intake": user_problem,
        "attestation": attestation,
        "fanout_cycles": fanout.get("count", 0),
        "archived_attempts": archived_attempts,
        "investigation_depth": _investigation_depth(tid),
        "publication_dispatch": {
            "decision": outcome,
            "rendered_artifacts": [
                {"output": f"{outcome}_html", "artifact_path": str(out_path)}
            ],
        },
    }
    (pdir / "production_run_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": "ok", "outcome": outcome, "artifact_path": str(out_path)}


def handle_render_final_paper(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.publishing.sakana_paper import render_sakana_paper, SakanaPaperError

    tid = args["thread_id"]
    # DUAL GATE: both AC accept/revise AND user_goal_attestation.achieved=true
    # must hold before paper render is allowed. Either gate alone is insufficient.
    ac_pre = _read_json(_rebuttal_dir(tid) / "ac_decision.json") or {}
    ac_decision_val = ac_pre.get("decision")
    if ac_decision_val not in {"accept", "revise", "revise_with_new_measurements"}:
        return {
            "status": "rejected",
            "reason": (
                f"DUAL GATE blocked: AC decision is {ac_decision_val!r} — "
                "paper render requires AC ∈ {accept, revise, revise_with_new_measurements}. "
                "Call propose_alternative_root_directions if reject_and_diversify, "
                "or revise_root_after_reject if reject."
            ),
        }
    attestation = _read_json(_rebuttal_dir(tid) / "user_goal_attestation.json") or {}
    if not attestation:
        return {
            "status": "rejected",
            "reason": (
                "DUAL GATE blocked: user_goal_attestation missing. "
                "Call submit_professor_user_goal_attestation before render_final_paper. "
                "AC accept is necessary but not sufficient — the Professor must also "
                "attest the original user intake problem is addressable with this paper."
            ),
        }
    if not attestation.get("achieved"):
        return {
            "status": "rejected",
            "reason": (
                "DUAL GATE blocked: user_goal_attestation.achieved=false. "
                "Either (a) run the required_additional_research experiments and "
                "re-attest with achieved=true, (b) fan out via "
                "propose_alternative_root_directions, or (c) accept the honest-"
                "failure exit via render_honest_failure_paper."
            ),
        }
    paper_dir = _paper_dir(tid)
    outline = _read_json(paper_dir / "outline.json")
    if not outline:
        return {"status": "rejected", "reason": "submit_paper_outline missing"}
    sections_dir = paper_dir / "sections"
    section_files = sorted(sections_dir.glob("*.json")) if sections_dir.exists() else []
    expected = {s["section_id"] for s in outline["section_outline"]}
    submitted = {p.stem for p in section_files}
    missing = sorted(expected - submitted)
    if missing:
        return {"status": "rejected", "reason": f"missing sections: {missing}"}
    sections = {p.stem: _read_json(p) for p in section_files}
    figures_registry = _read_json(paper_dir / "figures.json") or {}

    rebuttal_dir = _rebuttal_dir(tid)
    ac = _read_json(rebuttal_dir / "ac_decision.json") or {}
    revision = _read_json(rebuttal_dir / "camera_ready_revision.json") or {}
    reduction = _read_json(rebuttal_dir / "orchestrator_reduction.json") or {}
    reviews_dir = rebuttal_dir / "rebuttal_reviews"
    reviews = [_read_json(p) for p in sorted(reviews_dir.glob("*.json"))] if reviews_dir.exists() else []

    ctx = _resolve_promoted_node(tid)
    node_dir = _thread_dir(tid) / "production" / "tree" / "nodes" / ctx["promoted_id"]
    worker_report = _read_json(node_dir / "worker_report.json") or {}

    publication_dir = _publication_dir(tid)
    publication_dir.mkdir(parents=True, exist_ok=True)
    try:
        outputs = render_sakana_paper(
            outline=outline,
            sections=sections,
            figures_registry=figures_registry,
            ac_decision=ac,
            camera_ready_revision=revision,
            orchestrator_reduction=reduction,
            rebuttal_reviews=reviews,
            node=ctx["node"],
            worker_report=worker_report,
            output_dir=publication_dir,
        )
    except SakanaPaperError as exc:
        return {"status": "rejected", "reason": f"paper render failed: {exc}"}

    # Build minimal production_run_summary.json so the frontend panel populates.
    summary = {
        "type": "production_run_summary",
        "repo_root": str(_repo_root()),
        "run_dir": str(_thread_dir(tid) / "production"),
        "backend": "mcp",
        "preflight_status": "passed",
        "tree_search_status": "completed",
        "promoted_node_ids": [ctx["promoted_id"]],
        "fallback_node_ids": [],
        "evidence_is_fake": False,
        "rebuttal_summary": {
            "promoted_node_id": ctx["promoted_id"],
            "ac_decision": ac,
            "mental_model_statement": revision.get("mental_model_statement"),
            "advisor_message_to_professor": ac.get("advisor_message_to_professor"),
            "camera_ready_directives": ac.get("camera_ready_directives", []),
        },
        "publication_dispatch": outputs,
    }
    (_thread_dir(tid) / "production" / "production_run_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # PR4: lesson harvest. Aggregate lesson_candidates from every rebuttal
    # critic + the orchestrator reduction's accepted_lesson_candidates into
    # a pending_lessons.yaml under the thread root. The operator approves
    # them out-of-band via the existing lesson_distillation gate; we never
    # mutate the canonical lessons.yaml from inside an MCP call.
    try:
        import yaml as _yaml
        pending: list[dict[str, Any]] = []
        for r in reviews:
            for lc in (r.get("lesson_candidates") or []):
                pending.append({
                    "text": lc,
                    "source_critic": r.get("critic_id"),
                    "source_thread": tid,
                    "source_node": ctx["promoted_id"],
                })
        for lc in (reduction.get("accepted_lesson_candidates") or []):
            pending.append({
                "text": lc,
                "source_critic": "orchestrator_reduction",
                "source_thread": tid,
                "source_node": ctx["promoted_id"],
            })
        if pending:
            pl_path = _thread_dir(tid) / "production" / "pending_lessons.yaml"
            pl_path.write_text(
                _yaml.safe_dump({"candidates": pending}, sort_keys=False),
                encoding="utf-8",
            )
    except Exception:  # noqa: BLE001
        pass

    return {"status": "ok", "publication_dispatch": outputs, "summary_path": str(_thread_dir(tid) / "production" / "production_run_summary.json")}


# --- JSON-RPC stdio loop -------------------------------------------------- #


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _handle_request(msg: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    method = msg.get("method", "")
    params = msg.get("params") or {}
    req_id = msg.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "research_harness", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_DEFINITIONS}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            if name == "get_research_state":
                result = handle_get_research_state(args, settings)
            elif name == "get_next_admissible_node":
                result = handle_get_next_admissible_node(args)
            elif name == "resume_production_state":
                result = handle_resume_production_state(args)
            elif name == "submit_feasibility_envelope":
                result = handle_submit_feasibility_envelope(args, settings)
            elif name == "compute_falsifier_result":
                result = handle_compute_falsifier_result(args)
            elif name == "pin_frozen_question":
                result = handle_pin_frozen_question(args)
            elif name == "submit_construct_adversary_report":
                result = handle_submit_construct_adversary_report(args)
            elif name == "design_initial_claim_contract":
                result = handle_design_initial_claim_contract(args, settings)
            elif name == "design_experiment_template":
                result = handle_design_experiment_template(args)
            elif name == "execute_node_experiment":
                result = handle_execute_node_experiment(args)
            elif name == "run_critic_reviews":
                result = handle_run_critic_reviews(args)
            elif name == "submit_grad_student_review":
                result = handle_submit_grad_student_review(args, settings)
            elif name == "submit_professor_decision":
                result = handle_submit_professor_decision(args, settings)
            elif name == "revise_root_after_reject":
                result = handle_revise_root_after_reject(args, settings)
            elif name == "decide_publication_readiness":
                result = handle_decide_publication_readiness(args)
            elif name == "prepare_rebuttal_packet":
                result = handle_prepare_rebuttal_packet(args)
            elif name == "submit_rebuttal_critic_review":
                result = handle_submit_rebuttal_critic_review(args)
            elif name == "submit_orchestrator_reduction":
                result = handle_submit_orchestrator_reduction(args)
            elif name == "submit_ac_decision":
                result = handle_submit_ac_decision(args)
            elif name == "submit_camera_ready_revision":
                result = handle_submit_camera_ready_revision(args)
            elif name == "prepare_paper_writing_context":
                result = handle_prepare_paper_writing_context(args)
            elif name == "submit_paper_outline":
                result = handle_submit_paper_outline(args)
            elif name == "register_paper_figure":
                result = handle_register_paper_figure(args)
            elif name == "submit_paper_section":
                result = handle_submit_paper_section(args)
            elif name == "submit_professor_user_goal_attestation":
                result = handle_submit_professor_user_goal_attestation(args)
            elif name == "propose_alternative_root_directions":
                result = handle_propose_alternative_root_directions(args)
            elif name == "select_alternative_root":
                result = handle_select_alternative_root(args, settings)
            elif name == "seed_alternative_root_formulation":
                result = handle_seed_alternative_root_formulation(args)
            elif name == "seed_forest_from_connector":
                result = handle_seed_forest_from_connector(args)
            elif name == "select_strongest_survivor":
                result = handle_select_strongest_survivor(args)
            elif name == "snapshot_root_terminal":
                result = handle_snapshot_root_terminal(args)
            elif name == "enqueue_operator_prompt":
                result = handle_enqueue_operator_prompt(args)
            elif name == "get_pending_operator_response":
                result = handle_get_pending_operator_response(args)
            elif name == "render_honest_failure_paper":
                result = handle_render_honest_failure_paper(args)
            elif name == "render_final_paper":
                result = handle_render_final_paper(args)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"},
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2, ensure_ascii=False),
                        }
                    ],
                    "isError": result.get("status") == "rejected",
                },
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"},
            }
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"unknown method: {method}"},
    }


def main() -> None:
    import argparse
    global _repo_root, _thread_dir
    parser = argparse.ArgumentParser(
        description="research_harness MCP server (stdio JSON-RPC)."
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help="Override the repo root (default: parent of research_harness/).",
    )
    args = parser.parse_args()
    repo = (args.repo_root or _repo_root()).resolve()
    _repo_root = lambda: repo  # noqa: E731
    _thread_dir = lambda tid: repo / "runs" / "threads" / tid  # noqa: E731
    try:
        settings = load_settings(repo)
    except (OSError, ValueError, json.JSONDecodeError):
        settings = {}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle_request(msg, settings)
        _send(response)


if __name__ == "__main__":
    main()
