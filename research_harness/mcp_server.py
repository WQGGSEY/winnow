"""Client-neutral MCP server for research-harness production tools.

This module exposes the research_harness state as a Model-Context-Protocol
server. The production supervisor starts a Codex session with this server's
command, arguments, and environment configured for that invocation. The model
acts as Professor and GradStudent by reading state through these tools and
committing decisions back.

Implementation note: this file uses a minimal stdio JSON-RPC subset of the
MCP spec, so no external `mcp` package is required.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from typing import Any

from research_harness.config import load_settings
from research_harness.schemas.validator import load_schema, schema_errors
from research_harness.orchestrator.llm_orchestrator.persona_validator import (
    validate_camera_ready_directives,
    validate_claim_contract,
    validate_claim_fits_envelope,
    validate_decision_rule_for_capability_claim,
    validate_grad_student_review,
)


# --- MCP tool definitions ------------------------------------------------- #


def _experiment_plan_input_schema() -> dict[str, Any]:
    schema = load_schema('experiment_plan')
    # The runner owns its workspace and binds the registered input snapshot.
    schema['required'] = [key for key in schema['required'] if key not in {'workspace', 'inputs'}]
    literal = schema['properties']['source_files']['items']
    reference = {'type': 'object', 'required': ['path', 'purpose', 'from_path', 'sha256'],
                 'additionalProperties': False, 'properties': {
                     'path': literal['properties']['path'], 'purpose': literal['properties']['purpose'],
                     'from_path': {'type': 'string', 'minLength': 1},
                     'sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                     'replacements': {'type': 'array', 'maxItems': 32, 'items': {
                         'type': 'object', 'required': ['old', 'new'], 'additionalProperties': False,
                         'properties': {'old': {'type': 'string', 'minLength': 1}, 'new': {'type': 'string'}}}}}}
    schema['properties']['source_files']['items'] = {'anyOf': [literal, reference]}
    return schema


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
    "  • Keep exactly one active direction; never attach follow-up children to "
    "a promoted or pruned direction.\n"
    "  • A pruned direction is closed from evidence. Its replacement is generated "
    "independently from the frozen goal contract.\n"
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
        "name": "finalize_submission_package",
        "description": "After rendering the manuscript, run two independent scientific reviews against the complete manuscript and evidence ledger, then compile the chosen official venue template into an anonymous PDF and source archive. Reuse reviews for unchanged content; fix returned objections in the manuscript before resubmitting. This is required for automatic completion; HTML alone is a preview. Supported: ICML/ICLR/NeurIPS/CVPR/COLT 2026 main and ICLR 2027 main. No external submission occurs.",
        "inputSchema": {"type": "object", "required": ["thread_id", "venue", "year"], "properties": {
            "thread_id": {"type": "string"}, "venue": {"type": "string", "enum": ["icml", "iclr", "neurips", "cvpr", "colt"]},
            "year": {"type": "integer"}, "track": {"type": "string", "enum": ["main"], "default": "main"}
        }, "additionalProperties": False},
    },
    {
        "name": "execute_confirmation_experiment",
        "description": "Freeze a verified public measurement program and declared checkpoint dependencies, independently review it, replay its public fixture in isolation, then draw the approved future confirmation data and execute once without an intervening agent decision. Requires qualified baselines and planned confirmation work. No training or tuning is permitted. Metric comes from execution artifacts. The program reads RESEARCH_HARNESS_CONFIRMATION_BANK when set; additional_files lists all thread-local dependencies/checkpoints absent from the reference plan.",
        "inputSchema": {"type": "object", "required": ["thread_id", "work_id", "reference_scope", "reference_node_id", "additional_files"], "properties": {
            "thread_id": {"type": "string"}, "work_id": {"type": "string"},
            "reference_scope": {"type": "string", "enum": ["nodes", "baseline_preflight"]},
            "reference_node_id": {"type": "string"},
            "additional_files": {"type": "array", "items": {"type": "object", "required": ["path", "sha256"], "properties": {"path": {"type": "string"}, "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}, "additionalProperties": False}}
        }, "additionalProperties": False},
    },
    {
        "name": "revise_evaluation_protocol",
        "description": "Submit replacement protocol notes and a development rationale for independent review of the current protocol_revision work. If its protocol_change is component_binding, first prepare the actual source files using design_experiment_template with the same work_id, then bind their returned paths and hashes; a promise to prepare later cannot complete this work. Only a prospective amendment before qualification/final evaluation is allowed. The goal, resources, structured predicate and endpoint meanings must be preserved. With replace_holdout=true, a server-sealed bank can replace the entire contaminated partition under a new prospective protocol; old cells and results are retired. With defer_holdout_generation=true, register the provisioned future sampler instead; data are drawn only after implementation/checkpoint freeze. Previous registration and amendments remain disclosed; this does not approve results.",
        "inputSchema": {"type": "object", "required": ["thread_id", "work_id", "notes", "rationale"], "properties": {"thread_id": {"type": "string"}, "work_id": {"type": "string"}, "notes": {"type": "string", "minLength": 1}, "rationale": {"type": "string", "minLength": 1}, "replace_holdout": {"type": "boolean", "default": False}, "defer_holdout_generation": {"type": "boolean", "default": False}}, "additionalProperties": False},
    },
    {
        "name": "retrieve_research_source",
        "description": "Acquire primary material through the public HTTP/cache boundary. Supply work_id for the current analysis with source_mode=acquire, or reference_id for retrieved bibliography during manuscript revision. Records content and provenance or a concrete failure. Work acquisitions continue with resolve_research_work; reference acquisitions continue with prepare_paper_writing_context. No experiment or scientific approval.",
        "inputSchema": {"type": "object", "required": ["thread_id", "url"], "properties": {"thread_id": {"type": "string"}, "work_id": {"type": "string"}, "reference_id": {"type": "string", "description": "A retrieved bibliography ID. Use instead of work_id to acquire primary literature during manuscript revision without reopening experiments or confirmation."}, "url": {"type": "string"}}, "additionalProperties": False},
    },
    {
        "name": "resolve_research_work",
        "description": "Resolve a planned analysis work from existing sources and artifacts. The harness reads the evidence and records a sourced answer or the smallest missing observation. No new experiment, baseline approval or scientific claim approval is implied.",
        "inputSchema": {"type": "object", "required": ["thread_id", "work_id"], "properties": {"thread_id": {"type": "string"}, "work_id": {"type": "string"}}, "additionalProperties": False},
    },
    {
        "name": "plan_research_work",
        "description": "Choose one evidence-bound research work unit before a new execution. The harness interprets development results, distinguishes implementation problems from scientific hypotheses, records competing predictions and a bounded test. Resume the returned work_id; after execution plan the next unit from the new evidence. No human approval or scientific claim approval is implied.",
        "inputSchema": {"type": "object", "required": ["thread_id"], "properties": {"thread_id": {"type": "string"}, "reconsider_reason": {"type": "string", "minLength": 1, "description": "Use after implementation or protocol rejection reveals missing evidence or an unsuitable procedure. Preserve the objective and evidence while reconsidering the procedure instead of retrying an impossible implementation."}}, "additionalProperties": False},
    },
    {
        "name": "develop_research_hypotheses",
        "description": "Before baseline qualification or long training, develop three causally distinct hypotheses using the research question, connector ideas and literature packets. The harness independently critiques and revises them, stores unverified candidates for the graph, and selects a small discriminating diagnostic. This never approves a baseline or a scientific claim.",
        "inputSchema": {"type": "object", "required": ["thread_id"], "properties": {"thread_id": {"type": "string"}, "revision_request": {"type": "string", "description": "Findings for a new round after the current round completes. An unfinished round resumes its frozen context even if this text changes."}, "run_id": {"type": "string", "pattern": "^[a-f0-9]{64}$", "description": "Returned run_id to resume or replay. Omit to resume the unfinished current round automatically."}}, "additionalProperties": False},
    },
    {
        "name": "search_paper_references",
        "description": "Retrieve bibliographic records from Crossref or arXiv and retain them for this thread's manuscript citations. Use returned paper IDs in citation_source_ids. Search results are metadata, not proof of method fidelity, relevance, or novelty; inspect the primary sources before making those claims. This does not change baseline qualification or authorize an experiment.",
        "inputSchema": {
            "type": "object", "required": ["thread_id", "query"], "additionalProperties": False,
            "properties": {"thread_id": {"type": "string"}, "query": {"type": "string", "minLength": 1},
                           "provider": {"type": "string", "enum": ["crossref", "arxiv"]},
                           "max_results": {"type": "integer", "minimum": 1, "maximum": 10}},
        },
    },
    {
        "name": "update_baseline_sources",
        "description": "Update this thread's unqualified literature dossier while baseline qualification is pending. Supply a full baseline_dossier object and candidate_details mapping each candidate ID to source/method analysis text. The harness assigns all file paths. This does not approve a baseline. Use this tool instead of editing dossier files through the shell.",
        "inputSchema": {
            "type": "object", "required": ["thread_id", "dossier", "candidate_details"],
            "properties": {"thread_id": {"type": "string"}, "dossier": load_schema("baseline_dossier"), "candidate_details": {"type": "object", "additionalProperties": {"type": "string"}}},
        },
    },
    {
        "name": "execute_baseline_preflight",
        "description": "While baseline qualification is pending, write and execute ONE baseline implementation or bounded development diagnostic. Prefer supplying experiment_plan without node: the harness derives operational metadata, input snapshot, and the identical claim contract automatically. An explicit node remains subject to exact contract validation. source_files may use from_path (absolute existing thread file), sha256, and optional replacements:[{old,new}] instead of content; every old text must match exactly once, and the original file is preserved. To revise a saved dispatch, use its absolute request_path plus updates (path segments and new value), without retransmitting unchanged source code or writing files from the read-only shell. To change node identity, update experiment_plan.node_id; update node.id as well only if the saved request supplied an explicit node. Inline objects work for initial requests. Use a new node ID when changing a previously executed plan; completed receipts are immutable. The harness binds the operator input snapshot, enforces compute limits and runs LocalRunner. For a baseline role, use exactly one baseline_evidence_requirement and report only that baseline key. The harness derives the dispatch role from the plan; no separate role input is needed. For a selected diagnostic_experiment or a standalone competence probe without a comparator, use mandatory_baselines=[] and baseline_evidence_requirements=[]; emit metrics without a baseline key. For the same already permitted diagnostic, independent pre-execution approval also records an authoritative source-binding receipt before launch; no separate binding-only amendment is needed unless the protocol explicitly requires a separate transaction. Diagnostic receipts cannot support a scientific claim. This is execution evidence, not scientific approval. Implement methods yourself after retrieving primary sources.",
        "inputSchema": {
            "type": "object",
            "anyOf": [{"required": ["request_path"]}, {"required": ["thread_id", "experiment_plan"]}],
            "properties": {
                "thread_id": {"type": "string"}, "node": load_schema("node"), "experiment_plan": _experiment_plan_input_schema(),
                "role": {"type": "string", "enum": ["current_best_known", "naive", "random_or_null", "diagnostic"], "description": "Optional consistency check. The harness derives the role from the plan's baseline requirements; empty comparisons require selected diagnostic_experiment or competence work."},
                "work_id": {"type": "string", "description": "work_id returned by plan_research_work; required for a new execution."},
                "request_path": {"type": "string", "description": "Absolute path of the saved request JSON inside this thread. Do not also supply inline node or plan."},
                "updates": {"type": "array", "maxItems": 32, "items": {"type": "object", "required": ["path", "value"], "additionalProperties": False,
                    "properties": {"path": {"type": "array", "minItems": 1, "items": {"anyOf": [{"type": "string"}, {"type": "integer", "minimum": 0}]}}, "value": {}}}},
            },
        },
    },
    {
        "name": "submit_baseline_qualification",
        "description": "Before freezing the goal, qualify sourced baseline candidates using replayable preflight execution artifacts under production/tree. An independent harness reviewer checks the exact implementation and evidence. Rejection returns required work; no human approval is requested.",
        "inputSchema": {
            "type": "object", "required": ["thread_id", "qualification"],
            "properties": {"thread_id": {"type": "string"}, "qualification": load_schema("baseline_qualification")},
        },
    },
    {
        "name": "get_research_state",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Returns the current research state for a thread: thread.json, "
            "grilling extraction, market brief, search_state (if production "
            "started), roadmap, readiness history, and per-node dialogs. "
            "Production defaults to current work, operational constraints and artifact paths. "
            "Use view=full only when historical detail is needed. Call before a new decision; "
            "repeating an unchanged state read adds no evidence."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string"}, "view": {"type": "string", "enum": ["current", "full"]}},
        },
    },
    {
        "name": "get_next_admissible_node",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Returns the SINGLE next node the lab should work on. Selection "
            "is deterministic: the durable reorientation binding identifies "
            "exactly one authoritative node. Historical nodes remain readable "
            "for audit but cannot be dispatched or mutated.\n"
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
        "name": "advance_research",
        "description": (
            f"{PROFESSOR_CONTRACT}\n\n"
            "Advance the blind sequential research engine by one durable step. "
            "Use a fresh command_id for each intended step. When the result is "
            "direction_ready, inspect direction.data_needs and call this tool "
            "again with a new command_id plus one ordered acquisition entry per "
            'need. A registered candidate is exactly {"kind":"registered_adapter","adapter_id":"..."}; '
            "the harness resolves its pinned snapshot, so do not add snapshot_id. Public "
            "candidates may use public_api, public_page, or robots-compliant "
            "crawl. A checkpoint is resumable and is not a scientific result."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "command_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "command_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "expected_checkpoint_id": {"type": "string", "minLength": 1},
                "expected_strong_result_receipt_sha256": {
                    "type": "string",
                    "pattern": "^sha256:[a-f0-9]{64}$",
                },
                "acquisition": {
                    "type": "object",
                    "required": ["needs"],
                    "properties": {
                        "needs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["need_index", "candidates"],
                                "properties": {
                                    "need_index": {"type": "integer", "minimum": 0},
                                    "candidates": {
                                        "type": "array",
                                        "items": {
                                            "oneOf": [
                                                {
                                                    "type": "object", "required": ["kind", "adapter_id"],
                                                    "properties": {
                                                        "kind": {"const": "registered_adapter"},
                                                        "adapter_id": {"type": "string", "minLength": 1},
                                                    },
                                                    "additionalProperties": False,
                                                },
                                                {
                                                    "type": "object", "required": ["kind", "uri"],
                                                    "properties": {
                                                        "kind": {"enum": ["public_api", "public_page", "crawl"]},
                                                        "uri": {"type": "string", "minLength": 1},
                                                        "credential_profile_name": {"type": ["string", "null"]},
                                                        "license_evidence": {"type": ["string", "null"]},
                                                        "crawl_max_pages": {"type": "integer", "minimum": 1, "maximum": 1000},
                                                        "crawl_max_depth": {"type": "integer", "minimum": 0, "maximum": 10},
                                                        "crawl_max_total_bytes": {"type": "integer", "minimum": 1, "maximum": 1073741824},
                                                    },
                                                    "additionalProperties": False,
                                                },
                                            ],
                                        },
                                    },
                                },
                            },
                        },
                        "budget": {
                            "type": "object",
                            "properties": {
                                "max_requests": {"type": "integer", "minimum": 0, "maximum": 100},
                                "max_download_bytes": {"type": "integer", "minimum": 0, "maximum": 1073741824},
                                "max_wall_seconds": {"type": "integer", "minimum": 0, "maximum": 300},
                            },
                        },
                    },
                },
            },
        },
    },
    {
        "name": "submit_bar_sanity_result",
        "description": (
            "Cycle-1 skill-isolation gate (active only when "
            "execution_constraints.require_skill_isolation is set; otherwise a "
            "no-op). Report the best value a NO-SKILL exposure baseline (e.g. a "
            "leveraged buy-and-hold sweep) reaches on the SAME deployment metric "
            "the search is graded on. The harness checks it against the "
            "deployment predicate deterministically (air-gap — it trusts the "
            "scalar, owns the pass/fail): if a zero-skill exposure CLEARS the "
            "bar, the bar measures exposure not skill, and get_next_admissible_"
            "node blocks the search (bar_broken) until the bar is revised. Run "
            "this FIRST, before the search spends a cycle."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "no_skill_exposure_metric"],
            "properties": {
                "thread_id": {"type": "string"},
                "no_skill_exposure_metric": {
                    "type": "number",
                    "description": (
                        "Best value a NO-SKILL exposure baseline (e.g. a "
                        "leveraged buy-and-hold sweep) reaches on the deployment "
                        "metric. If it clears the deployment predicate, the bar "
                        "is exposure-gameable."
                    ),
                },
                "detail": {
                    "type": "string",
                    "description": "How computed: the exposure sweep + data + method.",
                },
            },
        },
    },
    {
        "name": "resume_production_state",
        "description": (
            "Recovery tool. Call this if the previous agent session "
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
            "BEFORE designing any claim, the Professor proposes the "
            "FeasibilityEnvelope — what the harness actually has at this "
            "thread's disposal: registered real-data adapters, available "
            "LLM oracles (subscription / live API / proxy), compute budget "
            "(seconds per node / concurrent nodes / total hours), "
            "paper-cited baselines from the market dossier, and the "
            "operator's target deploy_grade_scope. This anchors every "
            "subsequent claim: 'deployment' scope is blocked when no real "
            "adapter is registered, 'live LLM' oracle is blocked when no "
            "billing_ack is set, etc. An independent harness reviewer evaluates "
            "the proposed protocol and registers it if approved. Rejections "
            "return actionable findings. Initialized resources, compute ceilings "
            "and frozen research goals cannot be expanded through this tool."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "envelope"],
            "properties": {
                "thread_id": {"type": "string"},
                "envelope": load_schema("feasibility_envelope"),
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
            "Prepare or repair code within the SAME planned execution or protocol-revision work: supply work_id and plan_metadata; node_id is not needed. This writes versioned source_files without execution or scientific approval, records exact paths/hashes, and keeps the scientific work planned with the same work_id. Do not create a separate implementation work. For a qualified formal claim, instead supply node_id without work_id to install its normal experiment template, then execute with the existing scientific work_id. Submit a "
            "plan_metadata dict (task_class, objective, entrypoint, resources, "
            "expected_outputs, baseline_evidence_requirements, source_files). "
            "Reuse or revise source_files without retranscribing them: supply {path, purpose, from_path:absolute_existing_thread_file, sha256:base_file_hash, replacements:[{old:exact_text,new:replacement_text}]} instead of content. Each old string must match exactly once; omit replacements to copy unchanged bytes. Original files are not edited. "
            "source_files paths starting with `_lib/` are shared across the "
            "whole thread (do this once at the root); other paths land under "
            "the per-node dir. REUSE existing shared modules whenever they "
            "suffice — call get_research_state to see what's already there. "
            "The harness writes the files to disk; this tool does not run "
            "the experiment (that's execute_node_experiment). This thread may "
            "mandate execution_constraints.required_modules and/or "
            "required_data_sources (set in the feasibility_envelope or as a "
            "thread setting); when it does, EVERY experiment MUST import those "
            "modules AND reference those data sources, or this tool rejects the "
            "source — use the registered tooling and REAL data rather than "
            "hand-rolling a substitute or self-authoring synthetic data. "
            "REGISTERED INPUT CONTRACT: read the JSON file named by "
            "`RESEARCH_HARNESS_INPUT_MANIFEST`; resolve the dataset as "
            "`manifest_path.parent / manifest['primary_dataset']['relative_path']`. "
            "The canonical field is `primary_dataset.relative_path` at the "
            "manifest root, not under an `inputs` wrapper, and it is relative "
            "to the node workspace. Do not search ambient repository paths. "
            "EVIDENCE OUTPUT CONTRACT: every declared metrics file must contain "
            "a top-level `metrics` object with a numeric candidate value at each "
            "exact metric_key and a top-level `baselines` object with a numeric "
            "comparison value at each exact baseline_key. Add one requirement "
            "for every mandatory metric/baseline comparison; use distinct keys "
            "such as `word_accuracy` and `word_macro_f1` when one baseline is "
            "compared on multiple metrics. A promotion-eligible run must also "
            "emit top-level `claim_verdict_candidate='supported'`, "
            "`disproof_conditions_hit=[]`, and `unexpected_observations=[]`; "
            "omitting the verdict is deterministically `inconclusive` and cannot "
            "be promoted. Nested model result tables may be "
            "included under additional keys, but they do not satisfy this "
            "machine-readable contract."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "plan_metadata"],
            "anyOf": [{"required": ["work_id"]}, {"required": ["node_id"]}],
            "properties": {
                "thread_id": {"type": "string"},
                "node_id": {"type": "string"},
                "plan_metadata": {"type": "object"},
                "work_id": {"type": "string"},
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
                "work_id": {"type": "string", "description": "The current planned work_id. Omit to use the current work unit."},
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
            "Promote or prune a node after seeing the worker report and critic "
            "reviews. For a negative result submit pruned. The harness closes "
            "that direction from persisted "
            "evidence, keeps its lesson private, and independently generates the "
            "next direction from the frozen contract."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "node_id", "next_transition"],
            "properties": {
                "thread_id": {"type": "string"},
                "node_id": {"type": "string"},
                "next_transition": {
                    "type": "string",
                    "enum": ["promoted", "pruned"],
                },
                "final_verdict": {"type": "string"},
                "response_to_grad_student": {"type": "string"},
                "command_id": {
                    "type": "string",
                    "description": "Idempotency key for this decision command.",
                },
                "expected_revision": {
                    "type": "integer",
                    "description": "Optional adaptive-state revision for stale-writer rejection.",
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
            "required": ["thread_id", "node_id", "submit"],
            "properties": {
                "thread_id": {"type": "string"},
                "node_id": {"type": "string"},
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
            "blocked and the blind engine continues research. Schema enforces: at least 2 "
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
            "the canonical blind engine continues through advance_research. After preview rendering call finalize_submission_package; HTML alone does not finish the publication goal."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string"}},
        },
    },
]

# --- Tool handlers -------------------------------------------------------- #


_REPO_ROOT_SCOPE: ContextVar[Path | None] = ContextVar(
    "research_harness_mcp_repo_root",
    default=None,
)


def _repo_root() -> Path:
    scoped = _REPO_ROOT_SCOPE.get()
    return scoped if scoped is not None else Path(__file__).resolve().parents[1]


@contextmanager
def _repo_root_scope(repo_root: Path):
    token = _REPO_ROOT_SCOPE.set(repo_root)
    try:
        yield
    finally:
        _REPO_ROOT_SCOPE.reset(token)


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


def _write_search_state_atomic(path: Path, state: dict[str, Any]) -> None:
    """Commit the single-writer search snapshot without exposing partial JSON."""

    _write_json_atomic(path, state)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if temporary.exists():
            temporary.unlink()
        raise


@contextmanager
def _exclusive_adaptive_writer(tid: str):
    from research_harness.orchestrator.blind_mcp_adapter import (
        adaptive_writer_lock,
    )

    with adaptive_writer_lock(_thread_dir(tid)):
        yield


def _ensure_adaptive_state(
    tid: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Attach the problem-level adaptive envelope on first active-path use."""

    existing = state.get("adaptive")
    if isinstance(existing, dict):
        return existing

    from research_harness.orchestrator.adaptive_search import (
        build_research_goal,
        initialize_adaptive_state,
    )

    thread = _read_json(_thread_dir(tid) / "thread.json") or {}
    grilling = _read_json(
        _thread_dir(tid) / "grilling" / "grilling_session.json"
    ) or {}
    envelope = _read_json(
        _thread_dir(tid) / "production" / "feasibility_envelope.json"
    ) or {}
    if not (
        thread.get("user_goal")
        or grilling.get("user_goal")
        or (grilling.get("extracted") or {}).get("claim_under_test")
    ):
        roots = [
            node
            for node in state.get("nodes") or []
            if isinstance(node, dict) and node.get("parent") is None
        ]
        root_goal_ids = {
            str((node.get("lineage") or {}).get("root_goal_id") or "")
            for node in roots
        }
        if not roots or len(root_goal_ids) != 1:
            raise ValueError(
                "missing pre-generation goal and legacy roots do not share one root_goal_id"
            )
        legacy_contracts = {
            json.dumps(
                node.get("claim_contract") or {},
                ensure_ascii=False,
                sort_keys=True,
            )
            for node in roots
        }
        if len(legacy_contracts) != 1:
            raise ValueError(
                "missing pre-generation goal and legacy root claim contracts disagree"
            )
        legacy_contract = roots[0].get("claim_contract") or {}
        legacy_question = (
            legacy_contract.get("claim_under_test")
            or next(iter(root_goal_ids), "")
            or f"Legacy research goal {roots[0].get('id', 'unknown')}"
        )
        thread = {"user_goal": legacy_question}
        grilling = {
            "user_goal": legacy_question,
            "extracted": dict(legacy_contract),
        }
        goal = build_research_goal(
            thread=thread,
            grilling=grilling,
            envelope=envelope,
        )
        goal["source"] = "legacy_single_root_migration"
        goal["strong_completion_blocked"] = True
    else:
        goal = build_research_goal(
            thread=thread,
            grilling=grilling,
            envelope=envelope,
        )
    adaptive = initialize_adaptive_state(goal)
    state["adaptive"] = adaptive
    return adaptive


def _adaptive_command_id(args: dict[str, Any]) -> str:
    explicit = str(args.get("command_id") or "").strip()
    if explicit:
        return explicit
    import hashlib

    payload = {
        key: value
        for key, value in args.items()
        if key not in {"command_id", "expected_revision"}
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"implicit:{digest}"


def _json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _adaptive_capabilities(tid: str) -> set[str]:
    from research_harness.orchestrator.adaptive_search import (
        capabilities_from_envelope,
    )

    envelope = _read_json(
        _thread_dir(tid) / "production" / "feasibility_envelope.json"
    ) or {}
    return capabilities_from_envelope(envelope)


def _strong_candidate_for_terminal(
    tid: str,
    state: dict[str, Any],
    *,
    authoritative_node_id: str,
) -> dict[str, Any] | None:

    adaptive = state.get("adaptive") or {}
    known_strategy_ids = {
        strategy.get("id")
        for strategy in adaptive.get("strategies") or []
        if isinstance(strategy, dict) and strategy.get("id")
    }
    if not known_strategy_ids:
        return None
    nodes_by_id = {node["id"]: node for node in state.get("nodes") or []}
    frontier_by_id = {
        item["node_id"]: item for item in state.get("frontier") or []
    }
    candidates: list[tuple[int, float, str, dict[str, Any]]] = []
    for node_id in state.get("promoted_node_ids") or []:
        if node_id != authoritative_node_id:
            continue
        node = nodes_by_id.get(node_id) or {}
        strategy_id = (node.get("strategy") or {}).get("id")
        if strategy_id not in known_strategy_ids:
            continue
        worker = _read_json(
            _thread_dir(tid)
            / "production"
            / "tree"
            / "nodes"
            / node_id
            / "worker_report.json"
        ) or {}
        if (
            worker.get("status") != "completed"
            or worker.get("claim_verdict_candidate") != "supported"
            or (worker.get("baseline_evidence_status") or {}).get("overall")
            != "passed"
            or worker.get("disproof_conditions_hit")
        ):
            continue
        frontier = frontier_by_id.get(node_id) or {}
        candidates.append(
            (
                int(frontier.get("depth", 0)),
                float(
                    (frontier.get("priority_components") or {}).get(
                        "score", frontier.get("priority", 0.0)
                    )
                ),
                node_id,
                node,
            )
        )
    if not candidates:
        return None
    _, _, node_id, node = max(candidates)
    return {
        "node_id": node_id,
        "node_type": node.get("type"),
        "strategy_id": (node.get("strategy") or {}).get("id"),
    }


def _authoritative_active_node_id(tid: str) -> str | None:
    binding = _authoritative_strong_binding(tid)
    return binding.node_id if binding is not None else None


def _authoritative_strong_binding(tid: str, node_id: str | None = None):
    from research_harness.orchestrator.blind_reorientation import (
        parse_reorientation_state,
    )
    from research_harness.orchestrator.blind_sequential_research import (
        BlindSequentialResearchError,
        resolve_strong_result_binding,
    )

    root = _thread_dir(tid) / "production" / "reorientation"
    raw_state = _read_json(root / "state.json")
    raw_node_attempts = _read_json(root / "node_attempts.json")
    if not isinstance(raw_state, dict) or not isinstance(raw_node_attempts, dict):
        return None
    try:
        state = parse_reorientation_state(raw_state)
        return resolve_strong_result_binding(
            state,
            raw_node_attempts,
            node_id=node_id,
        )
    except (BlindSequentialResearchError, TypeError, ValueError):
        return None


def _require_authoritative_node(tid: str, node_id: str) -> dict[str, Any] | None:
    authoritative = _authoritative_active_node_id(tid)
    if authoritative == node_id:
        return None
    return {
        "status": "rejected",
        "reason": (
            f"node {node_id!r} is not the authoritative blind node for the "
            "active direction"
        ),
    }


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
    """Remove trailing legacy Claude Code tool-call XML envelope leakage from a
    string before persisting it to dialog.json. No-op for non-strings or
    clean strings. Idempotent.
    """
    if not isinstance(text, str):
        return text  # type: ignore[return-value]
    stripped = _TOOL_ENVELOPE_TAILS.sub("", text).rstrip()
    return stripped


def handle_develop_research_hypotheses(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.orchestrator.hypothesis_development import develop_hypotheses
    from research_harness.adapters.codex_cli import CodexCliError

    tid = args["thread_id"]
    with _exclusive_adaptive_writer(tid):
        try:
            return develop_hypotheses(_repo_root(), _thread_dir(tid), revision_request=args.get("revision_request", ""), run_id=args.get("run_id"))
        except (OSError, ValueError, KeyError, TypeError, CodexCliError) as exc:
            return {"status": "needs_revision", "reason": str(exc), "next_step": "Preserved hypothesis drafts remain unverified. Resolve the generation or evidence error and retry."}


def handle_plan_research_work(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.orchestrator.research_control import plan_research_work
    from research_harness.adapters.codex_cli import CodexCliError
    from research_harness.adapters.call_budget import CallBudgetExhausted

    tid = args['thread_id']
    with _exclusive_adaptive_writer(tid):
        try:
            return plan_research_work(_repo_root(), _thread_dir(tid), reconsider_reason=args.get('reconsider_reason', ''))
        except (CodexCliError, CallBudgetExhausted) as exc:
            if os.environ.get('RESEARCH_HARNESS_CALL_BUDGET'):
                return {'status': 'checkpoint', 'reason': str(exc),
                        'next_tool_to_call': None,
                        'scope': 'Bounded transport failure; preserve research state and stop this run. No scientific conclusion.'}
            return {'status': 'planning_failed', 'reason': str(exc), 'next_tool_to_call': 'plan_research_work'}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {'status': 'planning_failed', 'reason': str(exc), 'next_tool_to_call': 'plan_research_work'}


def handle_execute_confirmation_experiment(args: dict[str, Any]) -> dict[str, Any]:
    from dataclasses import asdict
    from research_harness.orchestrator.confirmation_execution import execute_confirmation_experiment
    from research_harness.orchestrator.research_control import StaleResearchWork
    from research_harness.settings_scoped import resolve_for_thread

    tid = args['thread_id']
    with _exclusive_adaptive_writer(tid):
        try:
            binding = _authoritative_strong_binding(tid)
            if binding is None:
                raise ValueError('Confirmation requires an authoritative active direction')
            return execute_confirmation_experiment(
                _repo_root(), _thread_dir(tid), work_id=args['work_id'],
                reference_scope=args['reference_scope'], reference_node_id=args['reference_node_id'],
                additional_files=args['additional_files'], binding=asdict(binding),
                settings=resolve_for_thread(_repo_root(), tid),
            )
        except StaleResearchWork as exc:
            return {'status': 'work_required', 'reason': str(exc), 'next_tool_to_call': 'plan_research_work'}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {'status': 'rejected', 'reason': str(exc), 'next_tool_to_call': 'execute_confirmation_experiment'}


def handle_revise_evaluation_protocol(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.orchestrator.protocol_revision import revise_evaluation_protocol
    from research_harness.orchestrator.research_control import StaleResearchWork
    from research_harness.adapters.codex_cli import CodexCliError

    tid = args['thread_id']
    with _exclusive_adaptive_writer(tid):
        try:
            return revise_evaluation_protocol(_repo_root(), _thread_dir(tid), work_id=args['work_id'], notes=args['notes'], rationale=args['rationale'], replace_holdout=args.get('replace_holdout', False), defer_holdout_generation=args.get('defer_holdout_generation', False))
        except StaleResearchWork as exc:
            return {'status': 'work_required', 'reason': str(exc), 'next_tool_to_call': 'plan_research_work'}
        except (OSError, ValueError, KeyError, TypeError, CodexCliError) as exc:
            from research_harness.orchestrator.research_control import current_work, _write
            thread = _thread_dir(tid)
            work = current_work(thread)
            if work.get('work_id') == args['work_id'] and work.get('status') == 'planned':
                work.update(protocol_review_error=str(exc), reconsideration_available=True)
                _write(thread / 'production/research_control/current.json', work)
                _write(thread / 'production/research_control/work' / work['work_id'] / 'work.json', work)
            return {'status': 'rejected', 'reason': str(exc), 'next_tool_to_call': 'revise_evaluation_protocol'}


def handle_resolve_research_work(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.orchestrator.research_control import resolve_research_work, StaleResearchWork
    from research_harness.adapters.codex_cli import CodexCliError

    tid = args['thread_id']
    with _exclusive_adaptive_writer(tid):
        try:
            return resolve_research_work(_repo_root(), _thread_dir(tid), args['work_id'])
        except StaleResearchWork as exc:
            return {'status': 'work_required', 'reason': str(exc), 'next_tool_to_call': 'plan_research_work'}
        except (OSError, ValueError, KeyError, TypeError, CodexCliError) as exc:
            return {'status': 'analysis_failed', 'reason': str(exc), 'next_tool_to_call': 'resolve_research_work'}


def handle_retrieve_research_source(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.orchestrator.research_sources import acquire_source, acquire_reference_source

    with _exclusive_adaptive_writer(args['thread_id']):
        try:
            if bool(args.get('work_id')) == bool(args.get('reference_id')):
                raise ValueError('Supply exactly one work_id or reference_id.')
            if args.get('reference_id'):
                return {**acquire_reference_source(_thread_dir(args['thread_id']), args['reference_id'], args['url']),
                        'next_tool_to_call': 'prepare_paper_writing_context'}
            return acquire_source(_thread_dir(args['thread_id']), args['work_id'], args['url'])
        except (OSError, ValueError) as exc:
            return {'status': 'source_acquisition_failed', 'reason': str(exc),
                    'next_tool_to_call': 'search_paper_references' if args.get('reference_id') else 'plan_research_work'}


def handle_get_research_state(args: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    from research_harness.runner.baseline_preflight import baseline_preparation_state
    from research_harness.orchestrator.research_control import current_work, runtime_input_example, executed_diagnostic_bindings

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
        or "gpt-5.6-sol"
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
    state = {
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
        "baseline_qualification": _read_json(market_dir / "baseline_qualification.json"),
        "baseline_preparation": baseline_preparation_state(d),
        "research_work": current_work(d),
        "observation_binding_contract": {
            "required_for": "Every empirical ResearchWork before implementation review",
            "plan_field": "observation_bindings",
            "entry": {"count_name": {"artifact_path": "a declared expected_outputs.metrics_files entry",
                                      "json_pointer": "/metrics/count_name or exact existing nested value",
                                      "producer": "source file and producer location"}},
            "scope": "Bind exactly the required_observations names. No aliases or implicit sums. Each alternative declares its required subset; the reviewer checks emission and the host resolves values after execution."},
        "runtime_input_example": runtime_input_example(d),
        "executed_diagnostic_bindings": executed_diagnostic_bindings(d),
        "publication_target": _read_json(d / "production/submission_target.json"),
        "baseline_preparation_contract": (
            "Create the research claim and plan through advance_research before waiting for baseline qualification. "
            "An empty GoalContract.baseline_evidence means assignments are pending, not approved. "
            "Source updates and baseline preflight remain available until approval. "
            "Use diagnostics to resolve prerequisites; qualify baselines before promoting a comparative claim."
        ),
        "hypotheses": _read_json(d / "production/hypotheses/current.json"),
        "reference_papers": reference_papers,
        "_market_usage_contract": (
            "Search results are unqualified candidates. Preserve all candidates and "
            "choose current_best_known, naive, and random_or_null using sourced "
            "methods and matched task/data/split/budget/metric conditions. Reproduce "
            "them in preflight execution artifacts and submit_baseline_qualification "
            "before promoting a comparative claim. Published scores on different tasks are "
            "not comparable thresholds. A source citation does not prove suitability."
        ),
        "search_state": _read_json(d / "production" / "tree" / "search_state.json"),
        "tree_summary": _read_json(d / "production" / "tree" / "tree_search_summary.json"),
        "intake_to_claim": _read_json(d / "production" / "intake_to_claim_dialog.json"),
    }
    from research_harness.orchestrator.research_control import PLANNING_POLICY_VERSION
    from research_harness.orchestrator.research_knowledge import research_brief, brief_context
    from research_harness.orchestrator.research_control import execution_handoff
    state['research_brief'] = research_brief(d)
    if state['research_work'] and state['research_work'].get('planning_policy_version') != PLANNING_POLICY_VERSION:
        state['research_work'] = {**state['research_work'], 'next_tool_to_call': 'plan_research_work',
                                  'requires_replanning': True, 'reason': 'Research capabilities changed; old work routing is stale.'}
    state['execution_handoff'] = execution_handoff(d, state['research_work'])
    if args.get('view', 'current' if thread.get('current_phase') == 'production' else 'full') == 'full':
        return state
    work = dict(state['research_work'])
    work.pop('source_observations', None)
    envelope = _read_json(d / 'production/feasibility_envelope.json') or {}
    return {
        **{key: state[key] for key in ('thread', 'operator_model_preference', '_model_note',
                                      'baseline_preparation', 'baseline_preparation_contract', '_market_usage_contract', 'publication_target')},
        'view': 'current', 'thread_dir': str(d.resolve()), 'research_work': work,
        'observation_binding_contract': state['observation_binding_contract'],
        'execution_handoff': state['execution_handoff'],
        'research_brief': brief_context(d, state['research_brief']),
        'runtime_input_example': state['runtime_input_example'],
        'executed_diagnostic_bindings': state['executed_diagnostic_bindings'],
        'operator_intent': envelope.get('operator_intent'),
        'compute_budget': envelope.get('compute_budget'),
        'external_falsifier': envelope.get('external_falsifier'),
        'nodes': [{'id': n['id'], 'status': n['status'],
                   'claim': n.get('claim_contract', {}).get('claim_under_test'),
                   'artifact_dir': str((d / 'production/tree/nodes' / n['id']).resolve())}
                  for n in (state['search_state'] or {}).get('nodes', [])],
        'artifact_paths': {key: str((d / path).resolve()) for key, path in {
            'grilling': 'grilling/grilling_session.json', 'market_brief': 'market/market_research_brief.json',
            'baseline_dossier': 'market/baseline_dossier_candidate.yaml', 'baseline_analysis': 'market/baseline_analysis.md',
            'qualification': 'market/baseline_qualification.json', 'hypotheses': 'production/hypotheses/current.json',
            'search_state': 'production/tree/search_state.json', 'research_work': 'production/research_control/current.json',
            'goal_contract': 'production/reorientation/goal_contract.json',
            'registered_protocol': 'production/feasibility_envelope.json',
            'approved_protocol_history': 'production/protocol_revisions',
        }.items() if (d / path).exists()},
        'detail_access': 'Read the named artifact for a specific question, or request view=full. Historical drafts are not current execution results.',
    }


_RESUME_NEXT_TOOL = {
    "running": "execute_node_experiment",
    "completed_worker_report": "run_critic_reviews",
    "critic_reviewed": "submit_professor_decision",
    "orchestrator_reduced": "submit_professor_decision",
}


def _bar_sanity_gate_status(tid: str) -> dict[str, Any] | None:
    """Cycle-1 skill-isolation gate status, or None when the gate is inactive.

    When execution_constraints.require_skill_isolation is set (thread setting or
    feasibility envelope), the search may not dispatch any node until a NO-SKILL
    exposure baseline (e.g. a leveraged buy-and-hold sweep, reported via
    submit_bar_sanity_result) has been shown NOT to clear the deployment
    predicate. A bar a zero-skill exposure clears measures exposure, not skill —
    so an impossibility found against it is an instrument failure, not a fact
    about the world. The check runs before any search cycle is spent. Off-flag
    threads get None and proceed normally.

    Returns one of:
      None                     — gate inactive; proceed with normal selection.
      {"state": "required"}    — flag on, no bar_sanity result reported yet.
      {"state": "broken", ...} — reported exposure CLEARS the bar (gameable).
      {"state": "ok", ...}     — reported exposure does not clear the bar.
    """
    from research_harness.settings_scoped import resolve_for_thread as _rft

    repo = _repo_root()
    pdir = _thread_dir(tid) / "production"
    env = _read_json(pdir / "feasibility_envelope.json") or {}
    skill_iso = bool(
        _rft(repo, tid).get_dotted(
            "execution_constraints.require_skill_isolation", False
        )
        or (env.get("execution_constraints") or {}).get("require_skill_isolation")
    )
    if not skill_iso:
        return None
    bs = _read_json(pdir / "bar_sanity.json") or {}
    null_val = bs.get("no_skill_exposure_metric")
    if null_val is None:
        return {"state": "required"}
    pred = (env.get("external_falsifier") or {}).get("predicate") or {}
    gamed = False
    if pred.get("op") and "threshold" in pred:
        try:
            from research_harness.falsifier import evaluate_predicate as _ep

            gamed = bool(_ep(float(null_val), pred["op"], float(pred["threshold"])))
        except (TypeError, ValueError):
            gamed = False
    if gamed:
        return {
            "state": "broken",
            "no_skill_exposure_metric": null_val,
            "predicate": pred,
        }
    return {"state": "ok", "no_skill_exposure_metric": null_val}


_BLIND_REORIENTATION_REQUIRED = "_blind_reorientation_required"


def _blind_reorientation_required(reason: str) -> dict[str, Any]:
    return {"status": _BLIND_REORIENTATION_REQUIRED, "reason": reason}


def _blind_command_id(
    tid: str,
    trigger: str,
    *,
    bind_state: bool = True,
) -> str:
    identity: dict[str, Any] = {"thread_id": tid, "trigger": trigger, "planning_version": 4}
    if bind_state:
        thread_dir = _thread_dir(tid)
        identity["search"] = _read_json(
            thread_dir / "production" / "tree" / "search_state.json"
        )
        identity["reorientation"] = _read_json(thread_dir / "production/reorientation/state.json")
        if not (thread_dir / "production/reorientation/goal_contract.json").exists():
            identity["preparation"] = {
                name: _read_json(thread_dir / name)
                for name in (
                    "grilling/grilling_session.json", "market/market_research_brief.json",
                    "market/baseline_qualification.json", "production/feasibility_envelope.json",
                    "production/intake_to_claim_dialog.json", "production/hypotheses/current.json",
                )
            }
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "mcp_blind_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _advance_command_receipt_path(tid: str, command_id: str) -> Path:
    digest = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
    return (
        _thread_dir(tid)
        / "production"
        / "reorientation"
        / "mcp_commands"
        / f"{digest}.json"
    )


def _build_blind_research_engine(tid: str):
    from research_harness.orchestrator.blind_mcp_adapter import (
        build_blind_research_engine,
    )
    from research_harness.settings_scoped import resolve_for_thread

    return build_blind_research_engine(
        repo_root=_repo_root(),
        thread_dir=_thread_dir(tid),
        writer_lock=lambda: _exclusive_adaptive_writer(tid),
        settings=resolve_for_thread(_repo_root(), tid),
    )


def handle_advance_research(
    args: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    from research_harness.orchestrator.blind_mcp_adapter import (
        BlindMcpAdapterError,
        build_acquisition_command,
    )
    from research_harness.acquisition import (
        AcquisitionContractError,
        parse_command,
        serialize_command,
    )

    tid = args["thread_id"]
    command_id = args.get("command_id")
    if not isinstance(command_id, str) or not command_id.strip():
        return {"status": "rejected", "reason": "command_id must be non-empty"}
    receipt_path = _advance_command_receipt_path(tid, command_id)
    input_digest = _json_sha256(args)
    engine = None
    with _exclusive_adaptive_writer(tid):
        receipt = _read_json(receipt_path)
        if receipt is None and receipt_path.exists():
            return {
                "status": "rejected",
                "reason": "advance command receipt is malformed",
            }
        if receipt is not None:
            if (
                receipt.get("version") != 1
                or receipt.get("command_id") != command_id
                or receipt.get("input_digest") != input_digest
            ):
                return {
                    "status": "rejected",
                    "reason": "command_id was already used for a different advance request",
                }
            if receipt.get("status") == "committed":
                result = receipt.get("result")
                if not isinstance(result, dict):
                    return {
                        "status": "rejected",
                        "reason": "advance command receipt is malformed",
                    }
                return result
            if receipt.get("status") != "prepared":
                return {
                    "status": "rejected",
                    "reason": "advance command receipt is malformed",
                }
            command_document = receipt.get("acquisition_command")
            try:
                acquisition_command = (
                    parse_command(command_document)
                    if command_document is not None
                    else None
                )
            except AcquisitionContractError as exc:
                return {
                    "status": "rejected",
                    "reason": f"advance command receipt is malformed: {exc}",
                }
        else:
            engine = _build_blind_research_engine(tid)
            try:
                acquisition_command = (
                    build_acquisition_command(
                        engine,
                        args["acquisition"],
                        thread_dir=_thread_dir(tid),
                    )
                    if "acquisition" in args
                    else None
                )
            except BlindMcpAdapterError as exc:
                return {"status": "rejected", "reason": str(exc)}
            command_document = (
                serialize_command(acquisition_command)
                if acquisition_command is not None
                else None
            )
            receipt = {
                "version": 1,
                "command_id": command_id,
                "input_digest": input_digest,
                "status": "prepared",
                "acquisition_command": command_document,
            }
            _write_json_atomic(receipt_path, receipt)
    if engine is None:
        engine = _build_blind_research_engine(tid)
    advance_kwargs: dict[str, Any] = {
        "command_id": command_id,
        "acquisition_command": acquisition_command,
    }
    expected_strong_receipt = args.get(
        "expected_strong_result_receipt_sha256"
    )
    if expected_strong_receipt is not None:
        advance_kwargs["expected_strong_result_receipt_sha256"] = (
            expected_strong_receipt
        )
    if args.get("expected_revision") is not None:
        advance_kwargs["expected_revision"] = args["expected_revision"]
    if args.get("expected_checkpoint_id") is not None:
        advance_kwargs["expected_checkpoint_id"] = args["expected_checkpoint_id"]
    result = engine.advance_research(**advance_kwargs)
    with _exclusive_adaptive_writer(tid):
        current = _read_json(receipt_path)
        if (
            current is None
            or current.get("status") not in {"prepared", "committed"}
            or current.get("command_id") != command_id
            or current.get("input_digest") != input_digest
        ):
            return {
                "status": "rejected",
                "reason": "advance command receipt changed before commit",
            }
        if current.get("status") == "committed":
            committed = current.get("result")
            return committed if isinstance(committed, dict) else result
        _write_json_atomic(
            receipt_path,
            {**current, "status": "committed", "result": result},
        )
    return result


def _advance_after_selector(tid: str) -> dict[str, Any]:
    return handle_advance_research(
        {
            "thread_id": tid,
            "command_id": _blind_command_id(tid, "selector"),
        },
        load_settings(_repo_root()),
    )


def _blind_terminal_receipt_path(tid: str, receipt_digest: str) -> Path:
    return (
        _thread_dir(tid)
        / "production"
        / "reorientation"
        / "terminal_commands"
        / f"{receipt_digest[7:]}.json"
    )


def _validate_blind_terminal_receipt(
    receipt: dict[str, Any],
    *,
    receipt_digest: str,
    attestation: dict[str, Any],
    strong_receipt: dict[str, Any],
) -> None:
    expected = {
        "strong_result_receipt_sha256": receipt_digest,
        "attestation": attestation,
        "strong_result_receipt": strong_receipt,
    }
    if (
        receipt.get("version") != 1
        or receipt.get("status") not in {"prepared", "committed"}
        or any(receipt.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("strong terminal receipt conflicts with durable state")


def _prepare_blind_strong_terminal(
    tid: str,
    *,
    receipt_digest: str,
    state_input_digest: str,
    attestation: dict[str, Any],
    strong_receipt: dict[str, Any],
) -> Path:
    path = _blind_terminal_receipt_path(tid, receipt_digest)
    current = _read_json(path)
    if current is None and path.exists():
        raise ValueError("strong terminal receipt is malformed")
    stable_payload = {
        "strong_result_receipt_sha256": receipt_digest,
        "attestation": attestation,
        "strong_result_receipt": strong_receipt,
    }
    if current is not None:
        _validate_blind_terminal_receipt(
            current,
            receipt_digest=receipt_digest,
            attestation=attestation,
            strong_receipt=strong_receipt,
        )
        return path
    _write_json_atomic(
        path,
        {
            "version": 1,
            "status": "prepared",
            "state_input_digest": state_input_digest,
            **stable_payload,
        },
    )
    return path


def _commit_blind_strong_terminal(
    tid: str,
    *,
    receipt_digest: str,
    state_input_digest: str,
    attestation: dict[str, Any],
    strong_receipt: dict[str, Any],
) -> dict[str, Any]:
    from research_harness.orchestrator.blind_sequential_research import (
        BlindSequentialResearchError,
    )

    try:
        with _exclusive_adaptive_writer(tid):
            receipt_path = _prepare_blind_strong_terminal(
                tid,
                receipt_digest=receipt_digest,
                state_input_digest=state_input_digest,
                attestation=attestation,
                strong_receipt=strong_receipt,
            )
        result = _build_blind_research_engine(tid).advance_research(
            command_id=_blind_command_id(
                tid,
                f"strong_terminal:{receipt_digest}",
                bind_state=False,
            ),
            expected_strong_result_receipt_sha256=receipt_digest,
        )
    except (BlindSequentialResearchError, OSError, ValueError) as exc:
        return {"status": "rejected", "reason": str(exc)}
    if result.get("status") != "goal_achieved":
        return result
    try:
        with _exclusive_adaptive_writer(tid):
            current = _read_json(receipt_path)
            if current is None:
                raise ValueError("strong terminal receipt changed before commit")
            _validate_blind_terminal_receipt(
                current,
                receipt_digest=receipt_digest,
                attestation=attestation,
                strong_receipt=strong_receipt,
            )
            if current.get("status") == "prepared":
                _write_json_atomic(
                    receipt_path,
                    {
                        **current,
                        "status": "committed",
                        "reorientation_result": result,
                    },
                )
    except (OSError, ValueError) as exc:
        return {"status": "rejected", "reason": str(exc)}
    return result


def recover_prepared_blind_terminal(
    tid: str,
    *,
    repo_root: Path,
) -> dict[str, Any] | None:
    with _repo_root_scope(repo_root):
        return _recover_prepared_blind_terminal(tid)


def _recover_prepared_blind_terminal(tid: str) -> dict[str, Any] | None:
    thread_dir = _thread_dir(tid)
    directory = (
        thread_dir / "production" / "reorientation" / "terminal_commands"
    )
    if not directory.exists():
        return None
    prepared = []
    for path in sorted(directory.glob("*.json")):
        receipt = _read_json(path)
        if receipt is None:
            return {"status": "rejected", "reason": "terminal receipt is malformed"}
        if receipt.get("status") == "prepared":
            prepared.append(receipt)
    if not prepared:
        return None
    if len(prepared) != 1:
        return {
            "status": "rejected",
            "reason": "multiple prepared strong terminal receipts exist",
        }
    pending = prepared[0]
    attestation = pending.get("attestation")
    expected_digest = pending.get("strong_result_receipt_sha256")
    if not isinstance(attestation, dict) or not isinstance(expected_digest, str):
        return {"status": "rejected", "reason": "terminal receipt is malformed"}
    return handle_submit_professor_user_goal_attestation(
        {"thread_id": tid, "attestation": attestation},
        expected_strong_result_receipt_sha256=expected_digest,
    )


def _professor_source_digest(tid: str, node_id: str) -> str | None:
    from research_harness.data_adapters import AdapterError, fingerprint_path

    source = (
        _thread_dir(tid)
        / "production"
        / "professor_templates"
        / node_id
        / "src"
    )
    if not source.exists():
        return None
    try:
        digest, _, _ = fingerprint_path(source)
    except (AdapterError, OSError):
        return None
    return "sha256:" + digest


def _persisted_experiment_id(state_path: Path, node_id: str) -> str | None:
    from research_harness.orchestrator.adaptive_search import (
        experiment_fingerprint,
    )

    plan = _read_json(
        state_path.parent / "nodes" / node_id / "experiment_plan.json"
    )
    return experiment_fingerprint(plan) if isinstance(plan, dict) else None


def handle_get_next_admissible_node(args: dict[str, Any]) -> dict[str, Any]:
    tid = args["thread_id"]
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    if not state_path.exists():
        return _advance_after_selector(tid)
    with _exclusive_adaptive_writer(tid):
        result = _handle_get_next_admissible_node_locked(args)
    if result.get("status") == _BLIND_REORIENTATION_REQUIRED:
        return _advance_after_selector(tid)
    return result


def _handle_get_next_admissible_node_locked(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Selector with resume support.

    Priority order:
      1. Render a verified terminal result that still lacks publication files.
      2. Advance blind research unless exactly one durable active attempt is
         awaiting evidence.
      3. Resume, retry, or dispatch only that attempt's materialized node.
    """
    from research_harness.orchestrator.search_state import (
        transition_node,
        validate_search_state,
    )

    tid = args["thread_id"]
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path)
    if not state or not state.get("nodes"):
        return _blind_reorientation_required(
            "no authoritative blind direction has been materialized"
        )
    raw_adaptive = state.get("adaptive")
    existing_adaptive = raw_adaptive if isinstance(raw_adaptive, dict) else {}
    raw_attestation = _read_json(
        _rebuttal_dir(tid) / "user_goal_attestation.json"
    )
    achieved_attestation = (
        raw_attestation if isinstance(raw_attestation, dict) else {}
    )
    if (
        existing_adaptive.get("disposition") == "goal_achieved"
        and achieved_attestation.get("achieved") is True
    ):
        from research_harness.thread_supervisor import is_terminal

        terminal_verified, _ = is_terminal(
            _repo_root(),
            tid,
            require_rendered=False,
        )
        if not terminal_verified:
            return {
                "status": "hard_external_block",
                "code": "operator_scope_conflict",
                "required_external_action": (
                    "Repair or remove the invalid adaptive strong-result receipt "
                    "before resuming research."
                ),
            }
        publication = _publication_dir(tid)
        submission_status = _read_json(publication / 'submission_status.json') or {}
        next_tool = 'prepare_paper_writing_context'
        if (publication / 'publication_receipt.json').exists():
            next_tool = 'finalize_submission_package'
            from research_harness.confirmation_sampling import _hash
            if (submission_status.get('status') in {'revision_required', 'export_failed'}
                    and submission_status.get('readiness', {}).get('manuscript_sha256') == _hash(publication / 'paper.html')):
                next_tool = 'prepare_paper_writing_context'
        return {
            "status": "goal_achieved_render_pending",
            "promoted_node_id": achieved_attestation.get("promoted_node_id"),
            "next_tool_to_call": next_tool,
            "reason": (
                "The verified strong-result receipt and achieved attestation "
                "exist, but the supervisor terminal also requires rendered "
                "publication artifacts, independent manuscript review and an official-template PDF/source package. Finish "
                "the paper writer; do not resume search or rebuttal."
            ),
        }
    authoritative_node_id = _authoritative_active_node_id(tid)
    if authoritative_node_id is None:
        return _blind_reorientation_required(
            "no authoritative active blind node is awaiting evidence"
        )
    had_adaptive_state = isinstance(state.get("adaptive"), dict)
    try:
        adaptive = _ensure_adaptive_state(tid, state)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "paused_needs_expansion",
            "search_disposition": "paused_needs_expansion",
            "reason": f"cannot freeze problem-level research goal: {exc}",
            "next_tool_to_call": "resume_production_state",
        }
    if not had_adaptive_state:
        _write_search_state_atomic(state_path, state)

    bar_sanity = _bar_sanity_gate_status(tid)
    if bar_sanity is not None and bar_sanity["state"] != "ok":
        if bar_sanity["state"] == "required":
            return {
                "status": "bar_sanity_required",
                "next_tool_to_call": "submit_bar_sanity_result",
                "reason": (
                    "require_skill_isolation is set: before the search spends a "
                    "cycle, rule out that your OWN bar is the binding constraint. "
                    "Run a NO-SKILL exposure baseline (e.g. a leveraged buy-and-"
                    "hold sweep) through the SAME deployment metric and report it "
                    "with submit_bar_sanity_result(no_skill_exposure_metric=<best "
                    "value a zero-skill exposure reaches>, detail=<sweep + how "
                    "computed>). A bar a zero-skill exposure clears measures "
                    "exposure, not skill. The world is ground-truth — suspect "
                    "the instrument before spending the search."
                ),
            }
        # state == "broken": the reported no-skill exposure clears the bar.
        pred = bar_sanity.get("predicate") or {}
        return {
            "status": "bar_broken",
            "next_tool_to_call": "submit_bar_sanity_result",
            "no_skill_exposure_metric": bar_sanity.get("no_skill_exposure_metric"),
            "predicate": pred,
            "reason": (
                f"bar-sanity gate (require_skill_isolation): a NO-SKILL exposure "
                f"baseline reaches {bar_sanity.get('no_skill_exposure_metric')} on "
                f"metric {pred.get('metric')!r}, which CLEARS your bar "
                f"({pred.get('op')} {pred.get('threshold')}). A bar zero-skill "
                "exposure (leverage) clears measures EXPOSURE, not skill — the "
                "search would be graded against a mis-specified bar. Revise it to "
                "isolate skill (cash-relative / risk-adjusted / exposure-matched), "
                "re-run the baseline, and do NOT spend the search against an "
                "exposure-gameable bar. The search is blocked until the bar is "
                "fixed."
            ),
        }

    strong_candidate = _strong_candidate_for_terminal(
        tid,
        state,
        authoritative_node_id=authoritative_node_id,
    )
    if strong_candidate is not None:
        readiness = _read_json(
            _thread_dir(tid) / "production" / "tree" / "mcp_readiness.json"
        ) or {}
        if (
            readiness.get("submit") is not True
            or readiness.get("node_id") != authoritative_node_id
        ):
            return {
                "status": "strong_candidate_ready",
                **strong_candidate,
                "next_tool_to_call": "decide_publication_readiness",
                "reason": (
                    "An adaptive strategy has supported runner evidence and a "
                    "mandatory-baseline pass. Stop spending the frontier and move "
                    "this candidate through rebuttal and the remaining strong-result gates."
                ),
            }
        return {
            "status": "rebuttal_ready",
            **strong_candidate,
            "next_tool_to_call": "prepare_rebuttal_packet",
            "reason": (
                "Publication readiness is recorded for the verified strong "
                "candidate; begin rebuttal rather than dispatching more search nodes."
            ),
        }

    nodes_by_id = {n["id"]: n for n in state["nodes"]}

    contract = _read_json(_thread_dir(tid) / 'production/reorientation/goal_contract.json') or {}
    if (contract.get('baseline_evidence') == [] and authoritative_node_id in nodes_by_id
            and not (_thread_dir(tid) / 'market/baseline_qualification.json').exists()):
        from research_harness.orchestrator.research_control import PLANNING_POLICY_VERSION, current_work
        work = current_work(_thread_dir(tid))
        return {'status': 'preparation_work', 'node_id': authoritative_node_id,
                'research_work': work,
                'next_tool_to_call': work['next_tool_to_call'] if work.get('status') == 'planned' and work.get('planning_policy_version') == PLANNING_POLICY_VERSION else 'plan_research_work',
                'reason': 'The claim exists, but comparative evidence needs qualified baselines. Resolve the recorded research uncertainty; submit qualification when the development evidence supports it. Do not repeat claim critic reviews while this prerequisite is pending.'}

    for node in state["nodes"]:
        if node.get("id") != authoritative_node_id:
            continue
        if node.get("status") != "critic_reviewed":
            continue
        if args.get("_allow_evidence_retry", True) is not True:
            break
        from research_harness.orchestrator.attempt_evidence import (
            ConclusiveFailure,
            StrongCandidate,
            derive_attempt_evidence,
        )

        worker_report = _read_json(
            state_path.parent / "nodes" / node["id"] / "worker_report.json"
        )
        evidence = derive_attempt_evidence(
            worker_report,
            data_status="satisfied",
        )
        if isinstance(evidence, (ConclusiveFailure, StrongCandidate)):
            continue
        transition_node(
            state,
            node["id"],
            "ready",
            event="inconclusive_evidence_retry",
            reason=evidence.reason.value,
        )
        node["outputs"]["evidence_retry"] = {
            "reason": evidence.reason.value,
            "prior_experiment_id": _persisted_experiment_id(
                state_path,
                node["id"],
            ),
            "prior_source_digest": _professor_source_digest(
                tid,
                node["id"],
            ),
        }
        state["status"] = "running"
        adaptive["revision"] = int(adaptive["revision"]) + 1
        validate_search_state(state)
        _write_search_state_atomic(state_path, state)
        return {
            "status": "retry_evidence",
            "node_id": node["id"],
            "node_type": node.get("type"),
            "evidence_reason": evidence.reason.value,
            "next_tool_to_call": "design_experiment_template",
            "reason": (
                "The persisted run is not a scientific failure. Revise this "
                "direction's experiment template to produce evaluable evidence "
                "before promotion or pruning."
            ),
        }

    # --- Resume path: pick up where the previous session stopped. -----
    incomplete = [
        node
        for node in state["nodes"]
        if node.get("id") == authoritative_node_id
        and node.get("status") in _RESUME_NEXT_TOOL
    ]
    if incomplete:
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

    candidates: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    unavailable_nodes: list[tuple[str, list[str]]] = []
    available_capabilities = _adaptive_capabilities(tid)
    from research_harness.orchestrator.adaptive_search import (
        normalize_required_capabilities,
    )
    for item in state.get("frontier", []):
        if item.get("node_id") != authoritative_node_id:
            continue
        if item.get("status") != "queued":
            continue
        node = nodes_by_id.get(item["node_id"])
        if not node:
            continue
        components = item.get("priority_components") or {}
        strategy = node.get("strategy") or {}
        required_capabilities = normalize_required_capabilities(
            strategy.get("required_capabilities") or []
        )
        missing = sorted(required_capabilities - available_capabilities)
        if strategy and missing:
            unavailable_nodes.append((item["node_id"], missing))
            continue
        if strategy and float(components.get("capability_fit", 0.0)) <= 0.0:
            components = dict(components)
            components["capability_fit"] = 1.0
            components["score"] = round(
                float(components.get("score", 0.0)) + 1.0,
                6,
            )
            item["priority_components"] = components
            item["priority"] = components["score"]
        candidates.append(
            (int(item.get("depth", 0)), item["node_id"], node, item)
        )
    if not candidates:
        for unavailable_node_id, missing in unavailable_nodes:
            unavailable_node = nodes_by_id[unavailable_node_id]
            if unavailable_node.get("status") != "ready":
                continue
            transition_node(
                state,
                unavailable_node_id,
                "blocked",
                event="capability_unavailable_for_blind_reorientation",
                reason="missing capabilities: " + ", ".join(missing),
            )
        if unavailable_nodes:
            state["status"] = "blocked"
            adaptive["pause"] = None
            adaptive["disposition"] = "continue"
            adaptive["revision"] = int(adaptive["revision"]) + 1
            validate_search_state(state)
            _write_search_state_atomic(state_path, state)
        return _blind_reorientation_required(
            "the authoritative active node is not runnable"
        )
    depth, nid, node, frontier_item = candidates[0]
    if adaptive.get("pause") is not None:
        adaptive["pause"] = None
        adaptive["disposition"] = "continue"
        adaptive["revision"] = int(adaptive["revision"]) + 1
        state["status"] = "running"
        validate_search_state(state)
        _write_search_state_atomic(state_path, state)
    response = {
        "status": "ok",
        "active_stage": "blind_sequential_research",
        "next_tool_to_call": "design_experiment_template",
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
        "research_goal": adaptive["goal"],
    }
    response["priority_components"] = frontier_item.get(
        "priority_components"
    ) or {}
    response["strategy"] = node.get("strategy") or {}
    response["_selection_policy"] = (
        f"Picked {nid}, the sole node bound to the active blind direction."
    )
    return response


def handle_submit_bar_sanity_result(args: dict[str, Any]) -> dict[str, Any]:
    """Record the no-skill exposure baseline for the cycle-1 skill-isolation
    gate. The Professor runs a NO-SKILL exposure sweep (e.g. a leveraged buy-
    and-hold sweep) through the SAME deployment metric the search is graded on
    and reports the best value it reaches; the harness checks that reported
    number against the deployment predicate deterministically (air-gap — it
    trusts the scalar and owns only the pass/fail, exactly as
    compute_falsifier_result does). If the no-skill exposure clears the bar, the
    bar measures exposure not skill and get_next_admissible_node blocks the
    search until the bar is revised. Recording is unconditional; the gate (which
    only fires when require_skill_isolation is set) decides whether to act on
    it."""
    tid = args["thread_id"]
    metric = args.get("no_skill_exposure_metric")
    if metric is None:
        return {
            "status": "rejected",
            "reason": (
                "submit_bar_sanity_result requires no_skill_exposure_metric — the "
                "best value a NO-SKILL exposure baseline (e.g. a leveraged buy-and-"
                "hold sweep) reaches on the SAME deployment metric the search is "
                "graded on."
            ),
        }
    try:
        metric_f = float(metric)
    except (TypeError, ValueError):
        return {"status": "rejected", "reason": "no_skill_exposure_metric must be a number."}
    pdir = _thread_dir(tid) / "production"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "bar_sanity.json").write_text(
        json.dumps(
            {"no_skill_exposure_metric": metric_f, "detail": args.get("detail", "")},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"status": "accepted", "bar_sanity": _bar_sanity_gate_status(tid)}


def handle_resume_production_state(args: dict[str, Any]) -> dict[str, Any]:
    """Operator-invoked recovery: if a node has been stuck in `running` for
    a long time (the agent session died before execute_node_experiment
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
    """Independently review evaluation criteria within the existing resource envelope."""
    from research_harness.orchestrator.research_review import review_research_packet
    from research_harness.schemas.validator import validate_named_schema
    from research_harness.adapters.codex_cli import CodexCliError

    tid = args["thread_id"]
    env = args["envelope"]
    try:
        validate_named_schema("feasibility_envelope", env)
    except ValueError as exc:
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}
    if env.get("thread_id") != tid:
        return {"status": "rejected", "reason": "envelope thread_id mismatch"}
    with _exclusive_adaptive_writer(tid):
        tdir = _thread_dir(tid)
        existing = _read_json(tdir / "production/feasibility_envelope.json")
        if existing == env:
            return {"status": "ok", "reason": "unchanged registration"}
        if not existing:
            return {"status": "rejected", "reason": "Supervisor must initialize the resource envelope before protocol review."}
        if (tdir / "production/reorientation/goal_contract.json").exists():
            return {"status": "rejected", "reason": "goal contract is frozen; start a new research revision"}
        for field in ("data_sources_available", "llm_oracles_available", "runtime_capabilities", "operator_intent", "execution_constraints"):
            if env.get(field) != existing.get(field):
                return {"status": "rejected", "reason": f"Protocol submission cannot change initialized {field}."}
        if any(value > existing["compute_budget"][key] for key, value in env["compute_budget"].items()):
            return {"status": "rejected", "reason": "Protocol submission cannot increase the compute budget."}
        evidence = {}
        for path in sorted((tdir / "production/tree/baseline_preflight").glob("*/worker_report.json")):
            evidence[str(path)] = _read_json(path)
        packet = {"proposal": env, "resource_envelope": existing,
                  "registration_state": "proposed_not_installed",
                  "commit_on_approval": "The harness atomically installs the reviewed envelope and stamps its registrant and attestation ceiling after approval. The agent cannot install it before this review.",
                  "research_problem": _read_json(tdir / "thread.json"),
                  "baseline_qualification": _read_json(tdir / "market/baseline_qualification.json"),
                  "development_evidence": evidence}
        try:
            review = review_research_packet(
                _repo_root(), tdir / "production/protocol_reviews", packet,
                purpose="Register meaningful prospective evaluation criteria, independent of inspected development results.",
            )
        except (OSError, ValueError, CodexCliError) as exc:
            return {"status": "rejected", "reason": f"Independent protocol review failed: {exc}"}
        if review["assessment"]["decision"] != "approve":
            return {"status": "rejected", "review": review, "next_step": "Revise the protocol to resolve the review findings and resubmit."}
        return _register_feasibility_envelope(args, settings, thread_dir=tdir)


def _register_feasibility_envelope(
    args: dict[str, Any], settings: dict[str, Any], *, thread_dir: Path | None = None,
) -> dict[str, Any]:
    """Persist a protocol after independent review; not an exposed tool."""
    from research_harness.schemas.validator import validate_named_schema
    tid = args["thread_id"]
    tdir = thread_dir if thread_dir is not None else _thread_dir(tid)
    env = json.loads(json.dumps(args["envelope"]))
    try:
        validate_named_schema("feasibility_envelope", env)
    except Exception as exc:  # noqa: BLE001
        return {"status": "rejected", "reason": f"schema validation failed: {exc}"}
    if env["thread_id"] != tid:
        return {"status": "rejected", "reason": "envelope thread_id mismatch"}
    if env.get("external_falsifier", {}).get("kind", "none") != "none":
        env["external_falsifier"]["registered_by"] = "adversary_pass"

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

    env_path = tdir / "production" / "feasibility_envelope.json"
    if (tdir / "production" / "reorientation" / "goal_contract.json").exists():
        if _read_json(env_path) != env:
            return {"status": "rejected", "reason": "goal contract is frozen; start a new research revision"}
    state = _read_json(
        tdir / "production" / "tree" / "search_state.json"
    ) or {}
    adaptive = state.get("adaptive")
    if isinstance(adaptive, dict):
        frozen_bar = (adaptive.get("goal") or {}).get("bar") or {}
        proposed_intent = env.get("operator_intent") or {}
        frozen_fields = {
            "target_scope": frozen_bar.get("target_scope"),
            "data_source_anchor": frozen_bar.get("data_source_anchor"),
            "data_source_snapshot_id": frozen_bar.get("data_source_snapshot_id"),
            "external_falsifier": frozen_bar.get("external_falsifier") or {},
        }
        proposed_fields = {
            "target_scope": proposed_intent.get("target_deploy_grade_scope"),
            "data_source_anchor": proposed_intent.get("data_source_anchor"),
            "data_source_snapshot_id": proposed_intent.get("data_source_snapshot_id"),
            "external_falsifier": env.get("external_falsifier") or {},
        }
        if frozen_fields != proposed_fields:
            return {
                "status": "rejected",
                "reason": (
                    "the adaptive research goal is already frozen; target scope, "
                    "data selection, snapshot, and external falsifier cannot be "
                    "overwritten in place. Start a new goal revision instead."
                ),
                "frozen": frozen_fields,
                "proposed": proposed_fields,
            }
    env_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(env_path, env)
    return {
        "status": "ok",
        "envelope_path": str(env_path),
        "registered_real_adapters": sorted(registered),
        "target_scope": target,
        "max_attestable_status": env["max_attestable_status"],
        "next_tool_to_call": "advance_research",
        "next_step": (
            "Call advance_research to freeze the success bar and generate the research claim. The claim's "
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
    with _exclusive_adaptive_writer(args["thread_id"]):
        return _handle_compute_falsifier_result_locked(args)


def _handle_compute_falsifier_result_locked(args: dict[str, Any]) -> dict[str, Any]:
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
        binding = _authoritative_strong_binding(tid)
        if binding is None:
            return {"status": "rejected", "reason": "falsifier requires an authoritative active direction"}
        if falsifier.get("kind") == "real_holdout":
            from contextlib import nullcontext
            from research_harness.orchestrator.confirmation_use import consume_confirmation, digest_confirmation_evidence

            from research_harness.confirmation_sampling import active_sampling_registration
            if active_sampling_registration(_thread_dir(tid)):
                from dataclasses import asdict
                from research_harness.orchestrator.confirmation_execution import verified_confirmation_receipt
                receipt = verified_confirmation_receipt(_thread_dir(tid), asdict(binding))
                evidence = {**evidence, "observed": receipt["observed"]}
            engine = _build_blind_research_engine(tid)
            contract = engine.read_goal_contract()
            consume_confirmation(
                engine.paths.confirmation_use, contract,
                binding={"contract_id": binding.contract_id, "attempt_id": binding.attempt_id,
                         "direction_id": binding.direction_id, "node_id": binding.node_id,
                         "manifest_id": binding.manifest_id},
                evidence_digest=digest_confirmation_evidence(contract, evidence), writer_lock=nullcontext,
            )
        result = compute_falsifier_result(
            thread_id=tid,
            falsifier=falsifier,
            evidence=evidence,
            measured_behavioral_distance=measured_distance,
            measured_known_baseline_transfer=measured_known,
            guard_thresholds=guard_thresholds,
        )
        binding = _authoritative_strong_binding(tid)
        if binding is None:
            return {
                "status": "rejected",
                "reason": (
                    "falsifier result requires one authoritative active blind "
                    "direction"
                ),
            }
        result.update(
            {
                "contract_id": binding.contract_id,
                "attempt_id": binding.attempt_id,
                "direction_id": binding.direction_id,
                "node_id": binding.node_id,
                "manifest_id": binding.manifest_id,
            }
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
            "This active direction is conclusively closed. Call advance_research "
            "so the harness records the private lesson and generates a structurally "
            "different blind direction."
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
    bd_path = _thread_dir(tid) / "market" / "baseline_dossier_candidate.yaml"
    dump_signals = _detect_deterministic_dump_dossier(bd_path)
    if dump_signals:
        return {
            "status": "rejected",
            "reason": (
                "baseline_dossier_candidate.yaml is a deterministic dump. The operator "
                "must review the market_research candidates and resolve the placeholder "
                "before production can start. Signals: " + "; ".join(dump_signals)
            ),
        }
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
    return {
        "status": "accepted",
        "new_contract": new_claim,
        "search_state_initialized": False,
        "next_step": "Call advance_research to freeze the contract and generate one blind direction.",
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
    tid = args["thread_id"]
    node_id = args["node_id"]
    rejected = _require_authoritative_node(tid, node_id)
    if rejected is not None:
        return rejected
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
    with _exclusive_adaptive_writer(args['thread_id']):
        try:
            return _handle_design_experiment_template_locked(args)
        except (OSError, ValueError) as exc:
            return {'status': 'rejected', 'reason': str(exc)}


def _handle_design_experiment_template_locked(args: dict[str, Any]) -> dict[str, Any]:
    """Persist Professor-authored experiment code (source_files dict) to the
    thread's professor_templates/<node_id>/ + _lib/ on disk."""
    from research_harness.orchestrator.experiment_plan import (
        _professor_template_root,
        python_source_diagnostics,
        resolve_source_files,
        write_professor_template,
    )

    tid = args["thread_id"]
    node_id = args.get("node_id")
    if not args.get('work_id'):
        if not node_id:
            raise ValueError('Supply the current implementation work_id or an authoritative node_id')
        rejected = _require_authoritative_node(tid, node_id)
        if rejected is not None:
            return rejected
    plan_meta = args["plan_metadata"]
    if not isinstance(plan_meta, dict):
        return {"status": "rejected", "reason": "plan_metadata must be an object"}
    source_files = plan_meta.get("source_files") or []
    if not isinstance(source_files, list):
        return {"status": "rejected", "reason": "plan_metadata.source_files must be a list"}
    source_files = resolve_source_files(_thread_dir(tid), source_files)
    plan_meta = {**plan_meta, 'source_files': source_files}

    if args.get('work_id'):
        from research_harness.orchestrator.research_control import current_work, _digest, _write, development_evidence, PLANNING_POLICY_VERSION
        from research_harness.confirmation_sampling import _hash
        thread = _thread_dir(tid)
        work = current_work(thread)
        if (thread / 'production/confirmation_execution.json').exists():
            raise ValueError('Confirmation is reserved; implementation changes are closed')
        if work.get('work_id') != args['work_id']:
            raise ValueError('Supply the current research work_id')
        if work.get('status') == 'completed' and work.get('outcome', {}).get('execution_result') == 'implementation_prepared':
            if work['outcome']['template_digest'] != _digest(plan_meta):
                raise ValueError('This work already prepared another implementation')
            for item in work['outcome']['source_files']:
                if _hash(Path(item['path'])) != item['sha256']:
                    raise ValueError('Prepared implementation changed')
            return {**work, 'research_work_checkpoint': work['work_id']}
        if work.get('decision', {}).get('kind') not in {
            'protocol_revision', 'diagnostic_experiment', 'competence', 'comparison', 'replication', 'confirmation'
        }:
            raise ValueError('Prepare source within the planned execution or protocol-revision work, not a source-only analysis')
        if (work.get('status') != 'planned' or work.get('planning_policy_version') != PLANNING_POLICY_VERSION
                or work.get('protocol_digest') != _digest(_read_json(thread / 'production/feasibility_envelope.json'))
                or work.get('evidence_digest') != _digest(development_evidence(thread))):
            raise ValueError('Implementation work is stale; call plan_research_work')
        if not source_files:
            raise ValueError('Implementation preparation requires source files')
        template_digest = _digest(plan_meta)
        existing = work.get('prepared_implementation', {})
        if existing.get('template_digest') == template_digest:
            for item in existing['source_files']:
                if _hash(Path(item['path'])) != item['sha256']:
                    raise ValueError('Prepared implementation changed')
            return {**work, 'preparation_checkpoint': template_digest}
        work_dir = thread / 'production/research_control/work' / work['work_id']
        draft = work_dir / 'implementation_revisions' / template_digest
        files = write_professor_template(draft, draft, plan_meta)
        _write(work_dir / 'implementation_requests' / (template_digest + '.json'), args['plan_metadata'])
        sources = [{'path': str((draft / relative).resolve()), 'relative_path': relative,
                    'sha256': _hash(draft / relative)} for relative in files]
        from research_harness.orchestrator.experiment_plan import PLAN_METADATA_FILENAME
        prepared = {'execution_result': 'implementation_prepared',
                    'evidence_id': 'implementation_' + work['work_id'] + '_' + template_digest,
                    'template_digest': template_digest, 'source_files': sources,
                    'plan_metadata_path': str((draft / PLAN_METADATA_FILENAME).resolve()),
                    'source_diagnostics': python_source_diagnostics(source_files),
                    'new_observation': False, 'scientific_verdict': 'unverified'}
        _write(work_dir / 'implementation_preparations' / (template_digest + '.json'), prepared)
        work['prepared_implementation'] = prepared
        _write(thread / 'production/research_control/current.json', work)
        _write(thread / 'production/research_control/work' / work['work_id'] / 'work.json', work)
        return {**work, 'preparation_checkpoint': template_digest}

    # (가) Operator-mandated module gate — deterministic, NOT an LLM critic. If the
    # feasibility_envelope declares execution_constraints.required_modules, the
    # submitted experiment source MUST import each one. A cheap boundary string-scan
    # so the success-seeking Professor cannot hand-roll around an operator-registered
    # evaluator/tool (general: the modules are whatever the operator declared).
    from research_harness.settings_scoped import resolve_for_thread as _rft
    # Thread-scoped setting (frontend-editable, ADR 0005) ∪ envelope (MCP-set).
    _required: list[str] = list(
        _rft(_repo_root(), tid).get_dotted("execution_constraints.required_modules", []) or []
    )
    _env = _read_json(_thread_dir(tid) / "production" / "feasibility_envelope.json") or {}
    _required += list((_env.get("execution_constraints") or {}).get("required_modules") or [])
    _required = list(dict.fromkeys(m for m in _required if isinstance(m, str) and m.strip()))
    if _required:
        import re as _re
        _blob = "\n".join(
            str(sf.get("content") or "") for sf in source_files if isinstance(sf, dict)
        )
        _missing = [
            m for m in _required
            if isinstance(m, str) and m.strip()
            and not _re.search(
                r"(?:^|\n)[ \t]*(?:import|from)[ \t]+" + _re.escape(m.strip()) + r"(?:\b|\.)",
                _blob,
            )
        ]
        if _missing:
            return {
                "status": "rejected",
                "reason": (
                    "feasibility_envelope.execution_constraints.required_modules "
                    f"mandates {_required}, but the submitted experiment source does "
                    f"not import {_missing}. The operator registered these as this "
                    "thread's mandatory evaluation/tooling (importable in the harness "
                    "env). Import and use them — do NOT hand-roll a substitute."
                ),
            }

    # Operator-mandated DATA gate — the sibling of required_modules, same
    # deterministic boundary scan. If the thread declares
    # execution_constraints.required_data_sources, the submitted source MUST
    # reference each declared marker (the registered real-data adapter id / path /
    # loader symbol) so the success-seeking Professor cannot quietly evaluate on
    # self-authored synthetic data instead of the registered REAL data. NOT an
    # import pattern (data is referenced by id/path, not imported): a plain
    # presence scan. General: the markers are whatever the operator declared.
    _required_data: list[str] = list(
        _rft(_repo_root(), tid).get_dotted(
            "execution_constraints.required_data_sources", []
        ) or []
    )
    _required_data += list(
        (_env.get("execution_constraints") or {}).get("required_data_sources") or []
    )
    _required_data = list(
        dict.fromkeys(d for d in _required_data if isinstance(d, str) and d.strip())
    )
    if _required_data:
        _blob_d = "\n".join(
            str(sf.get("content") or "") for sf in source_files if isinstance(sf, dict)
        )
        _missing_data = [d for d in _required_data if d.strip() not in _blob_d]
        if _missing_data:
            return {
                "status": "rejected",
                "reason": (
                    "feasibility_envelope.execution_constraints.required_data_sources "
                    f"mandates {_required_data}, but the submitted experiment source "
                    f"does not reference {_missing_data}. The operator registered these "
                    "as this thread's mandatory REAL data (load it via the harness env, "
                    "e.g. COIN_DATA_DIR). Reference and evaluate on the registered real "
                    "data — do NOT substitute self-authored synthetic data."
                ),
            }

    tree_dir = _thread_dir(tid) / "production" / "tree"
    template_root = _professor_template_root(tree_dir)
    node_dir = template_root / node_id
    from research_harness.workers.workspace import ensure_path_inside
    ensure_path_inside(template_root, _thread_dir(tid) / 'production', 'template root')
    files_written = write_professor_template(template_root, node_dir, plan_meta)
    return {
        "status": "accepted",
        "files_written": files_written,
        "source_diagnostics": python_source_diagnostics(source_files),
        "node_template_dir": str(node_dir),
        "_lib_dir": str(template_root / "_lib"),
    }


def handle_execute_node_experiment(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.orchestrator.research_control import current_work, finish_work

    with _exclusive_adaptive_writer(args["thread_id"]):
        result = _handle_execute_node_experiment_locked(args)
        work = current_work(_thread_dir(args['thread_id']))
        if work.get('status') == 'running' and work.get('binding', {}).get('node_id') == args['node_id']:
            return finish_work(_thread_dir(args['thread_id']), result)
        return result


def _handle_execute_node_experiment_locked(args: dict[str, Any]) -> dict[str, Any]:
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
    rejected = _require_authoritative_node(tid, node_id)
    if rejected is not None:
        return rejected
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
    evidence_retry = (node.get("outputs") or {}).get("evidence_retry")

    run_dir = state_path.parent
    node_run_dir = run_dir / "nodes" / node_id
    node_run_dir.mkdir(parents=True, exist_ok=True)

    contract = node["claim_contract"]
    adapter_id = contract.get("data_source_anchor")
    snapshot_id = contract.get("data_source_snapshot_id")
    runtime_inputs = None
    if isinstance(adapter_id, str) and adapter_id.startswith(
        "acquisition_manifest:"
    ):
        from research_harness.acquisition import (
            AcquisitionContractError,
            AcquisitionStorageError,
            PublicAcquisition,
        )
        from research_harness.runtime_inputs import (
            RuntimeInputError,
            bind_acquisition_manifest,
        )

        manifest_id = adapter_id.split(":", 1)[1]
        expected_snapshot_id = "as_" + manifest_id.removeprefix(
            "acqmanifest_"
        )
        artifacts = (node.get("outputs") or {}).get("artifacts") or []
        if (
            snapshot_id != expected_snapshot_id
            or artifacts.count(f"acquisition_manifest:{manifest_id}") != 1
        ):
            return {
                "status": "rejected",
                "reason": "claim acquisition manifest binding is inconsistent",
            }
        cache_root = (
            _thread_dir(tid)
            / "production"
            / "reorientation"
            / "acquisition_cache"
        )
        try:
            manifest = PublicAcquisition(cache_root).verify_manifest(
                manifest_id,
                node_id=node_id,
            )
            runtime_inputs = bind_acquisition_manifest(
                manifest=manifest,
                cache_root=cache_root,
                workspace=node_run_dir / "workspace",
            )
        except (
            AcquisitionContractError,
            AcquisitionStorageError,
            RuntimeInputError,
            OSError,
        ) as exc:
            return {
                "status": "rejected",
                "reason": f"acquisition runtime input binding failed: {exc}",
            }
    elif adapter_id or snapshot_id:
        if not adapter_id or not snapshot_id:
            return {
                "status": "rejected",
                "reason": "claim has an incomplete registered adapter selection",
            }
        from research_harness.data_adapters import AdapterError, require_thread_snapshot
        from research_harness.runtime_inputs import RuntimeInputError, bind_runtime_input
        try:
            snapshot = require_thread_snapshot(
                _thread_dir(tid),
                adapter_id=str(adapter_id),
                snapshot_id=str(snapshot_id),
            )
            runtime_inputs = bind_runtime_input(
                snapshot=snapshot,
                workspace=node_run_dir / "workspace",
                repo_root=repo,
            )
        except (AdapterError, RuntimeInputError, OSError) as exc:
            return {"status": "rejected", "reason": f"runtime input binding failed: {exc}"}
    if contract.get("deploy_grade_scope") == "deployment" and runtime_inputs is None:
        return {
            "status": "rejected",
            "reason": "deployment experiment requires a bound runtime input",
        }

    plan, template_used = build_experiment_plan_for_node(
        repo, node, run_dir, settings=settings_local
    )
    if runtime_inputs is not None:
        plan["inputs"] = runtime_inputs
    validate_experiment_plan(node, plan, run_dir)
    from research_harness.orchestrator.adaptive_search import (
        experiment_fingerprint,
    )

    experiment_id = experiment_fingerprint(plan)
    if isinstance(evidence_retry, dict):
        prior_experiment_id = evidence_retry.get("prior_experiment_id")
        prior_source_digest = evidence_retry.get("prior_source_digest")
        current_source_digest = _professor_source_digest(tid, node_id)
        unchanged = (
            experiment_id == prior_experiment_id
            if isinstance(prior_experiment_id, str)
            else current_source_digest is None
            or current_source_digest == prior_source_digest
        )
        if unchanged:
            return {
                "status": "rejected",
                "reason": (
                    "inconclusive evidence requires a materially changed executable "
                    "plan or source code; prose-only edits do not qualify"
                ),
                "evidence_reason": evidence_retry.get("reason"),
                "next_tool_to_call": "design_experiment_template",
            }
    adaptive = state.get("adaptive")
    experiment_record: dict[str, Any] | None = None
    if isinstance(adaptive, dict):
        duplicate = next(
            (
                record
                for record in adaptive.get("experiments") or []
                if isinstance(record, dict)
                and record.get("id") == experiment_id
                and record.get("node_id") != node_id
            ),
            None,
        )
        if duplicate is not None:
            rejection = {
                "kind": "duplicate_experiment",
                "experiment_id": experiment_id,
                "node_id": node_id,
                "duplicate_of": duplicate.get("node_id"),
            }
            adaptive["duplicate_rejections"].append(rejection)
            adaptive["revision"] = int(adaptive["revision"]) + 1
            _write_search_state_atomic(state_path, state)
            return {
                "status": "rejected",
                "reason": "duplicate_experiment",
                "experiment_id": experiment_id,
                "duplicate_of": duplicate.get("node_id"),
                "adaptive_revision": adaptive["revision"],
            }
        experiment_record = next(
            (
                record
                for record in adaptive.get("experiments") or []
                if isinstance(record, dict)
                and record.get("id") == experiment_id
                and record.get("node_id") == node_id
            ),
            None,
        )
        if experiment_record is None:
            experiment_record = {
                "id": experiment_id,
                "node_id": node_id,
                "strategy_id": (node.get("strategy") or {}).get("id"),
                "status": "reserved",
            }
            adaptive["experiments"].append(experiment_record)
            adaptive["revision"] = int(adaptive["revision"]) + 1
    from research_harness.orchestrator.research_control import bind_work, current_work, review_work_implementation
    from research_harness.adapters.codex_cli import CodexCliError
    work = current_work(_thread_dir(tid))
    try:
        bind_work(_thread_dir(tid), args.get('work_id', work.get('work_id')), node_id, plan, scope='nodes')
    except ValueError as exc:
        return {'status': 'work_required', 'reason': str(exc), 'next_tool_to_call': 'plan_research_work'}
    try:
        manifest = build_job_manifest_from_experiment_plan(node, plan, run_dir)
        validate_named_schema("job_manifest", manifest)
        review_work_implementation(repo, _thread_dir(tid), work['work_id'], node, plan)
    except (OSError, ValueError, KeyError, TypeError, CodexCliError) as exc:
        from research_harness.orchestrator.research_review import ReviewContractError
        if isinstance(exc, ReviewContractError):
            return {'status': 'review_invalid', 'reason': str(exc), 'next_tool_to_call': 'execute_node_experiment'}
        return {'status': 'rejected', 'reason': f'experiment dispatch failed: {exc}',
                'next_tool_to_call': 'design_experiment_template'}
    (node_run_dir / "experiment_plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (node_run_dir / "job_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if node["status"] == "ready":
        transition_node(
            state, node_id, "running",
            event="mcp_dispatch",
            reason="MCP server execute_node_experiment after runtime input binding",
        )
        _write_search_state_atomic(state_path, state)
    runner = LocalRunner(run_dir, settings=settings_local)
    from research_harness.orchestrator.research_control import mark_experiment_running
    mark_experiment_running(_thread_dir(tid), work['work_id'])
    runner_result = runner.execute(manifest)
    validate_named_schema("runner_result", runner_result)
    if runtime_inputs is not None:
        from research_harness.runtime_inputs import (
            RuntimeInputError,
            validate_runtime_input_reference,
        )

        try:
            post_run_input = validate_runtime_input_reference(
                manifest["inputs"],
                workspace=Path(runner_result["workspace"]),
            )
        except (RuntimeInputError, OSError) as exc:
            transition_node(
                state,
                node_id,
                "ready",
                event="runtime_input_integrity_rejected",
                reason=str(exc),
            )
            if experiment_record is not None:
                experiment_record["status"] = "evidence_rejected"
            _write_search_state_atomic(state_path, state)
            return {
                "status": "rejected",
                "reason": f"runtime input changed during experiment execution: {exc}",
            }
        if post_run_input != runner_result.get("input_evidence"):
            transition_node(
                state,
                node_id,
                "ready",
                event="runtime_input_integrity_rejected",
                reason="runner input evidence changed after execution",
            )
            if experiment_record is not None:
                experiment_record["status"] = "evidence_rejected"
            _write_search_state_atomic(state_path, state)
            return {
                "status": "rejected",
                "reason": "runner input evidence changed after execution",
            }
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
    baseline_status = worker_report.get("baseline_evidence_status") or {}
    if baseline_status.get("overall") == "not_evaluable":
        reasons = [
            str(item.get("reason"))
            for item in baseline_status.get("results") or []
            if item.get("status") == "not_evaluable" and item.get("reason")
        ]
        transition_node(
            state,
            node_id,
            "ready",
            event="evidence_contract_rejected",
            reason="; ".join(reasons) or "baseline evidence is not evaluable",
        )
        if experiment_record is not None:
            experiment_record["status"] = "evidence_rejected"
        _write_search_state_atomic(state_path, state)
        return {
            "status": "rejected",
            "reason": (
                "experiment evidence did not satisfy the declared baseline contract: "
                + ("; ".join(reasons) or "baseline evidence is not evaluable")
                + ". Redesign the experiment output and execute this re-queued node "
                "again. Each metrics file must expose top-level numeric `metrics` "
                "and `baselines` values under the exact declared keys."
            ),
            "worker_status": worker_report["status"],
            "claim_verdict_candidate": worker_report.get("claim_verdict_candidate"),
            "baseline_evidence_overall": "not_evaluable",
            "required_evidence_keys": {
                "metrics": sorted(
                    {
                        item["metric_key"]
                        for item in manifest["baseline_evidence_requirements"]
                        if item["required"]
                    }
                ),
                "baselines": sorted(
                    {
                        item["baseline_key"]
                        for item in manifest["baseline_evidence_requirements"]
                        if item["required"]
                    }
                ),
            },
            "template_used": template_used,
        }
    transition_node(
        state, node_id, "completed_worker_report",
        event="worker_report",
        reason=f"worker status: {worker_report['status']}",
    )
    if experiment_record is not None:
        experiment_record["status"] = "executed"
    _write_search_state_atomic(state_path, state)
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
    from research_harness.schemas.validator import validate_named_schema
    from research_harness.critics.review_runner import run_critic_reviews as _run
    from research_harness.orchestrator.search_state import (
        transition_node,
        validate_search_state,
    )

    tid = args["thread_id"]
    node_id = args["node_id"]
    rejected = _require_authoritative_node(tid, node_id)
    if rejected is not None:
        return rejected
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


def handle_submit_professor_decision(
    args: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    tid = args["thread_id"]
    with _exclusive_adaptive_writer(args["thread_id"]):
        response = _handle_submit_professor_decision_locked(args, settings)
    if response.get("status") == "accepted" and args["next_transition"] == "pruned":
        command_id = _adaptive_command_id(args)
        advanced = handle_advance_research(
            {
                "thread_id": tid,
                "command_id": _blind_command_id(
                    tid,
                    f"professor:{command_id}",
                    bind_state=False,
                ),
            },
            settings,
        )
        response = dict(response)
        response["research_advance"] = advanced
        response["next_tool_to_call"] = advanced.get("next_tool_to_call")
    return response


def _handle_submit_professor_decision_locked(
    args: dict[str, Any], _settings: dict[str, Any]
) -> dict[str, Any]:
    from research_harness.orchestrator.search_state import (
        transition_node,
        validate_search_state,
    )

    tid = args["thread_id"]
    node_id = args["node_id"]
    transition = args["next_transition"]
    if "follow_up_children" in args:
        return {
            "status": "rejected",
            "reason": "follow_up_children is retired; submit only promoted or pruned",
        }
    if transition not in {"promoted", "pruned"}:
        return {
            "status": "rejected",
            "reason": "next_transition must be promoted or pruned",
        }
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path)
    if not state:
        return {"status": "rejected", "reason": "search_state.json missing"}
    try:
        adaptive = _ensure_adaptive_state(tid, state)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "rejected",
            "reason": f"cannot freeze problem-level research goal: {exc}",
        }
    command_id = _adaptive_command_id(args)
    prior_receipt = adaptive["command_receipts"].get(command_id)
    if isinstance(prior_receipt, dict):
        if set(prior_receipt) == {"input_digest", "result"}:
            if prior_receipt["input_digest"] != _json_sha256(args):
                return {
                    "status": "rejected",
                    "reason": "command_id was already used for a different professor decision",
                }
            result = prior_receipt["result"]
            if isinstance(result, dict):
                return result
            return {
                "status": "rejected",
                "reason": "professor decision receipt is malformed",
            }
        explicit_command_id = str(args.get("command_id") or "").strip()
        if not explicit_command_id and command_id.startswith("implicit:"):
            return prior_receipt
        return {
            "status": "rejected",
            "reason": (
                "legacy professor receipt cannot verify this explicit command_id; "
                "retry with a new command_id"
            ),
        }
    expected_revision = args.get("expected_revision")
    if (
        expected_revision is not None
        and int(expected_revision) != int(adaptive["revision"])
    ):
        return {
            "status": "rejected",
            "reason": "stale_adaptive_revision",
            "expected_revision": adaptive["revision"],
            "received_revision": int(expected_revision),
        }

    expected = _handle_get_next_admissible_node_locked(
        {"thread_id": tid, "_allow_evidence_retry": False}
    )
    if not (
        expected.get("status") == "resume"
        and expected.get("node_id") == node_id
        and expected.get("next_tool_to_call") == "submit_professor_decision"
    ):
        return {
            "status": "rejected",
            "reason": (
                f"node {node_id!r} is not the authoritative blind node awaiting "
                "a Professor decision"
            ),
        }
    validate_search_state(state)
    node = next((n for n in state["nodes"] if n["id"] == node_id), None)
    if not node:
        return {"status": "rejected", "reason": f"node {node_id} not in search_state"}
    from research_harness.memory.baseline_review import require_goal_baseline_approval

    try:
        require_goal_baseline_approval(_repo_root(), _thread_dir(tid),
            _read_json(_thread_dir(tid) / "production/reorientation/goal_contract.json") or {})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "rejected", "reason": str(exc), "next_tool_to_call": "submit_baseline_qualification"}
    if transition == "promoted":
        worker_report = _read_json(
            state_path.parent / "nodes" / node_id / "worker_report.json"
        ) or {}
        if (
            worker_report.get("status") != "completed"
            or worker_report.get("claim_verdict_candidate") != "supported"
            or (worker_report.get("baseline_evidence_status") or {}).get("overall")
            != "passed"
            or worker_report.get("disproof_conditions_hit")
        ):
            return {
                "status": "rejected",
                "reason": (
                    "promotion_requires_supported_execution_evidence: worker "
                    "report must be completed, supported, pass every mandatory "
                    "baseline requirement, and hit no disproof condition"
                ),
            }
    elif transition == "pruned":
        from research_harness.orchestrator.attempt_evidence import (
            ConclusiveFailure,
            derive_attempt_evidence,
        )

        worker_report = _read_json(
            state_path.parent / "nodes" / node_id / "worker_report.json"
        ) or {}
        evidence = derive_attempt_evidence(
            worker_report,
            data_status="satisfied",
        )
        if not isinstance(evidence, ConclusiveFailure):
            return {
                "status": "rejected",
                "reason": (
                    "prune_requires_conclusive_failure: the completed worker "
                    "evidence must conclusively contradict the direction"
                ),
            }

    decision_path = (
        _thread_dir(tid) / "production" / "tree" / "nodes" / node_id /
        "mcp_professor_decision.json"
    )
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(
        json.dumps(args, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    dialog_path = decision_path.parent / "dialog.json"
    existing = _read_json(dialog_path) or {"node_id": node_id, "entries": []}
    command_already_logged = any(
        isinstance(entry, dict)
        and (entry.get("metadata") or {}).get("command_id") == command_id
        for entry in existing.get("entries") or []
    )
    response_text = _strip_tool_envelope_leak(
        (args.get("response_to_grad_student") or "").strip()
    )
    final_verdict = args.get("final_verdict") or ""
    if response_text and not command_already_logged:
        existing["entries"].append({
            "speaker": "professor",
            "intent": "verdict",
            "text": response_text,
            "metadata": {
                "final_verdict": final_verdict,
                "next_transition": transition,
                "source": "mcp_server",
                "command_id": command_id,
            },
        })
    elif (final_verdict or transition) and not command_already_logged:
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
                "command_id": command_id,
            },
        })
    dialog_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    transition_node(
        state, node_id, "orchestrator_reduced",
        event="mcp_professor_decision",
        reason=args.get("final_verdict") or "mcp decision",
    )

    if transition == "promoted":
        transition_node(
            state, node_id, "promoted",
            event="mcp_promotion", reason="mcp accepted promotion",
        )
    elif transition == "pruned":
        transition_node(
            state,
            node_id,
            "pruned",
            event="mcp_direction_closed",
            reason="direction evidence reduced without a successor child",
        )

    final_verdict = args.get("final_verdict") or ""
    if transition == "pruned" and final_verdict:
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
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "rejected",
                "reason": f"failure lesson could not be persisted: {exc}",
                "next_tool_to_call": "submit_professor_decision",
            }

    has_queued_work = any(
        item.get("node_id") == node_id and item.get("status") == "queued"
        for item in state["frontier"]
    )
    state["status"] = "running" if has_queued_work else "blocked"
    adaptive["pause"] = None
    adaptive["disposition"] = (
        "goal_achieved" if adaptive.get("strong_result_receipt") else "continue"
    )
    adaptive["revision"] = int(adaptive["revision"]) + 1
    response: dict[str, Any] = {
        "status": "accepted",
        "applied_transition": transition,
        "created_child_ids": [],
        "search_state_status": state["status"],
        "search_disposition": adaptive["disposition"],
        "adaptive_revision": adaptive["revision"],
    }
    adaptive["command_receipts"][command_id] = {
        "input_digest": _json_sha256(args),
        "result": response,
    }
    validate_search_state(state)
    _write_search_state_atomic(state_path, state)
    return response


def handle_decide_publication_readiness(args: dict[str, Any]) -> dict[str, Any]:
    tid = args["thread_id"]
    node_id = args.get("node_id")
    if not isinstance(node_id, str):
        return {"status": "rejected", "reason": "node_id is required"}
    rejected = _require_authoritative_node(tid, node_id)
    if rejected is not None:
        return rejected
    readiness_path = (
        _thread_dir(tid) / "production" / "tree" / "mcp_readiness.json"
    )
    readiness_path.parent.mkdir(parents=True, exist_ok=True)
    readiness_path.write_text(
        json.dumps(args, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"status": "recorded"}


def handle_update_baseline_sources(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.memory.baseline_review import update_baseline_sources

    tid = args["thread_id"]
    with _exclusive_adaptive_writer(tid):
        try:
            return update_baseline_sources(_repo_root(), _thread_dir(tid), args["dossier"], args["candidate_details"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"status": "rejected", "reason": f"baseline source update failed: {exc}"}


def handle_search_paper_references(args: dict[str, Any]) -> dict[str, Any]:
    import hashlib
    from research_harness.agents.market_research import search_literature, _default_http_fetcher

    tid = args['thread_id']
    query = args['query'].strip()
    count = args.get('max_results', 5)
    if not query or type(count) is not int or not 1 <= count <= 10:
        return {'status': 'rejected', 'reason': 'A nonempty query and 1–10 results are required.'}
    with _exclusive_adaptive_writer(tid):
        responses = []
        def fetch(url):
            raw = _default_http_fetcher(url)
            responses.append({'url': url, 'sha256': hashlib.sha256(raw).hexdigest(),
                              'body': raw.decode('utf-8')})
            return raw
        try:
            provider = args.get('provider', 'crossref')
            papers = search_literature(fetch, query, count, provider=provider)
            receipt = {'query': query, 'provider': provider, 'responses': responses, 'papers': papers}
            digest = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
            market = _thread_dir(tid) / 'market'
            receipt_path = market / 'reference_searches' / (digest + '.json')
            _write_json_atomic(receipt_path, receipt)
            path = market / 'paper_references.json'
            catalog = {p['id']: p for p in (_read_json(path) or {}).get('papers', [])}
            for paper in papers:
                catalog[paper['id']] = {**paper, 'retrieval_receipt_path': str(receipt_path.resolve())}
            _write_json_atomic(path, {'papers': list(catalog.values())})
            return {'status': 'recorded', 'papers': [catalog[p['id']] for p in papers],
                    'receipt_path': str(receipt_path.resolve()), 'scientific_approval': False}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {'status': 'unavailable', 'reason': str(exc), 'scientific_approval': False}


def handle_submit_baseline_qualification(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.memory.baseline_review import baseline_roles_frozen, propose_baselines
    from research_harness.adapters.codex_cli import CodexCliError

    tid = args["thread_id"]
    with _exclusive_adaptive_writer(tid):
        qualification = args["qualification"]
        if (baseline_roles_frozen(_thread_dir(tid))
                and _read_json(_thread_dir(tid) / "market/baseline_qualification.json") != qualification):
            return {"status": "rejected", "reason": "baseline roles are already qualified and frozen"}
        try:
            return propose_baselines(_repo_root(), _thread_dir(tid), qualification)
        except (OSError, ValueError, KeyError, TypeError, CodexCliError) as exc:
            return {"status": "rejected", "reason": f"baseline qualification failed: {exc}"}


def handle_execute_baseline_preflight(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.runner.baseline_preflight import build_preflight_node, execute_baseline_preflight, resolve_preflight_role
    from research_harness.orchestrator.experiment_plan import resolve_source_files
    from research_harness.memory.baseline_review import baseline_roles_frozen
    from research_harness.settings_scoped import resolve_for_thread
    from research_harness.orchestrator.research_control import StaleResearchWork, bind_work, current_work, finish_work
    from research_harness.adapters.codex_cli import CodexCliError

    tid = args.get('thread_id')
    if not tid:
        try:
            relative = Path(args['request_path']).resolve().relative_to((_repo_root() / 'runs/threads').resolve())
            if len(relative.parts) < 2:
                raise ValueError('request_path must name a file inside a research thread')
            tid = relative.parts[0]
        except (KeyError, TypeError, ValueError) as exc:
            return {'status': 'rejected', 'reason': f'Provide thread_id for inline requests or a saved request inside runs/threads: {exc}'}
    with _exclusive_adaptive_writer(tid):
        if baseline_roles_frozen(_thread_dir(tid)):
            return {"status": "rejected", "reason": "baseline preparation is closed for the approved baseline roles"}
        bound = False
        request_resolved = not args.get('request_path')
        replay_source = None
        try:
            if args.get('request_path'):
                if 'node' in args or 'experiment_plan' in args:
                    raise ValueError('Use request_path or inline node/plan, not both.')
                request_path = Path(args['request_path']).resolve()
                request_path.relative_to(_thread_dir(tid).resolve())
                request = _read_json(request_path)
                if not isinstance(request, dict) or request.get('thread_id', tid) != tid:
                    raise ValueError('Saved dispatch request must be an object for this thread.')
                replay_source = request_path
                updates = args.get('updates', [])
                if not isinstance(updates, list) or len(updates) > 32:
                    raise ValueError('Dispatch updates must be an array of at most 32 replacements.')
                for update in updates:
                    keys = update['path']
                    if not isinstance(keys, list) or not keys or not all(type(key) in (str, int) for key in keys):
                        raise ValueError('Each update path must be an array of object keys and array indices, e.g. ["experiment_plan", "source_files", 0, "content"].')
                    if keys[0] not in {'node', 'experiment_plan', 'role', 'work_id'}:
                        raise ValueError('Updates may only change dispatch node, plan, role or work_id.')
                    target = request
                    for key in keys[:-1]:
                        if isinstance(target, list):
                            index = int(key)
                            if index < 0 or index >= len(target):
                                raise ValueError('Dispatch update index is outside its array.')
                            target = target[index]
                        else:
                            target = target[key]
                    if isinstance(target, list):
                        index = int(keys[-1])
                        if index < 0 or index >= len(target):
                            raise ValueError('Dispatch update index is outside its array.')
                        target[index] = update['value']
                    else:
                        target[keys[-1]] = update['value']
                args = {**request, 'thread_id': tid}
                request_resolved = True
            elif args.get('updates'):
                raise ValueError('Dispatch updates require request_path.')
            plan_input = dict(args['experiment_plan'])
            nested_role = plan_input.pop('role', None)
            input_schema = _experiment_plan_input_schema()
            intent = _read_json(_thread_dir(tid) / 'production/feasibility_envelope.json').get('operator_intent', {})
            if not intent.get('data_source_anchor'):
                input_schema['required'].append('inputs')
            errors = schema_errors(input_schema, plan_input)
            if errors:
                raise ValueError('Invalid experiment_plan:\n' + '\n'.join(errors))
            role = resolve_preflight_role(plan_input, args.get('role'))
            resolve_preflight_role(plan_input, nested_role)
            args = {**args, 'experiment_plan': plan_input, 'role': role}
            plan = {**args['experiment_plan'], 'source_files': resolve_source_files(_thread_dir(tid), args['experiment_plan']['source_files'])}
            node = args['node'] if 'node' in args else build_preflight_node(_thread_dir(tid), plan)
            node_id = node['id']
            path = _thread_dir(tid) / 'production/tree/baseline_preflight' / node_id
            path.resolve().relative_to((_thread_dir(tid) / 'production/tree/baseline_preflight').resolve())
            if not (path / 'worker_report.json').exists():
                bind_work(_thread_dir(tid), args.get('work_id'), node_id, plan)
                bound = True
                _write_json_atomic(_thread_dir(tid) / 'production/research_control/work' / args['work_id'] / 'dispatch_request.json', args)
            result = execute_baseline_preflight(
                _repo_root(), _thread_dir(tid), node=node, plan=plan,
                role=args["role"], settings=resolve_for_thread(_repo_root(), tid),
                research_work_id=args.get('work_id'),
            )
            return finish_work(_thread_dir(tid), result) if bound else result
        except (OSError, ValueError, KeyError, TypeError, CodexCliError) as exc:
            result = {"status": "rejected", "reason": f"baseline preflight failed: {exc}", "next_tool_to_call": "plan_research_work"}
            from research_harness.orchestrator.research_review import ReviewContractError
            from research_harness.adapters.call_budget import CallBudgetExhausted
            operational_stop = isinstance(exc, CallBudgetExhausted) or (isinstance(exc, CodexCliError) and bool(os.environ.get('RESEARCH_HARNESS_CALL_BUDGET')))
            if operational_stop:
                result.update(status='checkpoint', next_tool_to_call=None)
            if isinstance(exc, ReviewContractError):
                result.update(status='review_invalid', next_tool_to_call='execute_baseline_preflight')
            if isinstance(exc, StaleResearchWork):
                return {**result, 'status': 'work_required'}
            work = current_work(_thread_dir(tid))
            if not bound and work.get('status') == 'planned' and args.get('work_id') == work.get('work_id'):
                request_path = (_thread_dir(tid) / 'production/research_control/work' / work['work_id'] / 'dispatch_request.json').resolve()
                if request_resolved:
                    _write_json_atomic(request_path, args)
                else:
                    # A rejected patch is not a replacement execution plan.
                    _write_json_atomic(request_path.parent / 'rejected_dispatch_request.json', args)
                saved_request_path = str(request_path) if request_resolved else (str(replay_source) if replay_source else None)
                work['outcome'] = {'execution_result': result['status'], 'reason': result['reason'],
                                   'observation': None, 'new_observation': False, 'scientific_verdict': 'unverified',
                                   'dispatch_request_path': saved_request_path}
                _write_json_atomic(request_path.parent / 'work.json', work)
                _write_json_atomic(_thread_dir(tid) / 'production/research_control/current.json', work)
                if operational_stop:
                    return {**result, 'work_id': work['work_id'], 'dispatch_request_path': saved_request_path,
                            'next_step': 'Stop this bounded run. Resume the same saved request with a later budget; no experiment ran and no code correction is implied.'}
                return {**result, 'work_id': work['work_id'], 'dispatch_request_path': saved_request_path,
                        'next_tool_to_call': 'execute_baseline_preflight',
                        'next_step': 'Correct the saved request and retry the same work; no new experiment ran.'}
            return finish_work(_thread_dir(tid), result) if bound else result


# --- LLM-driven rebuttal + paper writer (Phase C / D) -------------------- #


def _rebuttal_dir(tid: str) -> Path:
    return _thread_dir(tid) / "production" / "rebuttal"


def _publication_dir(tid: str) -> Path:
    return _thread_dir(tid) / "production" / "publication"


def _figures_dir(tid: str) -> Path:
    return _publication_dir(tid) / "figures"


def _resolve_promoted_node(
    tid: str, override_id: str | None = None, *, allow_completed: bool = False,
) -> dict[str, Any]:
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path)
    if not state:
        raise ValueError("search_state.json missing — production not initialized")
    promoted = state.get("promoted_node_ids") or []
    if not promoted:
        raise ValueError("no promoted nodes yet — rebuttal requires a promoted root")
    nodes_by_id = {node["id"]: node for node in state["nodes"]}
    target = _authoritative_active_node_id(tid)
    if target is None and allow_completed:
        from research_harness.thread_supervisor import is_terminal

        verified, _ = is_terminal(_repo_root(), tid, require_rendered=False)
        if verified:
            target = state["adaptive"]["strong_result_receipt"]["promoted_node_id"]
    if target is None or target not in promoted:
        raise ValueError("the authoritative blind node is not promoted")
    if override_id is not None and override_id != target:
        raise ValueError(
            f"promoted node override {override_id!r} is not the authoritative node"
        )
    node = nodes_by_id.get(target)
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
    try:
        ctx = _resolve_promoted_node(tid, args.get("promoted_node_id"))
    except ValueError as exc:
        return {"status": "rejected", "reason": str(exc)}
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
        "submission_review": _read_json(_publication_dir(tid) / "submission_status.json") or {},
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
    fr: dict[str, Any] | None,
    falsifier: dict[str, Any],
    *,
    expected_binding: dict[str, str] | None = None,
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
    if (fr.get("predicate") or {}) != (falsifier.get("predicate") or {}):
        return (
            "falsifier_result.predicate does not match the frozen external "
            "falsifier predicate. Recompute the result against the exact frozen bar."
        )
    if expected_binding is not None:
        mismatched = [
            key
            for key, expected in expected_binding.items()
            if fr.get(key) != expected
        ]
        if mismatched:
            return (
                "falsifier_result does not match the active blind attempt binding: "
                + ", ".join(mismatched)
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
    active = _authoritative_strong_binding(tid)
    if active is not None:
        expected_binding = {
            "contract_id": active.contract_id,
            "attempt_id": active.attempt_id,
            "direction_id": active.direction_id,
            "node_id": active.node_id,
            "manifest_id": active.manifest_id,
        }
    else:
        state = _read_json(
            _thread_dir(tid) / "production" / "tree" / "search_state.json"
        ) or {}
        receipt = (state.get("adaptive") or {}).get("strong_result_receipt") or {}
        expected_binding = {
            "contract_id": receipt.get("contract_id"),
            "attempt_id": receipt.get("attempt_id"),
            "direction_id": receipt.get("direction_id"),
            "node_id": receipt.get("promoted_node_id"),
            "manifest_id": receipt.get("acquisition_manifest_id"),
        }
        if any(not isinstance(value, str) or not value for value in expected_binding.values()):
            return False
    fr = _read_json(_rebuttal_dir(tid) / "falsifier_result.json")
    return (
        _falsifier_result_blocks_achievement(
            fr,
            falsifier,
            expected_binding=expected_binding,
        )
        is None
    )


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

def _adversarial_dominance_enabled(settings: dict[str, Any]) -> bool:
    """ADR 0007 lever 0: a grounded critic kill freezes accept. Default ON."""
    g = _persona_cfg(settings).get("adversarial_dominance")
    if isinstance(g, dict):
        return g.get("enabled", True) is not False
    return True


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
    anchor_check: dict[str, list[Any]] | None = None,
) -> list[str]:
    """Collect signals that justify auto-clamping AC confidence to 'low'."""
    reasons: list[str] = []
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

    anchor_check = {"unbound": [], "unmeasured": []}
    try:
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

    downclamp_reasons = _compute_ac_downclamp_signals(tid, anchor_check)
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
        else "AC rejected. Call advance_research to continue the canonical blind engine."
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


def _paper_market_brief(tid: str) -> dict[str, Any]:
    market = _thread_dir(tid) / 'market'
    brief = _read_json(market / 'market_research_brief.json') or {}
    papers = {p['id']: p for p in brief.get('papers', [])}
    papers.update({p['id']: p for p in (_read_json(market / 'paper_references.json') or {}).get('papers', [])})
    return {**brief, 'papers': list(papers.values())}


def _paper_evidence_bundle(tid: str, node_dir: Path) -> dict[str, Any]:
    from research_harness.orchestrator.protocol_revision import approved_protocol_revisions
    from research_harness.orchestrator.research_sources import retrieved_sources
    from research_harness.orchestrator.research_knowledge import research_brief
    production = _thread_dir(tid) / "production"
    return {
        "worker_report": _read_json(node_dir / "worker_report.json") or {},
        "experiment_plan": _read_json(node_dir / "experiment_plan.json") or {},
        "runner_result": _read_json(node_dir / "workspace" / "runner_result.json") or {},
        "falsifier_result": _read_json(_rebuttal_dir(tid) / "falsifier_result.json") or {},
        "construct_adversary_report": _read_json(_rebuttal_dir(tid) / "construct_adversary_report.json") or {},
        "goal_contract": _read_json(production / "reorientation" / "goal_contract.json") or {},
        "protocol_revisions": approved_protocol_revisions(_thread_dir(tid)),
        "confirmation_execution": _read_json(_thread_dir(tid) / "production/confirmation_execution.json") or {},
        "market_brief": _paper_market_brief(tid),
        "primary_sources": retrieved_sources(_thread_dir(tid)),
        "development_research_brief": research_brief(_thread_dir(tid)),
    }


def handle_prepare_paper_writing_context(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.publishing.sakana_paper import PAPER_WRITING_REQUIREMENTS
    from research_harness.orchestrator.protocol_revision import approved_protocol_revisions

    tid = args["thread_id"]
    ctx = _resolve_promoted_node(tid, allow_completed=True)
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
    production = _thread_dir(tid) / "production"
    market = _thread_dir(tid) / "market"
    market_brief = _paper_market_brief(tid)
    analysis_path = market / "baseline_analysis.md"

    # Surface keys available for figure data_spec lookups.
    metric_keys = sorted((worker_report.get("metrics") or {}).keys())
    baseline_keys = sorted((worker_report.get("baselines") or {}).keys())

    from research_harness.publishing.venue_export import load_venue_profiles
    target = _read_json(production / 'submission_target.json') or {}
    profile_key = f"{target.get('venue')}-{target.get('year')}-{target.get('track', 'main')}"
    submission_profile = load_venue_profiles().get(profile_key)

    # Internal lessons provide review context, not citable external literature.
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
        "experiment_plan": _read_json(node_dir / "experiment_plan.json"),
        "runner_result": _read_json(node_dir / "workspace" / "runner_result.json"),
        "falsifier_result": _read_json(rebuttal_dir / "falsifier_result.json"),
        "construct_adversary_report": _read_json(rebuttal_dir / "construct_adversary_report.json"),
        "frozen_question": _read_json(production / "frozen_question.json"),
        "goal_contract": _read_json(production / "reorientation" / "goal_contract.json"),
        "protocol_revisions": approved_protocol_revisions(_thread_dir(tid)),
        "confirmation_execution": _read_json(_thread_dir(tid) / "production/confirmation_execution.json") or {},
        "protocol_disclosure_requirement": "Describe approved development amendments and their timing in the methods. Do not portray an amended design as the original preregistration or use prior results as prospective evidence for the amendment.",
        "market_brief": market_brief,
        "reference_papers": market_brief.get("papers") or [],
        "reference_retrieval_tool": "search_paper_references adds retrieved records to this writing context and evidence bundle without changing baseline assignments. Use it when the actual methods or closest works are absent from reference_papers. Then use retrieve_research_source with the returned reference_id and a primary-source URL to inspect full text without reopening experimental work.",
        "citation_format": "Declare citation_source_ids from reference_papers[].id and embed <a href='#ref_ID'>citation</a>. The references section is generated from those retrieved records; do not hand-copy metadata.",
        "evidence_anchor_format": "artifact.path.to.value, optionally =JSON_VALUE (checked for equality); only the supplied research artifacts are allowed.",
        "baseline_analysis_md": analysis_path.read_text(encoding="utf-8") if analysis_path.exists() else None,
        "writing_requirements": list(PAPER_WRITING_REQUIREMENTS),
        "submission_profile": submission_profile,
        "venue_statement_requirement": "Include authored ai_use_statement when required by the supplied profile. Disclose actual uses and verification; do not invent human review or author actions.",
        "rebuttal_reviews": reviews,
        "orchestrator_reduction": reduction,
        "ac_decision": ac,
        "camera_ready_revision": revision,
        "mental_model_statement": revision.get("mental_model_statement"),
        "active_lessons": active_lessons,
        "submission_review": _read_json(_publication_dir(tid) / "submission_status.json") or {},
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
    from research_harness.publishing.figures import render_figure, FigureRenderError, figure_source_projection

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

    ctx = _resolve_promoted_node(tid, allow_completed=True)
    node_dir = _thread_dir(tid) / "production" / "tree" / "nodes" / ctx["promoted_id"]
    worker_report = _read_json(node_dir / "worker_report.json") or {}
    ac = _read_json(_rebuttal_dir(tid) / "ac_decision.json") or {}

    figures_dir = _figures_dir(tid)
    figures_dir.mkdir(parents=True, exist_ok=True)
    try:
        projection = figure_source_projection(req["figure_type"], req.get("data_spec", {}), worker_report, ac)
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
    registry[req["figure_id"]] = {
        **req, "artifact_path": str(artifact_path),
        "source_digest": projection.digest, "source_paths": list(projection.source_paths),
        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
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
    from research_harness.publishing.manuscript import ManuscriptError, validate_sections
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

    if section["thread_id"] != tid:
        return {"status": "rejected", "reason": "section thread_id differs from request"}
    ctx = _resolve_promoted_node(tid, allow_completed=True)
    node_dir = _thread_dir(tid) / "production" / "tree" / "nodes" / ctx["promoted_id"]
    try:
        validate_sections({section["section_id"]: section}, _paper_evidence_bundle(tid, node_dir),
                          identity_tokens=(tid,), require_citations=False)
    except ManuscriptError as exc:
        return {"status": "rejected", "reason": str(exc)}

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


def handle_submit_professor_user_goal_attestation(
    args: dict[str, Any],
    *,
    expected_strong_result_receipt_sha256: str | None = None,
) -> dict[str, Any]:
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
    state_path = _thread_dir(tid) / "production" / "tree" / "search_state.json"
    state = _read_json(state_path) or {}
    state_input_digest = _json_sha256(state)
    adaptive = state.get("adaptive")
    if isinstance(adaptive, dict):
        frozen_falsifier = (
            ((adaptive.get("goal") or {}).get("bar") or {}).get(
                "external_falsifier"
            )
            or {}
        )
        current_falsifier = (envelope or {}).get("external_falsifier") or {}
        if current_falsifier != frozen_falsifier:
            return {
                "status": "rejected",
                "reason": (
                    "the current feasibility envelope does not match the frozen "
                    "research goal's external falsifier"
                ),
            }
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
    if attestation.get("achieved") and not isinstance(adaptive, dict):
        return {
            "status": "rejected",
            "reason": (
                "achieved=true requires an active blind research state and its "
                "attempt-bound strong-result receipt"
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

    attestation_sha256 = hashlib.sha256(
        json.dumps(attestation, sort_keys=True).encode("utf-8")
    ).hexdigest()
    existing_strong_receipt = (
        adaptive.get("strong_result_receipt")
        if isinstance(adaptive, dict)
        else None
    )
    if attestation.get("achieved") and isinstance(existing_strong_receipt, dict):
        if (
            existing_strong_receipt.get("attestation_sha256")
            != attestation_sha256
        ):
            return {
                "status": "rejected",
                "reason": (
                    "the completed strong result is bound to a different "
                    "attestation"
                ),
            }
        from research_harness.thread_supervisor import is_terminal

        evidence_valid, _ = is_terminal(
            _repo_root(),
            tid,
            require_rendered=False,
        )
        if not evidence_valid:
            return {
                "status": "rejected",
                "reason": "the existing strong result receipt no longer verifies",
            }
        from research_harness.orchestrator.blind_sequential_research import (
            strong_result_receipt_sha256,
        )

        replay_receipt_digest = strong_result_receipt_sha256(
            existing_strong_receipt
        )
        if (
            expected_strong_result_receipt_sha256 is not None
            and replay_receipt_digest
            != expected_strong_result_receipt_sha256
        ):
            return {
                "status": "rejected",
                "reason": "prepared strong result changed during recovery",
            }
        blind_terminal = _commit_blind_strong_terminal(
            tid,
            receipt_digest=replay_receipt_digest,
            state_input_digest=state_input_digest,
            attestation=attestation,
            strong_receipt=existing_strong_receipt,
        )
        if blind_terminal.get("status") != "goal_achieved":
            return {
                "status": "rejected",
                "reason": "authoritative strong terminal recovery did not commit",
                "blind_reorientation": blind_terminal,
            }
        return {
            "status": "ok",
            "achieved": True,
            "attested_status": "goal_achieved",
            "verdict_strength": strength,
            "referent_ledger": ledger,
            "strong_result_receipt": existing_strong_receipt,
            "blind_reorientation": blind_terminal,
            "next_step": (
                "transfer_valid: the verified strong result is committed; "
                "proceed to final paper rendering."
            ),
        }

    strong_receipt: dict[str, Any] | None = None
    if attestation.get("achieved") and isinstance(adaptive, dict):
        if (adaptive.get("goal") or {}).get("strong_completion_blocked"):
            return {
                "status": "rejected",
                "reason": (
                    "strong completion is blocked because the goal was migrated "
                    "from a legacy root rather than frozen from pre-generation artifacts"
                ),
            }
        falsifier_result = _read_json(
            _rebuttal_dir(tid) / "falsifier_result.json"
        ) or {}
        construct_result = _read_json(
            _rebuttal_dir(tid) / "construct_adversary_report.json"
        ) or {}
        reduction = _read_json(
            _rebuttal_dir(tid) / "orchestrator_reduction.json"
        ) or {}
        undefeated_kills = _undefeated_kills(
            _collect_rebuttal_kills(tid),
            reduction.get("blocking_objections"),
        )
        promoted = _resolve_promoted_node(tid)
        promoted_node = promoted.get("node") or {}
        promoted_strategy = promoted_node.get("strategy") or {}
        promoted_strategy_id = promoted_strategy.get("id")
        known_strategy_ids = {
            strategy.get("id")
            for strategy in adaptive.get("strategies") or []
            if isinstance(strategy, dict) and strategy.get("id")
        }
        registered_strategy = next(
            (
                strategy
                for strategy in adaptive.get("strategies") or []
                if isinstance(strategy, dict)
                and strategy.get("id") == promoted_strategy_id
            ),
            None,
        )
        node_dir = (
            _thread_dir(tid)
            / "production"
            / "tree"
            / "nodes"
            / str(promoted.get("promoted_id"))
        )
        experiment_plan = _read_json(node_dir / "experiment_plan.json") or {}
        worker_report = _read_json(node_dir / "worker_report.json") or {}
        from research_harness.orchestrator.adaptive_search import (
            experiment_fingerprint,
            strategy_fingerprint,
        )
        from research_harness.orchestrator.strong_result import (
            StrongExecutionEvidenceError,
            verify_strong_execution_evidence,
        )

        try:
            derived_strategy_id = strategy_fingerprint(
                mechanism=str(promoted_strategy.get("mechanism") or ""),
                intervention=str(promoted_strategy.get("intervention") or ""),
            )
        except Exception:  # noqa: BLE001
            derived_strategy_id = None
        try:
            derived_experiment_id = experiment_fingerprint(experiment_plan)
        except Exception:  # noqa: BLE001
            derived_experiment_id = None
        executed_experiment = next(
            (
                experiment
                for experiment in adaptive.get("experiments") or []
                if isinstance(experiment, dict)
                and experiment.get("id") == derived_experiment_id
                and experiment.get("node_id") == promoted.get("promoted_id")
                and experiment.get("strategy_id") == promoted_strategy_id
                and experiment.get("status") == "executed"
            ),
            None,
        )
        missing_gates: list[str] = []
        strong_binding = None
        try:
            from research_harness.orchestrator.blind_sequential_research import (
                BlindSequentialResearchError,
                resolve_strong_result_binding,
            )

            blind_engine = _build_blind_research_engine(tid)
            reorientation_state = blind_engine.read_state()
            if reorientation_state is None:
                raise BlindSequentialResearchError(
                    "reorientation state is missing"
                )
            strong_binding = resolve_strong_result_binding(
                reorientation_state,
                _read_json(blind_engine.paths.node_attempts) or {},
                node_id=str(promoted.get("promoted_id")),
            )
            from research_harness.orchestrator.confirmation_use import (
                confirmation_evidence_from_result, digest_confirmation_evidence,
                verify_terminal_confirmation,
            )
            contract = blind_engine.read_goal_contract()
            from research_harness.memory.baseline_review import require_goal_baseline_approval
            from research_harness.orchestrator.goal_contract import serialize_goal_contract
            require_goal_baseline_approval(_repo_root(), _thread_dir(tid), serialize_goal_contract(contract))
            from research_harness.confirmation_sampling import active_sampling_registration
            if active_sampling_registration(_thread_dir(tid)):
                from dataclasses import asdict
                from research_harness.orchestrator.confirmation_execution import verified_confirmation_receipt
                measured = verified_confirmation_receipt(_thread_dir(tid), asdict(strong_binding))
                if confirmation_evidence_from_result(falsifier_result).get('observed') != measured['observed']:
                    raise ValueError('Falsifier differs from private confirmation measurement')
            verify_terminal_confirmation(
                blind_engine.paths.confirmation_use, contract,
                binding={"contract_id": strong_binding.contract_id,
                         "attempt_id": strong_binding.attempt_id,
                         "direction_id": strong_binding.direction_id,
                         "node_id": strong_binding.node_id,
                         "manifest_id": strong_binding.manifest_id},
                evidence_digest=digest_confirmation_evidence(
                    contract, confirmation_evidence_from_result(falsifier_result)),
            )
        except (BlindSequentialResearchError, OSError, ValueError, KeyError, TypeError) as exc:
            missing_gates.append(f"active blind attempt binding: {exc}")
        execution_evidence: dict[str, Any] | None = None
        try:
            execution_evidence = verify_strong_execution_evidence(
                node=promoted_node,
                experiment_plan=experiment_plan,
                worker_report=worker_report,
                node_dir=node_dir,
                tree_dir=state_path.parent,
                settings=settings,
            )
        except StrongExecutionEvidenceError as exc:
            missing_gates.append(f"verified runner evidence: {exc}")
        if ac.get("decision") not in {"accept", "revise"}:
            missing_gates.append("AC accept or revise")
        if falsifier_result.get("passed") is not True:
            missing_gates.append("passing external falsifier")
        if construct_result.get("harness_verdict") != "survived":
            missing_gates.append("survived construct adversary")
        if construct_result.get("construction_ref") != promoted.get("promoted_id"):
            missing_gates.append("construct adversary bound to the promoted strategy")
        if strength != _V.REALITY_VERDICT:
            missing_gates.append("transfer_valid referent strength")
        if undefeated_kills:
            missing_gates.append(
                "defeat every blocking critic: "
                + ", ".join(
                    str(kill.get("critic_id")) for kill in undefeated_kills
                )
            )
        if not promoted_strategy_id or promoted_strategy_id not in known_strategy_ids:
            missing_gates.append("promoted adaptive strategy")
        if (
            derived_strategy_id != promoted_strategy_id
            or promoted_strategy.get("goal_id") != adaptive["goal"]["id"]
            or registered_strategy != promoted_strategy
        ):
            missing_gates.append("content-valid strategy identity")
        if promoted_node.get("status") != "promoted":
            missing_gates.append("promoted node status")
        if executed_experiment is None:
            missing_gates.append("executed experiment for the promoted strategy")
        if (
            experiment_plan.get("node_id") != promoted.get("promoted_id")
            or worker_report.get("node_id") != promoted.get("promoted_id")
        ):
            missing_gates.append("experiment and worker evidence bound to promoted node")
        if (
            worker_report.get("status") != "completed"
            or worker_report.get("claim_verdict_candidate") != "supported"
            or (worker_report.get("baseline_evidence_status") or {}).get("overall")
            != "passed"
            or worker_report.get("disproof_conditions_hit")
        ):
            missing_gates.append("supported worker evidence with every baseline passed")
        if attestation.get("promoted_node_id") != promoted.get("promoted_id"):
            missing_gates.append("attestation bound to the promoted strategy")
        if missing_gates:
            return {
                "status": "rejected",
                "reason": (
                    "verified strong-result receipt cannot be constructed; missing: "
                    + ", ".join(missing_gates)
                ),
            }
        assert execution_evidence is not None
        assert strong_binding is not None

        strong_receipt = {
            "verified": True,
            "contract_id": strong_binding.contract_id,
            "attempt_id": strong_binding.attempt_id,
            "direction_id": strong_binding.direction_id,
            "acquisition_manifest_id": strong_binding.manifest_id,
            "goal_id": adaptive["goal"]["id"],
            "bar_digest": adaptive["goal"]["bar_digest"],
            "strategy_id": promoted_strategy_id,
            "promoted_node_id": promoted.get("promoted_id"),
            "experiment_id": derived_experiment_id,
            "verdict_strength": strength,
            "falsifier_result_sha256": hashlib.sha256(
                json.dumps(falsifier_result, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "construct_adversary_sha256": hashlib.sha256(
                json.dumps(construct_result, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "ac_decision_sha256": hashlib.sha256(
                json.dumps(ac, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "critic_resolution_sha256": hashlib.sha256(
                json.dumps(reduction, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "attestation_sha256": attestation_sha256,
            "experiment_plan_sha256": hashlib.sha256(
                json.dumps(experiment_plan, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "worker_report_sha256": hashlib.sha256(
                json.dumps(worker_report, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "job_manifest_sha256": execution_evidence["job_manifest_sha256"],
            "runner_result_sha256": execution_evidence["runner_result_sha256"],
            "metrics_evidence_sha256": execution_evidence[
                "metrics_evidence_sha256"
            ],
        }

    out_path = _rebuttal_dir(tid) / "user_goal_attestation.json"
    blind_terminal = None
    if strong_receipt is not None:
        from research_harness.orchestrator.blind_sequential_research import (
            strong_result_receipt_sha256,
        )
        from research_harness.orchestrator.search_state import validate_search_state

        receipt_digest = strong_result_receipt_sha256(strong_receipt)
        if (
            expected_strong_result_receipt_sha256 is not None
            and receipt_digest != expected_strong_result_receipt_sha256
        ):
            return {
                "status": "rejected",
                "reason": "prepared strong result changed during recovery",
            }
        with _exclusive_adaptive_writer(tid):
            current_state = _read_json(state_path) or {}
            if _json_sha256(current_state) != state_input_digest:
                return {
                    "status": "rejected",
                    "reason": (
                        "search state changed while the strong result was being "
                        "verified; retry attestation against the current state"
                    ),
                }
            current_adaptive = current_state.get("adaptive")
            if not isinstance(current_adaptive, dict):
                return {
                    "status": "rejected",
                    "reason": "adaptive state disappeared before strong commit",
                }
            existing_receipt = current_adaptive.get("strong_result_receipt")
            if (
                existing_receipt is not None
                and existing_receipt != strong_receipt
            ):
                return {
                    "status": "rejected",
                    "reason": (
                        "a different strong result receipt is already committed; "
                        "repair its bound evidence instead of replacing it"
                    ),
                }
            live_artifacts = {
                "falsifier_result_sha256": _read_json(
                    _rebuttal_dir(tid) / "falsifier_result.json"
                ),
                "construct_adversary_sha256": _read_json(
                    _rebuttal_dir(tid) / "construct_adversary_report.json"
                ),
                "ac_decision_sha256": _read_json(
                    _rebuttal_dir(tid) / "ac_decision.json"
                ),
                "critic_resolution_sha256": _read_json(
                    _rebuttal_dir(tid) / "orchestrator_reduction.json"
                ),
                "experiment_plan_sha256": _read_json(
                    node_dir / "experiment_plan.json"
                ),
                "worker_report_sha256": _read_json(
                    node_dir / "worker_report.json"
                ),
            }
            stale_artifacts = [
                key
                for key, value in live_artifacts.items()
                if value is None
                or strong_receipt.get(key)
                != hashlib.sha256(
                    json.dumps(value, sort_keys=True).encode("utf-8")
                ).hexdigest()
            ]
            if strong_receipt.get("attestation_sha256") != attestation_sha256:
                stale_artifacts.append("attestation_sha256")
            try:
                live_reorientation = blind_engine.read_state()
                if live_reorientation is None:
                    raise BlindSequentialResearchError(
                        "reorientation state is missing"
                    )
                live_binding = resolve_strong_result_binding(
                    live_reorientation,
                    _read_json(blind_engine.paths.node_attempts) or {},
                    node_id=str(promoted.get("promoted_id")),
                )
                live_execution_evidence = verify_strong_execution_evidence(
                    node=promoted_node,
                    experiment_plan=experiment_plan,
                    worker_report=worker_report,
                    node_dir=node_dir,
                    tree_dir=state_path.parent,
                    settings=settings,
                )
                from research_harness.acquisition import PublicAcquisition

                live_manifest = PublicAcquisition(
                    blind_engine.paths.root / "acquisition_cache"
                ).verify_manifest(
                    live_binding.manifest_id,
                    node_id=live_binding.node_id,
                )
                if (
                    live_binding != strong_binding
                    or live_manifest.attempt_id != live_binding.attempt_id
                    or live_manifest.direction_id != live_binding.direction_id
                ):
                    stale_artifacts.append("active strong binding")
                if active_sampling_registration(_thread_dir(tid)):
                    measured = verified_confirmation_receipt(_thread_dir(tid), asdict(live_binding))
                    if confirmation_evidence_from_result(falsifier_result).get('observed') != measured['observed']:
                        stale_artifacts.append('private confirmation measurement')
                if any(
                    live_execution_evidence[key] != strong_receipt[key]
                    for key in (
                        "job_manifest_sha256",
                        "runner_result_sha256",
                        "metrics_evidence_sha256",
                    )
                ):
                    stale_artifacts.append("active strong execution evidence")
            except (
                BlindSequentialResearchError,
                OSError,
                StrongExecutionEvidenceError,
                ValueError,
            ):
                stale_artifacts.append("active strong execution evidence")
            if stale_artifacts:
                return {
                    "status": "rejected",
                    "reason": (
                        "strong evidence changed before commit: "
                        + ", ".join(stale_artifacts)
                    ),
                }
            _prepare_blind_strong_terminal(
                tid,
                receipt_digest=receipt_digest,
                state_input_digest=state_input_digest,
                attestation=attestation,
                strong_receipt=strong_receipt,
            )
            _write_json_atomic(out_path, attestation)
            if existing_receipt is None:
                current_adaptive["strong_result_receipt"] = strong_receipt
                current_adaptive["disposition"] = "goal_achieved"
                current_adaptive["pause"] = None
                current_adaptive["revision"] = int(current_adaptive["revision"]) + 1
                current_state["status"] = "completed"
                validate_search_state(current_state)
                _write_search_state_atomic(state_path, current_state)
        blind_terminal = _commit_blind_strong_terminal(
            tid,
            receipt_digest=receipt_digest,
            state_input_digest=state_input_digest,
            attestation=attestation,
            strong_receipt=strong_receipt,
        )
        if blind_terminal.get("status") != "goal_achieved":
            return {
                "status": "rejected",
                "reason": (
                    "verified adaptive evidence did not commit the authoritative "
                    "blind goal transition"
                ),
                "blind_reorientation": blind_terminal,
            }
    else:
        _write_json_atomic(out_path, attestation)

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
            "referent). Continue through advance_research until a real holdout "
            "supports a verified strong result."
        )
    elif status == "unverified_screen":
        next_step = (
            "attested_status=unverified_screen (internally_valid floor): no funded "
            "adversary has survived against the frozen question yet. Before "
            "continuing, EARN construct_valid. Pin_frozen_question (if not pinned), "
            "then submit_construct_adversary_report with a real funded search. If "
            "that direction closes, call advance_research."
        )
    else:  # not_achieved
        next_step = (
            "attested_status=not_achieved. A real referent IS registered but its "
            "falsifier has not passed. If confirmation has not run, follow the "
            "registered frozen procedure with compute_falsifier_result and re-attest. "
            "A failed confirmation closes this attempt; call advance_research. "
            "Never repair the implementation against observed holdout outcomes "
            "or reuse those outcomes as fresh confirmation."
        )
    return {
        "status": "ok",
        "achieved": attestation.get("achieved"),
        "attested_status": status,
        "verdict_strength": strength,
        "referent_ledger": ledger,
        "strong_result_receipt": strong_receipt,
        "blind_reorientation": blind_terminal,
        "next_step": next_step,
    }


def handle_finalize_submission_package(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.publishing.submission import finalize_submission
    from research_harness.publishing.venue_export import VenueTarget
    from research_harness.publishing.integrity import verify_publication_receipt, issue_publication_receipt, publication_inputs
    from research_harness.orchestrator.blind_sequential_research import strong_result_receipt_sha256
    from research_harness.thread_supervisor import is_terminal

    tid = args['thread_id']
    with _exclusive_adaptive_writer(tid):
        try:
            if not is_terminal(_repo_root(), tid, require_rendered=False)[0]:
                raise ValueError('Submission preparation requires a verified research result')
            production = _thread_dir(tid) / 'production'
            summary_path = production / 'production_run_summary.json'
            summary = _read_json(summary_path) or {}
            state = _read_json(production / 'tree/search_state.json')
            strong_digest = strong_result_receipt_sha256(state['adaptive']['strong_result_receipt'])
            dispatch = summary.get('publication_dispatch') or {}
            if not verify_publication_receipt(production, dispatch, summary.get('publication_receipt_sha256'), strong_digest):
                return {'status': 'rejected', 'reason': 'The manuscript preview is missing or stale', 'next_tool_to_call': 'render_final_paper'}
            target = _read_json(production / 'submission_target.json') or {}
            requested_target = {'venue': args['venue'], 'year': args['year'], 'track': args.get('track', 'main')}
            if target and requested_target != {key: target[key] for key in requested_target}:
                return {'status': 'rejected', 'reason': 'Use the configured publication target', 'publication_target': target,
                        'next_tool_to_call': 'finalize_submission_package'}
            node_id = state['adaptive']['strong_result_receipt']['promoted_node_id']
            model = (_read_json(_thread_dir(tid) / 'thread.json') or {}).get('mcp_model') or 'gpt-5.6-sol'
            writing_inputs = publication_inputs(production)
            result = finalize_submission(_repo_root(), production / 'publication',
                target=VenueTarget(args['venue'], args['year'], args.get('track', 'main')),
                worker_report=_read_json(production / 'tree/nodes' / node_id / 'worker_report.json'), model=model)
            if result['status'] != 'completed':
                return result
            receipt = result['receipt']
            artifacts = [item for item in dispatch['rendered_artifacts'] if item['output'] not in {'paper_pdf', 'submission_zip'}]
            artifacts.extend({'output': name, 'artifact_path': str(production / 'publication' / receipt[key])}
                             for name, key in [('paper_pdf', 'pdf_path'), ('submission_zip', 'archive_path')])
            dispatch = {**dispatch, 'rendered_artifacts': artifacts}
            summary['publication_dispatch'] = dispatch
            summary['publication_receipt_sha256'] = issue_publication_receipt(production, dispatch, strong_digest, expected_inputs=writing_inputs)
            _write_json_atomic(summary_path, summary)
            _write_json_atomic(production / 'publication/publication_dispatch.json', dispatch)
            return {**result, 'publication_dispatch': dispatch, 'publication_checkpoint': True}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            status_path = _publication_dir(tid) / 'submission_status.json'
            status = _read_json(status_path) or {}
            from research_harness.confirmation_sampling import _hash
            paper = _publication_dir(tid) / 'paper.html'
            _write_json_atomic(status_path, {**status, 'status': 'export_failed', 'reason': str(exc),
                'readiness': {**status.get('readiness', {}), 'manuscript_sha256': _hash(paper) if paper.exists() else None}})
            return {'status': 'rejected', 'reason': str(exc), 'next_tool_to_call': 'prepare_paper_writing_context'}


def handle_render_final_paper(args: dict[str, Any]) -> dict[str, Any]:
    with _exclusive_adaptive_writer(args["thread_id"]):
        return _handle_render_final_paper_locked(args)


def _handle_render_final_paper_locked(args: dict[str, Any]) -> dict[str, Any]:
    from research_harness.publishing.sakana_paper import render_sakana_paper, SakanaPaperError
    from research_harness.publishing.integrity import (
        PublicationIntegrityError, issue_publication_receipt, publication_inputs,
    )
    from research_harness.thread_supervisor import is_terminal

    tid = args["thread_id"]
    verified, _ = is_terminal(_repo_root(), tid, require_rendered=False)
    if not verified:
        return {
            "status": "rejected",
            "reason": "paper render requires a currently verified strong result; "
                      "finish or repair the evidence and goal attestation first",
        }
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
                "Call advance_research to continue the canonical blind engine."
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
                "Run the required additional research and re-attest, or call "
                "advance_research when the direction closes."
            ),
        }
    writing_inputs = publication_inputs(_thread_dir(tid) / "production")
    paper_dir = _paper_dir(tid)
    outline = _read_json(paper_dir / "outline.json")
    if not outline:
        return {"status": "rejected", "reason": "submit_paper_outline missing"}
    sections_dir = paper_dir / "sections"
    section_files = sorted(sections_dir.glob("*.json")) if sections_dir.exists() else []
    expected = {s["section_id"] for s in outline["section_outline"]}
    submitted = {p.stem for p in section_files}
    required_sections = {"abstract", "references"}
    if not required_sections <= expected:
        return {
            "status": "rejected",
            "reason": "final manuscript outline requires authored abstract and references sections",
        }
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

    ctx = _resolve_promoted_node(tid, allow_completed=True)
    node_dir = _thread_dir(tid) / "production" / "tree" / "nodes" / ctx["promoted_id"]
    worker_report = _read_json(node_dir / "worker_report.json") or {}

    publication_dir = _publication_dir(tid)
    publication_dir.mkdir(parents=True, exist_ok=True)
    from research_harness.publishing.manuscript import (
        ManuscriptError, bibliography_html, validate_sections,
    )
    from research_harness.publishing.figures import FigureRenderError, figure_source_projection
    try:
        for figure_id, figure in figures_registry.items():
            projection = figure_source_projection(figure["figure_type"], figure.get("data_spec", {}), worker_report, ac)
            path = publication_dir / "figures" / f"{figure_id}.png"
            if (projection.digest != figure.get("source_digest")
                    or list(projection.source_paths) != figure.get("source_paths")
                    or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != figure.get("artifact_sha256")):
                raise ManuscriptError(f"figure source or file changed: {figure_id}; register it again")
        ledger = validate_sections(sections, _paper_evidence_bundle(tid, node_dir),
                                   identity_tokens=(tid,))
        if publication_inputs(_thread_dir(tid) / "production") != writing_inputs:
            raise ManuscriptError("writing inputs changed during validation")
        (paper_dir / "evidence_ledger.json").write_text(
            json.dumps(ledger, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        writing_inputs = publication_inputs(_thread_dir(tid) / "production")
        sections["references"] = {**sections["references"], "prose_html": bibliography_html(ledger)}
    except (ManuscriptError, FigureRenderError) as exc:
        return {"status": "rejected", "reason": f"manuscript validation failed: {exc}"}
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

    from research_harness.orchestrator.blind_sequential_research import strong_result_receipt_sha256

    state = _read_json(_thread_dir(tid) / "production" / "tree" / "search_state.json")
    try:
        receipt_digest = issue_publication_receipt(
            _thread_dir(tid) / "production", outputs,
            strong_result_receipt_sha256(state["adaptive"]["strong_result_receipt"]),
            expected_inputs=writing_inputs,
        )
    except (OSError, PublicationIntegrityError) as exc:
        return {"status": "rejected", "reason": f"publication integrity failed: {exc}"}

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
        "publication_receipt_sha256": receipt_digest,
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

    return {"status": "ok", "next_tool_to_call": "finalize_submission_package", "publication_dispatch": outputs, "summary_path": str(_thread_dir(tid) / "production" / "production_run_summary.json")}


# --- JSON-RPC stdio loop -------------------------------------------------- #


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _handle_request(msg: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    method = msg.get("method", "")
    params = msg.get("params") or {}
    req_id = msg.get("id")
    scoped_thread = os.environ.get("RESEARCH_HARNESS_THREAD_ID")
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
        definitions = TOOL_DEFINITIONS
        if scoped_thread:
            definitions = deepcopy(TOOL_DEFINITIONS)
            for tool in definitions:
                schema = tool["inputSchema"]
                if "thread_id" not in schema.get("properties", {}):
                    continue
                schema["properties"]["thread_id"].update({
                    "enum": [scoped_thread],
                    "description": "Optional: the server supplies the current research thread automatically.",
                })
                for branch in [schema, *schema.get("anyOf", [])]:
                    if "required" in branch:
                        branch["required"] = [key for key in branch["required"] if key != "thread_id"]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": definitions}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object")
            definition = next((tool for tool in TOOL_DEFINITIONS if tool["name"] == name), None)
            if scoped_thread and definition and "thread_id" in definition["inputSchema"].get("properties", {}):
                if "thread_id" in args and args["thread_id"] != scoped_thread:
                    raise ValueError("thread_id conflicts with this server's research thread")
                args = {**args, "thread_id": scoped_thread}
            if name == "get_research_state":
                result = handle_get_research_state(args, settings)
            elif name == "plan_research_work":
                result = handle_plan_research_work(args)
            elif name == "retrieve_research_source":
                result = handle_retrieve_research_source(args)
            elif name == "resolve_research_work":
                result = handle_resolve_research_work(args)
            elif name == "execute_confirmation_experiment":
                result = handle_execute_confirmation_experiment(args)
            elif name == "revise_evaluation_protocol":
                result = handle_revise_evaluation_protocol(args)
            elif name == "develop_research_hypotheses":
                result = handle_develop_research_hypotheses(args)
            elif name == "get_next_admissible_node":
                result = handle_get_next_admissible_node(args)
            elif name == "advance_research":
                result = handle_advance_research(args, settings)
            elif name == "submit_bar_sanity_result":
                result = handle_submit_bar_sanity_result(args)
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
            elif name == "submit_baseline_qualification":
                result = handle_submit_baseline_qualification(args)
            elif name == "update_baseline_sources":
                result = handle_update_baseline_sources(args)
            elif name == "search_paper_references":
                result = handle_search_paper_references(args)
            elif name == "execute_baseline_preflight":
                result = handle_execute_baseline_preflight(args)
            elif name == "register_paper_figure":
                result = handle_register_paper_figure(args)
            elif name == "submit_paper_section":
                result = handle_submit_paper_section(args)
            elif name == "submit_professor_user_goal_attestation":
                result = handle_submit_professor_user_goal_attestation(args)
            elif name == "finalize_submission_package":
                result = handle_finalize_submission_package(args)
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
