# ADR 0003 — operator_frontend stack: FastAPI + Jinja2 + HTMX

- **Status**: Accepted
- **Date**: 2026-05-25
- **Supersedes**: none
- **Relates to**: ADR 0002 (in-process process model), `CONTEXT.md`
  (operator_frontend, phase_accordion)

## Context

ADR 0002 fixes the frontend as in-process Python sitting alongside the
existing harness. What it does *not* fix is the rendering stack — the
choice between a server-rendered Python UI and a client-side SPA. That
choice propagates to dev-loop complexity, dependency surface, tooling,
and how code in this repo "feels" to a future contributor.

Two codebase signals pull strongly toward the lighter end of the
spectrum:

1. `pyproject.toml` declares **zero runtime dependencies** for the
   harness core. The author has deliberately kept the harness
   stdlib-only.
2. The existing publication artifact path (`publishing/html.py`,
   `publishing/slides_html.py`) renders HTML by hand using f-strings
   and inline CSS — no Jinja, no template engine. The aesthetic is
   "stdlib + plain HTML".

The frontend cannot be stdlib-only because it needs an async server
capable of SSE streaming for multi-turn agent rounds, and `http.server`
is synchronous and unfit for this. FastAPI is essentially mandated for
the server. The remaining question is what sits on top of FastAPI.

## Decision

The operator frontend uses **FastAPI + Jinja2 + HTMX + vanilla JS
(native `EventSource` for SSE)**. Concretely:

- Server: FastAPI on `uvicorn`, bound to localhost only.
- Templating: Jinja2 (one added dependency beyond FastAPI's transitive
  set).
- Interactivity: HTMX served as a single vendored static file
  (`static/htmx.min.js`) — no CDN, no npm, no build step.
- Streaming: FastAPI `StreamingResponse` with SSE framing; the client
  consumes it via `EventSource` (or `hx-sse` for HTML swaps).
- No Node.js, no Vite, no React/Svelte/Solid, no bundler.

Total added dependency surface beyond stdlib: `fastapi`, `uvicorn`,
`jinja2`.

## Consequences

**Accepted trade-offs**

- HTMX swap-based interactivity is less expressive than a real SPA
  framework. This is acceptable for the MVP feature set (sidebar +
  accordion + chat + tables + iframe + raw-JSON `<details>`). When and
  if richer client state genuinely arrives (drag-reorder, optimistic
  multi-tab edits, etc.), the JSON endpoints under this design are
  reusable by an SPA layer added later.
- Two-language coordination is avoided: all rendering and state
  transitions live in Python. The contributor cost stays low for a
  single-operator project.
- HTMX is a less mainstream stack than React; a hypothetical future
  contributor would have a small learning curve. This is judged
  acceptable because the project is currently single-operator and the
  HTMX surface is intentionally small.

**Rejected: FastAPI + Vite + React/Svelte/SolidJS SPA**

- Introduces `npm` / `node_modules` / a build pipeline that contradicts
  the codebase's deliberate zero-runtime-dep stance.
- Client-state complexity is not high enough to justify the framework
  cost: only one active live phase at a time, a fixed phase pipeline,
  and a single-user audience.
- Two-language friction (TS + Python) on a one-person project.

**Rejected: FastAPI + hand-rolled f-string HTML (no Jinja)**

- Maximally matches `publishing/html.py` aesthetic, but template logic
  (conditional cards per phase, repeated tables, lazy-load accordion
  panels) escapes the readable f-string envelope quickly.
- One added dependency (Jinja2) buys a large legibility improvement.

## Operator usage note

HTMX is vendored as a static file under `static/` rather than loaded
from a CDN. This guarantees the UI works fully offline and removes any
implicit network dependency at page-load time, matching the rest of
the harness which can run without external network access (modulo the
explicit live-Claude calls behind acks).
