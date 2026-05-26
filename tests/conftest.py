"""Shared pytest fixtures.

Sets RESEARCH_HARNESS_DISABLE_CLAUDE_WEBSEARCH so tests that call
run_market_research don't actually spawn a `claude` subprocess. The
test-suite for the enrichment module itself patches around this via
mock.patch.object(mrc, "_which", ...) etc.
"""

from __future__ import annotations

import os


def pytest_configure(config):  # noqa: ARG001
    os.environ.setdefault("RESEARCH_HARNESS_DISABLE_CLAUDE_WEBSEARCH", "1")
