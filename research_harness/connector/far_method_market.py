"""[[market research (per-reading, far-method)]] — P-BLIND method research.

The existing market_research role, narrowed to one job and run once per coherent
reading: research the **far-domain method** (the one the reading surfaced) in its
OWN field — does it exist, what is its canonical form, what is known — so
[[reduction]] has material and the resulting claim does not reinvent the wheel.

**P-BLIND by signature** (a distinct invocation from the legacy P-aware market):
the field was chosen behind the [[firewall]], so handing P here would bias the
method search toward P-convenient findings. This is NOT the P-claim's baseline
dossier (current_best / naive / random) — those are resolved later, P-aware, in
the production validity stage.

Reuses the arXiv search + dedup machinery from ``agents/market_research`` rather
than duplicating the Atom parsing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research_harness.agents.market_research import (
    HttpFetcher,
    _arxiv_search,
    _deduplicate_papers,
    _default_http_fetcher,
)


def research_far_method(
    field: dict[str, Any],
    field_method: str,
    *,
    http_fetcher: HttpFetcher | None = None,
    max_papers: int = 6,
) -> dict[str, Any]:
    """Search for prior work on ``field_method`` within ``field``. P-blind.

    Returns ``{field, method, query, papers, num_papers, warnings}``. Search
    failure degrades gracefully to an empty paper list with a warning — the
    far method may simply have no arXiv presence, which is itself signal for
    reduction; it never breaks the pipeline.
    """
    fetcher = http_fetcher or _default_http_fetcher
    method = (field_method or "").strip()
    field_name = str(field.get("name") or "").strip()
    # Query carries ONLY the field + method — never P.
    query = (method + " " + field_name).strip() or field_name or "method"

    papers: list[dict[str, Any]] = []
    warnings: list[str] = []
    if method:
        try:
            papers = _deduplicate_papers(_arxiv_search(fetcher, query, max_papers))[:max_papers]
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            warnings.append(f"far-method arxiv search failed: {exc}")
    else:
        warnings.append("reading produced no field_method; skipping method search")

    slim = [
        {
            "title": p.get("title"),
            "url": p.get("url"),
            "year": p.get("year"),
            "abstract": (str(p.get("abstract") or "")[:1200]) or None,
        }
        for p in papers
    ]
    return {
        "field": {"code": field.get("code"), "name": field.get("name"), "archive": field.get("archive")},
        "method": method,
        "query": query,
        "papers": slim,
        "num_papers": len(slim),
        "warnings": warnings,
    }
