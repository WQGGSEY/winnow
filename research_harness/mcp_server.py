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
            ],
            "properties": {
                "thread_id": {"type": "string"},
                "claim_under_test": {"type": "string"},
                "mandatory_baselines": {"type": "array", "items": {"type": "string"}},
                "success_criteria": {"type": "array", "items": {"type": "string"}},
                "disproof_conditions": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
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
            ],
            "properties": {
                "thread_id": {"type": "string"},
                "new_claim_under_test": {"type": "string"},
                "mandatory_baselines": {"type": "array", "items": {"type": "string"}},
                "success_criteria": {"type": "array", "items": {"type": "string"}},
                "disproof_conditions": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
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
    index_path.write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")


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
    if not prev_ac or prev_ac.get("decision") != "reject":
        return {
            "status": "rejected",
            "reason": (
                "revise_root_after_reject is only valid after an AC reject. "
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

    out_path = _rebuttal_dir(tid) / "user_goal_attestation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(attestation, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if attestation.get("achieved"):
        next_step = (
            "Dual gate cleared: AC decision + user-goal attestation both pass. "
            "Proceed to prepare_paper_writing_context / submit_paper_outline / "
            "register_paper_figure / submit_paper_section / render_final_paper."
        )
    else:
        next_step = (
            "achieved=false. Two paths: (a) trigger the required_additional_"
            "research experiments via the existing tree-search loop "
            "(get_next_admissible_node → execute_node_experiment → "
            "submit_professor_decision) and re-attest when evidence arrives; "
            "(b) if the gap is structural and the direction is hopeless, call "
            "propose_alternative_root_directions to fan out to N>=3 different "
            "angles, OR call render_honest_failure_paper for the final exit."
        )
    return {"status": "ok", "achieved": attestation.get("achieved"), "next_step": next_step}


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

    import html as _h
    pub_dir = pdir / "publication"
    pub_dir.mkdir(parents=True, exist_ok=True)
    out_path = pub_dir / "honest_failure.html"
    body_html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Honest Failure — {_h.escape(tid)}</title>"
        "<style>body{font-family:Georgia,serif;max-width:900px;margin:2em auto;padding:0 2em;line-height:1.55;}"
        "h1{color:#c25450;}h2{border-bottom:1px solid #ccc;padding-bottom:4px;}"
        ".callout{background:#fbeeee;border-left:3px solid #c25450;padding:10px 14px;margin:1em 0;}"
        "code{background:#f4f4f4;padding:1px 4px;}"
        "</style></head><body>"
        "<h1>Honest Failure Report</h1>"
        f"<p><strong>Thread:</strong> <code>{_h.escape(tid)}</code></p>"
        "<div class='callout'>"
        "This thread did not produce a deployable result. Rather than publish a "
        "weak paper to hide that, the harness terminates with this honest "
        "failure report. The user's original problem, every attempt that was "
        "made, and what would change the answer are below."
        "</div>"
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
        "</body></html>"
    )
    out_path.write_text(body_html, encoding="utf-8")

    summary = {
        "type": "production_run_summary",
        "outcome": "honest_failure",
        "thread_id": tid,
        "user_intake": user_problem,
        "attestation": attestation,
        "fanout_cycles": fanout.get("count", 0),
        "archived_attempts": archived_attempts,
        "publication_dispatch": {
            "decision": "honest_failure",
            "rendered_artifacts": [
                {"output": "honest_failure_html", "artifact_path": str(out_path)}
            ],
        },
    }
    (pdir / "production_run_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": "ok", "outcome": "honest_failure", "artifact_path": str(out_path)}


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
