"""Claude Code-driven web-search enrichment for market_research.

The legacy in-process path (run_market_research) hits arXiv + Google
Scholar only. This module runs a one-shot `claude -p` (non-interactive)
subprocess that uses Claude Code's native WebSearch + WebFetch tools to
find additional non-academic sources (industry benchmarks, GitHub
repos, blog posts, working-paper preprints) that arXiv misses.

Why non-interactive (`-p`) instead of pty/interactive:
  - `claude -p` runs the full agentic loop (WebSearch, WebFetch, etc.)
    and returns the final answer on stdout.
  - No TTY dialog issues (model selection, project picker, onboarding).
  - Works with subscription OAuth without us reproducing the TTY.
  - Subprocess exits naturally when claude finishes — no hang.

Subscription pool only. No API key. Single subprocess. Stdlib
subprocess.run with timeout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 360.0   # 6 min — plenty for 4-6 WebSearch calls
DEFAULT_MODEL = "claude-sonnet-4-6"
# Sonnet is enough for web-search summarization. Cheaper / faster than Opus.

LOG = sys.stderr


class ClaudeWebSearchError(RuntimeError):
    pass


def _which(name: str) -> str | None:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        full = os.path.join(p, name)
        if os.access(full, os.X_OK):
            return full
    return None


def _build_prompt(
    *,
    query: str,
    user_goal: str,
    existing_papers: list[dict[str, Any]],
) -> str:
    existing_titles = [
        (p.get("title") or "")[:160]
        for p in existing_papers[:10]
    ]
    existing_block = (
        "\n".join(f"  - {t}" for t in existing_titles)
        if existing_titles
        else "  (none)"
    )
    return (
        "You are running a one-shot market-research web search for the "
        "research_harness. NO conversational chit-chat, NO follow-up "
        "questions. Just run the searches and return the JSON.\n\n"
        f"User goal: {user_goal}\n"
        f"Research query: {query}\n\n"
        "arXiv/Google Scholar have already been searched. Already-found "
        f"titles:\n{existing_block}\n\n"
        "Your job: use the WebSearch tool (and WebFetch if needed) to find "
        "ADDITIONAL prior work that arXiv missed:\n"
        "  - industry benchmarks / blog posts / working papers\n"
        "  - open-source repos with reported metrics\n"
        "  - workshop papers / NeurIPS-style preprints not on arXiv\n"
        "  - whitepapers from quant funds / labs\n\n"
        "Rules:\n"
        "  1. EVERY candidate must have a real, accessible URL.\n"
        "  2. EVERY candidate should have a reported metric or method name "
        "a practitioner would recognize. NO self-made baselines.\n"
        "  3. Skip duplicates of the already-found titles above.\n"
        "  4. Cap at 6 new candidates.\n\n"
        "Output format — return ONLY a JSON array (no prose around it, "
        "no Markdown code fences) with this schema for each item:\n"
        "  {\n"
        "    \"id\": \"web_<short_slug>\",\n"
        "    \"source\": \"websearch\",\n"
        "    \"title\": \"...\",\n"
        "    \"url\": \"https://...\",\n"
        "    \"authors\": [\"...\"],   // best-effort; empty list OK\n"
        "    \"published\": null,      // best-effort ISO date or null\n"
        "    \"abstract\": \"...\",   // 1-3 sentence summary\n"
        "    \"arxiv_id\": null,\n"
        "    \"reported_metric\": \"...\",  // e.g. 'OOS rank IC = 0.06'\n"
        "    \"role_hint\": \"current_best_known\" | \"naive\" | \"random_or_null\"\n"
        "  }\n\n"
        "If you find nothing useful, return an empty array `[]`. Do not "
        "explain why; just return the JSON. The harness consumes stdout "
        "and parses the array."
    )


def enrich_with_claude_websearch(
    *,
    query: str,
    user_goal: str,
    existing_papers: list[dict[str, Any]],
    run_dir: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    model: str = DEFAULT_MODEL,
) -> list[dict[str, Any]]:
    """Spawn `claude -p` (non-interactive) to enrich existing arxiv/
    scholar findings with WebSearch results. Returns the list of new
    paper dicts (already normalized to the reference_paper schema).

    Returns [] when claude is not on PATH, when the subprocess times
    out, or when the JSON is malformed — never raises into the caller.
    Logs the full stdout + stderr to run_dir/claude_websearch.log so
    operators can debug.

    Subscription pool only — no API key needed.
    """
    claude_bin = _which("claude")
    if not claude_bin:
        print(
            "[market_research_claude] `claude` CLI not on PATH — skipping "
            "web-search enrichment.",
            file=LOG,
        )
        return []

    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "claude_websearch.log"
    prompt = _build_prompt(
        query=query,
        user_goal=user_goal,
        existing_papers=existing_papers,
    )

    # `claude -p "<prompt>" --output-format text` runs headless, exits
    # when the agentic loop completes, prints the final answer on stdout.
    cmd = [claude_bin, "-p", prompt, "--model", model, "--output-format", "text"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(run_dir),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        try:
            log_path.write_text(
                f"TIMEOUT after {timeout_seconds:.0f}s\n\nstdout so far:\n{(exc.stdout or b'').decode('utf-8', 'replace')}\n\nstderr so far:\n{(exc.stderr or b'').decode('utf-8', 'replace')}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        print(
            f"[market_research_claude] claude -p timed out after "
            f"{timeout_seconds:.0f}s — see {log_path}",
            file=LOG,
        )
        return []
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"[market_research_claude] claude -p failed to start: {exc}",
            file=LOG,
        )
        return []

    # Persist full stdout/stderr for operator debug, regardless of parse
    # success.
    try:
        log_path.write_text(
            f"exit_code: {proc.returncode}\n\n=== stdout ===\n{proc.stdout}\n\n=== stderr ===\n{proc.stderr}\n",
            encoding="utf-8",
        )
    except OSError:
        pass

    if proc.returncode != 0:
        print(
            f"[market_research_claude] claude -p exit={proc.returncode}; "
            f"see {log_path}",
            file=LOG,
        )
        return []

    raw = (proc.stdout or "").strip()
    if not raw:
        return []

    # claude sometimes wraps in ```json fences or adds prose. Recover
    # by isolating the first `[` to the matching last `]`.
    if raw.startswith("```"):
        fence_end = raw.find("\n", 3)
        raw = raw[fence_end + 1:] if fence_end >= 0 else raw
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")].rstrip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start >= 0 and end > start:
        raw = raw[start: end + 1]

    try:
        papers = json.loads(raw)
        if not isinstance(papers, list):
            raise ValueError("expected top-level JSON list")
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[market_research_claude] could not parse stdout as JSON list: {exc}",
            file=LOG,
        )
        return []

    # Normalize each candidate to the reference_paper schema shape so
    # downstream code can validate + ingest without case-by-case fixups.
    normalized: list[dict[str, Any]] = []
    for p in papers:
        if not isinstance(p, dict):
            continue
        if not p.get("url") or not p.get("title"):
            continue
        norm = {
            "id": str(p.get("id") or f"web_{abs(hash(p['url'])) % (10**8):08d}"),
            "source": "websearch",
            "title": str(p["title"])[:400],
            "url": str(p["url"])[:1024],
            "authors": [str(a) for a in (p.get("authors") or []) if a][:8],
            "published": p.get("published"),
            "abstract": str(p.get("abstract") or "")[:2000] or None,
            "arxiv_id": None,
            "download_status": "skipped",
            "download_error": None,
            "pdf_path": None,
            "pdf_bytes": None,
            "fetched_at": str(p.get("fetched_at") or _now_iso()),
            "reported_metric": str(p.get("reported_metric") or "")[:300] or None,
            "role_hint": str(p.get("role_hint") or "current_best_known")[:64],
        }
        normalized.append(norm)
    return normalized


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
