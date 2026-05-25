from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from research_harness.config import (
    load_settings,
    resolve_agent_budget,
    resolve_agent_model,
)
from research_harness.memory.baseline_dossier import validate_baseline_dossier
from research_harness.schemas.validator import validate_named_schema


BILLING_ACK_ENV = "RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE"
BILLING_ACK_VALUE = "subscription_ack"
EXECUTION_ACK_ENV = "RESEARCH_HARNESS_EXECUTE_CLAUDE_LIVE"
EXECUTION_ACK_VALUE = "live_smoke_ack"
ANALYSIS_TIMEOUT_SECONDS = 180


CommandRunner = Callable[..., subprocess.CompletedProcess]


ARXIV_API_URL = "http://export.arxiv.org/api/query"
GOOGLE_SCHOLAR_URL = "https://scholar.google.com/scholar"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (research_harness market_research_agent)"
)


HttpFetcher = Callable[[str], bytes]
PdfFetcher = Callable[[str], bytes]


class MarketResearchError(ValueError):
    """Raised when the market research agent cannot proceed."""


@dataclass
class _CandidatePaper:
    paper: dict[str, Any]
    role_hint: str | None = None


@dataclass
class MarketResearchOutcome:
    brief: dict[str, Any]
    dossier: dict[str, Any]
    papers: list[dict[str, Any]] = field(default_factory=list)


def run_market_research(
    repo_root: Path,
    grilling_session: dict[str, Any],
    *,
    run_dir: Path | None = None,
    max_papers: int = 10,
    enable_google_scholar: bool = True,
    write_dossier_to_memory: bool = True,
    http_fetcher: HttpFetcher | None = None,
    pdf_fetcher: PdfFetcher | None = None,
    enable_sonnet_analysis: bool = False,
    billing_ack: bool | None = None,
    execution_ack: bool | None = None,
    claude_path: str | None = None,
    analysis_command_runner: CommandRunner | None = None,
) -> MarketResearchOutcome:
    """Run an agent-level paper-search pass and emit a baseline dossier candidate.

    Unlike the worker contract, this agent is allowed external tools (HTTP for
    arXiv and Google Scholar). The harness still owns memory mutation: the
    generated dossier is only persisted into ``memory/baseline_dossiers/`` when
    ``write_dossier_to_memory`` is true, and the candidate detail files are
    deterministically generated from the search result rather than by Claude.
    """

    validate_named_schema("grilling_session", grilling_session)
    repo_root = repo_root.resolve()
    fetcher = http_fetcher or _default_http_fetcher
    pdf_get = pdf_fetcher or _default_http_fetcher

    extracted = grilling_session["extracted"]
    query = str(extracted.get("search_query_seed") or extracted.get("claim_under_test"))
    run_dir = (
        run_dir
        or repo_root / "runs" / "market_research" / grilling_session["session_id"]
    ).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    reference_dir = run_dir / "reference_papers"
    reference_dir.mkdir(parents=True, exist_ok=True)

    papers: list[dict[str, Any]] = []
    sources_attempted: list[str] = []
    warnings: list[str] = []

    try:
        arxiv_papers = _arxiv_search(fetcher, query, max_papers)
        sources_attempted.append("arxiv")
        papers.extend(arxiv_papers)
    except Exception as exc:
        warnings.append(f"arxiv search failed: {exc}")
        sources_attempted.append("arxiv")

    if enable_google_scholar:
        try:
            gs_papers = _google_scholar_search(fetcher, query, max_papers)
            sources_attempted.append("google_scholar")
            papers.extend(gs_papers)
        except Exception as exc:
            warnings.append(f"google scholar search failed: {exc}")
            sources_attempted.append("google_scholar")

    papers = _deduplicate_papers(papers)[:max_papers]

    download_count = 0
    failed_count = 0
    for paper in papers:
        if not paper.get("url") or paper.get("source") != "arxiv":
            paper["download_status"] = "skipped"
            paper["download_error"] = None
            paper["pdf_path"] = None
            paper["pdf_bytes"] = None
            continue
        pdf_url = _arxiv_pdf_url(paper.get("arxiv_id") or "")
        if not pdf_url:
            paper["download_status"] = "skipped"
            paper["download_error"] = "no arxiv pdf url"
            paper["pdf_path"] = None
            paper["pdf_bytes"] = None
            continue
        try:
            pdf_bytes = pdf_get(pdf_url)
            pdf_path = reference_dir / f"{paper['id']}.pdf"
            pdf_path.write_bytes(pdf_bytes)
            paper["pdf_path"] = str(pdf_path)
            paper["pdf_bytes"] = len(pdf_bytes)
            paper["download_status"] = "downloaded"
            paper["download_error"] = None
            download_count += 1
        except Exception as exc:
            paper["pdf_path"] = None
            paper["pdf_bytes"] = None
            paper["download_status"] = "failed"
            paper["download_error"] = str(exc)
            failed_count += 1

    for paper in papers:
        paper.setdefault("download_status", "not_attempted")
        paper.setdefault("download_error", None)
        paper.setdefault("pdf_path", None)
        paper.setdefault("pdf_bytes", None)
        validate_named_schema("reference_paper", paper)

    sources_used = sources_attempted if sources_attempted else ["arxiv"]

    dossier, dossier_paths = _build_baseline_dossier(
        repo_root=repo_root,
        grilling_session=grilling_session,
        query=query,
        papers=papers,
        run_dir=run_dir,
        write_to_memory=write_dossier_to_memory,
    )

    analysis_md_path = run_dir / "baseline_analysis.md"
    analysis_source, analysis_usage = _generate_baseline_analysis_md(
        repo_root=repo_root,
        grilling_session=grilling_session,
        papers=papers,
        output_path=analysis_md_path,
        enable_sonnet_analysis=enable_sonnet_analysis,
        billing_ack=billing_ack,
        execution_ack=execution_ack,
        claude_path=claude_path,
        runner=analysis_command_runner,
        warnings=warnings,
    )

    brief_path = run_dir / "market_research_brief.json"
    if not papers:
        status = "failed"
    elif warnings or failed_count:
        status = "completed_with_warnings"
    else:
        status = "completed"

    brief = {
        "brief_id": "mrb_" + uuid.uuid4().hex[:12],
        "type": "market_research_brief",
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "grilling_session_id": grilling_session["session_id"],
        "search_query_seed": query,
        "sources_used": sources_used,
        "papers": papers,
        "baseline_dossier_id": dossier["id"],
        "baseline_dossier_candidate_path": str(dossier_paths["staging_yaml"]),
        "baseline_dossier_path": str(dossier_paths["memory_yaml"])
        if dossier_paths["memory_yaml"]
        else str(dossier_paths["staging_yaml"]),
        "reference_papers_dir": str(reference_dir),
        "brief_path": str(brief_path),
        "baseline_analysis_md_path": str(analysis_md_path),
        "baseline_analysis_source": analysis_source,
        "baseline_analysis_usage": analysis_usage,
        "usage": {
            "papers_found": len(papers),
            "papers_downloaded": download_count,
            "papers_failed": failed_count,
            "sources_attempted": sources_attempted,
        },
        "warnings": warnings,
        "error": None if status != "failed" else "no papers were retrieved",
    }
    validate_named_schema("market_research_brief", brief)
    brief_path.write_text(json.dumps(brief, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return MarketResearchOutcome(brief=brief, dossier=dossier, papers=papers)


def _arxiv_search(fetcher: HttpFetcher, query: str, max_results: int) -> list[dict[str, Any]]:
    params = {
        "search_query": f"all:{query}",
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    url = ARXIV_API_URL + "?" + urllib.parse.urlencode(params)
    raw = fetcher(url)
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    root = ET.fromstring(text)
    papers: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = _text(entry.find("atom:title", ATOM_NS))
        summary = _text(entry.find("atom:summary", ATOM_NS))
        published = _text(entry.find("atom:published", ATOM_NS))
        link_url = ""
        for link in entry.findall("atom:link", ATOM_NS):
            if link.get("type") == "text/html" or link.get("rel") == "alternate":
                link_url = link.get("href", "")
                break
        authors = [
            _text(name.find("atom:name", ATOM_NS))
            for name in entry.findall("atom:author", ATOM_NS)
            if name.find("atom:name", ATOM_NS) is not None
        ]
        atom_id = _text(entry.find("atom:id", ATOM_NS))
        arxiv_id = _extract_arxiv_id(atom_id)
        year: int | None = None
        if published and len(published) >= 4 and published[:4].isdigit():
            year = int(published[:4])
        papers.append(
            {
                "id": f"arxiv_{arxiv_id or _slug(title)[:24]}",
                "source": "arxiv",
                "title": title or "untitled",
                "authors": [a for a in authors if a],
                "abstract": summary or None,
                "url": link_url or atom_id or "",
                "year": year,
                "arxiv_id": arxiv_id,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return papers


def _google_scholar_search(
    fetcher: HttpFetcher, query: str, max_results: int
) -> list[dict[str, Any]]:
    params = {"q": query, "hl": "en"}
    url = GOOGLE_SCHOLAR_URL + "?" + urllib.parse.urlencode(params)
    raw = fetcher(url)
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    if "Our systems have detected unusual traffic" in text or "captcha" in text.lower():
        raise MarketResearchError("google scholar returned anti-bot challenge")
    pattern = re.compile(
        r'<h3[^>]*class="gs_rt"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    papers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for match in pattern.finditer(text):
        url_candidate = match.group(1)
        title_html = match.group(2)
        title = _strip_html(title_html)
        if not url_candidate or url_candidate in seen_urls:
            continue
        seen_urls.add(url_candidate)
        papers.append(
            {
                "id": "scholar_" + _slug(title)[:24],
                "source": "google_scholar",
                "title": title or "untitled",
                "authors": [],
                "abstract": None,
                "url": url_candidate,
                "year": None,
                "arxiv_id": None,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if len(papers) >= max_results:
            break
    return papers


def _arxiv_pdf_url(arxiv_id: str) -> str | None:
    if not arxiv_id:
        return None
    return f"http://arxiv.org/pdf/{arxiv_id}"


def _extract_arxiv_id(atom_id: str) -> str | None:
    if not atom_id:
        return None
    match = re.search(r"abs/([0-9A-Za-z\.\-/]+)", atom_id)
    if match:
        return match.group(1)
    return None


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return " ".join(node.text.split())


def _strip_html(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", "", text)
    return " ".join(no_tags.split())


def _slug(text: str) -> str:
    cleaned = []
    for char in text.lower():
        if char.isalnum():
            cleaned.append(char)
        elif char in {" ", "-", "_"}:
            cleaned.append("_")
    slug = "".join(cleaned).strip("_") or "untitled"
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug


def _deduplicate_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for paper in papers:
        title_key = paper.get("title", "").lower().strip()
        url_key = paper.get("url", "").strip()
        if url_key and url_key in seen_urls:
            continue
        if title_key and title_key in seen_titles:
            continue
        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        deduped.append(paper)
    return deduped


def _default_http_fetcher(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as resp:
        return resp.read()


def _generate_baseline_analysis_md(
    *,
    repo_root: Path,
    grilling_session: dict[str, Any],
    papers: list[dict[str, Any]],
    output_path: Path,
    enable_sonnet_analysis: bool,
    billing_ack: bool | None,
    execution_ack: bool | None,
    claude_path: str | None,
    runner: CommandRunner | None,
    warnings: list[str],
) -> tuple[str, dict[str, Any]]:
    """Write the baseline analysis md and return (source, usage).

    source is one of:
      - deterministic_metadata: enable_sonnet_analysis was False, or no papers.
      - sonnet_analysis: sonnet successfully produced an analysis.
      - sonnet_failed_fallback: sonnet was requested but failed; fell back.
    """

    default_usage = {"cost_usd": None, "input_tokens": 0, "output_tokens": 0}
    if not enable_sonnet_analysis or not papers:
        output_path.write_text(
            _deterministic_baseline_md(grilling_session, papers, reason=(
                "Sonnet analysis disabled; this file is a deterministic dump "
                "of fetched paper metadata. Operator should review and refine "
                "before the dossier candidate is used as evidence."
            )),
            encoding="utf-8",
        )
        return "deterministic_metadata", default_usage

    settings = load_settings(repo_root)
    live_backend = (
        settings.get("runtime", {})
        .get("worker_backends", {})
        .get("claude_code_live", {})
    )
    if not _billing_ack_ok(billing_ack):
        warnings.append(
            "sonnet baseline analysis blocked by billing_ack; using deterministic metadata."
        )
        output_path.write_text(
            _deterministic_baseline_md(grilling_session, papers, reason="Sonnet blocked by billing_ack."),
            encoding="utf-8",
        )
        return "sonnet_failed_fallback", default_usage
    if not _execution_ack_ok(execution_ack):
        warnings.append(
            "sonnet baseline analysis blocked by execution_ack; using deterministic metadata."
        )
        output_path.write_text(
            _deterministic_baseline_md(grilling_session, papers, reason="Sonnet blocked by execution_ack."),
            encoding="utf-8",
        )
        return "sonnet_failed_fallback", default_usage

    detected_claude = claude_path or shutil.which("claude") or "claude"
    cmd_runner = runner or subprocess.run
    model = resolve_agent_model(settings, "market_research_agent")
    max_budget = resolve_agent_budget(settings, "market_research_agent")

    try:
        md_text, usage = _call_sonnet_for_baseline_md(
            runner=cmd_runner,
            claude_path=detected_claude,
            model=model,
            max_budget=max_budget,
            grilling_session=grilling_session,
            papers=papers,
        )
    except Exception as exc:
        warnings.append(f"sonnet baseline analysis failed: {exc}")
        output_path.write_text(
            _deterministic_baseline_md(
                grilling_session, papers, reason=f"Sonnet failed: {exc}."
            ),
            encoding="utf-8",
        )
        return "sonnet_failed_fallback", default_usage

    output_path.write_text(md_text, encoding="utf-8")
    return "sonnet_analysis", usage


def _deterministic_baseline_md(
    grilling_session: dict[str, Any],
    papers: list[dict[str, Any]],
    *,
    reason: str,
) -> str:
    extracted = grilling_session["extracted"]
    lines = [
        f"# Baseline Analysis (deterministic) — {extracted['domain']}",
        "",
        f"- Source: {reason}",
        f"- Grilling session: {grilling_session['session_id']}",
        f"- Search query seed: {extracted.get('search_query_seed', 'n/a')}",
        f"- Claim under test: {extracted['claim_under_test']}",
        "",
        "## Grilled baseline expectations",
    ]
    for baseline in extracted.get("mandatory_baselines") or []:
        lines.append(f"- {baseline}")
    lines.extend(["", "## Retrieved papers"])
    if not papers:
        lines.append("- (no papers retrieved)")
    for paper in papers:
        lines.append(f"### {paper.get('title', 'untitled')}")
        lines.append(f"- Source: {paper.get('source')}  ·  Year: {paper.get('year') or 'n/a'}")
        lines.append(f"- URL: {paper.get('url', 'n/a')}")
        if paper.get("authors"):
            lines.append(f"- Authors: {', '.join(paper['authors'])}")
        if paper.get("abstract"):
            lines.append("")
            lines.append("> " + paper["abstract"].replace("\n", " ")[:1200])
        lines.append("")
    lines.extend(
        [
            "## Operator follow-up",
            "- Read the linked papers and replace this deterministic dump with the actual",
            "  current-best / naive / random baselines plus their reported metrics and",
            "  evaluation splits.",
        ]
    )
    return "\n".join(lines) + "\n"


def _call_sonnet_for_baseline_md(
    *,
    runner: CommandRunner,
    claude_path: str,
    model: str,
    max_budget: str,
    grilling_session: dict[str, Any],
    papers: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    system_prompt = (
        "You are a senior research analyst. Given a research goal and a set of "
        "retrieved papers (title, authors, year, abstract, url), write a "
        "concise markdown brief that identifies (1) the most likely "
        "current-best method, (2) a naive baseline, (3) a random or null "
        "baseline, with reported metrics and their evaluation splits when "
        "available, citing the specific paper URLs. Be explicit when a paper "
        "does not state a baseline or metric. Output one markdown document; "
        "no JSON, no code fences."
    )
    extracted = grilling_session["extracted"]
    paper_blocks = []
    for paper in papers:
        paper_blocks.append(
            "- title: " + str(paper.get("title", ""))
            + "\n  url: " + str(paper.get("url", ""))
            + "\n  year: " + str(paper.get("year") or "")
            + "\n  authors: " + ", ".join(paper.get("authors") or [])
            + "\n  abstract: "
            + (str(paper.get("abstract") or "")[:1500])
        )
    user_prompt = (
        "# Research Goal\n"
        f"{extracted['claim_under_test']}\n\n"
        f"Domain: {extracted['domain']}\n"
        f"Search query seed: {extracted.get('search_query_seed', '')}\n\n"
        "# Mandatory Baselines From Grilling\n"
        + "\n".join(f"- {b}" for b in extracted.get("mandatory_baselines") or [])
        + "\n\n# Retrieved Papers\n"
        + "\n".join(paper_blocks)
        + "\n\n# Required Output\n"
        "A single markdown brief with sections: "
        "## Current-best, ## Naive, ## Random/Null, ## Reported Metrics Table, "
        "## Open Gaps. Cite paper URLs inline. Mark uncertainty explicitly."
    )
    cmd = [
        claude_path,
        "-p",
        "--model",
        model,
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--system-prompt",
        system_prompt,
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--no-session-persistence",
        "--max-budget-usd",
        max_budget,
    ]
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    completed = runner(
        cmd,
        input=user_prompt,
        capture_output=True,
        text=True,
        timeout=ANALYSIS_TIMEOUT_SECONDS,
        check=False,
        env=env,
    )
    if completed.returncode not in (0, None):
        raise MarketResearchError(
            f"claude CLI exit code {completed.returncode}: {(completed.stderr or '').strip()[:200]}"
        )
    cli_result = json.loads(completed.stdout or "")
    if not isinstance(cli_result, dict) or cli_result.get("type") != "result":
        raise MarketResearchError("claude CLI returned unexpected payload")
    if cli_result.get("is_error"):
        raise MarketResearchError(
            f"claude CLI reported error subtype {cli_result.get('subtype')!r}"
        )
    inner = cli_result.get("result")
    if not isinstance(inner, str) or not inner.strip():
        raise MarketResearchError("claude CLI returned empty analyst text")
    usage = cli_result.get("usage") if isinstance(cli_result.get("usage"), dict) else {}
    return inner.strip(), {
        "cost_usd": float(cli_result.get("total_cost_usd") or 0.0),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }


def _billing_ack_ok(billing_ack: bool | None) -> bool:
    if billing_ack is not None:
        return bool(billing_ack)
    return os.environ.get(BILLING_ACK_ENV) == BILLING_ACK_VALUE


def _execution_ack_ok(execution_ack: bool | None) -> bool:
    if execution_ack is not None:
        return bool(execution_ack)
    return os.environ.get(EXECUTION_ACK_ENV) == EXECUTION_ACK_VALUE


def _build_baseline_dossier(
    *,
    repo_root: Path,
    grilling_session: dict[str, Any],
    query: str,
    papers: list[dict[str, Any]],
    run_dir: Path,
    write_to_memory: bool,
) -> tuple[dict[str, Any], dict[str, Path | None]]:
    extracted = grilling_session["extracted"]
    today = date.today().isoformat()
    session_slug = re.sub(r"[^a-z0-9]+", "_", grilling_session["session_id"].lower()).strip("_")
    dossier_id = f"bd_{session_slug}_{today.replace('-', '')}"

    paper_candidates = [
        _CandidatePaper(paper=paper, role_hint=_role_hint_for(paper, extracted))
        for paper in papers
    ]
    candidates, selected_candidate = _assign_candidates(extracted, paper_candidates)
    source_index = _build_source_index(papers, today)

    if write_to_memory:
        dossier_dir = repo_root / "memory" / "baseline_dossiers"
    else:
        dossier_dir = run_dir / "memory_staging" / "baseline_dossiers"
    dossier_dir.mkdir(parents=True, exist_ok=True)
    candidates_dir = dossier_dir / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)

    candidates_index = []
    for candidate in candidates:
        detail_rel = f"candidates/{candidate['id']}.md"
        detail_path = dossier_dir / detail_rel
        detail_path.write_text(_render_candidate_detail(candidate, extracted), encoding="utf-8")
        candidates_index.append(
            {
                "id": candidate["id"],
                "method": candidate["method"],
                "decision": candidate["decision"],
                "reason_tags": candidate["reason_tags"],
                "detail_file": detail_rel,
            }
        )

    dossier = {
        "id": dossier_id,
        "created_at": today,
        "query": query or "automated market research query",
        "problem_scope": {
            "domain": extracted["domain"],
            "node_type": extracted["node_type"],
            "claim_under_test": extracted["claim_under_test"],
        },
        "selected": {
            "method": selected_candidate["method"],
            "role": "current_best_known",
            "candidate_id": selected_candidate["id"],
            "one_paragraph_reason": selected_candidate["reason"],
            "evidence_tags": selected_candidate.get("evidence_tags") or [
                "automated_market_research",
            ],
            "risk_tags": selected_candidate.get("risk_tags") or ["operator_should_review"],
        },
        "candidates_index": candidates_index,
        "source_index": source_index,
        "refresh_policy": {
            "required_before": [
                "final_claim_promotion",
                "external_writeup",
            ]
        },
    }

    staging_yaml = run_dir / "baseline_dossier_candidate.yaml"
    staging_yaml.write_text(_render_dossier_yaml(dossier), encoding="utf-8")

    memory_yaml: Path | None = None
    if write_to_memory:
        memory_yaml = dossier_dir / f"{dossier_id}.yaml"
        memory_yaml.write_text(_render_dossier_yaml(dossier), encoding="utf-8")
        validate_baseline_dossier(repo_root, dossier)
    else:
        # still validate JSON-schema; do not validate file-system existence
        validate_named_schema("baseline_dossier", dossier)

    return dossier, {"staging_yaml": staging_yaml, "memory_yaml": memory_yaml}


def _role_hint_for(paper: dict[str, Any], extracted: dict[str, Any]) -> str | None:
    title = paper.get("title", "").lower()
    if any(token in title for token in ["bm25", "tf-idf", "lead-3", "majority", "trivial"]):
        return "naive"
    if any(token in title for token in ["random", "null hypothesis"]):
        return "random_or_null"
    return None


def _assign_candidates(
    extracted: dict[str, Any],
    candidate_papers: list[_CandidatePaper],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Construct the three required candidate decisions (selected, naive, null).

    The first relevant paper gets the current-best slot. Naive and null slots
    prefer matching papers from the search if available; otherwise the harness
    falls back to placeholders derived from grilling.mandatory_baselines so
    that the dossier always satisfies invariants.
    """

    fallback_baselines = list(extracted.get("mandatory_baselines") or [])
    naive_hint = _first_or(fallback_baselines, 1, "BM25 or simple supervised baseline")
    null_hint = _first_or(fallback_baselines, 2, "random ranking / null hypothesis")

    selected_paper = candidate_papers[0].paper if candidate_papers else None
    naive_paper = _pick_paper(candidate_papers, "naive")
    null_paper = _pick_paper(candidate_papers, "random_or_null")

    candidates: list[dict[str, Any]] = []

    if selected_paper is not None:
        candidates.append(
            _candidate_from_paper(
                selected_paper,
                decision="selected",
                role="current_best_known",
                fallback_method=str(fallback_baselines[0]) if fallback_baselines else "current best",
                reason="Top-ranked search result for the grilled claim.",
            )
        )
    else:
        candidates.append(
            _placeholder_candidate(
                cid="c_current_best_placeholder",
                method=str(fallback_baselines[0]) if fallback_baselines else "current best (placeholder)",
                decision="selected",
                role="current_best_known",
                reason="No automated paper found; operator must review.",
            )
        )

    if naive_paper is not None and naive_paper["id"] != candidates[0]["id"]:
        candidates.append(
            _candidate_from_paper(
                naive_paper,
                decision="selected_as_naive",
                role="naive",
                fallback_method=naive_hint,
                reason="Matched naive baseline heuristic from grilling.",
            )
        )
    else:
        candidates.append(
            _placeholder_candidate(
                cid="c_naive_placeholder",
                method=naive_hint,
                decision="selected_as_naive",
                role="naive",
                reason="Operator must attach a naive baseline reference.",
            )
        )

    if null_paper is not None and null_paper["id"] not in {c["id"] for c in candidates}:
        candidates.append(
            _candidate_from_paper(
                null_paper,
                decision="selected_as_random_or_null",
                role="random_or_null",
                fallback_method=null_hint,
                reason="Matched random/null heuristic from grilling.",
            )
        )
    else:
        candidates.append(
            _placeholder_candidate(
                cid="c_null_placeholder",
                method=null_hint,
                decision="selected_as_random_or_null",
                role="random_or_null",
                reason="No random/null paper found; operator must review.",
            )
        )

    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["id"] in seen_ids:
            continue
        seen_ids.add(candidate["id"])
        deduped.append(candidate)

    return deduped, deduped[0]


def _pick_paper(
    candidate_papers: list[_CandidatePaper], role: str
) -> dict[str, Any] | None:
    for entry in candidate_papers:
        if entry.role_hint == role:
            return entry.paper
    return None


def _candidate_from_paper(
    paper: dict[str, Any],
    *,
    decision: str,
    role: str,
    fallback_method: str,
    reason: str,
) -> dict[str, Any]:
    method = paper.get("title") or fallback_method
    return {
        "id": "c_" + _slug(paper["id"])[:48],
        "method": method,
        "decision": decision,
        "role": role,
        "reason_tags": ["automated_market_research", role],
        "evidence_tags": ["automated_market_research", paper.get("source", "unknown")],
        "risk_tags": ["operator_should_review"],
        "reason": reason,
        "source_paper_id": paper["id"],
        "title": paper.get("title", ""),
        "url": paper.get("url", ""),
        "authors": paper.get("authors", []),
        "year": paper.get("year"),
        "arxiv_id": paper.get("arxiv_id"),
        "abstract": paper.get("abstract"),
    }


def _placeholder_candidate(
    *,
    cid: str,
    method: str,
    decision: str,
    role: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "id": cid,
        "method": method,
        "decision": decision,
        "role": role,
        "reason_tags": ["operator_review_required", role],
        "evidence_tags": ["operator_review_required"],
        "risk_tags": ["placeholder", "operator_must_resolve"],
        "reason": reason,
        "source_paper_id": None,
        "title": "",
        "url": "",
        "authors": [],
        "year": None,
        "arxiv_id": None,
        "abstract": None,
    }


def _build_source_index(papers: list[dict[str, Any]], today: str) -> list[dict[str, Any]]:
    source_index = []
    for index, paper in enumerate(papers, start=1):
        url = paper.get("url") or ""
        if not url.startswith(("http://", "https://")):
            continue
        source_index.append(
            {
                "id": f"s{index}",
                "url": url,
                "accessed_at": today,
                "supports": [paper["id"]],
            }
        )
    if not source_index:
        source_index.append(
            {
                "id": "s1",
                "url": "https://example.invalid/operator-must-replace",
                "accessed_at": today,
                "supports": ["operator_must_supply_source"],
            }
        )
    return source_index


def _first_or(items: list[str], index: int, default: str) -> str:
    if index < len(items):
        return items[index]
    return default


def _render_candidate_detail(candidate: dict[str, Any], extracted: dict[str, Any]) -> str:
    lines = [
        f"# {candidate['method']}",
        "",
        f"- Candidate id: {candidate['id']}",
        f"- Decision: {candidate['decision']}",
        f"- Role: {candidate['role']}",
        f"- Source: {candidate.get('source_paper_id') or 'operator placeholder'}",
        f"- URL: {candidate.get('url') or 'n/a'}",
        f"- Year: {candidate.get('year') or 'n/a'}",
        f"- ArXiv id: {candidate.get('arxiv_id') or 'n/a'}",
        "",
        "## Reason",
        candidate["reason"],
        "",
        "## Authors",
    ]
    authors = candidate.get("authors") or []
    if authors:
        for author in authors:
            lines.append(f"- {author}")
    else:
        lines.append("- (unknown)")
    if candidate.get("abstract"):
        lines.extend(["", "## Abstract", candidate["abstract"]])
    lines.extend(
        [
            "",
            "## Provenance",
            f"- Generated for grilling claim: {extracted['claim_under_test']}",
            "- Source: automated market research agent. Operator review required.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_dossier_yaml(dossier: dict[str, Any]) -> str:
    selected = dossier["selected"]
    candidates_index = dossier["candidates_index"]
    source_index = dossier["source_index"]
    refresh = dossier["refresh_policy"]

    lines = [
        f"id: {dossier['id']}",
        f"created_at: \"{dossier['created_at']}\"",
        f"query: {json.dumps(dossier['query'])}",
        "problem_scope:",
    ]
    for key, value in dossier["problem_scope"].items():
        lines.append(f"  {key}: {json.dumps(str(value))}")
    lines.extend(
        [
            "selected:",
            f"  method: {json.dumps(selected['method'])}",
            f"  role: {selected['role']}",
            f"  candidate_id: {selected['candidate_id']}",
            f"  one_paragraph_reason: {json.dumps(selected['one_paragraph_reason'])}",
            "  evidence_tags:",
        ]
    )
    for tag in selected["evidence_tags"]:
        lines.append(f"    - {json.dumps(str(tag))}")
    lines.append("  risk_tags:")
    for tag in selected["risk_tags"]:
        lines.append(f"    - {json.dumps(str(tag))}")

    lines.append("candidates_index:")
    for candidate in candidates_index:
        lines.append(f"  - id: {candidate['id']}")
        lines.append(f"    method: {json.dumps(candidate['method'])}")
        lines.append(f"    decision: {candidate['decision']}")
        lines.append("    reason_tags:")
        for tag in candidate["reason_tags"]:
            lines.append(f"      - {json.dumps(str(tag))}")
        lines.append(f"    detail_file: {candidate['detail_file']}")

    lines.append("source_index:")
    for source in source_index:
        lines.append(f"  - id: {source['id']}")
        lines.append(f"    url: {json.dumps(source['url'])}")
        lines.append(f"    accessed_at: \"{source['accessed_at']}\"")
        lines.append("    supports:")
        for support in source["supports"]:
            lines.append(f"      - {json.dumps(str(support))}")

    lines.append("refresh_policy:")
    lines.append("  required_before:")
    for required in refresh["required_before"]:
        lines.append(f"    - {json.dumps(str(required))}")

    return "\n".join(lines) + "\n"
