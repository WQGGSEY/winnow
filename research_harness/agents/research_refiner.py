"""research_refiner agent.

Runs after market_research and before root_node generation. Multi-turn
live sonnet (or whichever model settings.runtime.agent_models picks for
``research_refiner_agent``) drives a 3-action protocol:

- ``ASK``  - question to the operator, harness reads stdin reply.
- ``PROPOSE_DATASET`` - candidate dataset_spec; harness silently dispatches
  to the materializer registry and injects the result into the next round.
- ``DONE`` - emit the final refined_research_plan plus the dataset_manifest.

The agent owns the decision layer: validation_procedure, dataset_specs,
sota_reconciliations, acknowledged_limitations. Materialization itself
is deterministic.
"""

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
from research_harness.datasets import materialize
from research_harness.orchestrator.experiment_plan import list_available_domains
from research_harness.schemas.validator import (
    SchemaValidationError,
    validate_named_schema,
)


BILLING_ACK_ENV = "RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE"
BILLING_ACK_VALUE = "subscription_ack"
EXECUTION_ACK_ENV = "RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE"
EXECUTION_ACK_VALUE = "live_smoke_ack"
DEFAULT_MAX_ROUNDS = 12
DEFAULT_ROUND_TIMEOUT_SECONDS = 240
MAX_BASELINE_MD_CHARS = 6000


CommandRunner = Callable[..., subprocess.CompletedProcess]
InputProvider = Callable[[str], str]


class RefinerError(ValueError):
    """Raised when the refiner cannot produce a valid plan."""


@dataclass
class _RoundCall:
    raw_action_text: str
    parsed: dict[str, Any]
    cost_usd: float
    input_tokens: int
    output_tokens: int


def run_research_refiner(
    repo_root: Path,
    *,
    grilling_session: dict[str, Any],
    market_research_brief: dict[str, Any],
    run_dir: Path | None = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    plan_id: str | None = None,
    claude_path: str | None = None,
    billing_ack: bool | None = None,
    execution_ack: bool | None = None,
    round_timeout_seconds: int = DEFAULT_ROUND_TIMEOUT_SECONDS,
    command_runner: CommandRunner | None = None,
    input_provider: InputProvider | None = None,
    allowed_domains: list[str] | None = None,
) -> dict[str, Any]:
    """Drive the 3-action loop and emit refined_research_plan + dataset_manifest."""

    validate_named_schema("grilling_session", grilling_session)
    validate_named_schema("market_research_brief", market_research_brief)
    if max_rounds < 1 or max_rounds > 50:
        raise RefinerError("max_rounds must be between 1 and 50")

    repo_root = repo_root.resolve()
    settings = load_settings(repo_root)
    live_backend = (
        settings.get("runtime", {})
        .get("worker_backends", {})
        .get("claude_code_live", {})
    )
    model = resolve_agent_model(settings, "research_refiner_agent")
    max_budget = str(live_backend.get("max_budget_usd", "0.25"))

    if allowed_domains is None:
        try:
            allowed_domains = list_available_domains(repo_root, settings)
        except Exception:
            allowed_domains = []
    allowed_domains = list(allowed_domains or [])

    plan_id = plan_id or "rrp_" + uuid.uuid4().hex[:12]
    run_dir = (run_dir or repo_root / "runs" / "research_refiner" / plan_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    plan_path = run_dir / "refined_research_plan.json"
    manifest_path = run_dir / "dataset_manifest.json"

    created_at = datetime.now(timezone.utc).isoformat()
    base_plan: dict[str, Any] = {
        "type": "refined_research_plan",
        "plan_id": plan_id,
        "status": "in_progress",
        "source_grilling_session_id": grilling_session["session_id"],
        "source_market_brief_id": market_research_brief["brief_id"],
        "claim_under_test": grilling_session["extracted"]["claim_under_test"],
        "mandatory_baselines": list(grilling_session["extracted"]["mandatory_baselines"]),
        "success_criteria": list(grilling_session["extracted"]["success_criteria"]),
        "disproof_conditions": list(grilling_session["extracted"]["disproof_conditions"]),
        "validation_procedure": _placeholder_validation_procedure(),
        "dataset_specs": [],
        "unresolved_dataset_specs": [],
        "sota_reconciliations": [],
        "acknowledged_limitations": [],
        "rounds": [],
        "model": model,
        "created_at": created_at,
        "usage_estimate": {
            "rounds_used": 0,
            "total_cost_usd": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        },
        "plan_path": str(plan_path),
        "dataset_manifest_path": str(manifest_path),
        "error": None,
    }

    if not _billing_ack_ok(billing_ack):
        return _abort(
            plan_path,
            base_plan,
            status="blocked_by_gate",
            error=f"set {BILLING_ACK_ENV}={BILLING_ACK_VALUE} to enable refiner",
        )
    if not _execution_ack_ok(execution_ack):
        return _abort(
            plan_path,
            base_plan,
            status="blocked_by_execution_ack",
            error=f"set {EXECUTION_ACK_ENV}={EXECUTION_ACK_VALUE} to actually invoke Claude for refiner",
        )

    detected_claude = claude_path or shutil.which("claude") or "claude"
    runner = command_runner or subprocess.run
    asker = input_provider or _stdin_input_provider

    baseline_md = _load_baseline_md(market_research_brief)
    rounds: list[dict[str, Any]] = []
    verified_dataset_specs: list[dict[str, Any]] = []
    manifest_entries: dict[str, dict[str, Any]] = {}
    usage = {
        "rounds_used": 0,
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }
    final_plan_payload: dict[str, Any] | None = None
    status = "in_progress"
    error: str | None = None

    try:
        for index in range(max_rounds):
            call = _call_refiner_round(
                runner=runner,
                claude_path=detected_claude,
                model=model,
                max_budget=max_budget,
                grilling_session=grilling_session,
                market_brief=market_research_brief,
                baseline_md=baseline_md,
                rounds=rounds,
                allowed_domains=allowed_domains,
                force_done=False,
                round_timeout_seconds=round_timeout_seconds,
            )
            _accumulate(usage, call)
            usage["rounds_used"] = index + 1
            action_type = str(call.parsed.get("action", "")).upper()

            if action_type == "ASK":
                question = str(call.parsed.get("question", "")).strip()
                if not question:
                    raise RefinerError("ASK action missing question text")
                user_response = asker(question)
                rounds.append(
                    {
                        "round_index": index,
                        "action": "ASK",
                        "payload": {"question": question},
                        "user_response": str(user_response),
                        "materialize_result": None,
                    }
                )
                continue

            if action_type == "PROPOSE_DATASET":
                spec = call.parsed.get("spec")
                if not isinstance(spec, dict):
                    raise RefinerError("PROPOSE_DATASET missing spec object")
                spec_id = str(spec.get("id") or f"ds_round{index}_{uuid.uuid4().hex[:6]}")
                spec["id"] = spec_id
                result = materialize(spec, repo_root=repo_root)
                if result.status == "ok":
                    verified_dataset_specs.append(spec)
                    manifest_entries[spec_id] = {
                        "spec": spec,
                        "materialized_path": (
                            str(result.materialized_path) if result.materialized_path else None
                        ),
                        "verified_at": datetime.now(timezone.utc).isoformat(),
                        "size_bytes": result.size_bytes,
                        "fetcher_type": result.fetcher_type,
                        "detail": result.detail or {},
                    }
                rounds.append(
                    {
                        "round_index": index,
                        "action": "PROPOSE_DATASET",
                        "payload": {"spec": spec},
                        "user_response": None,
                        "materialize_result": result.to_dict(),
                    }
                )
                continue

            if action_type == "DONE":
                final_plan_payload = call.parsed.get("plan")
                if not isinstance(final_plan_payload, dict):
                    raise RefinerError("DONE action missing plan object")
                rounds.append(
                    {
                        "round_index": index,
                        "action": "DONE",
                        "payload": {"plan": _compact_payload_for_audit(final_plan_payload)},
                        "user_response": None,
                        "materialize_result": None,
                    }
                )
                status = "done"
                break

            raise RefinerError(f"unsupported refiner action: {action_type!r}")
        else:
            final = _call_refiner_round(
                runner=runner,
                claude_path=detected_claude,
                model=model,
                max_budget=max_budget,
                grilling_session=grilling_session,
                market_brief=market_research_brief,
                baseline_md=baseline_md,
                rounds=rounds,
                allowed_domains=allowed_domains,
                force_done=True,
                round_timeout_seconds=round_timeout_seconds,
            )
            _accumulate(usage, final)
            if str(final.parsed.get("action", "")).upper() != "DONE":
                raise RefinerError(
                    "refiner exhausted max_rounds without DONE on forced finalization"
                )
            final_plan_payload = final.parsed.get("plan")
            if not isinstance(final_plan_payload, dict):
                raise RefinerError("force-DONE missing plan object")
            rounds.append(
                {
                    "round_index": max_rounds,
                    "action": "DONE",
                    "payload": {"plan": _compact_payload_for_audit(final_plan_payload)},
                    "user_response": None,
                    "materialize_result": None,
                }
            )
            status = "max_rounds_reached"
    except RefinerError as exc:
        error = str(exc)
        status = "aborted"

    plan = _assemble_plan(
        base=base_plan,
        status=status,
        error=error,
        rounds=rounds,
        usage=usage,
        verified_dataset_specs=verified_dataset_specs,
        llm_payload=final_plan_payload,
    )
    _write_plan_or_replace_with_aborted(plan_path, plan)

    if status in {"done", "max_rounds_reached"}:
        manifest = _build_manifest(plan_id, manifest_entries)
        validate_named_schema("dataset_manifest", manifest)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if status == "aborted":
        raise RefinerError(error or "refiner aborted")
    return plan


def _call_refiner_round(
    *,
    runner: CommandRunner,
    claude_path: str,
    model: str,
    max_budget: str,
    grilling_session: dict[str, Any],
    market_brief: dict[str, Any],
    baseline_md: str,
    rounds: list[dict[str, Any]],
    allowed_domains: list[str],
    force_done: bool,
    round_timeout_seconds: int,
) -> _RoundCall:
    system_prompt = _system_prompt(allowed_domains, baseline_md)
    user_prompt = _user_prompt(
        grilling_session, market_brief, rounds, force_done=force_done
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
        raise RefinerError(
            f"claude CLI timed out after {round_timeout_seconds}s during refiner round"
        ) from exc
    if completed.returncode not in (0, None):
        raise RefinerError(
            f"claude CLI exited with code {completed.returncode}: "
            f"{(completed.stderr or '').strip()[:200]}"
        )
    raw = completed.stdout or ""
    try:
        cli_result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RefinerError(f"claude CLI did not return JSON: {raw[:200]!r}") from exc
    if not isinstance(cli_result, dict) or cli_result.get("type") != "result":
        raise RefinerError(f"claude CLI returned unexpected payload: {raw[:200]!r}")
    if cli_result.get("is_error"):
        raise RefinerError(
            f"claude CLI reported error subtype {cli_result.get('subtype')!r}"
        )
    inner = cli_result.get("result")
    if not isinstance(inner, str) or not inner.strip():
        raise RefinerError("claude CLI returned empty assistant text")
    action_text = _strip_fence(inner.strip())
    try:
        parsed = json.loads(action_text)
    except json.JSONDecodeError as exc:
        raise RefinerError(
            f"refiner output was not parseable JSON: {action_text[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RefinerError("refiner output must be a JSON object")
    usage = cli_result.get("usage") if isinstance(cli_result.get("usage"), dict) else {}
    return _RoundCall(
        raw_action_text=action_text,
        parsed=parsed,
        cost_usd=float(cli_result.get("total_cost_usd") or 0.0),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
    )


def _system_prompt(allowed_domains: list[str], baseline_md: str) -> str:
    base = (
        "You are a research_refiner agent. You take the grilled claim and the "
        "market_research baseline analysis and turn them into a validation-ready "
        "research plan, deciding every dataset the experiment depends on. "
        "Each turn you emit exactly one JSON object and nothing else. No "
        "markdown, no code fences, no prose around the JSON. "
        "Allowed actions: "
        '{"action":"ASK","question":"<one specific question>"} '
        '| {"action":"PROPOSE_DATASET","spec":{...dataset_spec...}} '
        '| {"action":"DONE","plan":{...full refined_research_plan payload...}}. '
        "Use PROPOSE_DATASET silently to verify candidates with the deterministic "
        "fetcher; the harness injects the materialize result into the next round, "
        "the user does not see PROPOSE_DATASET. Use ASK only when you need user "
        "input that the transcript cannot provide. Use DONE only when every "
        "dataset_spec you intend to keep has been verified by a successful "
        "PROPOSE_DATASET in this session (or you explicitly route it under "
        'unresolved_dataset_specs). DONE.plan must include: claim_under_test, '
        "mandatory_baselines (>=1), success_criteria (>=1), disproof_conditions (>=1), "
        "validation_procedure {primary_metric{name,operator,threshold}, splits, "
        "statistical_test{name,alpha}, n_seeds, decision_rule}, dataset_specs (each "
        'has id, type in [raw_data|benchmark|model_weights|factor_set|synthetic|custom], '
        "role, plus type-specific fields), sota_reconciliations (one entry per "
        "conflict you surfaced via ASK), acknowledged_limitations (explicit user "
        "concessions). SOTA reconciliation paths: narrow_claim | change_baseline | "
        "acknowledge_limitation. Read the baseline analysis below carefully and "
        "surface every conflict against the user's claim before DONE."
    )
    if allowed_domains:
        base += (
            " The grilled domain belongs to "
            f"{json.dumps(allowed_domains)}; do not invent a new domain name."
        )
    base += "\n\n## Baseline analysis (from market_research)\n" + (baseline_md or "(empty)")
    return base


def _user_prompt(
    grilling_session: dict[str, Any],
    market_brief: dict[str, Any],
    rounds: list[dict[str, Any]],
    *,
    force_done: bool,
) -> str:
    extracted = grilling_session["extracted"]
    lines = [
        "## Grilled Claim Inputs",
        f"- claim_under_test: {extracted['claim_under_test']}",
        f"- domain: {extracted['domain']}",
        f"- node_type: {extracted['node_type']}",
        "- mandatory_baselines:",
    ]
    for b in extracted.get("mandatory_baselines") or []:
        lines.append(f"  - {b}")
    lines.append("- success_criteria:")
    for s in extracted.get("success_criteria") or []:
        lines.append(f"  - {s}")
    lines.append("- disproof_conditions:")
    for d in extracted.get("disproof_conditions") or []:
        lines.append(f"  - {d}")
    if extracted.get("goal_facets"):
        lines.append("- goal_facets: " + ", ".join(extracted["goal_facets"]))

    lines.extend(["", "## Market Research Inputs"])
    lines.append(
        f"- baseline_dossier_id: {market_brief.get('baseline_dossier_id', 'n/a')}"
    )
    lines.append(
        f"- baseline_analysis_source: {market_brief.get('baseline_analysis_source', 'n/a')}"
    )
    papers = market_brief.get("papers") or []
    if papers:
        lines.append(f"- retrieved papers: {len(papers)}")
        for p in papers[:5]:
            lines.append(
                f"  - {p.get('title', 'untitled')} ({p.get('url', '')})"
            )
    lines.extend(["", "## Transcript so far"])
    if not rounds:
        lines.append("(no rounds yet)")
    for entry in rounds:
        action = entry["action"]
        if action == "ASK":
            lines.append(f"Round {entry['round_index'] + 1} ASK: {entry['payload']['question']}")
            lines.append(f"Round {entry['round_index'] + 1} USER: {entry.get('user_response', '')}")
        elif action == "PROPOSE_DATASET":
            spec = entry["payload"]["spec"]
            result = entry["materialize_result"] or {}
            outcome = result.get("status", "unknown")
            err = (result.get("error") or "")[:200]
            extra = f"path={result.get('materialized_path')}" if outcome == "ok" else f"error={err}"
            lines.append(
                f"Round {entry['round_index'] + 1} PROPOSE_DATASET "
                f"id={spec.get('id')} type={spec.get('type')} → {outcome} ({extra})"
            )
        elif action == "DONE":
            lines.append(f"Round {entry['round_index'] + 1} DONE")
    lines.append("")
    if force_done:
        lines.append(
            "## Force-DONE\n"
            "Max rounds reached. Do not ASK or PROPOSE_DATASET. Output exactly one "
            "DONE action with the best plan you can assemble from what is already "
            "verified above. Any specs you could not verify must go under "
            "unresolved_dataset_specs, not dataset_specs."
        )
    else:
        lines.append(
            "## Your next turn\n"
            "Output exactly one JSON action object (ASK | PROPOSE_DATASET | DONE)."
        )
    return "\n".join(lines)


def _placeholder_validation_procedure() -> dict[str, Any]:
    return {
        "primary_metric": {
            "name": "pending_refiner",
            "operator": "greater_than",
            "threshold": 0.0,
        },
        "splits": {},
        "statistical_test": {"name": "pending_refiner", "alpha": 0.05},
        "n_seeds": 1,
        "decision_rule": "pending_refiner",
    }


def _assemble_plan(
    *,
    base: dict[str, Any],
    status: str,
    error: str | None,
    rounds: list[dict[str, Any]],
    usage: dict[str, Any],
    verified_dataset_specs: list[dict[str, Any]],
    llm_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    plan = {**base, "status": status, "error": error, "rounds": rounds, "usage_estimate": usage}
    if llm_payload is None:
        plan["dataset_specs"] = verified_dataset_specs
        return plan

    plan["claim_under_test"] = (
        str(llm_payload.get("claim_under_test") or base["claim_under_test"])
    )
    plan["mandatory_baselines"] = _coerce_list(
        llm_payload.get("mandatory_baselines"), base["mandatory_baselines"]
    )
    plan["success_criteria"] = _coerce_list(
        llm_payload.get("success_criteria"), base["success_criteria"]
    )
    plan["disproof_conditions"] = _coerce_list(
        llm_payload.get("disproof_conditions"), base["disproof_conditions"]
    )
    vp = llm_payload.get("validation_procedure")
    if isinstance(vp, dict):
        plan["validation_procedure"] = _normalize_validation_procedure(vp)
    declared_specs = [
        _normalize_dataset_spec(spec)
        for spec in (llm_payload.get("dataset_specs") or [])
        if isinstance(spec, dict)
    ]
    verified_ids = {spec["id"] for spec in verified_dataset_specs}
    plan["dataset_specs"] = [
        spec for spec in declared_specs if spec.get("id") in verified_ids
    ] or verified_dataset_specs
    plan["unresolved_dataset_specs"] = [
        spec for spec in declared_specs if spec.get("id") not in verified_ids
    ] + [
        _normalize_dataset_spec(spec)
        for spec in (llm_payload.get("unresolved_dataset_specs") or [])
        if isinstance(spec, dict)
    ]
    plan["sota_reconciliations"] = llm_payload.get("sota_reconciliations") or []
    plan["acknowledged_limitations"] = list(
        llm_payload.get("acknowledged_limitations") or []
    )
    return plan


def _normalize_validation_procedure(vp: dict[str, Any]) -> dict[str, Any]:
    metric = vp.get("primary_metric") if isinstance(vp.get("primary_metric"), dict) else {}
    splits_raw = vp.get("splits")
    if isinstance(splits_raw, dict):
        splits = {str(k): str(v) for k, v in splits_raw.items()}
    elif isinstance(splits_raw, list):
        splits = {f"split_{i}": str(item) for i, item in enumerate(splits_raw)}
    else:
        splits = {}
    stat = vp.get("statistical_test") if isinstance(vp.get("statistical_test"), dict) else {}
    try:
        threshold = float(metric.get("threshold") or 0.0)
    except (TypeError, ValueError):
        threshold = 0.0
    try:
        alpha = float(stat.get("alpha") if stat.get("alpha") is not None else 0.05)
    except (TypeError, ValueError):
        alpha = 0.05
    if alpha < 0 or alpha > 1:
        alpha = 0.05
    try:
        n_seeds = max(1, int(vp.get("n_seeds") or 1))
    except (TypeError, ValueError):
        n_seeds = 1
    operator = str(metric.get("operator") or "greater_than")
    if operator not in {"greater_than", "greater_equal", "less_than", "less_equal"}:
        operator = "greater_than"
    return {
        "primary_metric": {
            "name": str(metric.get("name") or "pending_refiner"),
            "operator": operator,
            "threshold": threshold,
        },
        "splits": splits,
        "statistical_test": {
            "name": str(stat.get("name") or "pending_refiner"),
            "alpha": alpha,
        },
        "n_seeds": n_seeds,
        "decision_rule": str(vp.get("decision_rule") or "pending_refiner"),
    }


_ALLOWED_DATASET_TYPES = {
    "raw_data",
    "benchmark",
    "model_weights",
    "factor_set",
    "synthetic",
    "custom",
}
_ALLOWED_DATASET_ROLES = {
    "training",
    "evaluation",
    "baseline_reference",
    "baseline_implementation",
    "other",
}


def _normalize_dataset_spec(spec: dict[str, Any]) -> dict[str, Any]:
    out = dict(spec)
    spec_type = str(out.get("type") or "custom")
    out["type"] = spec_type if spec_type in _ALLOWED_DATASET_TYPES else "custom"
    role = str(out.get("role") or "other")
    out["role"] = role if role in _ALLOWED_DATASET_ROLES else "other"
    if not out.get("id"):
        out["id"] = "ds_" + uuid.uuid4().hex[:8]
    return out


def _coerce_list(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        return list(fallback)
    return [str(item) for item in value if str(item).strip()]


def _build_manifest(plan_id: str, entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "dataset_manifest",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_plan_id": plan_id,
        "datasets": entries,
    }


def _compact_payload_for_audit(payload: dict[str, Any]) -> dict[str, Any]:
    # Avoid embedding the full plan twice (we already store it at the top level).
    keys = ["claim_under_test", "validation_procedure", "dataset_specs"]
    return {k: payload[k] for k in keys if k in payload}


def _load_baseline_md(market_brief: dict[str, Any]) -> str:
    path = market_brief.get("baseline_analysis_md_path")
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > MAX_BASELINE_MD_CHARS:
        return text[:MAX_BASELINE_MD_CHARS] + "\n\n... (truncated)"
    return text


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def _accumulate(usage: dict[str, Any], call: _RoundCall) -> None:
    usage["total_cost_usd"] = float(usage.get("total_cost_usd") or 0.0) + call.cost_usd
    usage["total_input_tokens"] = int(usage.get("total_input_tokens") or 0) + call.input_tokens
    usage["total_output_tokens"] = int(usage.get("total_output_tokens") or 0) + call.output_tokens


def _write_plan_or_replace_with_aborted(path: Path, plan: dict[str, Any]) -> None:
    try:
        validate_named_schema("refined_research_plan", plan)
    except SchemaValidationError as exc:
        plan = {
            **plan,
            "status": "aborted",
            "error": f"plan failed schema validation: {exc}",
        }
        validate_named_schema("refined_research_plan", plan)
    path.write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _abort(
    path: Path,
    base: dict[str, Any],
    *,
    status: str,
    error: str,
) -> dict[str, Any]:
    aborted = {**base, "status": status, "error": error}
    validate_named_schema("refined_research_plan", aborted)
    path.write_text(
        json.dumps(aborted, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return aborted


def _stdin_input_provider(question: str) -> str:
    print(f"\n[refiner] Q: {question}\n")
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
