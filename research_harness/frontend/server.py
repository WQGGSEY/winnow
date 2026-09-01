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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
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
from research_harness.frontend import acks, datasets, threads
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
    # `command_runner`, bypassing the real Codex CLI. Production code never
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
    _register_datasets_routes(app, state)
    _register_settings_routes(app, state)
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
        if phase not in threads.RECOGNIZED_PHASES:
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
        # Free-form would let typos through and silently send Codex a
        # nonexistent model id. Operators expand allowed_models in
        # settings.json when a new model ships.
        if not allowed:
            raise HTTPException(
                500,
                "settings.runtime.llm_orchestrator.mcp.allowed_models is "
                "empty; cannot accept any model. Populate the list with "
                "valid Codex model IDs.",
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

    @app.get("/api/threads/{thread_id}/operator_prompts")
    async def operator_prompts_list(thread_id: str) -> JSONResponse:
        from research_harness.orchestrator.operator_prompts import list_pending
        _require_thread(s.repo_root, thread_id)
        tdir = s.repo_root / "runs" / "threads" / thread_id
        return JSONResponse({"pending": list_pending(tdir)})

    @app.post("/api/threads/{thread_id}/operator_input")
    async def operator_input_submit(thread_id: str, req: Request) -> JSONResponse:
        from research_harness.orchestrator.operator_prompts import submit_response
        _require_thread(s.repo_root, thread_id)
        form = await req.form()
        event_id = (form.get("event_id") or "").strip()
        response = (form.get("response") or "").strip()
        if not event_id:
            raise HTTPException(400, "event_id is required")
        if not response:
            raise HTTPException(400, "response must not be empty")
        tdir = s.repo_root / "runs" / "threads" / thread_id
        try:
            submit_response(tdir, event_id=event_id, response=response)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse({"ok": True, "event_id": event_id})

    @app.post("/api/threads/{thread_id}/{phase}/retry")
    async def phase_retry(thread_id: str, phase: str, req: Request) -> JSONResponse:
        """Re-run a phase that ended in ``phase_status == "failed"``.

        Archives the failed phase directory to a sibling
        ``<phase>.attempt<N>/`` so the operator can still inspect what
        went wrong, then re-launches the phase fresh. Works for any
        phase including upstream failures we can't anticipate
        (Codex runtime failures, rate limits, content-policy hits, network
        drops). Without this the only escape from a failed phase was to
        hand-edit thread.json or delete the thread entirely.
        """
        if phase not in threads.RECOGNIZED_PHASES:
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
            # ADR 0012: the connector is meant to be re-run freely while testing
            # (re-runs the connector phase only, never grilling). Allow re-run
            # from a completed state too; the prior attempt is archived.
            or (phase == "connector" and index["phase_status"] == "complete")
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
        elif phase == "connector":
            await _launch_connector(s, index)
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

    @app.post("/api/threads/{thread_id}/connector/start")
    async def connector_start(thread_id: str) -> JSONResponse:
        index = _require_thread(s.repo_root, thread_id)
        await _launch_connector(s, index)
        return JSONResponse({"ok": True})

    @app.get("/api/threads/{thread_id}/connector/stream")
    async def connector_stream(thread_id: str) -> StreamingResponse:
        _require_thread(s.repo_root, thread_id)
        session = s.sessions.get(thread_id)
        if session is None or session.phase != "connector":
            raise HTTPException(409, "no live connector session for this thread")
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
        requested_adapter = str(body.get("data_source_anchor") or "").strip() or None
        from research_harness.data_adapters import (
            AdapterError,
            probe_registered_adapters,
            select_snapshot,
        )
        try:
            selected_snapshot = select_snapshot(
                probe_registered_adapters(s.repo_root), requested_adapter
            )
        except AdapterError as exc:
            raise HTTPException(400, str(exc)) from exc
        if target_scope == "deployment" and selected_snapshot is None:
            raise HTTPException(400, "deployment requires one ready registered adapter")
        selected_adapter = (
            selected_snapshot["adapter_id"] if selected_snapshot is not None else None
        )
        from research_harness.thread_supervisor import (
            ensure_existing_envelope_selection_matches,
        )
        try:
            ensure_existing_envelope_selection_matches(
                s.repo_root,
                thread_id,
                target_scope=target_scope,
                data_source_anchor=selected_adapter,
            )
        except AdapterError as exc:
            raise HTTPException(409, str(exc)) from exc
        pub_dir = s.repo_root / "runs" / "threads" / thread_id / "production" / "publication"
        if (pub_dir / "paper.html").exists():
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
        if selected_adapter is not None:
            cmd.extend(["--data-source-anchor", selected_adapter])
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
        # Wait briefly for the subprocess to acquire its lock (watch_thread
        # writes .supervisor.lock early, before bootstrap + the idle loop).
        # Returning only once the lock exists means the frontend's immediate
        # refreshProductionPanel() swap renders the running branch (with the
        # live-log section) on the first try — no manual reload, and no
        # dependence on the 4s poll catching the lock-write race.
        running = False
        for _ in range(50):  # up to ~5s
            if lock_path.exists():
                running = True
                break
            if proc.poll() is not None:
                break  # subprocess died during startup — stop waiting
            await asyncio.sleep(0.1)
        return JSONResponse({
            "ok": True,
            "pid": proc.pid,
            "running": running,
            "target_scope": target_scope,
            "data_source_anchor": selected_adapter,
            "log_path": str(stdout_path),
        })

    @app.post("/api/threads/{thread_id}/supervisor/stop")
    async def supervisor_stop(thread_id: str) -> JSONResponse:
        """Stop the supervisor with automatic SIGTERM → SIGKILL
        escalation. Always succeeds when called against a real lock —
        no second 'Force kill' button needed.

        Flow:
          1. SIGTERM → supervisor's handler cascades to its active
             Codex subprocess. Typical exit: 1-3 seconds.
          2. Poll PID liveness for up to 3 seconds (0.3s intervals).
          3. If still alive: SIGKILL.
          4. Unlink lock file if it survived (it shouldn't, but
             belt-and-suspenders so the UI returns to idle reliably).
        """
        _require_thread(s.repo_root, thread_id)
        lock_path = s.repo_root / "runs" / "threads" / thread_id / ".supervisor.lock"
        if not lock_path.exists():
            raise HTTPException(404, "no supervisor running for this thread")
        try:
            pid = int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise HTTPException(
                409, "supervisor lock file is corrupt; cleaned up — try again"
            )

        signals_sent: list[str] = []
        try:
            os.kill(pid, signal.SIGTERM)
            signals_sent.append("SIGTERM")
        except ProcessLookupError:
            # Already dead; just clean up the lock.
            try:
                lock_path.unlink()
            except OSError:
                pass
            return JSONResponse(
                {"ok": True, "signaled_pid": pid, "signals_sent": [], "note": "pid already dead; cleaned stale lock"}
            )

        # Poll for graceful exit. 0.3s × 10 = 3s max wait.
        await asyncio.sleep(0.3)
        for _ in range(10):
            try:
                os.kill(pid, 0)  # liveness probe
            except ProcessLookupError:
                break
            await asyncio.sleep(0.3)
        else:
            # Loop completed without break — process still alive after 3s.
            try:
                os.kill(pid, signal.SIGKILL)
                signals_sent.append("SIGKILL")
            except ProcessLookupError:
                pass

        # Reap reasonable-grace cleanup, then ensure lock is gone.
        await asyncio.sleep(0.2)
        try:
            if lock_path.exists():
                lock_path.unlink()
        except OSError:
            pass

        return JSONResponse(
            {"ok": True, "stopped_pid": pid, "signals_sent": signals_sent}
        )

    @app.get("/api/threads/{thread_id}/production/stream")
    async def production_stream(thread_id: str) -> StreamingResponse:
        _require_thread(s.repo_root, thread_id)
        session = s.sessions.get(thread_id)
        if session is None or session.phase != "production":
            raise HTTPException(409, "no live production session for this thread")
        return StreamingResponse(
            _sse_stream(session), media_type="text/event-stream"
        )

    @app.get("/api/threads/{thread_id}/supervisor/log/stream")
    async def supervisor_log_stream(thread_id: str) -> StreamingResponse:
        """Tail-follow supervisor.log + codex_subprocess.log as SSE.

        Initial: emits one `snapshot` event per file with the last 200
        lines. Then polls every 500ms and emits one `append` event per
        new line. Heartbeat ping every 15s keeps proxies from closing
        the stream. Survives the files not existing yet (they'll be
        created when the supervisor starts).
        """
        _require_thread(s.repo_root, thread_id)
        tdir = s.repo_root / "runs" / "threads" / thread_id
        return StreamingResponse(
            _tail_supervisor_logs(tdir),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
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


async def _tail_supervisor_logs(tdir: Path):
    """Tail two append-only logs (supervisor.log + codex_subprocess.log)
    as SSE. Emits a `snapshot` event per file with last 200 lines on
    connect, then `append` events per new line every 500ms.

    Files may not exist yet; we poll until they appear. If a file
    shrinks (rotation/truncation), we reset its offset to 0 and resync.
    """
    SNAPSHOT_LINES = 200
    POLL = 0.5
    HEARTBEAT = 5.0
    sup_path = tdir / "supervisor.log"
    sub_path = tdir / "codex_subprocess.log"

    state = {
        "supervisor": {"path": sup_path, "offset": 0, "snapshotted": False, "buf": ""},
        "subprocess": {"path": sub_path, "offset": 0, "snapshotted": False, "buf": ""},
    }

    # Send a first byte immediately. Without this the browser sees no data
    # until either a snapshot fires (file exists) or 5s heartbeat elapses;
    # some proxies (and EventSource impls under load) treat that silence
    # as a failed connect and trigger `onerror` → infinite reconnect loop.
    yield ": connected\n\n"
    last_beat = time.time()

    def _snapshot(p: Path) -> tuple[int, list[str]]:
        try:
            data = p.read_bytes()
        except OSError:
            return (0, [])
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        # If the file doesn't end with newline, the last line is partial;
        # exclude it from the snapshot so the live-tail isn't out of sync.
        if data and not data.endswith(b"\n") and lines:
            lines = lines[:-1]
        return (len(data), lines[-SNAPSHOT_LINES:])

    def _new_lines(st: dict) -> list[str]:
        p: Path = st["path"]
        try:
            size = p.stat().st_size
        except OSError:
            return []
        if size < st["offset"]:
            # rotated/truncated — resync from start
            st["offset"] = 0
            st["buf"] = ""
        if size == st["offset"]:
            return []
        try:
            with p.open("rb") as f:
                f.seek(st["offset"])
                chunk = f.read(size - st["offset"])
            st["offset"] = size
        except OSError:
            return []
        st["buf"] += chunk.decode("utf-8", errors="replace")
        parts = st["buf"].split("\n")
        # Last element is the partial line (or "" if chunk ended with \n).
        st["buf"] = parts[-1]
        return parts[:-1]

    try:
        while True:
            try:
                for kind, st in state.items():
                    p: Path = st["path"]
                    if not st["snapshotted"]:
                        if not p.exists():
                            continue
                        offset, lines = _snapshot(p)
                        st["offset"] = offset
                        st["snapshotted"] = True
                        payload = json.dumps(
                            {"kind": kind, "lines": lines}, ensure_ascii=False
                        )
                        yield f"event: snapshot\ndata: {payload}\n\n"
                        continue
                    new = _new_lines(st)
                    for ln in new:
                        payload = json.dumps(
                            {"kind": kind, "line": ln}, ensure_ascii=False
                        )
                        yield f"event: append\ndata: {payload}\n\n"
            except (OSError, UnicodeError, ValueError):
                # Per-cycle hiccup (race on file delete, decode glitch, etc.) —
                # swallow and keep the SSE alive; next poll will try again.
                pass

            now = time.time()
            if now - last_beat > HEARTBEAT:
                yield ": ping\n\n"
                last_beat = now

            await asyncio.sleep(POLL)
    except asyncio.CancelledError:
        # Client disconnected — exit cleanly so the task is reaped.
        return


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


async def _launch_connector(s: AppState, index: dict[str, Any]) -> None:
    """ADR 0012 domain-connector phase: P -> diverse far-framed claim_contracts.

    Mirrors _launch_market (background, no user input) but uses a live agent, so it
    passes acks=True — the operator's per-phase execute-ack modal click is the
    consent. Reads the thread's grilling_session and writes connector_session.json
    under the connector/ phase dir."""
    from research_harness.connector.orchestrator import run_domain_connector

    thread_id = index["thread_id"]
    if thread_id in s.sessions:
        return
    _refuse_if_lock_held_by_other(s, thread_id)
    grilling_path = (
        threads.phase_dir(s.repo_root, thread_id, "grilling") / "grilling_session.json"
    )
    if not grilling_path.exists():
        raise HTTPException(409, "connector requires grilling to be complete")
    grilling_session = json.loads(grilling_path.read_text(encoding="utf-8"))
    validate_named_schema("grilling_session", grilling_session)

    loop = asyncio.get_running_loop()
    session = LiveSession(thread_id=thread_id, phase="connector", loop=loop)
    s.sessions[thread_id] = session

    threads.update_thread(
        s.repo_root, thread_id, current_phase="connector", phase_status="running"
    )
    run_dir = threads.phase_dir(s.repo_root, thread_id, "connector")
    run_dir.mkdir(parents=True, exist_ok=True)

    def sync_event_emitter(event: dict) -> None:
        # Bridge per-step connector progress (emitted from the worker thread)
        # onto the SSE stream the operator's browser is watching live.
        asyncio.run_coroutine_threadsafe(session.out_queue.put(event), loop).result()

    async def run_loop():
        try:
            async with s.lock.acquire(thread_id, "connector"):
                outcome = await asyncio.to_thread(
                    run_domain_connector,
                    s.repo_root,
                    grilling_session,
                    run_dir=run_dir,
                    billing_ack=True,
                    execution_ack=True,
                    event_emitter=sync_event_emitter,
                )
            status = (outcome.session or {}).get("status")
            if status in {"aborted", "blocked_by_gate", "blocked_by_execution_ack"}:
                # run_domain_connector RETURNS (does not raise) on these, so mark
                # the phase failed here — otherwise an aborted run (e.g. a 401)
                # would look "complete" and hide the Retry button.
                threads.update_thread(s.repo_root, thread_id, phase_status="failed")
                await session.emit(
                    {"type": "phase_failed", "error": (outcome.session or {}).get("error")}
                )
            else:
                threads.update_thread(s.repo_root, thread_id, phase_status="complete")
                await session.emit({"type": "phase_complete"})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("connector failed for %s", thread_id)
            threads.update_thread(s.repo_root, thread_id, phase_status="failed")
            await session.emit({"type": "phase_failed", "error": str(exc)})
        finally:
            s.sessions.pop(thread_id, None)

    session.task = asyncio.create_task(run_loop())


async def _launch_production(s: AppState, index: dict[str, Any]) -> None:
    """Production launch is always refused from the frontend.

    Reasoning happens inside Codex through the per-invocation MCP server.
    The operator copies the handoff command from the 'Advance to
    production →' modal and runs it in a separate terminal. The legacy
    in-process production_runner path has been removed.
    """
    del s, index  # parameters kept for API compatibility with the route caller
    raise HTTPException(
        409,
        "Production launch is disabled at this endpoint. Use the supervisor "
        "card on the Production tab — click ▶ Start supervisor. The "
        "supervisor spawns Codex subprocesses under your login "
        "pool, auto-bootstraps the feasibility envelope, and drives "
        "production to dual-gate publish hands-free.",
    )


# --------------------------------------------------------------- phase data


def _read_supervisor_state(repo_root: Path, thread_id: str) -> dict[str, Any]:
    """PR10: surface thread_supervisor state for the production panel.

    Returns a dict the template can consume:
      - running: bool
      - pid: int | None
      - log_tail: list[str] (last ~30 lines of supervisor.log)
      - subprocess_log_tail: list[str] (last ~10 lines of codex_subprocess.log)
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

    # Codex subprocess log tail (last 10 lines; keep this view small).
    subproc_log = tdir / "codex_subprocess.log"
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
        # Live elapsed-counter anchor for the "Market-research agent is
        # searching…" indicator. Use the market dir's mtime (set when
        # the launch route mkdir's it) as the phase-start anchor; that
        # mutates exactly once per phase start, including on retry.
        with contextlib.suppress(OSError):
            result["phase_started_unix"] = pdir.stat().st_mtime
    elif phase == "connector":
        sp = pdir / "connector_session.json"
        if sp.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                result["session"] = json.loads(sp.read_text(encoding="utf-8"))
        with contextlib.suppress(OSError):
            result["phase_started_unix"] = pdir.stat().st_mtime
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
        # In-progress nodes: anything Codex is processing.
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
        if not result["supervisor"]["running"]:
            with contextlib.suppress(Exception):
                from research_harness.data_adapters import adapter_status_rows
                result["ready_adapters"] = [
                    row
                    for row in adapter_status_rows(repo_root)
                    if row.get("status") == "ready"
                ]
        with contextlib.suppress(Exception):
            from research_harness.orchestrator.operator_prompts import list_pending
            tdir = repo_root / "runs" / "threads" / thread_id
            pending = list_pending(tdir)
            if pending:
                result["pending_operator_prompts"] = pending
        paper_path = pdir / "publication" / "paper.html"
        result["publication_exists"] = paper_path.exists()
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
        # mtimes so the operator can see Codex's last action.
        mcp_progress: list[dict[str, Any]] = []
        for dp in sorted((pdir / "tree" / "nodes").glob("*/mcp_professor_decision.json")):
            with contextlib.suppress(OSError, json.JSONDecodeError):
                d = json.loads(dp.read_text(encoding="utf-8"))
                mcp_progress.append(
                    {
                        "node_id": dp.parent.name,
                        "transition": d.get("next_transition"),
                        "final_verdict": d.get("final_verdict"),
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
        if phase not in threads.RECOGNIZED_PHASES:
            raise HTTPException(404)
        # Stay inside the thread's phase directory — block path traversal.
        pdir = threads.phase_dir(s.repo_root, thread_id, phase)
        candidate = (pdir / filename).resolve()
        if not str(candidate).startswith(str(pdir.resolve())):
            raise HTTPException(404)
        if not candidate.is_file():
            raise HTTPException(404)
        return FileResponse(str(candidate))


# --------------------------------------------------------------- datasets (Phase 1b)


def _register_datasets_routes(app: FastAPI, s: AppState) -> None:
    """Operator-scope data adapter management: list, upload, register-by-path, delete.

    Writes land in ``settings.local.json`` under ``data_adapters.registered``.
    Upload streams the file into ``.dataset_cache/operator_uploads/<id>/`` so
    the local-path materializer can pick it up without a second copy.
    """
    env = s.env

    def _render_page(flash: str | None = None, error: str | None = None) -> HTMLResponse:
        tmpl = env.get_template("datasets.html")
        return HTMLResponse(
            tmpl.render(
                adapters=datasets.list_adapters(s.repo_root),
                materializer_types=sorted(datasets.MATERIALIZER_TYPES),
                dataset_roles=sorted(datasets.DATASET_ROLES),
                max_upload_mb=datasets.MAX_UPLOAD_BYTES // (1 << 20),
                flash=flash,
                error=error,
                full_auto_mode=acks.full_auto_mode(s.repo_root),
                has_subscription_ack=acks.has_subscription_ack(s.repo_root),
                ack_state=acks.get_state(s.repo_root),
                lock_holder=s.lock.holder,
            )
        )

    @app.get("/datasets", response_class=HTMLResponse)
    async def datasets_page() -> HTMLResponse:
        return _render_page()

    @app.post("/datasets/upload")
    async def upload_dataset(
        adapter_id: str = Form(...),
        materializer_type: str = Form(...),
        role: str = Form(...),
        provenance: str = Form(...),
        file: UploadFile = File(...),
    ) -> HTMLResponse:
        try:
            result = datasets.upload_file(
                s.repo_root,
                adapter_id=adapter_id,
                original_filename=file.filename or "uploaded.bin",
                source=file.file,
            )
            datasets.register_adapter(
                s.repo_root,
                adapter_id=result.adapter_id,
                materializer_type=materializer_type,
                role=role,
                source=f"file://{result.materialized_path}",
                provenance=provenance,
                upload_meta={
                    "uploaded_at": result.uploaded_at,
                    "sha256": result.sha256,
                    "size_bytes": result.size_bytes,
                    "original_filename": result.original_filename,
                },
            )
        except datasets.DatasetError as exc:
            return _render_page(error=str(exc))
        return _render_page(
            flash=(
                f"Uploaded and registered {result.adapter_id} "
                f"({result.size_bytes:,} bytes, sha256={result.sha256[:12]}…)"
            )
        )

    @app.post("/datasets/register")
    async def register_by_path(
        adapter_id: str = Form(...),
        materializer_type: str = Form(...),
        role: str = Form(...),
        source: str = Form(...),
        provenance: str = Form(...),
    ) -> HTMLResponse:
        try:
            entry = datasets.register_adapter(
                s.repo_root,
                adapter_id=adapter_id,
                materializer_type=materializer_type,
                role=role,
                source=source,
                provenance=provenance,
            )
        except datasets.DatasetError as exc:
            return _render_page(error=str(exc))
        return _render_page(flash=f"Registered {entry['id']}.")

    @app.post("/datasets/{adapter_id}/delete")
    async def delete_dataset(adapter_id: str) -> HTMLResponse:
        try:
            removed = datasets.delete_adapter(s.repo_root, adapter_id)
        except datasets.DatasetError as exc:
            return _render_page(error=str(exc))
        if not removed:
            return _render_page(
                error=f"adapter {adapter_id!r} not found at operator scope (project entries are read-only)"
            )
        return _render_page(flash=f"Removed {adapter_id}.")


# --------------------------------------------------------------- settings (Phase 3)


def _register_settings_routes(app: FastAPI, s: AppState) -> None:
    """FIELD_REGISTRY-driven Settings page (ADR 0005, Phase 3).

    - GET /settings?scope=project|operator|thread&thread_id=... — render
    - POST /api/settings/project|operator — bulk save for that scope
    - POST /api/settings/thread/{thread_id} — add/update one thread override
    - POST /api/settings/thread/{thread_id}/remove — drop one thread override
    """
    from research_harness.settings_scoped import (
        FIELD_REGISTRY,
        coerce_input,
        field_groups,
        fields_for_scope,
        find_spec,
        list_thread_overrides,
        resolve_for_thread,
        validate_resolved,
        validate_write,
        write_setting,
    )

    env = s.env

    def _enum_lookup_for(resolved):
        def lookup(spec):
            if spec.enum is not None:
                return list(spec.enum)
            if spec.enum_source:
                val = resolved.get_dotted(spec.enum_source)
                return list(val) if isinstance(val, list) else None
            return None

        return lookup

    def _render(
        scope: str,
        thread_id: str | None,
        *,
        flash: str | None = None,
        error: str | None = None,
        submitted_values: dict | None = None,
    ) -> HTMLResponse:
        resolved = resolve_for_thread(s.repo_root, thread_id)
        violations = validate_resolved(resolved)
        ctx = {
            "scope": scope,
            "active_thread_id": thread_id,
            "flash": flash,
            "error": error,
            "violations": violations,
            "project_dirty": False,  # git-state check could go here later
            "find_spec_func": find_spec,
            "enum_lookup": _enum_lookup_for(resolved),
        }
        if scope in ("project", "operator"):
            specs = fields_for_scope(scope)  # type: ignore[arg-type]
            grouped = field_groups(specs)
            values = {sp.path: resolved.get_dotted(sp.path) for sp in specs}
            sources = {sp.path: resolved.source_of(sp.path) for sp in specs}
            if submitted_values:
                values.update(submitted_values)
            ctx.update({"grouped": grouped, "values": values, "sources": sources})
        else:
            overrides = list_thread_overrides(s.repo_root, thread_id) if thread_id else {}
            thread_specs = [sp for sp in FIELD_REGISTRY if "thread" in sp.effective_ui_editable_in]
            ctx.update(
                {
                    "thread_overrides": overrides,
                    "thread_overridable": thread_specs,
                }
            )
        tmpl = env.get_template("settings_page.html")
        return HTMLResponse(tmpl.render(**ctx))

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(scope: str = "project", thread_id: str | None = None) -> HTMLResponse:
        if scope not in {"project", "operator", "thread"}:
            scope = "project"
        if scope == "thread" and not thread_id:
            scope = "project"
        return _render(scope, thread_id)

    async def _bulk_save(scope: str, request: Request) -> HTMLResponse:
        """Save every editable field for one scope from one big form."""
        form = await request.form()
        specs = [sp for sp in fields_for_scope(scope) if scope in sp.effective_ui_editable_in]  # type: ignore[arg-type]

        # Phase 1: validate everything. Don't write yet.
        planned: list[tuple[str, object]] = []  # (path, coerced_value | _UNSET)
        submitted_for_redisplay: dict = {}
        for spec in specs:
            raw = form.get(spec.path)
            if spec.type == "boolean":
                # If the bool field's marker is present, treat absence as False.
                marker = form.get(f"__bool__{spec.path}")
                if marker is not None:
                    raw = "true" if form.get(spec.path) else "false"
                elif raw is None:
                    continue  # field wasn't on this form at all
            if raw is None:
                continue
            try:
                value = coerce_input(spec, str(raw))
            except (ValueError, TypeError) as exc:
                return _render(
                    scope, None,
                    error=f"{spec.path}: cannot parse {raw!r} as {spec.type} ({exc})",
                    submitted_values={spec.path: raw},
                )
            # _UNSET sentinel is a non-public marker — detect via singleton identity
            from research_harness.settings_scoped import _UNSET as UNSET

            if value is UNSET:
                planned.append((spec.path, UNSET))
                continue
            try:
                validate_write(
                    spec.path, value, scope,  # type: ignore[arg-type]
                    enum_resolver=lambda p, _r=resolve_for_thread(s.repo_root): (
                        _r.get_dotted(p) if isinstance(_r.get_dotted(p), list) else None
                    ),
                )
            except ValueError as exc:
                submitted_for_redisplay[spec.path] = raw
                return _render(scope, None, error=str(exc), submitted_values=submitted_for_redisplay)
            planned.append((spec.path, value))
            submitted_for_redisplay[spec.path] = raw

        # Phase 2: all valid — write.
        for path, value in planned:
            write_setting(s.repo_root, scope, path, value)  # type: ignore[arg-type]

        return _render(scope, None, flash=f"Saved {len(planned)} field(s) at {scope} scope.")

    @app.post("/api/settings/project", response_class=HTMLResponse)
    async def save_project(request: Request) -> HTMLResponse:
        return await _bulk_save("project", request)

    @app.post("/api/settings/operator", response_class=HTMLResponse)
    async def save_operator(request: Request) -> HTMLResponse:
        return await _bulk_save("operator", request)

    @app.post("/api/settings/thread/{thread_id}", response_class=HTMLResponse)
    async def add_thread_override(thread_id: str, request: Request) -> HTMLResponse:
        form = await request.form()
        path = str(form.get("path", "")).strip()
        raw_value = str(form.get("value", ""))
        if not path:
            return _render("thread", thread_id, error="path is required")
        spec = find_spec(path)
        if spec is None:
            return _render("thread", thread_id, error=f"unknown field {path!r}")
        if "thread" not in spec.writable_at:
            return _render(
                "thread", thread_id,
                error=f"{path!r} is not thread-overridable (writable_at={list(spec.writable_at)})",
            )
        try:
            value = coerce_input(spec, raw_value)
        except (ValueError, TypeError) as exc:
            return _render(
                "thread", thread_id,
                error=f"{path}: cannot parse {raw_value!r} as {spec.type} ({exc})",
            )
        try:
            write_setting(s.repo_root, "thread", path, value, thread_id=thread_id)
        except ValueError as exc:
            return _render("thread", thread_id, error=str(exc))
        return _render("thread", thread_id, flash=f"Added override for {path}.")

    @app.post("/api/settings/thread/{thread_id}/remove", response_class=HTMLResponse)
    async def remove_thread_override(thread_id: str, request: Request) -> HTMLResponse:
        from research_harness.settings_scoped import _UNSET as UNSET

        form = await request.form()
        path = str(form.get("path", "")).strip()
        if not path:
            return _render("thread", thread_id, error="path is required")
        write_setting(s.repo_root, "thread", path, UNSET, thread_id=thread_id)
        return _render("thread", thread_id, flash=f"Removed override for {path}.")
