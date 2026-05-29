"""ADR 0010 (T1-01) — OFFLINE harvester.

Pulls OpenAlex Topics across all 4 domains as the raw substrate for the
method/abstraction extraction step (T1-02). Uses ``requests`` (the
``atlas-builder`` extra) and hits the public OpenAlex API — it is part of the
OFFLINE build and is NEVER imported by the air-gapped harness, which only ever
reads a pinned, calibration-passed atlas artifact.

Topics are subject-area clusters, NOT methods — so this is only the substrate;
T1-02 extracts the transferable method/abstraction nodes (the atlas's actual
nodes must be method-grain, not subject-field).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

OPENALEX_TOPICS = "https://api.openalex.org/topics"
# OpenAlex's 4 top-level domains — the atlas must span all of them (not CS-only).
DOMAINS = ("Physical Sciences", "Life Sciences", "Social Sciences", "Health Sciences")


def harvest_topics(*, per_domain: int = 8, mailto: str | None = None, timeout: int = 30) -> dict[str, Any]:
    """Top topics by works_count, balanced across the 4 OpenAlex domains."""
    params: dict[str, Any] = {"per_page": 200, "sort": "works_count:desc"}
    if mailto:
        params["mailto"] = mailto  # OpenAlex "polite pool" etiquette
    resp = requests.get(OPENALEX_TOPICS, params=params, timeout=timeout)
    resp.raise_for_status()
    by_domain: dict[str, list[dict[str, Any]]] = {d: [] for d in DOMAINS}
    for t in resp.json().get("results", []):
        dom = (t.get("domain") or {}).get("display_name")
        if dom in by_domain and len(by_domain[dom]) < per_domain:
            by_domain[dom].append({
                "openalex_id": t.get("id"),
                "display_name": t.get("display_name"),
                "description": (t.get("description") or "").strip(),
                "keywords": [k for k in (t.get("keywords") or []) if isinstance(k, str)],
                "domain": dom,
                "field": (t.get("field") or {}).get("display_name"),
                "subfield": (t.get("subfield") or {}).get("display_name"),
            })
    topics = [t for d in DOMAINS for t in by_domain[d]]
    return {
        "source": "openalex_topics",
        "harvested_per_domain": {d: len(by_domain[d]) for d in DOMAINS},
        "topics": topics,
    }


def write_harvest(out_path: Path, **kwargs: Any) -> dict[str, Any]:
    bundle = harvest_topics(**kwargs)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return bundle
