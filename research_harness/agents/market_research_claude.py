"""Claude Code-driven web-search enrichment for market_research.

The legacy in-process path (run_market_research) hits arXiv + Google
Scholar only. This module spawns a one-shot `claude` subprocess that
uses Claude Code's native WebSearch + WebFetch tools to find additional
non-academic sources (industry benchmarks, GitHub repos, blog posts,
working-paper preprints) that arXiv misses.

Same subscription-pool / pty / no-API-cost model as thread_supervisor.
Single subprocess, runs to natural completion, writes
`web_search_papers.json` to the market run_dir, exits.
"""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import sys
import time
from pathlib import Path
from typing import Any


# Allow callers (tests) to short-circuit the actual subprocess.
DEFAULT_TIMEOUT_SECONDS = 480.0  # 8 min — enough for 3-4 WebSearch + WebFetch
DEFAULT_BOOT_DELAY = 3.0
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
    output_path: Path,
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
        "research_harness. NO conversational chit-chat.\n\n"
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
        "  1. EVERY candidate must have a real URL.\n"
        "  2. EVERY candidate should have a reported metric or method "
        "name a practitioner would recognize. NO self-made baselines.\n"
        "  3. Skip duplicates of the already-found titles above.\n"
        "  4. Cap at 6 new candidates.\n\n"
        f"Write your results as a JSON list to {output_path}, then end "
        "your turn. Schema for each item:\n"
        "  {\n"
        "    \"id\": \"web_<slug>\",  // short id\n"
        "    \"source\": \"websearch\",\n"
        "    \"title\": \"...\",\n"
        "    \"url\": \"https://...\",\n"
        "    \"authors\": [\"...\"],   // best-effort; empty list OK\n"
        "    \"published\": null,      // best-effort ISO date or null\n"
        "    \"abstract\": \"...\",   // 1-3 sentence summary\n"
        "    \"arxiv_id\": null,\n"
        "    \"download_status\": \"skipped\",\n"
        "    \"download_error\": null,\n"
        "    \"pdf_path\": null,\n"
        "    \"pdf_bytes\": null,\n"
        "    \"fetched_at\": \"<UTC ISO timestamp>\",\n"
        "    \"reported_metric\": \"...\",  // e.g. 'OOS rank IC = 0.06'\n"
        "    \"role_hint\": \"current_best_known\" | \"naive\" | \"random_or_null\"\n"
        "  }\n\n"
        "After writing the file, briefly state how many candidates you "
        "wrote and exit your turn. Do NOT propose follow-ups or ask "
        "questions; the harness consumes the file and moves on."
    )


def enrich_with_claude_websearch(
    *,
    query: str,
    user_goal: str,
    existing_papers: list[dict[str, Any]],
    run_dir: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    boot_delay: float = DEFAULT_BOOT_DELAY,
    model: str = DEFAULT_MODEL,
) -> list[dict[str, Any]]:
    """Spawn a one-shot `claude` subprocess to enrich existing arxiv/
    scholar findings with WebSearch results. Returns the list of new
    paper dicts (already normalized to the reference_paper schema).

    Returns [] when claude is not on PATH, when the output file is never
    written, or when the JSON is malformed — never raises into the
    caller. Logs failures to stderr so the operator sees them.

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
    output_path = (run_dir / "web_search_papers.json").resolve()
    # If a stale file from a previous run exists, remove it so our wait
    # logic does not race.
    try:
        if output_path.exists():
            output_path.unlink()
    except OSError:
        pass

    prompt = _build_prompt(
        query=query,
        user_goal=user_goal,
        existing_papers=existing_papers,
        output_path=output_path,
    )
    log_path = run_dir / "claude_websearch.log"

    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvp(claude_bin, [claude_bin, "--model", model])

    log_fh = log_path.open("ab")
    deadline = time.time() + timeout_seconds
    prompt_sent = False
    try:
        # Boot delay so claude finishes printing its welcome banner.
        time.sleep(boot_delay)
        try:
            os.write(master_fd, (prompt + "\n").encode("utf-8"))
            prompt_sent = True
        except OSError as exc:
            print(f"[market_research_claude] write prompt failed: {exc}", file=LOG)

        # Drain output, looking for either the output file appearing OR
        # the subprocess naturally ending.
        while time.time() < deadline:
            try:
                ready, _, _ = select.select([master_fd], [], [], 1.5)
            except OSError:
                ready = []
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    log_fh.write(chunk)
                    log_fh.flush()
                except OSError:
                    pass

            # Has claude written its result file?
            if output_path.exists():
                # Give claude another ~10s of grace to finish + exit.
                grace_end = time.time() + 12
                while time.time() < grace_end:
                    try:
                        done_pid, _ = os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        done_pid = pid
                    if done_pid:
                        break
                    time.sleep(0.5)
                # If still alive, send SIGTERM — we already have the file.
                try:
                    os.kill(pid, 0)
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                break

            # Has the subprocess exited?
            try:
                done_pid, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done_pid = pid
            if done_pid:
                break

        # Final wait + cleanup.
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        else:
            # If still alive after deadline, kill.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        log_fh.close()

    if not prompt_sent or not output_path.exists():
        print(
            "[market_research_claude] no output file written by claude; "
            "enrichment yielded 0 new candidates.",
            file=LOG,
        )
        return []

    try:
        raw = output_path.read_text(encoding="utf-8").strip()
        # claude sometimes emits a leading ```json fence or trailing
        # commentary — try to recover by finding the first '[' and
        # matching ']'.
        if raw.startswith("```"):
            # strip code fence
            fence_end = raw.find("\n", 3)
            raw = raw[fence_end + 1:] if fence_end >= 0 else raw
            if raw.endswith("```"):
                raw = raw[: raw.rfind("```")].rstrip()
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
        papers = json.loads(raw)
        if not isinstance(papers, list):
            raise ValueError("expected top-level JSON list")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(
            f"[market_research_claude] could not parse output: {exc}", file=LOG
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
            # Extra fields that survive past _build_baseline_dossier
            # because the dossier copies them via dict-merge.
            "reported_metric": str(p.get("reported_metric") or "")[:300] or None,
            "role_hint": str(p.get("role_hint") or "current_best_known")[:64],
        }
        normalized.append(norm)
    return normalized


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
