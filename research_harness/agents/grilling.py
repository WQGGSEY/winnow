from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from research_harness.config import load_settings, resolve_agent_model
from research_harness.orchestrator.experiment_plan import list_available_domains
from research_harness.schemas.validator import (
    SchemaValidationError,
    validate_named_schema,
)


BILLING_ACK_ENV = "RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE"
BILLING_ACK_VALUE = "subscription_ack"
EXECUTION_ACK_ENV = "RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE"
EXECUTION_ACK_VALUE = "live_smoke_ack"
DEFAULT_MAX_ROUNDS = 8
DEFAULT_ROUND_TIMEOUT_SECONDS = 180

# Scaffold mode (see ADR 0004): activated when no existing domain matches.
SCAFFOLD_MAX_ROUNDS = 30
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]*[a-z0-9]$")
RESERVED_SLUGS: frozenset[str] = frozenset({"_fallback_demo"})
# Canonical files every scaffolded domain must produce. The agent may
# also propose optional extras (e.g. README.md, helper modules); only
# these 10 are checked by manifest enforcement before DONE is accepted.
# `<metric>` in eval is dynamic — replaced at validation time by whatever
# python module the agent puts under src/eval/ that isn't __init__.py.
REQUIRED_MANIFEST_TEMPLATE: tuple[str, ...] = (
    "plan.json",
    "src/__init__.py",
    "src/experiment.py",
    "src/proposed.py",
    "src/data.py",
    "src/baselines/__init__.py",
    "src/baselines/current_best.py",
    "src/baselines/naive.py",
    "src/baselines/random_baseline.py",
    "src/eval/__init__.py",
)


CommandRunner = Callable[..., subprocess.CompletedProcess]
InputProvider = Callable[[str], str]
# One-way notifications from the agent loop to whatever's driving it
# (the frontend SSE bridge, primarily). The default is a no-op so CLI
# callers can ignore it.
EventEmitter = Callable[[dict[str, Any]], None]


class GrillingError(ValueError):
    """Raised when the grilling agent cannot produce a valid session."""


@dataclass
class _RoundCall:
    raw_action_text: str
    parsed: dict[str, Any]
    cost_usd: float
    input_tokens: int
    output_tokens: int


def run_grilling_session(
    repo_root: Path,
    *,
    user_goal: str,
    run_dir: Path | None = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    session_id: str | None = None,
    claude_path: str | None = None,
    billing_ack: bool | None = None,
    execution_ack: bool | None = None,
    round_timeout_seconds: int = DEFAULT_ROUND_TIMEOUT_SECONDS,
    command_runner: CommandRunner | None = None,
    input_provider: InputProvider | None = None,
    allowed_domains: list[str] | None = None,
    initial_rounds: list[dict[str, Any]] | None = None,
    initial_usage: dict[str, Any] | None = None,
    created_at_override: str | None = None,
    event_emitter: EventEmitter | None = None,
) -> dict[str, Any]:
    """Drive a multi-turn grilling loop and emit a grilling_session.json."""

    if not user_goal or not user_goal.strip():
        raise GrillingError("user_goal must be a non-empty string")
    if max_rounds < 1 or max_rounds > 50:
        raise GrillingError("max_rounds must be between 1 and 50")

    repo_root = repo_root.resolve()
    settings = load_settings(repo_root)
    live_backend = (
        settings.get("runtime", {})
        .get("worker_backends", {})
        .get("claude_code_live", {})
    )
    model = resolve_agent_model(settings, "grilling_agent")
    max_budget = str(live_backend.get("max_budget_usd", "0.25"))

    if allowed_domains is None:
        try:
            allowed_domains = list_available_domains(repo_root, settings)
        except Exception:
            allowed_domains = []
    allowed_domains = list(allowed_domains or [])

    session_id = session_id or _new_session_id()
    run_dir = (run_dir or repo_root / "runs" / "grilling" / session_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    session_path = run_dir / "grilling_session.json"

    created_at = created_at_override or datetime.now(timezone.utc).isoformat()
    base_session: dict[str, Any] = {
        "session_id": session_id,
        "status": "in_progress",
        "user_goal": user_goal.strip(),
        "max_rounds": max_rounds,
        "model": model,
        "created_at": created_at,
        "rounds": [],
        "extracted": _placeholder_extracted(user_goal),
        "usage_estimate": {
            "rounds_used": 0,
            "total_cost_usd": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        },
        "session_path": str(session_path),
        "error": None,
    }

    # billing/execution gate
    if not _billing_ack_ok(billing_ack):
        return _abort_session(
            session_path,
            base_session,
            status="blocked_by_gate",
            error=f"set {BILLING_ACK_ENV}={BILLING_ACK_VALUE} to enable live grilling",
        )
    if not _execution_ack_ok(execution_ack):
        return _abort_session(
            session_path,
            base_session,
            status="blocked_by_execution_ack",
            error=f"set {EXECUTION_ACK_ENV}={EXECUTION_ACK_VALUE} to actually invoke Claude for grilling",
        )

    detected_claude = claude_path or shutil.which("claude") or "claude"
    runner = command_runner or subprocess.run
    asker = input_provider or _stdin_input_provider
    emit = event_emitter or (lambda _ev: None)

    rounds: list[dict[str, Any]] = list(initial_rounds or [])
    usage = dict(
        initial_usage
        or {
            "rounds_used": 0,
            "total_cost_usd": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        }
    )
    extracted: dict[str, Any] | None = None
    scaffold_state: dict[str, Any] | None = None
    status = "in_progress"
    error: str | None = None
    effective_max_rounds = max_rounds

    _flush_in_progress(
        session_path, base_session, rounds, usage, extracted, scaffold_state
    )

    try:
        index = len(rounds)
        while index < effective_max_rounds:
            scaffold_active = bool(scaffold_state and scaffold_state.get("active"))
            call = _call_claude_round(
                runner=runner,
                claude_path=detected_claude,
                model=model,
                max_budget=max_budget,
                user_goal=user_goal,
                rounds=rounds,
                force_extract=False,
                round_timeout_seconds=round_timeout_seconds,
                allowed_domains=allowed_domains,
                scaffold_state=scaffold_state,
            )
            _accumulate_usage(usage, call)
            usage["rounds_used"] = index + 1
            action_type = str(call.parsed.get("action", ""))

            if action_type == "DONE":
                extracted_candidate = call.parsed.get("extracted")
                # If scaffolding is active, finalize before accepting DONE.
                if scaffold_active:
                    finalize = _finalize_scaffold(
                        run_dir=run_dir,
                        repo_root=repo_root,
                        scaffold_state=scaffold_state,
                        thread_id=_infer_thread_id(run_dir),
                        session_id=session_id,
                        extracted_candidate=extracted_candidate,
                    )
                    emit(
                        {
                            "type": "dry_import",
                            "status": finalize["status"],
                            "error": finalize.get("dry_import_error"),
                            "manifest_missing": finalize.get("manifest_missing", []),
                        }
                    )
                    if finalize["status"] == "failed":
                        # Reject DONE: inject failure, keep looping for fix rounds.
                        rounds.append(
                            {
                                "round_index": index,
                                "action": "DONE",
                                "raw_action": call.raw_action_text,
                                "file_result": {
                                    "relative_path": "__finalize__",
                                    "status": "failed",
                                    "checks": {
                                        "manifest_missing": finalize.get(
                                            "manifest_missing", []
                                        ),
                                        "dry_import_error": finalize.get(
                                            "dry_import_error"
                                        ),
                                    },
                                    "staged_at": None,
                                },
                            }
                        )
                        scaffold_state["dry_import_result"] = {
                            "status": "failed",
                            "error": finalize.get("dry_import_error"),
                        }
                        _flush_in_progress(
                            session_path,
                            base_session,
                            rounds,
                            usage,
                            extracted,
                            scaffold_state,
                        )
                        index += 1
                        continue
                    scaffold_state.update(
                        {
                            "finalized": True,
                            "final_destination": finalize["final_destination"],
                            "dry_import_result": {"status": "ok", "error": None},
                        }
                    )
                    emit(
                        {
                            "type": "scaffold_complete",
                            "domain_slug": scaffold_state["domain_slug"],
                            "final_destination": finalize["final_destination"],
                        }
                    )
                    # Allow the new slug as a domain even though it wasn't in
                    # the original enum (we just added it to disk).
                    allowed_domains_for_finalize = list(allowed_domains or []) + [
                        scaffold_state["domain_slug"]
                    ]
                    extracted = _coerce_extracted(
                        extracted_candidate,
                        user_goal,
                        allowed_domains=allowed_domains_for_finalize,
                    )
                    # Force the domain to the scaffolded slug regardless of
                    # what the agent emitted (defensive).
                    extracted["domain"] = scaffold_state["domain_slug"]
                else:
                    extracted = _coerce_extracted(
                        extracted_candidate,
                        user_goal,
                        allowed_domains=allowed_domains,
                    )
                status = "done"
                break

            if action_type == "PROPOSE_FILE":
                file_payload = call.parsed.get("file")
                if not isinstance(file_payload, dict):
                    raise GrillingError("PROPOSE_FILE missing file object")
                # Lazy-init scaffold_state on first PROPOSE_FILE, deriving
                # the slug from the first path segment.
                if scaffold_state is None:
                    slug = _derive_slug_from_path(file_payload.get("relative_path"))
                    _validate_new_slug(slug, allowed_domains)
                    scaffold_state = _new_scaffold_state(slug, run_dir, repo_root)
                    effective_max_rounds = max(
                        effective_max_rounds, SCAFFOLD_MAX_ROUNDS
                    )
                    emit({"type": "scaffold_start", "domain_slug": slug})
                stage_result = _handle_propose_file(
                    file_payload=file_payload,
                    scaffold_state=scaffold_state,
                    round_index=index,
                )
                emit(
                    {
                        "type": "propose_file",
                        "relative_path": stage_result["relative_path"],
                        "status": stage_result["status"],
                        "checks": stage_result["checks"],
                        "manifest_progress": stage_result["manifest_progress"],
                    }
                )
                rounds.append(
                    {
                        "round_index": index,
                        "action": "PROPOSE_FILE",
                        "raw_action": call.raw_action_text,
                        "file": {
                            "relative_path": file_payload.get("relative_path", ""),
                            "purpose": str(file_payload.get("purpose") or ""),
                            # Echo a content snippet only — don't bloat the file
                            # by re-storing the full agent payload (it's on disk).
                            "content": "<staged>",
                        },
                        "file_result": stage_result,
                    }
                )
                _flush_in_progress(
                    session_path,
                    base_session,
                    rounds,
                    usage,
                    extracted,
                    scaffold_state,
                )
                index += 1
                continue

            if action_type != "ASK":
                raise GrillingError(
                    f"grilling agent returned unsupported action: {action_type!r}"
                )
            question = str(call.parsed.get("question", "")).strip()
            if not question:
                raise GrillingError("grilling agent emitted ASK without question")
            # Persist the pending ASK BEFORE blocking on the user. Without
            # this the question only existed in the SSE event + agent's
            # local variable, so a browser that navigated away after Q3
            # emit but before the reply would return to find an empty
            # awaiting_input chat (the question was unreachable).
            pending_ask = {
                "question": question,
                "raw_action": call.raw_action_text,
                "emitted_at": datetime.now(timezone.utc).isoformat(),
            }
            _flush_in_progress(
                session_path,
                base_session,
                rounds,
                usage,
                extracted,
                scaffold_state,
                pending_ask=pending_ask,
            )
            user_response = asker(question)
            rounds.append(
                {
                    "round_index": index,
                    "action": "ASK",
                    "question": question,
                    "user_response": str(user_response),
                    "raw_action": call.raw_action_text,
                }
            )
            # If the user's reply opts into scaffolding, bump the round budget
            # so the agent has room for the deep interview + manifest loop.
            if _user_opted_into_scaffolding(question, user_response):
                effective_max_rounds = max(effective_max_rounds, SCAFFOLD_MAX_ROUNDS)
            # Clear pending_ask: the reply has been captured into rounds.
            _flush_in_progress(
                session_path,
                base_session,
                rounds,
                usage,
                extracted,
                scaffold_state,
                pending_ask=None,
            )
            index += 1
        else:
            # Loop exhausted max_rounds. In scaffold mode, force-extracting
            # a placeholder claim contract would leave a half-built domain
            # on disk; treat this as scaffold_failed instead.
            if scaffold_state and scaffold_state.get("active") and not scaffold_state.get(
                "finalized"
            ):
                status = "max_rounds_reached"
                scaffold_state.setdefault("dry_import_result", None)
                # Keep staging dir for inspection; do NOT move to final.
                extracted = _placeholder_extracted(user_goal)
            else:
                final = _call_claude_round(
                    runner=runner,
                    claude_path=detected_claude,
                    model=model,
                    max_budget=max_budget,
                    user_goal=user_goal,
                    rounds=rounds,
                    force_extract=True,
                    round_timeout_seconds=round_timeout_seconds,
                    allowed_domains=allowed_domains,
                    scaffold_state=scaffold_state,
                )
                _accumulate_usage(usage, final)
                if str(final.parsed.get("action", "")) != "DONE":
                    raise GrillingError(
                        "grilling agent exhausted max_rounds without DONE on forced extraction"
                    )
                extracted = _coerce_extracted(
                    final.parsed.get("extracted"),
                    user_goal,
                    allowed_domains=allowed_domains,
                )
                status = "max_rounds_reached"
    except GrillingError as exc:
        error = str(exc)
        status = "aborted"

    if extracted is None:
        extracted = _placeholder_extracted(user_goal)

    session = {
        **base_session,
        "status": status,
        "rounds": rounds,
        "extracted": extracted,
        "usage_estimate": usage,
        "error": error,
    }
    if scaffold_state is not None:
        session["scaffold_state"] = scaffold_state
    _validate_session_or_replace(session)
    session_path.write_text(
        json.dumps(session, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if status == "aborted":
        raise GrillingError(error or "grilling session aborted")
    return session


def _call_claude_round(
    *,
    runner: CommandRunner,
    claude_path: str,
    model: str,
    max_budget: str,
    user_goal: str,
    rounds: list[dict[str, Any]],
    force_extract: bool,
    round_timeout_seconds: int,
    allowed_domains: list[str] | None = None,
    scaffold_state: dict[str, Any] | None = None,
) -> _RoundCall:
    system_prompt = _grilling_system_prompt(
        allowed_domains or [], scaffold_active=bool(scaffold_state and scaffold_state.get("active"))
    )
    user_prompt = _grilling_user_prompt(
        user_goal, rounds, force_extract=force_extract, scaffold_state=scaffold_state
    )
    cmd = [
        claude_path,
        "-p",
        "--model",
        model,
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--system-prompt",
        system_prompt,
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--no-session-persistence",
        "--max-budget-usd",
        max_budget,
    ]
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        completed = runner(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=round_timeout_seconds,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise GrillingError(
            f"claude CLI timed out after {round_timeout_seconds}s during grilling round"
        ) from exc
    if completed.returncode not in (0, None):
        raise GrillingError(
            f"claude CLI exited with code {completed.returncode}: {(completed.stderr or '').strip()[:200]}"
        )
    raw_stdout = completed.stdout or ""
    try:
        cli_result = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise GrillingError(f"claude CLI did not return JSON: {raw_stdout[:200]!r}") from exc
    if not isinstance(cli_result, dict) or cli_result.get("type") != "result":
        raise GrillingError(f"claude CLI returned unexpected payload: {raw_stdout[:200]!r}")
    if cli_result.get("is_error"):
        raise GrillingError(
            f"claude CLI reported error subtype {cli_result.get('subtype')!r}"
        )
    inner_result = cli_result.get("result")
    if not isinstance(inner_result, str) or not inner_result.strip():
        raise GrillingError("claude CLI returned empty assistant text")
    action_text = _strip_fence(inner_result.strip())
    try:
        parsed = json.loads(action_text)
    except json.JSONDecodeError as exc:
        raise GrillingError(
            f"grilling agent output was not parseable JSON: {action_text[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise GrillingError("grilling agent output must be a JSON object")
    usage = cli_result.get("usage") if isinstance(cli_result.get("usage"), dict) else {}
    return _RoundCall(
        raw_action_text=action_text,
        parsed=parsed,
        cost_usd=float(cli_result.get("total_cost_usd") or 0.0),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
    )


def _grilling_system_prompt(
    allowed_domains: list[str], *, scaffold_active: bool = False
) -> str:
    base = (
        "You are a research-grilling agent. Your role is to interview the user "
        "until you can extract a complete claim contract. "
        "Each turn you output exactly one JSON object and nothing else. "
        "No markdown, no prose, no code fences. "
        "Allowed actions: "
        '{"action":"ASK","question":"<one specific probing question>"}, '
        '{"action":"PROPOSE_FILE","file":{"relative_path":"<slug>/...","purpose":"...","content":"..."}}, '
        'or {"action":"DONE","extracted":{...}}. '
        "PROPOSE_FILE is only valid during deep-interview scaffolding mode "
        "(see below). Use ASK while information is missing. Use DONE only when "
        "you can fill every extracted field with concrete content drawn from "
        "the user. extracted must include: root_goal_id (slug like rg_xxx), "
        "domain, node_type (one of capability|validity|necessity|boundary|"
        "mechanism|constraint|taste|operational), claim_under_test (single "
        "sentence), mandatory_baselines (>=1 strings, ideally one current-"
        "best, one naive, one random/null), success_criteria (>=1 "
        "measurable), disproof_conditions (>=1), goal_facets (e.g. "
        "performance, efficiency, simplicity, interpretability), "
        "taste_constraints, and search_query_seed (string used to drive "
        "paper search). Probe necessity, baselines, and disproof conditions "
        "hard."
    )
    if allowed_domains:
        domains_json = json.dumps(allowed_domains)
        base += (
            " IMPORTANT: the domain field MUST be exactly one of "
            f"{domains_json}. Do not invent new domain names. "
            "Pick the closest fit, or ASK the user to clarify which of these "
            "domains their work belongs to. If absolutely none fit, ASK the "
            "user whether to scaffold a NEW domain (do NOT just invent one)."
        )
    base += (
        " === DOMAIN SCAFFOLDING (deep interview mode) === "
        "If the user explicitly opts into scaffolding a new domain (because "
        "none of the registered domains fit), enter deep interview mode. "
        "REQUIRED CHECKLIST — collect via ASK rounds BEFORE any PROPOSE_FILE: "
        "[1] domain_slug (snake_case, ascii, e.g. 'summarization_factscore'). "
        "[2] task_class (one of smoke_test | ablation | training | eval | "
        "analysis). [3] data_source (kind: synthetic | local_file | "
        "huggingface | torch_hub | s3_internal; plus shape/seed for "
        "synthetic, identifier+split+schema for external). [4] "
        "proposed_method_spec (inputs, outputs, one-paragraph pseudocode). "
        "[5] baseline_specs for current_best / naive / random_baseline "
        "(concrete algorithm or formula for each). [6] metric_spec (name "
        "like ndcg_at_10, formula or library reference, comparison "
        "operator vs baselines, margin threshold). [7] resource_budget "
        "(timeout_sec, gpu bool, cpu, memory_gb). [8] expected_outputs "
        "(default artifacts/metrics.json plus any extras). After each "
        "answer, summarize what you captured so the user can correct "
        "misinterpretation. "
        "PROPOSE_FILE order (bottom-up dependency, easier to localize "
        "import failures): plan.json → src/__init__.py → "
        "src/eval/__init__.py → src/eval/<metric>.py → "
        "src/baselines/__init__.py → src/baselines/random_baseline.py → "
        "src/baselines/naive.py → src/baselines/current_best.py → "
        "src/data.py → src/proposed.py → src/experiment.py. "
        "Every PROPOSE_FILE.relative_path MUST start with the domain_slug "
        "as its first path segment (e.g. 'summarization_factscore/plan.json'). "
        "The harness validates each file (syntax for .py, schema for "
        "plan.json) and injects the result into the next round; fix on "
        "failure by emitting another PROPOSE_FILE for the same path. "
        "On DONE the harness dry-imports the staged src/ tree and rejects "
        "DONE if any required file is missing or import fails — you'll see "
        "the rejection in the next round and must emit more PROPOSE_FILE "
        "to repair. Required manifest (exact path tails, all required): "
        + json.dumps(list(REQUIRED_MANIFEST_TEMPLATE))
        + ". You may propose additional optional files (e.g. README.md)."
    )
    if scaffold_active:
        base += (
            " === MODE STATUS === Scaffolding mode is ACTIVE. The slug is "
            "fixed. Continue with the checklist (if incomplete) or "
            "PROPOSE_FILE (if checklist done) until all required manifest "
            "files are staged, then emit DONE."
        )
    return base


def _grilling_user_prompt(
    user_goal: str,
    rounds: list[dict[str, Any]],
    *,
    force_extract: bool,
    scaffold_state: dict[str, Any] | None = None,
) -> str:
    lines = [
        "## User Goal",
        user_goal.strip(),
        "",
        "## Transcript",
    ]
    if not rounds:
        lines.append("(no rounds yet)")
    for entry in rounds:
        action = entry.get("action") or "ASK"
        if action == "ASK":
            q = entry.get("question") or ""
            a = entry.get("user_response") or ""
            lines.append(f"Q{entry['round_index'] + 1}: {q}")
            lines.append(f"A{entry['round_index'] + 1}: {a}")
        elif action == "PROPOSE_FILE":
            f = entry.get("file") or {}
            r = entry.get("file_result") or {}
            checks_str = json.dumps(r.get("checks") or {}, ensure_ascii=False)
            lines.append(
                f"P{entry['round_index'] + 1}: PROPOSE_FILE "
                f"path={f.get('relative_path', '')!r} "
                f"purpose={f.get('purpose', '')!r} "
                f"=> status={r.get('status', 'unknown')} checks={checks_str}"
            )
        elif action == "DONE":
            r = entry.get("file_result") or {}
            if r:
                lines.append(
                    f"D{entry['round_index'] + 1}: DONE rejected "
                    f"=> {json.dumps(r.get('checks') or {}, ensure_ascii=False)}"
                )
            else:
                lines.append(f"D{entry['round_index'] + 1}: DONE accepted")
    lines.append("")

    if scaffold_state and scaffold_state.get("active"):
        slug = scaffold_state.get("domain_slug") or "<unknown>"
        manifest_required = scaffold_state.get("manifest_required") or []
        manifest_staged = scaffold_state.get("manifest_staged") or []
        manifest_missing = [m for m in manifest_required if m not in manifest_staged]
        lines.extend(
            [
                "## Scaffold State",
                f"domain_slug: {slug}",
                f"staged: {len(manifest_staged)} / {len(manifest_required)}",
                f"missing: {json.dumps(manifest_missing, ensure_ascii=False)}",
                "",
            ]
        )

    if force_extract:
        lines.extend(
            [
                "## Force Extract",
                "Max rounds reached. Do not ASK. Output one DONE action with the best "
                "extracted contract you can derive from this transcript. Fill every "
                "required field; never leave them empty.",
            ]
        )
    else:
        lines.extend(
            [
                "## Your Next Turn",
                "Output exactly one JSON action object. ASK if anything required is "
                "still missing or ambiguous. DONE only when all required extracted "
                "fields can be filled with concrete content. PROPOSE_FILE only "
                "during scaffolding mode after the REQUIRED checklist is captured.",
            ]
        )
    return "\n".join(lines)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def _coerce_extracted(
    extracted: Any,
    user_goal: str,
    *,
    allowed_domains: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(extracted, dict):
        raise GrillingError("DONE action missing extracted object")
    required_lists = (
        "mandatory_baselines",
        "success_criteria",
        "disproof_conditions",
    )
    for key in required_lists:
        if not isinstance(extracted.get(key), list) or not extracted[key]:
            raise GrillingError(f"extracted.{key} must be a non-empty array")
    if not extracted.get("root_goal_id"):
        extracted["root_goal_id"] = "rg_" + _slugify(user_goal)[:40]
    if not extracted.get("domain"):
        extracted["domain"] = "unspecified_domain"
    if allowed_domains and extracted["domain"] not in allowed_domains:
        raise GrillingError(
            f"extracted.domain {extracted['domain']!r} is not in the "
            f"operator-registered domain list {allowed_domains!r}; the "
            "grilling agent must pick exactly one of those."
        )
    if not extracted.get("node_type"):
        extracted["node_type"] = "capability"
    if not extracted.get("claim_under_test"):
        extracted["claim_under_test"] = user_goal.strip()
    if not extracted.get("search_query_seed"):
        extracted["search_query_seed"] = user_goal.strip()
    if "goal_facets" not in extracted or not isinstance(extracted["goal_facets"], list):
        extracted["goal_facets"] = []
    if "taste_constraints" not in extracted or not isinstance(
        extracted["taste_constraints"], list
    ):
        extracted["taste_constraints"] = []
    extracted["goal_facets"] = [str(item) for item in extracted["goal_facets"]]
    extracted["taste_constraints"] = [str(item) for item in extracted["taste_constraints"]]
    for key in required_lists:
        extracted[key] = [str(item) for item in extracted[key]]
    return extracted


def _placeholder_extracted(user_goal: str) -> dict[str, Any]:
    return {
        "root_goal_id": "rg_" + _slugify(user_goal)[:40],
        "domain": "unspecified_domain",
        "node_type": "capability",
        "claim_under_test": user_goal.strip(),
        "mandatory_baselines": ["pending_grilling"],
        "success_criteria": ["pending_grilling"],
        "disproof_conditions": ["pending_grilling"],
        "goal_facets": [],
        "taste_constraints": [],
        "search_query_seed": user_goal.strip(),
    }


def _slugify(text: str) -> str:
    """ASCII-only slug. The downstream schema regex
    ``^rg_[A-Za-z0-9_\\-]+$`` rejects non-ASCII, and Python's
    ``str.isalnum()`` is *unicode-aware* — it returns True for Hangul,
    CJK, accented Latin, etc. Without the explicit ``isascii`` filter,
    a Korean user_goal would produce a root_goal_id like ``rg_나는_머신``
    that fails schema validation, which in turn causes
    ``_flush_in_progress`` to silently swallow the write and the user's
    grilling appears to "lose" every round.
    """
    out: list[str] = []
    for char in text.lower():
        if char.isascii() and char.isalnum():
            out.append(char)
        elif char in {" ", "-", "_"}:
            out.append("_")
    slug = "".join(out).strip("_") or "session"
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug


def _new_session_id() -> str:
    return "grill_" + uuid.uuid4().hex[:12]


def _accumulate_usage(usage: dict[str, Any], call: _RoundCall) -> None:
    usage["total_cost_usd"] = float(usage.get("total_cost_usd") or 0.0) + call.cost_usd
    usage["total_input_tokens"] = int(usage.get("total_input_tokens") or 0) + call.input_tokens
    usage["total_output_tokens"] = int(usage.get("total_output_tokens") or 0) + call.output_tokens


def _flush_in_progress(
    session_path: Path,
    base_session: dict[str, Any],
    rounds: list[dict[str, Any]],
    usage: dict[str, Any],
    extracted: dict[str, Any] | None,
    scaffold_state: dict[str, Any] | None = None,
    pending_ask: dict[str, Any] | None = None,
) -> None:
    """Persist mid-grilling state so a server crash never loses user input.

    Schema accepts status="in_progress"; flush is best-effort and never
    raises into the agent loop (loop continues regardless of disk error).
    The write is atomic (temp file + os.replace) so concurrent HTTP readers
    never observe a truncated session JSON.
    """
    snapshot = {
        **base_session,
        "status": "in_progress",
        "rounds": rounds,
        "extracted": extracted or base_session["extracted"],
        "usage_estimate": usage,
        "error": None,
    }
    if scaffold_state is not None:
        snapshot["scaffold_state"] = scaffold_state
    # pending_ask is intentionally written even when None — clearing it
    # after a user reply is part of the contract (otherwise the page would
    # show a stale ASK that was already answered).
    snapshot["pending_ask"] = pending_ask
    try:
        validate_named_schema("grilling_session", snapshot)
    except SchemaValidationError as exc:
        # Surface the failure on stderr so silent flush-loss never goes
        # unnoticed again. Past silent failures (e.g. a non-ASCII root_goal_id
        # rejected by the schema regex) made grilling appear to "lose" all
        # the user's rounds because the file was never written at all.
        import sys

        sys.stderr.write(
            f"[grilling] _flush_in_progress schema validation failed; "
            f"session file NOT written. error={exc}\n"
        )
        return
    _atomic_write_json(session_path, snapshot)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Best-effort atomic JSON write — temp file + rename, swallow OSError."""
    import os
    import tempfile

    try:
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix="." + path.name + ".", suffix=".tmp", dir=str(parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError:
        return


def load_resumable_session(session_path: Path) -> dict[str, Any] | None:
    """Load an in-progress grilling_session.json for resume, or None.

    Returns the parsed session dict if and only if the file exists, parses,
    validates, and is in status "in_progress". Anything else (missing file,
    schema mismatch, terminal status) returns None — callers should treat
    that as "nothing to resume, start a fresh session".
    """
    if not session_path.exists():
        return None
    try:
        loaded = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        validate_named_schema("grilling_session", loaded)
    except SchemaValidationError:
        return None
    if loaded.get("status") != "in_progress":
        return None
    return loaded


def _validate_session_or_replace(session: dict[str, Any]) -> None:
    try:
        validate_named_schema("grilling_session", session)
    except SchemaValidationError as exc:
        raise GrillingError(f"grilling session failed schema validation: {exc}") from exc


def _abort_session(
    session_path: Path,
    session: dict[str, Any],
    *,
    status: str,
    error: str,
) -> dict[str, Any]:
    aborted = {**session, "status": status, "error": error}
    validate_named_schema("grilling_session", aborted)
    session_path.write_text(
        json.dumps(aborted, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return aborted


def _infer_thread_id(run_dir: Path) -> str:
    """Recover the thread_id from the grilling run_dir path.

    The frontend places grilling output at
    runs/threads/<thread_id>/grilling/. CLI users may pick any run_dir,
    in which case this returns 'standalone' as a sentinel marker for
    the .scaffold_origin.json forensic field.
    """
    parts = run_dir.resolve().parts
    if "threads" in parts:
        idx = parts.index("threads")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return "standalone"


def _derive_slug_from_path(relative_path: Any) -> str:
    if not isinstance(relative_path, str) or "/" not in relative_path.strip("/"):
        raise GrillingError(
            "PROPOSE_FILE.file.relative_path must be '<slug>/<path-inside-domain>'"
        )
    slug = relative_path.strip("/").split("/", 1)[0]
    return slug


def _validate_new_slug(slug: str, allowed_domains: list[str] | None) -> None:
    if slug in RESERVED_SLUGS:
        raise GrillingError(f"domain slug {slug!r} is reserved; pick another")
    if not SLUG_PATTERN.match(slug):
        raise GrillingError(
            f"domain slug {slug!r} must match {SLUG_PATTERN.pattern} "
            "(lowercase ascii, underscores, no leading/trailing/double underscores)"
        )
    if "__" in slug:
        raise GrillingError(f"domain slug {slug!r} must not contain '__'")
    if allowed_domains and slug in allowed_domains:
        raise GrillingError(
            f"domain slug {slug!r} already exists in the registered enum; "
            "pick a distinct slug or refine the user goal to use the existing one"
        )


def _new_scaffold_state(
    slug: str, run_dir: Path, repo_root: Path
) -> dict[str, Any]:
    staging_dir = (run_dir / "scaffold_staging" / slug).resolve()
    staging_dir.mkdir(parents=True, exist_ok=True)
    final_destination = (
        repo_root / "experiment_plan_templates" / slug
    ).resolve()
    return {
        "active": True,
        "domain_slug": slug,
        "staging_dir": str(staging_dir),
        "final_destination": str(final_destination),
        "manifest_required": list(REQUIRED_MANIFEST_TEMPLATE),
        "manifest_staged": [],
        "manifest_missing": list(REQUIRED_MANIFEST_TEMPLATE),
        "proposed_files": [],
        "dry_import_result": None,
        "finalized": False,
    }


def _handle_propose_file(
    *,
    file_payload: dict[str, Any],
    scaffold_state: dict[str, Any],
    round_index: int,
) -> dict[str, Any]:
    """Stage one proposed file under the scaffold staging dir, validate,
    and return a result dict that mirrors the on-disk record."""
    raw_relative = str(file_payload.get("relative_path") or "")
    purpose = str(file_payload.get("purpose") or "")
    content = file_payload.get("content")
    slug = scaffold_state["domain_slug"]

    # Strip the slug prefix so the staged path mirrors the final layout
    # under experiment_plan_templates/<slug>/.
    if not raw_relative.startswith(slug + "/"):
        result = {
            "relative_path": raw_relative,
            "status": "failed",
            "checks": {
                "path": "error",
                "path_error": (
                    f"relative_path {raw_relative!r} must start with "
                    f"the scaffold slug '{slug}/'"
                ),
            },
            "staged_at": None,
            "manifest_progress": [
                len(scaffold_state["manifest_staged"]),
                len(scaffold_state["manifest_required"]),
            ],
        }
        scaffold_state["proposed_files"].append(
            {
                "relative_path": raw_relative,
                "purpose": purpose,
                "status": "failed",
                "round_index": round_index,
                "checks": result["checks"],
            }
        )
        return result

    inside_path = raw_relative[len(slug) + 1 :]
    if ".." in inside_path.split("/") or inside_path.startswith("/"):
        raise GrillingError(
            f"PROPOSE_FILE.relative_path {raw_relative!r} contains traversal segments"
        )
    if not isinstance(content, str):
        raise GrillingError("PROPOSE_FILE.file.content must be a string")

    staging_dir = Path(scaffold_state["staging_dir"])
    target = staging_dir / inside_path
    target.parent.mkdir(parents=True, exist_ok=True)

    checks: dict[str, Any] = {}
    if inside_path.endswith(".py"):
        syntax = _validate_python_source(content)
        checks["syntax"] = syntax["status"]
        if syntax["status"] == "error":
            checks["syntax_error"] = syntax["error"]
    elif inside_path == "plan.json":
        schema_result = _validate_plan_json_text(content)
        checks["schema"] = schema_result["status"]
        if schema_result["status"] == "error":
            checks["schema_errors"] = schema_result["errors"]
    else:
        # Optional / unknown file type: stage without strict validation.
        checks["validation"] = "skipped (not a manifest file type)"

    status = "ok"
    if checks.get("syntax") == "error" or checks.get("schema") == "error":
        status = "failed"

    # Always stage; failed validation still leaves the bad file on disk for
    # the agent to fix on the next round (idempotent overwrite).
    target.write_text(content, encoding="utf-8")

    # Manifest progress only counts files that PASSED validation.
    if status == "ok" and inside_path in scaffold_state["manifest_required"]:
        if inside_path not in scaffold_state["manifest_staged"]:
            scaffold_state["manifest_staged"].append(inside_path)
        scaffold_state["manifest_missing"] = [
            m
            for m in scaffold_state["manifest_required"]
            if m not in scaffold_state["manifest_staged"]
        ]

    scaffold_state["proposed_files"].append(
        {
            "relative_path": raw_relative,
            "purpose": purpose,
            "status": status,
            "round_index": round_index,
            "checks": checks,
        }
    )
    return {
        "relative_path": raw_relative,
        "status": status,
        "checks": checks,
        "staged_at": str(target),
        "manifest_progress": [
            len(scaffold_state["manifest_staged"]),
            len(scaffold_state["manifest_required"]),
        ],
    }


def _validate_python_source(content: str) -> dict[str, Any]:
    try:
        ast.parse(content)
    except SyntaxError as exc:
        return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
    return {"status": "ok"}


def _validate_plan_json_text(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return {"status": "error", "errors": [f"JSONDecodeError: {exc}"]}
    try:
        validate_named_schema("user_experiment_plan_metadata", parsed)
    except SchemaValidationError as exc:
        return {"status": "error", "errors": [str(exc)]}
    return {"status": "ok"}


def _finalize_scaffold(
    *,
    run_dir: Path,
    repo_root: Path,
    scaffold_state: dict[str, Any],
    thread_id: str,
    session_id: str,
    extracted_candidate: Any,
) -> dict[str, Any]:
    """Validate manifest + dry-import + atomic move to final destination."""
    missing = [
        m
        for m in scaffold_state["manifest_required"]
        if m not in scaffold_state["manifest_staged"]
    ]
    if missing:
        return {
            "status": "failed",
            "manifest_missing": missing,
            "dry_import_error": None,
        }

    staging_dir = Path(scaffold_state["staging_dir"])
    final_dir = Path(scaffold_state["final_destination"])
    slug = scaffold_state["domain_slug"]

    # Dry-import the staged src/ tree.
    dry_import = _dry_import_src(staging_dir, slug)
    if dry_import["status"] == "failed":
        return {
            "status": "failed",
            "manifest_missing": [],
            "dry_import_error": dry_import["error"],
        }

    # Write the forensic marker BEFORE the move so it lands with the dir.
    marker = {
        "scaffolded_by_thread": thread_id,
        "scaffolded_at": datetime.now(timezone.utc).isoformat(),
        "scaffold_session_id": session_id,
        "grilling_extracted_summary": (
            {
                "claim_under_test": extracted_candidate.get("claim_under_test")
                if isinstance(extracted_candidate, dict)
                else None,
                "domain": slug,
            }
        ),
    }
    (staging_dir / ".scaffold_origin.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Atomic move staging -> final. Same filesystem assumed; fall back to
    # copy+rename if rename fails across mounts.
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    if final_dir.exists():
        return {
            "status": "failed",
            "manifest_missing": [],
            "dry_import_error": (
                f"final destination {final_dir} already exists (race with another scaffold)"
            ),
        }
    try:
        os.rename(staging_dir, final_dir)
    except OSError:
        # Cross-mount or other rename failure: copy-then-swap.
        temp_dir = final_dir.with_name(final_dir.name + ".finalizing")
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        shutil.copytree(staging_dir, temp_dir)
        os.rename(temp_dir, final_dir)
        shutil.rmtree(staging_dir)

    return {
        "status": "ok",
        "final_destination": str(final_dir),
    }


def _dry_import_src(staging_dir: Path, slug: str) -> dict[str, Any]:
    """Import every .py module under <staging>/src/ as a module rooted at
    <slug>.src.*. Failure: the first ImportError / SyntaxError wins."""
    src_dir = staging_dir / "src"
    if not src_dir.is_dir():
        return {"status": "failed", "error": "src/ directory missing in staging"}

    # Build a fresh sys.path entry and a unique module namespace per slug.
    # Snapshot sys.modules so we can roll back after the dry run leaves
    # no global pollution (matters: a later real run materializes src into
    # node workspace and imports it as `src.*`, not `<slug>.src.*`).
    sentinel = f"_scaffold_dry_{slug}_{uuid.uuid4().hex[:6]}"
    path_str = str(staging_dir)
    sys.path.insert(0, path_str)
    pre_modules = set(sys.modules.keys())
    try:
        for py_file in sorted(src_dir.rglob("*.py")):
            rel = py_file.relative_to(staging_dir)
            mod_path = ".".join(part.removesuffix(".py") for part in rel.parts)
            # Skip __init__ self-reference; importing the package walks them.
            full_name = f"{sentinel}_{mod_path}"
            try:
                spec = importlib.util.spec_from_file_location(
                    full_name, str(py_file)
                )
                if spec is None or spec.loader is None:
                    raise ImportError(f"no loader for {py_file}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                return {
                    "status": "failed",
                    "error": f"{rel}: {exc.__class__.__name__}: {exc}",
                }
        return {"status": "ok", "error": None}
    finally:
        # Restore sys.path
        try:
            sys.path.remove(path_str)
        except ValueError:
            pass
        # Roll back any modules the dry-import injected.
        for name in list(sys.modules.keys()):
            if name not in pre_modules and name.startswith(sentinel):
                del sys.modules[name]


def _user_opted_into_scaffolding(question: str, user_response: Any) -> bool:
    """Cheap heuristic: did the user's latest reply opt into scaffolding?

    Returns True only when the agent's question clearly asked about
    scaffolding AND the user replied affirmatively. The system prompt is
    the actual gatekeeper; this is only used to bump max_rounds early so
    the agent has budget for the deep interview.
    """
    q = (question or "").lower()
    r = str(user_response or "").lower().strip()
    if "scaffold" not in q and "new domain" not in q:
        return False
    return any(token in r for token in ("yes", "ok", "sure", "scaffold", "create"))


def _stdin_input_provider(question: str) -> str:
    print(f"\n[grilling] Q: {question}\n")
    try:
        return input("> ").strip()
    except EOFError:
        return ""


def _billing_ack_ok(billing_ack: bool | None) -> bool:
    if billing_ack is not None:
        return bool(billing_ack)
    return os.environ.get(BILLING_ACK_ENV) == BILLING_ACK_VALUE


def _execution_ack_ok(execution_ack: bool | None) -> bool:
    if execution_ack is not None:
        return bool(execution_ack)
    return os.environ.get(EXECUTION_ACK_ENV) == EXECUTION_ACK_VALUE
