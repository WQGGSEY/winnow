from __future__ import annotations

import json
import os
import shutil
import subprocess
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


CommandRunner = Callable[..., subprocess.CompletedProcess]
InputProvider = Callable[[str], str]


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

    created_at = datetime.now(timezone.utc).isoformat()
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

    rounds: list[dict[str, Any]] = []
    usage = {
        "rounds_used": 0,
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }
    extracted: dict[str, Any] | None = None
    status = "in_progress"
    error: str | None = None

    try:
        for index in range(max_rounds):
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
            )
            _accumulate_usage(usage, call)
            usage["rounds_used"] = index + 1
            action_type = str(call.parsed.get("action", ""))
            if action_type == "DONE":
                extracted = _coerce_extracted(
                    call.parsed.get("extracted"),
                    user_goal,
                    allowed_domains=allowed_domains,
                )
                status = "done"
                break
            if action_type != "ASK":
                raise GrillingError(
                    f"grilling agent returned unsupported action: {action_type!r}"
                )
            question = str(call.parsed.get("question", "")).strip()
            if not question:
                raise GrillingError("grilling agent emitted ASK without question")
            user_response = asker(question)
            rounds.append(
                {
                    "round_index": index,
                    "question": question,
                    "user_response": str(user_response),
                    "raw_action": call.raw_action_text,
                }
            )
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
) -> _RoundCall:
    system_prompt = _grilling_system_prompt(allowed_domains or [])
    user_prompt = _grilling_user_prompt(user_goal, rounds, force_extract=force_extract)
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


def _grilling_system_prompt(allowed_domains: list[str]) -> str:
    base = (
        "You are a research-grilling agent. Your role is to interview the user "
        "until you can extract a complete claim contract. "
        "Each turn you output exactly one JSON object and nothing else. "
        "No markdown, no prose, no code fences. "
        "Allowed actions: "
        '{"action":"ASK","question":"<one specific probing question>"} or '
        '{"action":"DONE","extracted":{...}}. '
        "Use ASK while information is missing. Use DONE only when you can fill "
        "every extracted field with concrete content drawn from the user. "
        "extracted must include: root_goal_id (slug like rg_xxx), domain, "
        "node_type (one of capability|validity|necessity|boundary|mechanism|"
        "constraint|taste|operational), claim_under_test (single sentence), "
        "mandatory_baselines (>=1 strings, ideally one current-best, one "
        "naive, one random/null), success_criteria (>=1 measurable), "
        "disproof_conditions (>=1), goal_facets (e.g. performance, "
        "efficiency, simplicity, interpretability), taste_constraints, and "
        "search_query_seed (string used to drive paper search). "
        "Probe necessity, baselines, and disproof conditions hard."
    )
    if allowed_domains:
        domains_json = json.dumps(allowed_domains)
        base += (
            " IMPORTANT: the domain field MUST be exactly one of "
            f"{domains_json}. Do not invent new domain names. "
            "Pick the closest fit, or ASK the user to clarify which of these "
            "domains their work belongs to. If absolutely none fit, ASK the "
            "user instead of inventing one."
        )
    return base


def _grilling_user_prompt(
    user_goal: str,
    rounds: list[dict[str, Any]],
    *,
    force_extract: bool,
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
        lines.append(f"Q{entry['round_index'] + 1}: {entry['question']}")
        lines.append(f"A{entry['round_index'] + 1}: {entry['user_response']}")
    lines.append("")
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
                "fields can be filled with concrete content.",
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
    out = []
    for char in text.lower():
        if char.isalnum():
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
