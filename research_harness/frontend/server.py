"""FastAPI app for the operator_frontend.

Single-user, localhost-only UI in front of the research harness. Imports
the harness in-process (ADR 0002) and renders with Jinja2 + HTMX +
vanilla SSE (ADR 0003).

Routes are grouped:

- Page shells: ``GET /`` (full app), ``GET /threads/{tid}``
- Sidebar partials: ``GET /partials/sidebar``
- Thread CRUD: ``POST /api/threads``, ``GET /api/threads/{tid}``
- Phase panels: ``GET /partials/thread/{tid}/phase/{phase}``
- Live grilling: ``POST /api/threads/{tid}/grilling/start``,
  ``POST /api/threads/{tid}/grilling/reply``,
  ``GET /api/threads/{tid}/grilling/stream`` (SSE)
  (refine phase removed; Professor replaces placeholders at production entry)
- Background phases: ``POST /api/threads/{tid}/market/start``,
  ``POST /api/threads/{tid}/production/start``
- Settings: ``POST /api/settings/subscription_ack[/revoke]``,
  ``POST /api/settings/full_auto``

The harness's billing_ack / execution_ack are passed as booleans (True)
once the operator has granted ``subscription_ack`` and confirmed the
``execute_ack`` modal (or has ``full_auto_mode`` on).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from research_harness.agents.grilling import (
    load_resumable_session,
    run_grilling_session,
)
from research_harness.agents.market_research import run_market_research
from research_harness.config import load_settings
# research_refiner removed from the frontend pipeline — Professor designs
# the real claim contract at production entry instead.
from research_harness.frontend import acks, threads
from research_harness.frontend.lock import LockBusyError, SingleActiveRunLock
from research_harness.schemas.validator import validate_named_schema

LOG = logging.getLogger("research_harness.frontend")

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


# --------------------------------------------------------------- live sessions


@dataclass
class LiveSession:
    """In-flight multi-turn agent session (grilling).

    The agent loop runs on a worker thread (via ``asyncio.to_thread``)
    because the agent code is sync. The ``input_provider`` it receives
    bridges back into the event loop via ``run_coroutine_threadsafe``.
    """

    thread_id: str
    phase: str  # "grilling" | "market" | "production"
    out_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending_reply: asyncio.Future | None = None
    task: asyncio.Task | None = None
    loop: asyncio.AbstractEventLoop | None = None

    async def wait_for_reply(self) -> str:
        assert self.loop is not None
        fut = self.loop.create_future()
        self.pending_reply = fut
        return await fut

    def submit_reply(self, text: str) -> bool:
        if self.pending_reply is None or self.pending_reply.done():
            return False
        self.pending_reply.set_result(text)
        self.pending_reply = None
        return True

    async def emit(self, event: dict[str, Any]) -> None:
        await self.out_queue.put(event)


# --------------------------------------------------------------- application


@dataclass
class AppState:
    repo_root: Path
    lock: SingleActiveRunLock = field(default_factory=SingleActiveRunLock)
    sessions: dict[str, LiveSession] = field(default_factory=dict)
    env: Environment | None = None
    # Test hook: when set, the launcher passes this as the agent's
    # `command_runner`, bypassing the real claude CLI. Production code never
    # touches this field — only tests / browser e2e fakes inject here.
    command_runner_override: Any = None


def _build_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        enable_async=False,
    )

    from jinja2 import Undefined

    def _pretty_json(value: Any) -> str:
        # Defensive: Jinja's Undefined fires when a template touches an
        # absent dict key (e.g. ds.source on a synthetic-type dataset_spec
        # that has no source field). Without this guard json.dumps raises
        # TypeError and the entire thread page 500s.
        if isinstance(value, Undefined) or value is None:
            return ""
        try:
            return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
        except TypeError:
            return str(value)

    def _truncate_chars(s: Any, n: int = 80) -> Any:
        if isinstance(s, Undefined) or s is None:
            return ""
        if isinstance(s, str) and len(s) > n:
            return s[: n - 1] + "…"
        return s

    env.filters["pretty_json"] = _pretty_json
    env.filters["truncate_chars"] = _truncate_chars
    return env


def create_app(repo_root: Path | None = None) -> FastAPI:
    repo_root = (repo_root or Path.cwd()).resolve()
    state = AppState(repo_root=repo_root, env=_build_jinja_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repaired = threads.boot_repair(state.repo_root)
        if repaired:
            LOG.info("boot_repair demoted to awaiting_input: %s", repaired)
        yield

    app = FastAPI(title="Research Harness — Operator Frontend", lifespan=lifespan)
    app.state.s = state
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    _register_routes(app, state)
    _register_file_route(app, state)
    return app


# --------------------------------------------------------------- routes


def _register_routes(app: FastAPI, s: AppState) -> None:
    env = s.env

    def render(name: str, **ctx: Any) -> HTMLResponse:
        tmpl = env.get_template(name)
        ctx.setdefault("ack_state", acks.get_state(s.repo_root))
        ctx.setdefault("has_subscription_ack", acks.has_subscription_ack(s.repo_root))
        ctx.setdefault("full_auto_mode", acks.full_auto_mode(s.repo_root))
        ctx.setdefault("lock_holder", s.lock.holder)
        return HTMLResponse(tmpl.render(**ctx))

    # -------- page shells

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        thread_list = threads.list_threads(s.repo_root)
        return render("base.html", thread_list=thread_list, active_thread=None)

    @app.get("/threads/{thread_id}", response_class=HTMLResponse)
    async def thread_page(thread_id: str) -> HTMLResponse:
        try:
            active = threads.load_thread(s.repo_root, thread_id)
        except threads.ThreadError as exc:
            raise HTTPException(404, str(exc))
        thread_list = threads.list_threads(s.repo_root)
        phase_data = _collect_phase_data(s.repo_root, thread_id)
        # Tell templates whether this thread currently has an in-memory
        # live session — needed to distinguish "agent is alive, just
        # waiting on user input" from "phase_status is awaiting_input on
        # disk but the agent task is dead post-restart and the operator
        # needs to click Resume". Without this signal the reply form
        # looks usable but submits 409 — that's exactly what made grilling
        # appear to "lose" the conversation across restarts.
        # MCP model selection: surface the allowed list + the thread's
        # current choice so the dropdown can render server-side.
        try:
            settings = load_settings(s.repo_root)
        except (OSError, ValueError, json.JSONDecodeError):
            settings = {}
        mcp_cfg = (
            settings.get("runtime", {})
            .get("llm_orchestrator", {})
            .get("mcp", {})
        )
        return render(
            "base.html",
            thread_list=thread_list,
            active_thread=active,
            phase_data=phase_data,
            live_session_present=thread_id in s.sessions,
            mcp_allowed_models=mcp_cfg.get("allowed_models") or [],
            mcp_default_model=mcp_cfg.get("default_model") or "",
            llm_backend=(
                settings.get("runtime", {})
                .get("llm_orchestrator", {})
                .get("backend", "mock")
            ),
        )

    @app.get("/api/threads/{thread_id}/graph_data")
    async def graph_data(thread_id: str) -> JSONResponse:
        """JSON snapshot of the production claim graph for live polling.
        The standalone graph page polls this and re-renders incrementally
        when nodes / dialogs / coverage change on disk."""
        _require_thread(s.repo_root, thread_id)
        p = _read_phase_artifacts(s.repo_root, thread_id, "production")
        return JSONResponse(
            {
                "tree_state": p.get("tree_state") or {"nodes": []},
                "node_dialogs": p.get("node_dialogs") or {},
                "coverage_by_node_type": p.get("coverage_by_node_type") or {},
                "readiness_history": p.get("readiness_history") or [],
                "in_progress_nodes": p.get("in_progress_nodes") or [],
                "last_activity_mtime": p.get("last_activity_mtime") or 0,
            }
        )

    @app.get("/threads/{thread_id}/graph", response_class=HTMLResponse)
    async def thread_graph_page(thread_id: str) -> HTMLResponse:
        """Standalone full-page graph view of the production claim tree.

        Read-only. Pulls the production phase artifacts (tree_state +
        per-node dialogs) and ships them to a vanilla-SVG renderer in a
        new tab so the operator can pan / zoom / hover the actual research
        graph without giving up the existing thread page.
        """
        try:
            active = threads.load_thread(s.repo_root, thread_id)
        except threads.ThreadError as exc:
            raise HTTPException(404, str(exc))
        production = _read_phase_artifacts(s.repo_root, thread_id, "production")
        return render(
            "graph.html",
            active_thread=active,
            tree_state=production.get("tree_state") or {"nodes": []},
            node_dialogs=production.get("node_dialogs") or {},
            coverage_by_node_type=production.get("coverage_by_node_type") or {},
            readiness_history=production.get("readiness_history") or [],
            intake_to_claim=production.get("intake_to_claim"),
        )

    # -------- partials

    @app.get("/partials/sidebar", response_class=HTMLResponse)
    async def sidebar_partial() -> HTMLResponse:
        return render("sidebar.html", thread_list=threads.list_threads(s.repo_root))

    @app.get(
        "/partials/thread/{thread_id}/phase/{phase}", response_class=HTMLResponse
    )
    async def phase_partial(thread_id: str, phase: str) -> HTMLResponse:
        try:
            active = threads.load_thread(s.repo_root, thread_id)
        except threads.ThreadError as exc:
            raise HTTPException(404, str(exc))
        if phase not in threads.PHASES:
            raise HTTPException(400, f"unknown phase {phase!r}")
        data = _collect_phase_data(s.repo_root, thread_id)
        return render(
            f"phases/{phase}.html",
            thread=active,
            phase=phase,
            p=data.get(phase, {}),
            live_session_present=thread_id in s.sessions,
        )

    # -------- thread CRUD

    @app.post("/api/threads/{thread_id}/rename")
    async def rename_thread(thread_id: str, req: Request) -> JSONResponse:
        _require_thread(s.repo_root, thread_id)
        body = await _maybe_json(req)
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "title must not be empty")
        updated = threads.update_thread(s.repo_root, thread_id, title=title)
        return JSONResponse({"ok": True, "title": updated["title"]})

    @app.post("/api/threads/{thread_id}/mcp_model")
    async def set_thread_mcp_model(thread_id: str, req: Request) -> JSONResponse:
        """Per-thread MCP model selection. Validates against settings's
        allowed_models list so the dropdown can't smuggle an arbitrary model
        name through."""
        _require_thread(s.repo_root, thread_id)
        body = await _maybe_json(req)
        model = (body.get("mcp_model") or "").strip()
        if not model:
            raise HTTPException(400, "mcp_model must not be empty")
        try:
            settings = load_settings(s.repo_root)
        except (OSError, ValueError, json.JSONDecodeError):
            settings = {}
        allowed = (
            settings.get("runtime", {})
            .get("llm_orchestrator", {})
            .get("mcp", {})
            .get("allowed_models", [])
        )
        # Strict: model MUST be in allowed_models (no free-form input).
        # Free-form would let typos through and silently send Claude Code a
        # nonexistent model id. Operators expand allowed_models in
        # settings.json when a new model ships.
        if not allowed:
            raise HTTPException(
                500,
                "settings.runtime.llm_orchestrator.mcp.allowed_models is "
                "empty; cannot accept any model. Populate the list with "
                "valid Claude model ids.",
            )
        if model not in allowed:
            raise HTTPException(
                400,
                f"model {model!r} is not in allowed_models {allowed}. Edit "
                "settings.json to add it.",
            )
        updated = threads.update_thread(
            s.repo_root, thread_id, mcp_model=model
        )
        return JSONResponse({"ok": True, "mcp_model": updated.get("mcp_model")})

    @app.post("/api/threads/{thread_id}/delete")
    async def delete_thread(thread_id: str) -> JSONResponse:
        _require_thread(s.repo_root, thread_id)
        # Refuse if this thread holds the live lock — deleting a run dir
        # out from under a worker mid-write would race. The operator must
        # Abandon first.
        if s.lock.holder is not None and s.lock.holder.thread_id == thread_id:
            raise HTTPException(
                409,
                "thread is currently live; click Abandon first to release "
                "the lock, then delete.",
            )
        # Also clean any in-memory session reference (defensive — there
        # shouldn't be one without lock, but it's cheap to be sure).
        s.sessions.pop(thread_id, None)
        threads.delete_thread(s.repo_root, thread_id)
        return JSONResponse({"ok": True})

    @app.post("/api/threads")
    async def create_thread(req: Request) -> HTMLResponse:
        form = await req.form()
        user_goal = (form.get("user_goal") or "").strip()
        title = (form.get("title") or "").strip() or None
        if not user_goal:
            raise HTTPException(400, "user_goal must not be empty")
        try:
            new = threads.create_thread(
                s.repo_root, user_goal=user_goal, title=title
            )
        except threads.ThreadError as exc:
            raise HTTPException(400, str(exc))
        # HX-Redirect triggers a full client-side navigation, which is what we
        # want here: the new thread's accordion is a different page, not a
        # partial swap, and the dialog top-layer needs the body reset that
        # comes with a real navigation.
        resp = HTMLResponse("", status_code=200)
        resp.headers["HX-Redirect"] = f"/threads/{new['thread_id']}"
        return resp

    # -------- grilling

    @app.post("/api/threads/{thread_id}/grilling/start")
    async def grilling_start(thread_id: str, req: Request) -> JSONResponse:
        index = _require_thread(s.repo_root, thread_id)
        _require_subscription_ack(s.repo_root)
        body = await _maybe_json(req)
        mode = body.get("mode") or (
            "auto" if acks.full_auto_mode(s.repo_root) else "manual"
        )
        if not acks.full_auto_mode(s.repo_root) and mode != "manual":
            raise HTTPException(400, "execute_ack mode must match full_auto setting")
        await _launch_grilling(s, index, mode=mode)
        return JSONResponse({"ok": True, "thread_id": thread_id})

    @app.post("/api/threads/{thread_id}/grilling/reply")
    async def grilling_reply(thread_id: str, req: Request) -> JSONResponse:
        _require_thread(s.repo_root, thread_id)
        session = s.sessions.get(thread_id)
        if session is None or session.phase != "grilling":
            raise HTTPException(409, "no live grilling session for this thread")
        form = await req.form()
        reply = (form.get("reply") or "").strip()
        if not reply:
            raise HTTPException(400, "reply must not be empty")
        accepted = session.submit_reply(reply)
        if not accepted:
            raise HTTPException(409, "no pending question awaiting reply")
        # Echo into the SSE stream so the chat scrollback shows it.
        await session.emit({"type": "user_reply", "text": reply})
        return JSONResponse({"ok": True})

    @app.post("/api/threads/{thread_id}/{phase}/retry")
    async def phase_retry(thread_id: str, phase: str, req: Request) -> JSONResponse:
        """Re-run a phase that ended in ``phase_status == "failed"``.

        Archives the failed phase directory to a sibling
        ``<phase>.attempt<N>/`` so the operator can still inspect what
        went wrong, then re-launches the phase fresh. Works for any
        phase including upstream failures we can't anticipate
        (Claude API blips, rate limits, content-policy hits, network
        drops). Without this the only escape from a failed phase was to
        hand-edit thread.json or delete the thread entirely.
        """
        if phase not in threads.PHASES:
            raise HTTPException(400, f"unknown phase {phase!r}")
        index = _require_thread(s.repo_root, thread_id)
        # Retry is allowed in two cases:
        #   (1) phase_status == "failed" — clean failure, the launcher's
        #       except block ran and marked it.
        #   (2) phase_status in {running, awaiting_input} AND no in-memory
        #       LiveSession — orphan state, typically caused by a server
        #       crash between the agent raising and the launcher's except
        #       block writing phase_status=failed. The on-disk artifact is
        #       genuinely dead and there's no point trying to Resume.
        live = thread_id in s.sessions
        retryable = (
            index["phase_status"] == "failed"
            or (index["phase_status"] in {"running", "awaiting_input"} and not live)
        )
        if not retryable:
            raise HTTPException(
                409,
                f"retry is only available when the phase is failed or "
                f"orphaned (running/awaiting_input with no in-memory "
                f"session). current phase_status = {index['phase_status']!r}, "
                f"live_session_present = {live}",
            )
        # Refuse to retry a phase the operator hasn't started yet — that
        # would be a launch, not a retry.
        if index["current_phase"] != phase:
            raise HTTPException(
                409,
                f"thread's current_phase is {index['current_phase']!r}, "
                f"not {phase!r}; retry the actual failed phase instead",
            )
        # Archive the failed phase dir to a numbered sibling.
        pdir = threads.phase_dir(s.repo_root, thread_id, phase)
        attempt_idx: int | None = None
        if pdir.exists():
            attempt_idx = _next_attempt_number(pdir.parent, phase)
            archive = pdir.parent / f"{phase}.attempt{attempt_idx}"
            pdir.rename(archive)
        # Reset phase status to running and re-launch.
        threads.update_thread(
            s.repo_root, thread_id, phase_status="running"
        )
        # Live phase (grilling) needs ack/mode; market and production don't.
        if phase == "grilling":
            body = await _maybe_json(req)
            mode = body.get("mode") or (
                "auto" if acks.full_auto_mode(s.repo_root) else "manual"
            )
            _require_subscription_ack(s.repo_root)
            await _launch_grilling(s, index, mode=mode)
        elif phase == "market":
            await _launch_market(s, index)
        else:  # production
            await _launch_production(s, index)
        return JSONResponse({"ok": True, "archived_attempt": attempt_idx})

    @app.post("/api/threads/{thread_id}/grilling/abandon")
    async def grilling_abandon(thread_id: str) -> JSONResponse:
        """Bail out of a stuck live phase. Cancels the running task,
        releases the [[single_active_run]] lock, marks phase_status=failed,
        and keeps the partial session JSON on disk for inspection. The
        operator can then re-run the same phase (a new attempt directory
        if needed) without restarting the server."""
        _require_thread(s.repo_root, thread_id)
        session = s.sessions.pop(thread_id, None)
        if session and session.task and not session.task.done():
            session.task.cancel()
            # Unblock any pending input_provider awaiting a reply.
            if session.pending_reply and not session.pending_reply.done():
                session.pending_reply.set_exception(
                    asyncio.CancelledError("abandoned by operator")
                )
        # Force-release the lock if this thread holds it.
        if s.lock.holder is not None and s.lock.holder.thread_id == thread_id:
            s.lock._holder = None
            if s.lock._lock.locked():
                try:
                    s.lock._lock.release()
                except RuntimeError:
                    pass
        threads.update_thread(s.repo_root, thread_id, phase_status="failed")
        return JSONResponse({"ok": True})

    @app.get("/api/threads/{thread_id}/grilling/stream")
    async def grilling_stream(thread_id: str) -> StreamingResponse:
        _require_thread(s.repo_root, thread_id)
        session = s.sessions.get(thread_id)
        if session is None or session.phase != "grilling":
            raise HTTPException(409, "no live grilling session for this thread")
        return StreamingResponse(
            _sse_stream(session), media_type="text/event-stream"
        )

    # -------- market + production (background, no user input)

    @app.post("/api/threads/{thread_id}/market/start")
    async def market_start(thread_id: str) -> JSONResponse:
        index = _require_thread(s.repo_root, thread_id)
        await _launch_market(s, index)
        return JSONResponse({"ok": True})

    @app.get("/api/threads/{thread_id}/market/stream")
    async def market_stream(thread_id: str) -> StreamingResponse:
        _require_thread(s.repo_root, thread_id)
        session = s.sessions.get(thread_id)
        if session is None or session.phase != "market":
            raise HTTPException(409, "no live market session for this thread")
        return StreamingResponse(
            _sse_stream(session), media_type="text/event-stream"
        )

    @app.post("/api/threads/{thread_id}/production/start")
    async def production_start(thread_id: str) -> JSONResponse:
        index = _require_thread(s.repo_root, thread_id)
        await _launch_production(s, index)
        return JSONResponse({"ok": True})

    @app.post("/api/threads/{thread_id}/supervisor/start")
    async def supervisor_start(thread_id: str, req: Request) -> JSONResponse:
        """Spawn thread_supervisor as a detached background subprocess.
        The supervisor takes over until the dual-gate publication
        outcome is reached (or operator stops it)."""
        _require_thread(s.repo_root, thread_id)
        body = await _maybe_json(req)
        target_scope = (body.get("target_scope") or "directional").strip()
        if target_scope not in {"deployment", "feasibility", "directional"}:
            raise HTTPException(400, f"invalid target_scope: {target_scope!r}")
        # PR10b: refuse to start/resume once a publication artifact
        # exists. The frontend hides the button in this case, but we
        # enforce here as well so direct API hits respect the rule.
        pub_dir = s.repo_root / "runs" / "threads" / thread_id / "production" / "publication"
        if (pub_dir / "paper.html").exists() or (pub_dir / "honest_failure.html").exists():
            raise HTTPException(
                409,
                "publication already exists for this thread; supervisor "
                "resume is disabled. Remove production/publication/ first "
                "to re-enable.",
            )
        # Reject if a supervisor is already running for this thread.
        lock_path = s.repo_root / "runs" / "threads" / thread_id / ".supervisor.lock"
        if lock_path.exists():
            try:
                pid = int(lock_path.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)  # alive check
                raise HTTPException(409, f"supervisor already running (pid={pid})")
            except (ProcessLookupError, ValueError):
                # Stale lock — fall through; supervisor's lock acquire
                # will clean it up.
                pass
            except PermissionError:
                # PID exists in another user's session — block to be safe.
                raise HTTPException(409, f"supervisor lock exists; manual cleanup needed: {lock_path}")
        # Spawn detached so it survives this request.
        cmd = [
            sys.executable, "-m", "research_harness.thread_supervisor",
            "watch", thread_id,
            "--repo-root", str(s.repo_root),
            "--target-scope", target_scope,
        ]
        # Redirect stdio to a file so frontend can later tail it.
        log_dir = s.repo_root / "runs" / "threads" / thread_id
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "supervisor_subprocess.out"
        with open(stdout_path, "ab") as fh:
            fh.write(f"\n=== supervisor spawn at {time.strftime('%Y-%m-%d %H:%M:%S')}, target_scope={target_scope} ===\n".encode("utf-8"))
        out_handle = open(stdout_path, "ab")  # noqa: SIM115
        proc = subprocess.Popen(
            cmd,
            stdout=out_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        # We DON'T wait. The subprocess writes its own lock file as it
        # starts up; that's the canonical liveness signal.
        return JSONResponse({
            "ok": True,
            "pid": proc.pid,
            "target_scope": target_scope,
            "log_path": str(stdout_path),
        })

    @app.post("/api/threads/{thread_id}/supervisor/stop")
    async def supervisor_stop(thread_id: str) -> JSONResponse:
        _require_thread(s.repo_root, thread_id)
        lock_path = s.repo_root / "runs" / "threads" / thread_id / ".supervisor.lock"
        if not lock_path.exists():
            raise HTTPException(404, "no supervisor running for this thread")
        try:
            pid = int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            raise HTTPException(409, "supervisor lock file is corrupt")
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise HTTPException(404, f"supervisor pid {pid} not alive (cleaned stale lock)")
        return JSONResponse({"ok": True, "signaled_pid": pid})

    @app.get("/api/threads/{thread_id}/production/stream")
    async def production_stream(thread_id: str) -> StreamingResponse:
        _require_thread(s.repo_root, thread_id)
        session = s.sessions.get(thread_id)
        if session is None or session.phase != "production":
            raise HTTPException(409, "no live production session for this thread")
        return StreamingResponse(
            _sse_stream(session), media_type="text/event-stream"
        )

    # -------- settings

    @app.post("/api/settings/subscription_ack")
    async def settings_grant_subscription() -> JSONResponse:
        at = acks.grant_subscription_ack(s.repo_root)
        return JSONResponse({"ok": True, "subscription_ack_at": at})

    @app.post("/api/settings/subscription_ack/revoke")
    async def settings_revoke_subscription() -> JSONResponse:
        acks.revoke_subscription_ack(s.repo_root)
        return JSONResponse({"ok": True})

    @app.post("/api/settings/full_auto")
    async def settings_full_auto(req: Request) -> JSONResponse:
        body = await _maybe_json(req)
        enabled = bool(body.get("enabled"))
        acks.set_full_auto_mode(s.repo_root, enabled)
        return JSONResponse({"ok": True, "full_auto_mode": enabled})

    @app.get("/api/state")
    async def app_state_summary() -> JSONResponse:
        return JSONResponse(
            {
                "subscription_ack_at": acks.get_state(s.repo_root).get(
                    "subscription_ack_at"
                ),
                "full_auto_mode": acks.full_auto_mode(s.repo_root),
                "lock_holder": (
                    {
                        "thread_id": s.lock.holder.thread_id,
                        "phase": s.lock.holder.phase,
                        "acquired_at": s.lock.holder.acquired_at,
                    }
                    if s.lock.holder
                    else None
                ),
            }
        )


# --------------------------------------------------------------- helpers


def _require_thread(repo_root: Path, thread_id: str) -> dict[str, Any]:
    try:
        return threads.load_thread(repo_root, thread_id)
    except threads.ThreadError as exc:
        raise HTTPException(404, str(exc))


def _next_attempt_number(thread_root: Path, phase: str) -> int:
    """Return N such that ``<thread_root>/<phase>.attempt<N>/`` is unused.

    Scans for existing ``<phase>.attempt<digit>`` siblings to pick the
    next free index. Starts at 1.
    """
    existing: list[int] = []
    prefix = f"{phase}.attempt"
    for child in thread_root.iterdir():
        if child.is_dir() and child.name.startswith(prefix):
            suffix = child.name[len(prefix) :]
            if suffix.isdigit():
                existing.append(int(suffix))
    return (max(existing) + 1) if existing else 1


def _refuse_if_lock_held_by_other(s: AppState, thread_id: str) -> None:
    """Refuse a launch synchronously when another thread holds the lock.

    Without this guard, two threads could both POST .../start, the second
    succeeds at the HTTP layer (s.sessions[B] = session, task scheduled),
    then the task fails inside run_loop with LockBusyError and silently
    flips B's phase_status back to idle — leaving the user confused. The
    sync refusal puts the contention error in the response where it belongs.
    """
    holder = s.lock.holder
    if holder is not None and holder.thread_id != thread_id:
        raise HTTPException(
            409,
            (
                f"another thread is currently live: {holder.thread_id} / "
                f"{holder.phase} (since {holder.acquired_at}). Single-GPU "
                "constraint allows only one live phase at a time."
            ),
        )


def _require_subscription_ack(repo_root: Path) -> None:
    if not acks.has_subscription_ack(repo_root):
        raise HTTPException(
            403,
            "subscription_ack required; grant it at /api/settings/subscription_ack "
            "before launching a live phase",
        )


async def _maybe_json(req: Request) -> dict[str, Any]:
    ctype = req.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            body = await req.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}
    try:
        form = await req.form()
        return dict(form)
    except Exception:
        return {}


async def _sse_stream(session: LiveSession):
    """Async generator that drains LiveSession.out_queue as SSE frames.

    On attach, **drain any backlog first**. Items in the queue from a
    previous consumer (canceled when the user navigated away) are stale
    — the page that just re-rendered already reflects the current state
    from disk (rounds, pending_ask, scaffold_state). Replaying those
    buffered events would create visual duplicates (e.g. Q3 once from
    the server-rendered chat list, then again from the SSE).
    Live state on disk is the ground truth; SSE is for *future* updates.
    """
    while not session.out_queue.empty():
        try:
            session.out_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    while True:
        event = await session.out_queue.get()
        payload = json.dumps(event, ensure_ascii=False)
        yield f"data: {payload}\n\n"
        if event.get("type") in {"phase_complete", "phase_failed"}:
            # one terminal event then close — client EventSource will reopen
            # only if explicitly re-fetched, which is what we want
            break


# --------------------------------------------------------------- phase launchers


async def _launch_grilling(
    s: AppState, index: dict[str, Any], *, mode: str
) -> None:
    thread_id = index["thread_id"]
    if thread_id in s.sessions:
        # Already running in-process; the operator probably just clicked
        # Resume because their SSE dropped. Idempotent no-op — the running
        # agent is fine, the client just needs to reopen the SSE stream
        # (which it does after the launch POST resolves and the page
        # reload + DOMContentLoaded handler in app.js fires).
        return
    _refuse_if_lock_held_by_other(s, thread_id)

    loop = asyncio.get_running_loop()
    session = LiveSession(thread_id=thread_id, phase="grilling", loop=loop)
    s.sessions[thread_id] = session

    threads.update_thread(
        s.repo_root,
        thread_id,
        phase_status="running",
        append_execute_ack=acks.make_execute_ack_record("grilling", mode=mode),
    )

    run_dir = threads.phase_dir(s.repo_root, thread_id, "grilling")
    run_dir.mkdir(parents=True, exist_ok=True)

    # Resume support: if a prior in-progress session exists, seed its rounds.
    session_path = run_dir / "grilling_session.json"
    prior = load_resumable_session(session_path)
    initial_rounds = prior["rounds"] if prior else None
    initial_usage = prior["usage_estimate"] if prior else None
    session_id = prior["session_id"] if prior else None
    created_at_override = prior["created_at"] if prior else None

    def sync_input_provider(question: str) -> str:
        # Called from the worker thread. Push ASK and await reply.
        asyncio.run_coroutine_threadsafe(
            session.out_queue.put({"type": "ask", "question": question}),
            loop,
        ).result()
        threads.update_thread(
            s.repo_root, thread_id, phase_status="awaiting_input"
        )
        fut = asyncio.run_coroutine_threadsafe(session.wait_for_reply(), loop)
        reply = fut.result()
        threads.update_thread(s.repo_root, thread_id, phase_status="running")
        return reply

    def sync_event_emitter(event: dict) -> None:
        # Push scaffold lifecycle events into the SSE stream and mirror
        # status into thread.json so the sidebar / page-reload renders
        # the right badge even without an active SSE listener.
        asyncio.run_coroutine_threadsafe(
            session.out_queue.put(event), loop
        ).result()
        kind = event.get("type")
        if kind == "scaffold_start":
            threads.update_thread(
                s.repo_root,
                thread_id,
                domain_state="scaffolding",
            )
        elif kind == "scaffold_complete":
            threads.update_thread(
                s.repo_root,
                thread_id,
                domain=event.get("domain_slug"),
                domain_state="scaffold_complete",
            )

    async def run_loop():
        try:
            async with s.lock.acquire(thread_id, "grilling"):
                result = await asyncio.to_thread(
                    run_grilling_session,
                    s.repo_root,
                    user_goal=index["user_goal"],
                    run_dir=run_dir,
                    session_id=session_id,
                    billing_ack=True,
                    execution_ack=True,
                    input_provider=sync_input_provider,
                    initial_rounds=initial_rounds,
                    initial_usage=initial_usage,
                    created_at_override=created_at_override,
                    command_runner=s.command_runner_override,
                    event_emitter=sync_event_emitter,
                )
            if result["status"] in {"done", "max_rounds_reached"}:
                # Derive domain_state from the scaffold_state on result.
                scaffold = result.get("scaffold_state")
                if scaffold and scaffold.get("finalized"):
                    new_domain_state = "scaffold_complete"
                elif scaffold and scaffold.get("active"):
                    new_domain_state = "scaffold_failed"
                else:
                    new_domain_state = "matched"
                threads.update_thread(
                    s.repo_root,
                    thread_id,
                    phase_status="complete",
                    domain=result["extracted"].get("domain"),
                    domain_state=new_domain_state,
                )
                await session.emit({"type": "phase_complete"})
            else:
                threads.update_thread(
                    s.repo_root, thread_id, phase_status="failed"
                )
                await session.emit(
                    {"type": "phase_failed", "error": result.get("error")}
                )
        except LockBusyError as exc:
            threads.update_thread(s.repo_root, thread_id, phase_status="idle")
            await session.emit({"type": "phase_failed", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("grilling failed for %s", thread_id)
            threads.update_thread(s.repo_root, thread_id, phase_status="failed")
            await session.emit({"type": "phase_failed", "error": str(exc)})
        finally:
            s.sessions.pop(thread_id, None)

    session.task = asyncio.create_task(run_loop())


async def _launch_market(s: AppState, index: dict[str, Any]) -> None:
    thread_id = index["thread_id"]
    if thread_id in s.sessions:
        # Idempotent: market is already running for this thread.
        return
    _refuse_if_lock_held_by_other(s, thread_id)
    grilling_path = (
        threads.phase_dir(s.repo_root, thread_id, "grilling")
        / "grilling_session.json"
    )
    if not grilling_path.exists():
        raise HTTPException(409, "market requires grilling to be complete")
    grilling_session = json.loads(grilling_path.read_text(encoding="utf-8"))
    validate_named_schema("grilling_session", grilling_session)

    loop = asyncio.get_running_loop()
    session = LiveSession(thread_id=thread_id, phase="market", loop=loop)
    s.sessions[thread_id] = session

    threads.update_thread(
        s.repo_root,
        thread_id,
        current_phase="market",
        phase_status="running",
    )
    run_dir = threads.phase_dir(s.repo_root, thread_id, "market")
    run_dir.mkdir(parents=True, exist_ok=True)

    async def run_loop():
        try:
            async with s.lock.acquire(thread_id, "market"):
                await asyncio.to_thread(
                    run_market_research,
                    s.repo_root,
                    grilling_session,
                    run_dir=run_dir,
                    write_dossier_to_memory=True,
                )
            threads.update_thread(
                s.repo_root, thread_id, phase_status="complete"
            )
            await session.emit({"type": "phase_complete"})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("market failed for %s", thread_id)
            threads.update_thread(s.repo_root, thread_id, phase_status="failed")
            await session.emit({"type": "phase_failed", "error": str(exc)})
        finally:
            s.sessions.pop(thread_id, None)

    session.task = asyncio.create_task(run_loop())


async def _launch_production(s: AppState, index: dict[str, Any]) -> None:
    """Production launch is always refused from the frontend.

    Reasoning happens inside Claude Code interactive via the MCP server.
    The operator copies the handoff command from the 'Advance to
    production →' modal and runs it in a separate terminal. The legacy
    in-process production_runner path has been removed.
    """
    del s, index  # parameters kept for API compatibility with the route caller
    raise HTTPException(
        409,
        "Production launch is disabled from the frontend. Reasoning happens "
        "inside Claude Code interactive via the MCP server. Use the "
        "'Advance to production →' modal to copy the MCP handoff command, "
        "then run it in a separate terminal.",
    )


# --------------------------------------------------------------- phase data


def _read_supervisor_state(repo_root: Path, thread_id: str) -> dict[str, Any]:
    """PR10: surface thread_supervisor state for the production panel.

    Returns a dict the template can consume:
      - running: bool
      - pid: int | None
      - log_tail: list[str] (last ~30 lines of supervisor.log)
      - subprocess_log_tail: list[str] (last ~10 lines of claude_subprocess.log)
      - needed_resources: list[dict] (parsed from needed_resources.yaml)
      - target_scope: str | None (parsed from supervisor.log if present)
    """
    tdir = repo_root / "runs" / "threads" / thread_id
    out: dict[str, Any] = {
        "running": False,
        "pid": None,
        "log_tail": [],
        "subprocess_log_tail": [],
        "needed_resources": [],
        "target_scope": None,
    }

    # Liveness from lock file.
    lock_path = tdir / ".supervisor.lock"
    if lock_path.exists():
        try:
            pid = int(lock_path.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            out["running"] = True
            out["pid"] = pid
        except (OSError, ValueError, ProcessLookupError):
            pass

    # Supervisor log tail (last 30 lines).
    log_path = tdir / "supervisor.log"
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            out["log_tail"] = lines[-30:]
            for ln in reversed(lines):
                if "target_scope=" in ln:
                    # crude parse — find target_scope=...
                    idx = ln.find("target_scope=")
                    tail = ln[idx + len("target_scope="):]
                    # strip trailing chars / quotes / brackets
                    out["target_scope"] = tail.split()[0].strip().strip("',\"")
                    break
        except OSError:
            pass

    # Claude subprocess log tail (last 10 lines — heavy, keep small).
    subproc_log = tdir / "claude_subprocess.log"
    if subproc_log.exists():
        try:
            data = subproc_log.read_bytes()
            text = data.decode("utf-8", errors="replace")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            out["subprocess_log_tail"] = lines[-10:]
        except OSError:
            pass

    # needed_resources.yaml — parse with the harness's simple parser.
    nr_path = tdir / "needed_resources.yaml"
    if nr_path.exists():
        try:
            from research_harness.config import parse_simple_yaml
            parsed = parse_simple_yaml(nr_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("needs"), list):
                out["needed_resources"] = parsed["needs"]
        except Exception:  # noqa: BLE001
            pass

    return out


def _collect_phase_data(repo_root: Path, thread_id: str) -> dict[str, Any]:
    """Read every phase artifact present on disk and pass to templates."""
    out: dict[str, Any] = {}
    for phase in threads.PHASES:
        out[phase] = _read_phase_artifacts(repo_root, thread_id, phase)
    # Tell every phase template which orchestrator backend is configured so
    # they can surface mcp-mode banners and disable direct launch affordances.
    try:
        settings = load_settings(repo_root)
    except (OSError, ValueError, json.JSONDecodeError):
        settings = {}
    backend = (
        settings.get("runtime", {})
        .get("llm_orchestrator", {})
        .get("backend", "mock")
    )
    for phase_data in out.values():
        if isinstance(phase_data, dict):
            phase_data["llm_backend"] = backend
    return out


def _read_phase_artifacts(
    repo_root: Path, thread_id: str, phase: str
) -> dict[str, Any]:
    pdir = threads.phase_dir(repo_root, thread_id, phase)
    if not pdir.exists():
        return {"present": False}
    result: dict[str, Any] = {"present": True, "files": []}
    for path in sorted(pdir.iterdir()):
        if path.is_file():
            result["files"].append(path.name)
    if phase == "grilling":
        sp = pdir / "grilling_session.json"
        if sp.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                result["session"] = json.loads(sp.read_text(encoding="utf-8"))
    elif phase == "market":
        bp = pdir / "market_research_brief.json"
        if bp.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                result["brief"] = json.loads(bp.read_text(encoding="utf-8"))
        md = pdir / "baseline_analysis.md"
        if md.exists():
            with contextlib.suppress(OSError):
                result["baseline_md"] = md.read_text(encoding="utf-8")
    elif phase == "production":
        sp = pdir / "production_run_summary.json"
        if sp.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                result["summary"] = json.loads(sp.read_text(encoding="utf-8"))
        # interactive_summary.html is emitted into publication/ by the
        # publish dispatcher, not into the production root.
        candidates = [
            pdir / "publication" / "interactive_summary.html",
            pdir / "interactive_summary.html",  # legacy / fallback
        ]
        for cand in candidates:
            if cand.exists():
                rel = cand.relative_to(pdir).as_posix()
                result["interactive_html_url"] = (
                    f"/files/{thread_id}/production/{rel}"
                )
                break
        # NEW: claim-tree + per-node dialog so the operator can see the
        # search structure and the natural-language back-and-forth between
        # the Professor and the GradStudent.
        tree_state_path = pdir / "tree" / "search_state.json"
        if tree_state_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                state = json.loads(tree_state_path.read_text(encoding="utf-8"))
                result["tree_state"] = _summarize_tree_state(state)
        tree_summary_path = pdir / "tree" / "tree_search_summary.json"
        if tree_summary_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                ts = json.loads(tree_summary_path.read_text(encoding="utf-8"))
                result["readiness_history"] = ts.get("readiness_history", [])
                result["coverage_by_node_type"] = ts.get(
                    "coverage_by_node_type", {}
                )
                result["stage_history"] = ts.get("stage_history", [])
        intake_path = pdir / "intake_to_claim_dialog.json"
        if intake_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                result["intake_to_claim"] = json.loads(
                    intake_path.read_text(encoding="utf-8")
                )
        # Per-node dialog files keyed by node_id, for the UI to render
        # on-click.
        node_dialogs: dict[str, list[dict[str, Any]]] = {}
        for dp in sorted((pdir / "tree" / "nodes").glob("*/dialog.json")):
            with contextlib.suppress(json.JSONDecodeError, OSError):
                d = json.loads(dp.read_text(encoding="utf-8"))
                node_dialogs[dp.parent.name] = d.get("entries", [])
        if node_dialogs:
            result["node_dialogs"] = node_dialogs
        # In-progress nodes — anything Claude Code is mid-way through.
        # Mirrors the resume-aware selector in mcp_server so the operator
        # sees the same state machine.
        mid_states = {
            "running": "execute_node_experiment",
            "completed_worker_report": "run_critic_reviews",
            "critic_reviewed": "submit_professor_decision",
            "orchestrator_reduced": "submit_professor_decision",
        }
        in_progress: list[dict[str, Any]] = []
        for n in (result.get("tree_state") or {}).get("nodes", []):
            if n.get("status") in mid_states:
                in_progress.append(
                    {
                        "node_id": n["id"],
                        "node_type": n.get("type"),
                        "status": n["status"],
                        "next_tool": mid_states[n["status"]],
                        "claim_under_test": n.get("claim_under_test", ""),
                    }
                )
        if in_progress:
            result["in_progress_nodes"] = in_progress
        # Last-activity timestamp across all MCP commits so the UI can
        # show "last update Xs ago" honestly.
        last_activity = 0.0
        for cand in [
            pdir / "tree" / "search_state.json",
            pdir / "intake_to_claim_dialog.json",
            pdir / "production_run_summary.json",
        ]:
            if cand.exists():
                last_activity = max(last_activity, cand.stat().st_mtime)
        for dp in (pdir / "tree" / "nodes").glob("*/*.json"):
            if dp.is_file():
                last_activity = max(last_activity, dp.stat().st_mtime)
        if last_activity:
            result["last_activity_mtime"] = last_activity
        # PR10: supervisor state for the production panel.
        result["supervisor"] = _read_supervisor_state(repo_root, thread_id)
        # PR10b: distinguish "fresh thread" / "resumable mid-state" /
        # "publication complete" so the supervisor card can render the
        # correct button label. Resume = activity present, paper not yet.
        paper_path = pdir / "publication" / "paper.html"
        honest_failure_path = pdir / "publication" / "honest_failure.html"
        result["publication_exists"] = paper_path.exists() or honest_failure_path.exists()
        # "production has activity" = at least one MCP-produced file under
        # production/. We exclude the auto-bootstrapped feasibility
        # envelope because a fresh supervisor start writes it before any
        # real work — counting it as activity would mislabel every
        # supervisor-touched fresh thread as "resumable".
        result["has_production_activity"] = (
            (pdir / "tree" / "search_state.json").exists()
            or (pdir / "rebuttal").exists()
            or (pdir / "_drafts").exists()
            or (pdir / "production_run_summary.json").exists()
        )
        # MCP-mode progress: list per-node MCP decision files + their
        # mtimes so the operator can see Claude Code's last action.
        mcp_progress: list[dict[str, Any]] = []
        for dp in sorted((pdir / "tree" / "nodes").glob("*/mcp_professor_decision.json")):
            with contextlib.suppress(OSError, json.JSONDecodeError):
                d = json.loads(dp.read_text(encoding="utf-8"))
                mcp_progress.append(
                    {
                        "node_id": dp.parent.name,
                        "transition": d.get("next_transition"),
                        "final_verdict": d.get("final_verdict"),
                        "follow_up_count": len(d.get("follow_up_children") or []),
                        "updated_at": dp.stat().st_mtime,
                    }
                )
        if mcp_progress:
            result["mcp_progress"] = sorted(
                mcp_progress, key=lambda x: x["updated_at"], reverse=True
            )
    return result


def _summarize_tree_state(state: dict[str, Any]) -> dict[str, Any]:
    """Compact view of search_state for the frontend tree visualizer."""
    nodes = []
    for n in state.get("nodes", []):
        nodes.append(
            {
                "id": n["id"],
                "type": n.get("type"),
                "status": n.get("status"),
                "parent": n.get("parent"),
                "claim_under_test": (
                    n.get("claim_contract", {}) or {}
                ).get("claim_under_test", "")[:240],
                "verdict": (n.get("outputs", {}) or {}).get("verdict"),
            }
        )
    return {
        "search_id": state.get("search_id"),
        "status": state.get("status"),
        "nodes": nodes,
        "promoted_node_ids": state.get("promoted_node_ids", []),
        "pruned_node_ids": state.get("pruned_node_ids", []),
        "completed_node_ids": state.get("completed_node_ids", []),
    }


# --------------------------------------------------------------- file serving


def _register_file_route(app: FastAPI, s: AppState) -> None:
    """Serve files from within a thread's run directory for iframe embeds."""
    from fastapi.responses import FileResponse

    @app.get("/files/{thread_id}/{phase}/{filename:path}")
    async def _serve(thread_id: str, phase: str, filename: str) -> FileResponse:
        if phase not in threads.PHASES:
            raise HTTPException(404)
        # Stay inside the thread's phase directory — block path traversal.
        pdir = threads.phase_dir(s.repo_root, thread_id, phase)
        candidate = (pdir / filename).resolve()
        if not str(candidate).startswith(str(pdir.resolve())):
            raise HTTPException(404)
        if not candidate.is_file():
            raise HTTPException(404)
        return FileResponse(str(candidate))
