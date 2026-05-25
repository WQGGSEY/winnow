"""``python -m research_harness.frontend`` entry point.

Binds the FastAPI app to localhost only — this UI is for a single
operator on a single machine. Use ``--port`` to override the default
8765 if it conflicts.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from research_harness.frontend.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="research_harness.frontend",
        description="Localhost UI for the research harness.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind host; default 127.0.0.1 (localhost only)",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repo root containing runs/threads/ and settings.local.json",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="dev only: auto-reload on file changes",
    )
    args = parser.parse_args()

    app = create_app(args.repo_root)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
