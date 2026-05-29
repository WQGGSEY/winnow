"""Thread supervisor — true hands-free e2e automation.

Watches a single thread, spawns `claude` CLI subprocesses via pty when the
MCP server has been idle past a threshold, feeds the resume prompt
deterministically, and only terminates when the thread reaches a terminal
outcome (paper accept or honest_failure).

This is the missing piece between the MCP server (which exposes tools but
needs Claude Code interactive to drive them) and operator hands-free
operation. The operator runs:

    python -m research_harness.thread_supervisor watch <thread_id>

once. The supervisor handles every subsequent session respawn until the
thread terminates. No API tokens used — the spawned `claude` subprocess
runs under the operator's subscription pool.

Design constraints:
- Subscription pool only. No Anthropic API path, ever.
- pty stdlib only — zero new dependencies.
- Single supervisor per thread (lock file enforced).
- Survives subprocess crashes, claude CLI session limits, MCP daemon
  restarts. The only fatal state is operator SIGINT or thread terminal.
"""

from __future__ import annotations

import argparse
import atexit
import errno
import json
import os
import pty
import re
import select
import signal
import sys
import threading
import time
from pathlib import Path

LOG = sys.stderr


# --- thresholds (configurable via CLI flags) ----------------------------- #

DEFAULT_MAX_IDLE_SECONDS = 600.0   # 10 min — claude session typically idle
                                   #          between MCP calls during heavy
                                   #          experiments; we want to detect
                                   #          true death, not a long step.
DEFAULT_POLL_SECONDS = 30.0        # check every 30s
DEFAULT_MILESTONE_CYCLE = 10       # informational logging milestone (PR8:
                                   #          no longer a termination cause —
                                   #          supervisor never quits on count)
DEFAULT_CLAUDE_BOOT_DELAY = 3.0    # seconds to wait after fork before
                                   #          writing the resume prompt
DEFAULT_CLAUDE_MODEL = "claude-opus-4-7"
DEFAULT_CLAUDE_STALL_TIMEOUT = 600.0  # kill a claude cycle that emits NO output
                                      #   for this long (a hang — e.g. model/API
                                      #   stall after a tool call). Distinct from
                                      #   the fast-fail EXIT detector, which never
                                      #   fires on a hang because the process
                                      #   never exits. A healthy cycle streams
                                      #   tool_use/tool_result lines continuously.

# PR8: rate-limit backoff. When claude subprocess fails fast (exit < 30s)
# we treat it as a likely rate-limit and back off exponentially. Backoff
# resets when a cycle runs healthily for >= RATE_LIMIT_HEALTHY_CYCLE_SECONDS.
DEFAULT_RATE_LIMIT_BACKOFF_INITIAL = 60.0
DEFAULT_RATE_LIMIT_BACKOFF_MAX = 1800.0
RATE_LIMIT_FAST_FAIL_SECONDS = 30.0
RATE_LIMIT_HEALTHY_CYCLE_SECONDS = 300.0


# --- terminal-state detection ------------------------------------------- #


def _thread_dir(repo: Path, tid: str) -> Path:
    return repo / "runs" / "threads" / tid


def resolve_supervisor_model(repo: Path, tid: str) -> str:
    """Model the production supervisor drives `claude` with.

    Implements the documented contract (settings.json _model_comment):
    'thread.json.mcp_model overrides default_model'. Resolution order:
      1. thread.json.mcp_model        — per-thread frontend selection
      2. runtime.llm_orchestrator.mcp.default_model  — operator/project default
      3. DEFAULT_CLAUDE_MODEL          — last-resort fallback

    Previously the supervisor always spawned DEFAULT_CLAUDE_MODEL, so the
    production phase ignored every model setting the operator configured.
    """
    # 1. per-thread override.
    try:
        tj = json.loads((_thread_dir(repo, tid) / "thread.json").read_text(encoding="utf-8"))
        m = tj.get("mcp_model")
        if isinstance(m, str) and m.strip():
            return m.strip()
    except (OSError, json.JSONDecodeError):
        pass
    # 2. operator/project default from settings.json.
    try:
        s = json.loads((repo / "settings.json").read_text(encoding="utf-8"))
        m = (
            s.get("runtime", {})
            .get("llm_orchestrator", {})
            .get("mcp", {})
            .get("default_model")
        )
        if isinstance(m, str) and m.strip():
            return m.strip()
    except (OSError, json.JSONDecodeError):
        pass
    # 3. fallback.
    return DEFAULT_CLAUDE_MODEL


def is_terminal(repo: Path, tid: str) -> tuple[bool, str | None]:
    """Terminate on either honest publication outcome:

    - the dual gate: AC decision in {accept, revise} AND a paper rendered
      AND user_goal_attestation.achieved=true (now backed by an external
      falsifier — ADR 0006) → 'accept_with_goal_achieved'.
    - ADR 0006 honest ceiling: attested_status=='unverified_screen' with the
      honest-failure paper rendered → 'accept_with_unverified_screen'. This
      is a recorded terminal, NOT an infinite retry — the air-gapped run
      genuinely cannot register a falsifier, so the screen stands.

    Everything else (not_achieved honest_failure, max_cycles, rate limits)
    is a state to retreat from and retry.

    Returns (is_terminal, 'accept_with_goal_achieved' |
    'accept_with_unverified_screen' | None).
    """
    pdir = _thread_dir(repo, tid) / "production"
    summary_path = pdir / "production_run_summary.json"
    if not summary_path.exists():
        return False, None
    try:
        s = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, None

    rebuttal = s.get("rebuttal_summary") or {}
    rendered = (s.get("publication_dispatch") or {}).get("rendered_artifacts")

    # Resolve the attestation once (summary may embed it or it's on disk).
    attestation = rebuttal.get("user_goal_attestation")
    if not attestation:
        attestation_path = pdir / "rebuttal" / "user_goal_attestation.json"
        if attestation_path.exists():
            try:
                attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                attestation = None
    attestation = attestation or {}

    # ADR 0006/0008: an honest screen is a first-class terminal once the honest
    # paper exists. construct_valid_screen (Axis 1: funded adversary survived)
    # and unverified_screen (internally_valid floor) both terminate — distinct
    # outcome labels. Checked BEFORE the honest_failure retreat so they are not
    # swept into the retry loop.
    _screen_terminal = {
        "construct_valid_screen": "accept_with_construct_valid",
        "unverified_screen": "accept_with_unverified_screen",
    }
    _astatus = attestation.get("attested_status")
    if _astatus in _screen_terminal and (rendered or s.get("outcome") == "honest_failure"):
        return True, _screen_terminal[_astatus]

    # honest_failure (not_achieved) does not terminate — supervisor retries.
    if s.get("outcome") == "honest_failure":
        return False, None

    ac_decision = (rebuttal.get("ac_decision") or {}).get("decision")
    if ac_decision not in {"accept", "revise"} or not rendered:
        return False, None

    # Second half of the dual-gate: achieved=true (ADR 0006 ensures this was
    # only attestable with a passing external falsifier).
    if not attestation.get("achieved"):
        return False, None

    return True, "accept_with_goal_achieved"


def collect_needed_resources(repo: Path, tid: str) -> list[dict[str, object]]:
    """PR8: scan the thread state for unmet resource requirements that are
    blocking publication. Combines:
      - user_goal_attestation.required_additional_research (when achieved=false)
      - alternative_root_proposal.alternatives' implicit resource needs
      - feasibility_envelope gaps (e.g., target=deployment but no real adapter)
    The supervisor logs this to needed_resources.yaml so the operator can
    see what's blocking and add the missing pieces.
    """
    pdir = _thread_dir(repo, tid) / "production"
    needs: list[dict[str, object]] = []

    # Source 1: latest attestation's required_additional_research.
    att_path = pdir / "rebuttal" / "user_goal_attestation.json"
    if att_path.exists():
        try:
            a = json.loads(att_path.read_text(encoding="utf-8"))
            if not a.get("achieved"):
                for r in a.get("required_additional_research") or []:
                    needs.append({
                        "type": "additional_research",
                        "axis": r.get("axis"),
                        "experiment": r.get("experiment", "")[:300],
                        "rationale": r.get("rationale", "")[:200],
                        "source": "user_goal_attestation",
                    })
        except (OSError, json.JSONDecodeError):
            pass

    # Source 2: feasibility envelope gaps. If target_scope=deployment and
    # no real_adapter is declared, that's a structural blocker the operator
    # must address by registering an adapter.
    env_path = pdir / "feasibility_envelope.json"
    if env_path.exists():
        try:
            env = json.loads(env_path.read_text(encoding="utf-8"))
            target = (env.get("operator_intent") or {}).get("target_deploy_grade_scope")
            real = [s for s in env.get("data_sources_available", []) if s.get("kind") == "real_adapter"]
            if target == "deployment" and not real:
                needs.append({
                    "type": "data_adapter",
                    "spec": "register a real_adapter under settings.json.data_adapters.registered",
                    "rationale": (
                        "operator_intent.target_deploy_grade_scope='deployment' but "
                        "no real_adapter is in the envelope. Without one, deployment-"
                        "scope claims are blocked."
                    ),
                    "source": "feasibility_envelope",
                })
        except (OSError, json.JSONDecodeError):
            pass

    return needs


def _format_needed_resources_yaml(needs: list[dict[str, object]]) -> str:
    """Hand-rolled YAML emitter compatible with the harness's
    parse_simple_yaml. Mirrors mcp_server._format_failure_index style."""
    if not needs:
        return "needs: []\n"
    lines = ["needs:"]
    for n in needs:
        lines.append(f"  - type: \"{_q(n.get('type', ''))}\"")
        for k in ("axis", "spec", "experiment", "rationale", "source"):
            v = n.get(k)
            if v:
                lines.append(f"    {k}: \"{_q(str(v))}\"")
    return "\n".join(lines) + "\n"


def _q(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ")


def update_needed_resources_file(repo: Path, tid: str) -> list[dict[str, object]]:
    """Write the current needed_resources snapshot to disk. Returns the
    list so the supervisor can include it in the next resume prompt."""
    needs = collect_needed_resources(repo, tid)
    path = _thread_dir(repo, tid) / "needed_resources.yaml"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_format_needed_resources_yaml(needs), encoding="utf-8")
    except OSError:
        pass
    return needs


# --- MCP idle detection ------------------------------------------------- #


def mcp_idle_seconds(repo: Path, tid: str) -> float:
    """Return seconds since the most recent file mtime under the thread's
    production directory. Used as a proxy for MCP activity — any
    handle_submit_*, handle_render_*, or handle_revise_root_after_reject
    call writes at least one file.

    Returns inf when the production dir is empty or missing.
    """
    pdir = _thread_dir(repo, tid) / "production"
    if not pdir.exists():
        return float("inf")
    most_recent = 0.0
    for sub in pdir.rglob("*"):
        if sub.is_file():
            try:
                most_recent = max(most_recent, sub.stat().st_mtime)
            except OSError:
                continue
    if most_recent == 0.0:
        return float("inf")
    return max(0.0, time.time() - most_recent)


# --- envelope auto-bootstrap (supervisor side) -------------------------- #


def bootstrap_envelope_if_missing(
    repo: Path,
    tid: str,
    *,
    target_scope: str = "directional",
) -> dict[str, object] | None:
    """Auto-construct a FeasibilityEnvelope from settings.json + thread
    market dossier and write it to the thread's production dir, if no
    envelope exists yet.

    The operator should NOT have to hand-craft this on every new thread.
    The supervisor is the operator's stand-in: it reads what the harness
    actually has (registered data adapters, available oracle = the
    subscription Claude Code itself, compute envelope from settings) and
    builds the envelope. Returns the envelope dict that was written (or
    None if already present / failed).
    """
    env_path = _thread_dir(repo, tid) / "production" / "feasibility_envelope.json"
    if env_path.exists():
        return None  # already there; respect existing operator/Professor input.

    # Read settings.
    settings_path = repo / "settings.json"
    settings: dict[str, object] = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    data_adapters = (settings.get("data_adapters") or {}).get("registered") or []
    prod_term = settings.get("production_termination") or {}
    # Conservative default compute budget — operator can override by
    # adjusting settings.production_termination or by writing the
    # envelope themselves before launching supervisor.
    compute = {
        "max_runner_seconds_per_node": 900,
        "max_concurrent_nodes": 2,
        "max_total_node_hours": 8.0,
    }

    # data_sources_available: every registered real adapter + 'synthetic'.
    data_sources: list[dict[str, object]] = []
    for a in data_adapters:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        data_sources.append({
            "kind": "real_adapter",
            "id": a["id"],
            "scope_note": a.get("provenance", ""),
        })
    data_sources.append({"kind": "synthetic", "id": "synthetic_generator_default"})

    # Pull baseline provenance from the thread's market dossier candidate
    # (if present) so the envelope's baseline_provenance_available is real.
    baseline_prov: list[dict[str, object]] = []
    market_brief_path = _thread_dir(repo, tid) / "market" / "market_research_brief.json"
    if market_brief_path.exists():
        try:
            brief = json.loads(market_brief_path.read_text(encoding="utf-8"))
            for c in brief.get("baseline_dossier_candidates_index", []) or []:
                if not isinstance(c, dict) or not c.get("id"):
                    continue
                # Heuristic: anything cited via market_research has either
                # a filename, arxiv id, doi, or repo url.
                prov = (
                    c.get("paper_citation")
                    or c.get("arxiv_id")
                    or c.get("doi")
                    or c.get("repo_url")
                    or c.get("filename")
                    or c.get("title")
                    or ""
                )
                if prov:
                    baseline_prov.append({
                        "candidate_id": c["id"],
                        "provenance": str(prov),
                    })
        except (OSError, json.JSONDecodeError):
            pass
    if not baseline_prov:
        # Schema requires minItems=1; supply a transparent placeholder so
        # the envelope is schema-valid AND the operator can see that no
        # real baselines were grounded by market_research.
        baseline_prov.append({
            "candidate_id": "no_market_baselines_found",
            "provenance": "market_research did not produce paper-cited baseline candidates for this thread",
        })

    if target_scope not in {"deployment", "feasibility", "directional"}:
        target_scope = "directional"
    # acceptable_alternative_scopes always includes scopes weaker or equal
    # to the target — the system never silently upgrades.
    alts_order = ["deployment", "feasibility", "directional"]
    target_idx = alts_order.index(target_scope)
    acceptable = alts_order[target_idx:]

    # ADR 0006: the bootstrap NEVER invents a falsifier — it cannot author a
    # meaningful predicate or a held-out source the worker doesn't see. It
    # honestly declares kind="none", which caps the thread at
    # 'unverified_screen' (achieved=true structurally impossible). To reach
    # 'goal_achieved' the operator hand-writes a real_holdout falsifier (with
    # a predicate) before launch, or the Professor registers a
    # cross_generator_transfer falsifier via submit_feasibility_envelope.
    has_real = any(s.get("kind") == "real_adapter" for s in data_sources)
    falsifier = {"kind": "none", "registered_by": "supervisor_bootstrap"}
    max_attestable = "unverified_screen"
    bootstrap_note = (
        "Auto-bootstrapped by thread_supervisor from settings.json + "
        "market dossier. Operator can override by writing the envelope "
        "manually before launching supervisor. ADR 0006: external_falsifier "
        "defaults to kind='none' so max_attestable_status='unverified_screen' "
        "(achieved=true is refused). "
    )
    if has_real:
        bootstrap_note += (
            "A real_adapter IS registered — to reach goal_achieved, set "
            "external_falsifier.kind='real_holdout' with holdout_source_id "
            "= that adapter id and a predicate, then re-submit the envelope. "
        )
    else:
        bootstrap_note += (
            "Synthetic-only: the only air-gapped falsifier is "
            "cross_generator_transfer (held-out generator B), registered by "
            "the Professor before attestation. "
        )
    envelope = {
        "thread_id": tid,
        "data_sources_available": data_sources,
        "llm_oracles_available": [{
            "kind": "subscription_claude_code",
            "model": resolve_supervisor_model(repo, tid),
        }],
        "compute_budget": compute,
        "baseline_provenance_available": baseline_prov,
        "operator_intent": {
            "target_deploy_grade_scope": target_scope,
            "acceptable_alternative_scopes": acceptable,
        },
        "external_falsifier": falsifier,
        "max_attestable_status": max_attestable,
        "notes": bootstrap_note,
    }

    # Validate against the schema before writing. If validation fails, do
    # NOT write a bad envelope — Claude Code can then submit one itself.
    try:
        from research_harness.schemas.validator import validate_named_schema
        validate_named_schema("feasibility_envelope", envelope)
    except Exception as exc:  # noqa: BLE001
        # Surface the failure so the operator sees why the auto-bootstrap
        # didn't take.
        print(
            f"[supervisor] envelope auto-bootstrap failed schema check: {exc}",
            file=LOG,
        )
        return None

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        json.dumps(envelope, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return envelope


# --- resume prompt assembly --------------------------------------------- #


def _last_known_state_summary(repo: Path, tid: str) -> dict[str, object]:
    """Pull a few cheap facts from the thread state for the resume prompt.
    Best-effort — missing files return defaults."""
    pdir = _thread_dir(repo, tid) / "production"
    summary: dict[str, object] = {"thread_id": tid}
    state_path = pdir / "tree" / "search_state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            summary["promoted_node_ids"] = state.get("promoted_node_ids") or []
            summary["pruned_node_ids"] = state.get("pruned_node_ids") or []
            ready_or_running = [
                n["id"] for n in state.get("nodes", [])
                if n.get("status") in {
                    "ready", "running", "completed_worker_report",
                    "critic_reviewed", "orchestrator_reduced",
                }
            ]
            summary["mid_state_nodes"] = ready_or_running
            summary["search_state_status"] = state.get("status")
        except (OSError, json.JSONDecodeError):
            pass
    ac_path = pdir / "rebuttal" / "ac_decision.json"
    if ac_path.exists():
        try:
            ac = json.loads(ac_path.read_text(encoding="utf-8"))
            summary["last_ac_decision"] = ac.get("decision")
        except (OSError, json.JSONDecodeError):
            pass
    archived = sorted(p.name for p in _thread_dir(repo, tid).glob("production.attempt_*"))
    summary["archived_attempts"] = len(archived)
    return summary


def build_resume_prompt(repo: Path, tid: str, cycle: int) -> str:
    state = _last_known_state_summary(repo, tid)
    mid = state.get("mid_state_nodes") or []
    promoted = state.get("promoted_node_ids") or []
    archived = state.get("archived_attempts", 0)
    last_ac = state.get("last_ac_decision")
    needs = update_needed_resources_file(repo, tid)

    needs_block = ["needed_resources (불만족 상태 / 다음 cycle에 해결 시도):"]
    if needs:
        for n in needs:
            tag = n.get("type", "?")
            spec = n.get("spec") or n.get("experiment") or n.get("axis") or "-"
            needs_block.append(f"  - [{tag}] {str(spec)[:200]}")
    else:
        needs_block.append("  (없음 또는 첫 cycle)")

    lines = [
        f"research_harness MCP에 붙어있어. Thread {tid} 를 이어서 진행할 거야.",
        "",
        f"[supervisor cycle #{cycle}] 이전 세션이 한도로 종료됐어. 새 세션이야.",
        "디스크 상태를 읽고 어디서 멈췄는지 파악해서 이어가.",
        "",
        "**절대 종료 조건 (PR8 + ADR 0006):** 이 thread는 다음 두 honest",
        "종료 중 하나에 도달해야 끝나:",
        "  (A) GOAL ACHIEVED: submit_ac_decision ∈ {accept, revise} AND",
        "      submit_professor_user_goal_attestation achieved=true.",
        "      ※ ADR 0006: achieved=true는 external falsifier 통과 없이는",
        "        구조적으로 거부됨. 필요조건: envelope.max_attestable_status=",
        "        goal_achieved (= falsifier 등록됨) + 통과한 falsifier_result.",
        "  (B) UNVERIFIED SCREEN: envelope.max_attestable_status=unverified_screen",
        "      이면 (falsifier 등록 불가 = air-gapped synthetic-only) achieved=true는",
        "      불가능. achieved=false로 정직하게 attest → harness가 attested_status=",
        "      unverified_screen로 stamp → render_honest_failure_paper로 publish.",
        "      이건 retry가 아니라 RECORDED TERMINAL (정직한 screen 종료).",
        "→ 그 외(attested_status=not_achieved honest_failure)만 'still working'",
        "  = retreat 신호. 다음 cycle에 새 angle / chunk / 자원 / falsifier로 재시도.",
        "",
        "현재 상태 (디스크 스냅샷):",
        f"  - promoted nodes: {promoted}",
        f"  - mid-state nodes (ready/running/completed_worker_report/...): {mid}",
        f"  - archived previous attempts: {archived}",
        f"  - last AC decision: {last_ac!r}",
        f"  - search state status: {state.get('search_state_status')!r}",
        "",
        *needs_block,
        "",
        "방향:",
        f"  1. get_research_state(thread_id=\"{tid}\") 로 정확한 현재 상태 확인.",
        "  2. needed_resources가 있고 그게 해결 가능하면 (예: synthetic→다른 oracle,",
        "     scope 좁히기, chunk 쪼개기) 그쪽으로. 자원 자체가 부족하면 (real data adapter",
        "     없음 등) feasibility_envelope 다시 보고 scope을 'feasibility' 또는",
        "     'directional'로 narrow + dual-gate 통과 가능한 형태로 reframe.",
        "  3. mid-state 노드 있으면 그 노드의 다음 도구 호출 (resume_production_state",
        "     또는 get_next_admissible_node의 status=resume 응답).",
        "  4. 없으면 get_next_admissible_node로 다음 ready 노드 처리.",
        "  5. publish 직전 단계 도달하면 envelope.max_attestable_status 먼저 확인:",
        "     • goal_achieved (falsifier 등록됨): (a) submit_ac_decision ∈",
        "       {accept, revise}; (b) compute_falsifier_result로 held-out 검증",
        "       (cross_generator_transfer면 ranking_a / 다른 generator B의 ranking_b);",
        "       (c) 통과하면 submit_professor_user_goal_attestation achieved=true.",
        "       falsifier가 fail하면 achieved=true 불가 — 파이프라인 개선 후 재측정.",
        "     • unverified_screen (falsifier 등록 불가): achieved=true 시도하지 마.",
        "       achieved=false로 attest → attested_status=unverified_screen stamp →",
        "       render_honest_failure_paper로 honest screen publish → TERMINAL.",
        "  5b. (선택) air-gapped인데 정직한 약-falsifier를 쓰고 싶으면: 별개의",
        "      generator B를 holdout으로 잡고 submit_feasibility_envelope에",
        "      external_falsifier(kind=cross_generator_transfer, holdout_source_id,",
        "      predicate{spearman_rho>=θ}) 등록 → 위 goal_achieved 경로로.",
        "  6. claim 자체가 envelope에 맞지 않으면 revise_root_after_reject로",
        "     scope을 envelope-fit하게 narrow. (예: deployment → feasibility)",
        "",
        "Anti-laziness 룰 작동 중 (PR1):",
        "  - claim narrowing-without-breadth → reject",
        "  - self-made baseline (paper citation 없음) → reject",
        "  - synthetic data without bridging argument → reject",
        "  - disclaimer-only camera_ready_directives (revise일 때) → reject",
        "  - capability claim without decision_rule → reject",
        "Anti-laziness 통과 못 하면 retry 메시지가 explicit reason과 함께 와.",
        "",
        "Feasibility envelope (PR7, supervisor 자동 작성):",
        "  - feasibility_envelope.json이 production/ 에 이미 supervisor가 자동 작성한 상태.",
        "    submit_feasibility_envelope 다시 호출할 필요 없음. 그냥 거기 declared된",
        "    data_sources / llm_oracles / compute_budget / operator_intent 안에서 claim 설계.",
        "    envelope 못 맞추면 validate_claim_fits_envelope이 reject.",
        "  - envelope에 적힌 target_deploy_grade_scope을 claim_contract.deploy_grade_scope에",
        "    그대로 박고, data_source_anchor도 envelope에 적힌 real_adapter id 또는",
        "    'synthetic:<label>' 중 하나로.",
        "",
        "Memory (PR4):",
        "  - prior failures + active lessons는 prepare_rebuttal_packet /",
        "    prepare_paper_writing_context의 응답 payload에 inject됨.",
        "  - 너의 prune/contradicted 결정은 memory/failures/ 에 자동 기록됨.",
        "",
        "세션 한도 가까워지면 self-judge로 멈춰. 한 줄 status 남기고 종료해.",
        "supervisor가 곧 새 cycle spawn 해서 이어받을 거야. not_achieved 상태에서",
        "honest_failure로 끝내지 마 — 그건 retreat 신호. 단, max_attestable_status=",
        "unverified_screen이면 honest_failure render가 정당한 TERMINAL이다 (ADR 0006).",
        "",
        "지금 시작:",
    ]
    return "\n".join(lines)


# --- pty subprocess spawn ----------------------------------------------- #


def _which(name: str) -> str | None:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        full = os.path.join(p, name)
        if os.access(full, os.X_OK):
            return full
    return None


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[NOPDM]|[\x07\x08\x0b\x0c\x0e\x0f]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes + control chars from a string so the
    log file is human-readable. claude's startup banner emits a lot of
    color/cursor codes even in non-interactive mode."""
    return _ANSI_RE.sub("", text)


def _format_stream_json_event(line: str) -> str | None:
    """Parse one line of claude --output-format=stream-json output and
    return a readable single-line log entry. Returns None when the line
    is uninteresting (ping, system noise) and should be dropped."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(ev, dict):
        return None
    etype = ev.get("type", "")
    if etype == "system":
        sub = ev.get("subtype") or ev.get("session_id", "")
        return f"[system] {sub}"
    if etype == "assistant":
        msg = ev.get("message") or {}
        contents = msg.get("content") or []
        out: list[str] = []
        for c in contents:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text":
                txt = (c.get("text") or "").strip()
                if txt:
                    out.append(f"text> {txt[:300]}")
            elif ct == "tool_use":
                name = c.get("name", "?")
                inp = c.get("input") or {}
                # Only print key names so we don't dump full prompts.
                key_summary = ", ".join(sorted(inp.keys())[:6])
                out.append(f"tool_use> {name}({key_summary})")
            elif ct == "thinking":
                txt = (c.get("thinking") or c.get("text") or "")
                if txt:
                    out.append(f"thinking> {txt.strip()[:200]}")
        return "\n".join(out) if out else None
    if etype == "user":
        msg = ev.get("message") or {}
        contents = msg.get("content") or []
        for c in contents:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                content = c.get("content")
                if isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict):
                        txt = (first.get("text") or "")[:200]
                        return f"tool_result> {txt}"
                elif isinstance(content, str):
                    return f"tool_result> {content[:200]}"
        return None
    if etype == "result":
        subtype = ev.get("subtype", "")
        cost = ev.get("total_cost_usd")
        turns = ev.get("num_turns")
        return f"[result] subtype={subtype} turns={turns} cost=${cost}"
    return None


def _kill_process_tree(proc, sig: int) -> None:
    """Signal the whole process group (claude + any children, e.g. the MCP
    server) so a hung subprocess whose child still holds the stdout pipe is
    actually torn down — terminating only the leader can leave the readline
    loop blocked on a child that inherited the pipe. Falls back to signalling
    just the process if the group lookup fails (e.g. it already exited)."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except Exception:  # noqa: BLE001
            pass


def spawn_claude_session(
    prompt: str,
    *,
    model: str = DEFAULT_CLAUDE_MODEL,
    boot_delay: float = DEFAULT_CLAUDE_BOOT_DELAY,
    log_path: Path | None = None,
    active_child_ref: dict | None = None,
    stall_timeout: float = DEFAULT_CLAUDE_STALL_TIMEOUT,
) -> int:
    """Run `claude -p <prompt>` (non-interactive) as a managed
    subprocess with stream-json output. Each event (assistant text,
    tool_use, tool_result, result) is parsed and written as one
    readable line to log_path. ANSI escape codes are stripped.

    Why not pty + interactive `claude`: that path hung at empty stdout
    on every spawn (TTY dialog blocking prompt injection). Why not
    `--output-format text`: claude's startup banner emits ASCII art +
    ANSI codes, and tool-use progress is buffered until completion,
    so the log was unreadable cruft until cycle end. stream-json gives
    line-by-line visibility into tool calls + reasoning.

    Subscription pool only. No API path.
    """
    import subprocess as _subprocess

    claude_bin = _which("claude")
    if not claude_bin:
        raise RuntimeError(
            "`claude` CLI not found in PATH. Install Claude Code "
            "and ensure `claude` is on the operator's PATH. "
            "Supervisor uses the subscription pool — no API fallback."
        )
    del boot_delay  # kept for API compat; no TTY surface to wait on

    cmd = [
        claude_bin, "-p", prompt,
        "--model", model,
        "--output-format", "stream-json",
        "--verbose",  # stream-json requires --verbose in claude v2+
    ]
    log_fh = log_path.open("a", encoding="utf-8") if log_path else None
    proc = _subprocess.Popen(
        cmd,
        stdin=_subprocess.DEVNULL,
        stdout=_subprocess.PIPE,
        stderr=_subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered
        close_fds=True,
        start_new_session=True,  # own process group so the watchdog can tear
                                 # down claude + its MCP-server children together
    )
    if active_child_ref is not None:
        active_child_ref["pid"] = proc.pid

    if log_fh:
        log_fh.write(f"\n=== claude spawn pid={proc.pid} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log_fh.flush()

    # Stall watchdog: a healthy cycle streams events continuously. If the
    # subprocess emits NO output for stall_timeout (a hang — e.g. a model/API
    # stall after a tool call, which the fast-fail EXIT detector cannot catch
    # because the process never exits), terminate it so the supervisor recovers
    # instead of blocking forever in the readline loop below.
    last_activity = [time.time()]
    stop_watchdog = threading.Event()

    def _watchdog() -> None:
        check = max(1.0, min(15.0, stall_timeout / 4.0))
        while not stop_watchdog.wait(timeout=check):
            if time.time() - last_activity[0] <= stall_timeout:
                continue
            msg = (f"\n=== watchdog: no output for {stall_timeout:.0f}s — "
                   f"terminating hung claude (pid={proc.pid}) ===\n")
            for fh in (log_fh, LOG):
                try:
                    if fh:
                        fh.write(msg)
                        fh.flush()
                except Exception:  # noqa: BLE001
                    pass
            _kill_process_tree(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except _subprocess.TimeoutExpired:
                _kill_process_tree(proc, signal.SIGKILL)
            return

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            last_activity[0] = time.time()
            clean = _strip_ansi(line).rstrip("\n")
            formatted = _format_stream_json_event(clean)
            if formatted is None:
                # Fall back to raw line if it has content but isn't
                # one of our recognized event types — keeps surprises
                # visible rather than silently dropped.
                if clean.strip() and not clean.startswith("{"):
                    formatted = clean
                else:
                    continue
            stamped = f"[{time.strftime('%H:%M:%S')}] {formatted}"
            if log_fh:
                try:
                    log_fh.write(stamped + "\n")
                    log_fh.flush()
                except OSError:
                    pass
            try:
                LOG.write(stamped + "\n")
                LOG.flush()
            except Exception:  # noqa: BLE001
                pass
        proc.wait()
        return proc.returncode if proc.returncode is not None else 1
    except KeyboardInterrupt:
        _kill_process_tree(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except _subprocess.TimeoutExpired:
            _kill_process_tree(proc, signal.SIGKILL)
        raise
    finally:
        stop_watchdog.set()
        watchdog.join(timeout=1.0)
        if active_child_ref is not None:
            active_child_ref["pid"] = None
        if log_fh:
            log_fh.close()


# --- supervisor loop ---------------------------------------------------- #


class SupervisorLock:
    """Single-supervisor-per-thread enforcement. Lock file holds the
    supervisor PID; stale locks (PID dead) are auto-cleared."""

    def __init__(self, thread_dir: Path):
        self.path = thread_dir / ".supervisor.lock"

    def acquire(self) -> None:
        if self.path.exists():
            try:
                pid = int(self.path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = -1
            if pid > 0 and _pid_alive(pid):
                raise RuntimeError(
                    f"another supervisor (pid={pid}) is already running for "
                    f"this thread (lock: {self.path}). kill it first or wait."
                )
            # stale lock — clear it.
            try:
                self.path.unlink()
            except OSError:
                pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(self.release)

    def release(self) -> None:
        try:
            if self.path.exists() and self.path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else — treat as alive.


def _log(supervisor_log_path: Path, msg: str) -> None:
    """Append a timestamped line to the supervisor log AND mirror to stderr."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        supervisor_log_path.parent.mkdir(parents=True, exist_ok=True)
        with supervisor_log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    print(line, end="", file=LOG, flush=True)


def watch_thread(
    repo: Path,
    tid: str,
    *,
    max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    milestone_cycle: int = DEFAULT_MILESTONE_CYCLE,
    model: str = DEFAULT_CLAUDE_MODEL,
    boot_delay: float = DEFAULT_CLAUDE_BOOT_DELAY,
    rate_limit_backoff_initial: float = DEFAULT_RATE_LIMIT_BACKOFF_INITIAL,
    rate_limit_backoff_max: float = DEFAULT_RATE_LIMIT_BACKOFF_MAX,
    target_scope: str = "directional",  # bootstrap envelope target
    max_cycles: int | None = None,  # PR8: only honored when explicitly set;
                                    #      default behavior never quits on count.
) -> dict[str, object]:
    """PR8 supervisor loop. ONLY exits when the dual-gate publish outcome
    is reached (AC accept + Professor user_goal_attestation.achieved=true)
    OR the operator sends SIGINT/SIGTERM. honest_failure is treated as a
    retreat state — supervisor respawns claude with instructions to try a
    different angle. Subscription rate-limit fast-fails trigger
    exponential backoff (1m → 2m → 4m → ... cap).

    Exit conditions:
      - dual-gate publish (AC accept + attestation.achieved=true) → status='terminal'
      - SIGINT/SIGTERM → status='interrupted'
      - explicit max_cycles override hit → status='max_cycles_exceeded'
        (left available for tests + emergency operator stop; default = unlimited)
    """
    tdir = _thread_dir(repo, tid)
    if not tdir.exists():
        raise RuntimeError(f"thread directory not found: {tdir}")
    log_path = tdir / "supervisor.log"
    lock = SupervisorLock(tdir)
    lock.acquire()

    interrupted = {"flag": False}
    active_child: dict[str, int | None] = {"pid": None}

    def _sigint(_signum, _frame):
        interrupted["flag"] = True
        _log(
            log_path,
            "signal received — terminating active claude subprocess then exiting.",
        )
        # Cascade the signal to the active claude subprocess so the
        # current cycle ends in seconds, not minutes. Without this, the
        # supervisor's loop can only check the interrupted flag between
        # cycles — i.e., after the running claude subprocess exits on
        # its own — and the operator sees the supervisor 'still running'
        # in the UI for the whole subprocess lifetime.
        child_pid = active_child.get("pid")
        if child_pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(child_pid, sig)
                except ProcessLookupError:
                    break
                # Brief pause to let SIGTERM take effect before SIGKILL.
                time.sleep(0.5)
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    _log(log_path, f"supervisor starting for thread {tid} (pid={os.getpid()})")
    _log(
        log_path,
        f"max_idle={max_idle_seconds}s poll={poll_seconds}s milestone_cycle={milestone_cycle} "
        f"model={model} max_cycles={max_cycles!r} (None=unlimited) target_scope={target_scope!r}",
    )

    # PR7+PR8: auto-bootstrap the feasibility envelope from settings.json +
    # market dossier. Operator no longer needs to hand-craft the envelope
    # nor instruct Claude Code to submit it — supervisor is the operator's
    # stand-in.
    bootstrapped = bootstrap_envelope_if_missing(repo, tid, target_scope=target_scope)
    if bootstrapped is not None:
        adapter_ids = [
            s.get("id") for s in bootstrapped.get("data_sources_available", [])
            if s.get("kind") == "real_adapter"
        ]
        _log(
            log_path,
            f"feasibility envelope auto-bootstrapped: target_scope={target_scope!r}, "
            f"real_adapters={adapter_ids}, "
            f"baseline_provenance_count={len(bootstrapped.get('baseline_provenance_available') or [])}",
        )

    cycle = 0
    rate_limit_backoff = rate_limit_backoff_initial
    rate_limit_armed = False  # toggled after a fast-fail cycle
    while True:
        terminal, outcome = is_terminal(repo, tid)
        if terminal:
            _log(log_path, f"DUAL-GATE PASS: outcome={outcome!r} after {cycle} cycle(s). exiting cleanly.")
            return {"status": "terminal", "outcome": outcome, "cycles": cycle}

        if interrupted["flag"]:
            _log(log_path, "interrupted by signal — exiting.")
            return {"status": "interrupted", "cycles": cycle}

        if max_cycles is not None and cycle >= max_cycles:
            _log(log_path, f"explicit max_cycles override ({max_cycles}) hit. exiting.")
            return {"status": "max_cycles_exceeded", "cycles": cycle}

        # PR8 milestone-not-termination logging.
        if cycle > 0 and cycle % milestone_cycle == 0:
            needs = update_needed_resources_file(repo, tid)
            _log(
                log_path,
                f"[milestone] cycle {cycle} reached without dual-gate. "
                f"blocking on {len(needs)} resource(s); see needed_resources.yaml. "
                "supervisor continues indefinitely per never-quit policy.",
            )

        idle = mcp_idle_seconds(repo, tid)
        # Cold start: on the very first cycle there is no prior claude session to
        # be "idle" relative to — the only recent write is the envelope
        # auto-bootstrap, which would otherwise force a full max_idle wait before
        # the first spawn (the ~10-min cold-start delay). Spawn cycle #1
        # immediately; the idle gate governs only subsequent (resume) cycles.
        if cycle > 0 and idle <= max_idle_seconds:
            # Recent activity — MCP still being driven. Wait.
            time.sleep(poll_seconds)
            continue

        # Spawn a (new) claude cycle. update needed_resources before generating
        # the resume prompt so the LLM sees the latest gaps.
        update_needed_resources_file(repo, tid)
        cycle += 1
        prompt = build_resume_prompt(repo, tid, cycle)
        if cycle == 1:
            _log(log_path, "cycle #1: cold start — spawning first claude immediately (idle gate applies from cycle #2)")
        else:
            _log(log_path, f"cycle #{cycle}: idle={idle:.0f}s > {max_idle_seconds:.0f}s — spawning claude")
        spawn_started = time.time()
        try:
            exit_code = spawn_claude_session(
                prompt,
                model=model,
                boot_delay=boot_delay,
                log_path=tdir / "claude_subprocess.log",
                active_child_ref=active_child,
            )
            spawn_elapsed = time.time() - spawn_started
            _log(
                log_path,
                f"cycle #{cycle}: claude subprocess exited code={exit_code} after {spawn_elapsed:.1f}s",
            )
            # PR8 rate-limit detection. A real claude session normally runs
            # at least several minutes (MCP tool calls + reasoning). A
            # sub-30s exit usually means auth/rate-limit/binary failure.
            if spawn_elapsed < RATE_LIMIT_FAST_FAIL_SECONDS:
                rate_limit_armed = True
                _log(
                    log_path,
                    f"cycle #{cycle}: fast-fail ({spawn_elapsed:.1f}s) — likely rate limit / "
                    f"auth issue. backing off {rate_limit_backoff:.0f}s.",
                )
                time.sleep(rate_limit_backoff)
                rate_limit_backoff = min(
                    rate_limit_backoff_max, rate_limit_backoff * 2.0
                )
            elif spawn_elapsed >= RATE_LIMIT_HEALTHY_CYCLE_SECONDS:
                if rate_limit_armed:
                    _log(
                        log_path,
                        "cycle ran healthy — resetting rate-limit backoff.",
                    )
                rate_limit_armed = False
                rate_limit_backoff = rate_limit_backoff_initial
        except Exception as exc:  # noqa: BLE001
            _log(log_path, f"cycle #{cycle}: spawn raised {type(exc).__name__}: {exc}")
            time.sleep(min(60.0, poll_seconds * 2))


# --- CLI ----------------------------------------------------------------- #


def _repo_root_default() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="research_harness.thread_supervisor",
        description=(
            "True hands-free thread executor: spawn `claude` CLI subprocesses "
            "via pty under subscription pool until the thread reaches a "
            "terminal publication outcome."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_watch = sub.add_parser("watch", help="watch a thread to terminal outcome")
    p_watch.add_argument("thread_id", help="thread id (under runs/threads/)")
    p_watch.add_argument("--repo-root", type=Path, default=None)
    p_watch.add_argument("--max-idle-seconds", type=float, default=DEFAULT_MAX_IDLE_SECONDS)
    p_watch.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    p_watch.add_argument("--milestone-cycle", type=int, default=DEFAULT_MILESTONE_CYCLE,
                          help="informational logging interval (PR8: not a termination)")
    p_watch.add_argument("--max-cycles", type=int, default=None,
                          help="emergency operator override; default = unlimited (PR8 never-quit policy)")
    p_watch.add_argument(
        "--target-scope",
        choices=["deployment", "feasibility", "directional"],
        default="directional",
        help=(
            "PR7 target deploy_grade_scope. Default 'directional' (safest — "
            "any registered data adapter suffices). Use 'deployment' only "
            "when settings.json.data_adapters.registered has at least one "
            "real adapter for this thread's domain."
        ),
    )
    p_watch.add_argument(
        "--model", default=None,
        help=(
            "Override the production model. When omitted (the frontend path), "
            "the supervisor resolves it via thread.json.mcp_model -> "
            "settings.runtime.llm_orchestrator.mcp.default_model -> default."
        ),
    )
    p_watch.add_argument("--boot-delay", type=float, default=DEFAULT_CLAUDE_BOOT_DELAY)

    args = parser.parse_args(argv)
    if args.cmd == "watch":
        repo = (args.repo_root or _repo_root_default()).resolve()
        # Resolve the model from settings when not explicitly overridden, so
        # the production phase honours the operator's configured model instead
        # of the hard-coded default.
        model = args.model or resolve_supervisor_model(repo, args.thread_id)
        result = watch_thread(
            repo,
            args.thread_id,
            max_idle_seconds=args.max_idle_seconds,
            poll_seconds=args.poll_seconds,
            milestone_cycle=args.milestone_cycle,
            max_cycles=args.max_cycles,
            model=model,
            boot_delay=args.boot_delay,
            target_scope=args.target_scope,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("status") in {"terminal", "interrupted"} else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
