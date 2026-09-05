"""Blind sequential research front-half: the connector orchestrator.

Ties the per-step engine into the resample-to-quota loop (ADR 0012):

    abstraction (P-visible)
      ⟂ firewall: below here P-blind, abstraction text only ⟂
    → for each randomly-sampled field (uniform, no replacement):
          reading (P-blind) → prune-1 (P-blind)
          if prune-1 passes:  per-reading market (P-blind) → reduction (P-aware)
              if reduction yields a well-formed claim_contract: keep it
      stop when `quota` claims are kept, the field namespace is exhausted, or
      `max_fields_tried` is hit (a compute cap — recorded in `stopped_reason`,
      never silently swallowed).

Emits a schema-validated ``connector_session.json``. Kept claim contracts are
intake research context. Production freezes the problem and accepted success
bar into one GoalContract and does not seed parallel roots from them.

Reliability is the production gate's job, not this loop's: prune-1 / reduction
are best-effort and may pass garbage, absorbed downstream. A single field's LLM
error is logged on that attempt and skipped; only an abstraction failure aborts.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from research_harness.config import resolve_agent_model
from research_harness.connector.abstraction import generate_abstraction
from research_harness.connector.codex_call import CommandRunner, ConnectorLLMError
from research_harness.connector.far_method_market import research_far_method
from research_harness.connector.field_sampler import field_permutation
from research_harness.connector.prune1 import prune1_check
from research_harness.connector.reading import generate_reading
from research_harness.connector.reduction import reduce_to_claim
from research_harness.agents.market_research import HttpFetcher
from research_harness.schemas.validator import validate_named_schema
from research_harness.settings_scoped import resolve_for_thread, thread_id_from_run_dir

BILLING_ACK_ENV = "RESEARCH_HARNESS_ALLOW_CODEX_LIVE"
BILLING_ACK_VALUE = "subscription_ack"
EXECUTION_ACK_ENV = "RESEARCH_HARNESS_EXECUTE_CODEX_LIVE"
EXECUTION_ACK_VALUE = "live_smoke_ack"

DEFAULT_QUOTA = 6
DEFAULT_MAX_FIELDS_TRIED = 40
DEFAULT_MAX_REGEN = 2


@dataclass
class DomainConnectorOutcome:
    session: dict[str, Any]
    claims: list[dict[str, Any]] = dc_field(default_factory=list)


def _run_baseline_research(
    repo_root: Path,
    grilling_session: dict[str, Any],
    *,
    run_dir: Path,
    http_fetcher: HttpFetcher | None,
):
    from research_harness.agents.market_research import run_market_research

    return run_market_research(
        repo_root,
        grilling_session,
        run_dir=run_dir,
        write_dossier_to_memory=True,
        http_fetcher=http_fetcher,
        pdf_fetcher=http_fetcher,
    )


def _billing_ack_ok(billing_ack: bool | None) -> bool:
    if billing_ack is not None:
        return bool(billing_ack)
    return os.environ.get(BILLING_ACK_ENV) == BILLING_ACK_VALUE


def _execution_ack_ok(execution_ack: bool | None) -> bool:
    if execution_ack is not None:
        return bool(execution_ack)
    return os.environ.get(EXECUTION_ACK_ENV) == EXECUTION_ACK_VALUE


def _seed_from_session(session_id: str) -> int:
    """Deterministic field-draw seed from the grilling session id — same thread
    re-runs draw the same field order (reproducible), different threads differ."""
    return int.from_bytes(hashlib.sha256(session_id.encode("utf-8")).digest()[:8], "big")


def _resolve_knob(settings: Any, key: str, default: int) -> int:
    block = settings.get("domain_connector", {}) if hasattr(settings, "get") else {}
    if isinstance(block, dict):
        value = block.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
    return default


def _perspective_packet(
    field: dict[str, Any],
    reading: dict[str, Any],
    correspondence: list[dict[str, Any]],
    far_method: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the canonical P-blind source record for one connector attempt."""
    content = {
        "field": field,
        "reading": {
            "reading": reading["reading"],
            "field_mechanism": reading["field_mechanism"],
            "predicted_behavior": reading["predicted_behavior"],
        },
        "correspondence": correspondence,
        "far_method": (
            {
                "query": far_method["query"],
                "papers": far_method["papers"],
                "warnings": far_method["warnings"],
            }
            if far_method is not None
            else None
        ),
    }
    digest = hashlib.sha256(
        json.dumps(
            content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return {
        "packet_id": f"perspective_{digest}",
        "digest": f"sha256:{digest}",
        **content,
    }


def run_domain_connector(
    repo_root: Path,
    grilling_session: dict[str, Any],
    *,
    run_dir: Path | None = None,
    billing_ack: bool | None = None,
    execution_ack: bool | None = None,
    codex_path: str | None = None,
    command_runner: CommandRunner | None = None,
    http_fetcher: HttpFetcher | None = None,
    quota: int | None = None,
    max_fields_tried: int | None = None,
    max_regen: int = DEFAULT_MAX_REGEN,
    field_seed: int | None = None,
    timeout_seconds: int = 180,
    event_emitter: Any = None,
    baseline_research_runner: Callable[..., Any] | None = None,
) -> DomainConnectorOutcome:
    """Run the connector front-half and emit a connector_session.json.

    Returns a :class:`DomainConnectorOutcome` whose ``claims`` are the kept
    P-claim contracts used as intake research context.
    """
    import shutil
    import subprocess

    validate_named_schema("grilling_session", grilling_session)
    repo_root = repo_root.resolve()
    grilling_id = grilling_session["session_id"]
    session_id = "conn_" + uuid.uuid4().hex[:12]
    run_dir = (
        run_dir or repo_root / "runs" / "connector" / grilling_id
    ).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    session_path = run_dir / "connector_session.json"

    settings = resolve_for_thread(repo_root, thread_id_from_run_dir(run_dir))
    model = resolve_agent_model(settings, "domain_connector_agent")
    quota = quota if quota is not None else _resolve_knob(settings, "quota", DEFAULT_QUOTA)
    max_fields_tried = (
        max_fields_tried
        if max_fields_tried is not None
        else _resolve_knob(settings, "max_fields_tried", DEFAULT_MAX_FIELDS_TRIED)
    )
    seed = field_seed if field_seed is not None else _seed_from_session(grilling_id)

    base_session: dict[str, Any] = {
        "session_id": session_id,
        "type": "connector_session",
        "status": "in_progress",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "grilling_session_id": grilling_id,
        "model": model,
        "field_seed": seed,
        "quota": quota,
        "max_fields_tried": max_fields_tried,
        "fields_tried": 0,
        "quota_met": False,
        "stopped_reason": "not_started",
        "abstraction": None,
        "attempts": [],
        "claims": [],
        "baseline_research": None,
        "usage_estimate": {
            "llm_calls": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        },
        "session_path": str(session_path),
        "error": None,
    }

    def _finish(session: dict[str, Any]) -> DomainConnectorOutcome:
        validate_named_schema("connector_session", session)
        session_path.write_text(
            json.dumps(session, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return DomainConnectorOutcome(session=session, claims=list(session["claims"]))

    if not _billing_ack_ok(billing_ack):
        return _finish({**base_session, "status": "blocked_by_gate",
                        "error": f"set {BILLING_ACK_ENV}={BILLING_ACK_VALUE} to enable the connector"})
    if not _execution_ack_ok(execution_ack):
        return _finish({**base_session, "status": "blocked_by_execution_ack",
                        "error": f"set {EXECUTION_ACK_ENV}={EXECUTION_ACK_VALUE} to invoke Codex"})

    runner = command_runner or subprocess.run
    detected_codex = codex_path or shutil.which("codex") or "codex"
    usage = dict(base_session["usage_estimate"])
    # Optional live-progress sink (the frontend bridges this to the SSE stream
    # so the operator watches the connector work step by step). No-op by default.
    emit = event_emitter if callable(event_emitter) else (lambda _e: None)

    def _add_usage(u: dict[str, Any]) -> None:
        usage["llm_calls"] += 1
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            usage[key] += int(u.get(key) or 0)

    # --- abstraction (load-bearing; failure aborts) ---
    emit({"type": "abstraction_start"})
    try:
        abstraction = generate_abstraction(
            grilling_session, model=model,
            codex_path=detected_codex, runner=runner, max_regen=max_regen,
            timeout_seconds=timeout_seconds,
        )
    except (ConnectorLLMError, ValueError) as exc:
        return _finish({**base_session, "status": "aborted",
                        "error": f"abstraction step failed: {exc}"})
    for _ in range(abstraction["regen_attempts"]):
        usage["llm_calls"] += 1
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        usage[key] += int(abstraction["usage"].get(key) or 0)
    abstraction_text = abstraction["abstraction"]
    abstraction_record = {
        "abstraction": abstraction_text,
        "scanned_domain_terms": abstraction["scanned_domain_terms"],
        "residual_leaked_terms": abstraction["residual_leaked_terms"],
        "firewall_clean": abstraction["firewall_clean"],
        "regen_attempts": abstraction["regen_attempts"],
    }
    emit({"type": "abstraction_done", "abstraction": abstraction_text,
          "firewall_clean": abstraction["firewall_clean"],
          "regen_attempts": abstraction["regen_attempts"], "quota": quota})
    if not abstraction["firewall_clean"]:
        return _finish({
            **base_session,
            "status": "aborted",
            "abstraction": abstraction_record,
            "usage_estimate": usage,
            "error": "abstraction firewall rejected residual domain terms: "
                     + ", ".join(abstraction["residual_leaked_terms"]),
        })

    # --- resample-to-quota loop (P-blind below the firewall) ---
    perm = field_permutation(seed=seed)
    attempts: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    fields_tried = 0
    stopped_reason = "namespace_exhausted"

    for fld in perm:
        if len(claims) >= quota:
            stopped_reason = "quota_met"
            break
        if fields_tried >= max_fields_tried:
            stopped_reason = "max_fields_tried"
            break
        fields_tried += 1
        field_meta = {"code": fld.get("code"), "name": fld.get("name"), "archive": fld.get("archive")}
        attempt: dict[str, Any] = {
            "field": field_meta,
            "prune1_passed": False,
            "num_pairs": 0,
            "reduced": False,
            "perspective_packet": None,
            "error": None,
        }
        emit({"type": "field_start", "index": fields_tried, **field_meta})
        try:
            reading = generate_reading(
                abstraction_text, fld, model=model,
                codex_path=detected_codex, runner=runner, timeout_seconds=timeout_seconds,
            )
            _add_usage(reading["usage"])
            emit({"type": "reading_done", "code": field_meta["code"],
                  "field_mechanism": reading.get("field_mechanism"),
                  "predicted_behavior": reading.get("predicted_behavior")})
            p1 = prune1_check(
                abstraction_text, reading, model=model,
                codex_path=detected_codex, runner=runner, timeout_seconds=timeout_seconds,
            )
            _add_usage(p1["usage"])
            attempt["prune1_passed"] = p1["passed"]
            attempt["num_pairs"] = p1["num_pairs"]
            attempt["perspective_packet"] = _perspective_packet(
                field_meta, reading, p1["correspondence"], None
            )
            emit({"type": "prune1_done", "code": field_meta["code"],
                  "passed": p1["passed"], "num_pairs": p1["num_pairs"]})
            if p1["passed"]:
                material = research_far_method(
                    fld, reading["field_mechanism"], http_fetcher=http_fetcher
                )
                emit({"type": "market_done", "code": field_meta["code"],
                      "num_papers": material["num_papers"]})
                packet = _perspective_packet(
                    field_meta, reading, p1["correspondence"], material
                )
                attempt["perspective_packet"] = packet
                if not material["papers"]:
                    attempts.append(attempt)
                    continue
                red = reduce_to_claim(
                    grilling_session, reading, p1, method_research=material,
                    model=model, codex_path=detected_codex,
                    runner=runner, timeout_seconds=timeout_seconds,
                )
                _add_usage(red["usage"])
                attempt["reduced"] = red["reduced"]
                emit({"type": "reduction_done", "code": field_meta["code"],
                      "reduced": red["reduced"]})
                if red["reduced"]:
                    claims.append({
                        "field": red["field"],
                        "claim_contract": red["claim_contract"],
                        "far_ness_note": red["far_ness_note"],
                        "method_num_papers": material["num_papers"],
                        "perspective_packet_id": packet["packet_id"],
                        "perspective_packet_digest": packet["digest"],
                    })
                    emit({"type": "claim_kept", "code": field_meta["code"],
                          "kept": len(claims), "quota": quota,
                          "claim_under_test": red["claim_contract"]["claim_under_test"]})
        except (ConnectorLLMError, ValueError) as exc:
            # Best-effort: one bad field is logged + skipped, never fatal.
            attempt["error"] = str(exc)[:300]
            emit({"type": "field_error", "code": field_meta["code"], "error": str(exc)[:200]})
        attempts.append(attempt)
    else:
        # Loop fell through without break: namespace exhausted (or quota/cap
        # coincided with the last field). Re-derive the honest reason.
        if len(claims) >= quota:
            stopped_reason = "quota_met"
        elif fields_tried >= max_fields_tried:
            stopped_reason = "max_fields_tried"
        else:
            stopped_reason = "namespace_exhausted"

    thread_id = thread_id_from_run_dir(run_dir)
    market_dir = (
        repo_root / "runs" / "threads" / thread_id / "market"
        if thread_id is not None
        else run_dir / "baseline_market"
    )
    emit({"type": "baseline_research_start"})
    try:
        baseline_outcome = (baseline_research_runner or _run_baseline_research)(
            repo_root,
            grilling_session,
            run_dir=market_dir,
            http_fetcher=http_fetcher,
        )
        brief = baseline_outcome.brief
        baseline_research = {
            "status": brief["status"],
            "brief_path": brief["brief_path"],
            "baseline_dossier_id": brief["baseline_dossier_id"],
            "papers_found": int((brief.get("usage") or {}).get("papers_found", 0)),
        }
        emit({"type": "baseline_research_done", **baseline_research})
        if brief["status"] == "failed":
            raise ValueError("baseline research retrieved no admissible papers")
    except Exception as exc:  # noqa: BLE001
        session = {
            **base_session,
            "status": "aborted",
            "fields_tried": fields_tried,
            "quota_met": len(claims) >= quota,
            "stopped_reason": stopped_reason,
            "abstraction": abstraction_record,
            "attempts": attempts,
            "claims": claims,
            "usage_estimate": usage,
            "error": f"baseline research failed: {exc}",
        }
        emit({"type": "baseline_research_failed", "error": str(exc)})
        return _finish(session)

    status = "completed" if claims else "completed_no_claims"
    session = {
        **base_session,
        "status": status,
        "fields_tried": fields_tried,
        "quota_met": len(claims) >= quota,
        "stopped_reason": stopped_reason,
        "abstraction": abstraction_record,
        "attempts": attempts,
        "claims": claims,
        "baseline_research": baseline_research,
        "usage_estimate": usage,
        "error": None,
    }
    return _finish(session)
