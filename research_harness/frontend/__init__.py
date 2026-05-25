"""operator_frontend — local single-user UI for the research harness.

This package's top level is intentionally import-light: FastAPI / uvicorn
/ Jinja2 are *not* imported here, so headless users who installed
research-harness without the ``[frontend]`` extra never pay the cost of
those dependencies. The actual server lives in ``server.py`` and the
``__main__`` entry point.

See ``CONTEXT.md`` (operator_frontend, research_thread, thread.json,
single_active_run, phase_accordion, multi_turn_session_persistence,
live_acks) and ADRs 0002/0003 for design rationale.
"""
