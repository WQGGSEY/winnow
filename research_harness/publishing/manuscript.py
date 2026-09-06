"""Resolve manuscript references against supplied research artifacts."""
from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from research_harness.publishing.integrity import json_digest


class ManuscriptError(ValueError):
    pass


def resolve_anchor(bundle: dict[str, Any], anchor: str) -> dict[str, Any]:
    path, separator, claimed = anchor.partition("=")
    parts = path.split(".")
    if len(parts) < 2 or parts[0] not in bundle:
        raise ManuscriptError(f"unknown evidence anchor: {anchor}")
    value: Any = bundle
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ManuscriptError(f"unresolved evidence anchor: {anchor}")
    if separator:
        try:
            expected = json.loads(claimed)
        except json.JSONDecodeError:
            expected = claimed
        if type(value) is bool and type(expected) is not bool or value != expected:
            raise ManuscriptError(f"anchor value differs from evidence: {anchor}")
    return {"anchor": path, "source_sha256": json_digest(bundle[parts[0]]),
            "value_sha256": json_digest(value), "value": value}


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href" and value)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def validate_sections(
    sections: dict[str, dict[str, Any]], bundle: dict[str, Any],
    *, identity_tokens: tuple[str, ...] = (), require_citations: bool = True,
) -> dict[str, Any]:
    """Validate resolvable evidence and citation identity, not semantic truth."""
    sources = {p["id"]: p for p in bundle.get("market_brief", {}).get("papers", [])
               if isinstance(p, dict) and isinstance(p.get("id"), str)}
    ledger: dict[str, Any] = {"sections": {}, "citations": {}}
    if bundle.get('primary_sources'):
        ledger['primary_sources'] = bundle['primary_sources']
    for sid, section in sections.items():
        prose = section.get("prose_html", "")
        parser = _Links()
        parser.feed(prose)
        text = " ".join(parser.text)
        if re.search(r"(?:file://|/(?:home|Users|tmp|mnt)/|[A-Za-z]:\\\\)", prose):
            raise ManuscriptError(f"local path in manuscript section {sid}")
        if any(token and token.casefold() in text.casefold() for token in identity_tokens):
            raise ManuscriptError(f"configured author identity in manuscript section {sid}")
        anchors = section.get("evidence_anchors", [])
        if sid == "method" and bundle.get("protocol_revisions"):
            for index in range(len(bundle["protocol_revisions"])):
                prefix = f"protocol_revisions.{index}"
                if not any(a.split("=", 1)[0].strip() in {"protocol_revisions", prefix}
                           or a.startswith(prefix + ".") for a in anchors):
                    raise ManuscriptError(f"methods must disclose protocol amendment {index} with an evidence anchor")
        if sid in {"method", "experiments", "discussion"} and not anchors:
            raise ManuscriptError(f"section {sid} requires evidence anchors")
        ledger["sections"][sid] = [resolve_anchor(bundle, a) for a in anchors]
        cited = section.get("citation_source_ids", [])
        for source_id in cited:
            source = sources.get(source_id)
            if source is None or not all(source.get(k) for k in ("title", "authors", "year", "url")):
                raise ManuscriptError(f"citation lacks retrieved bibliographic metadata: {source_id}")
            if urlsplit(source["url"]).scheme not in {"http", "https"}:
                raise ManuscriptError(f"invalid source URL: {source_id}")
            ledger["citations"][source_id] = {"source": source, "sha256": json_digest(source)}
        actual = {href.removeprefix("#ref_") for href in parser.hrefs if href.startswith("#ref_")}
        if set(cited) != actual:
            raise ManuscriptError(f"declared and embedded citations differ in section {sid}")
        source_urls = {sources[key]["url"] for key in cited}
        if any(urlsplit(href).scheme in {"http", "https", "mailto"}
               and href not in source_urls for href in parser.hrefs):
            raise ManuscriptError(f"unregistered external reference in section {sid}")
    if require_citations and not ledger["citations"]:
        raise ManuscriptError("manuscript requires citations to retrieved sources")
    return ledger


def bibliography_html(ledger: dict[str, Any]) -> str:
    items = []
    for key, entry in sorted(ledger["citations"].items()):
        source = entry["source"]
        esc = html.escape
        items.append(f'<li id="ref_{esc(key, quote=True)}">'
                     f'{esc(", ".join(source["authors"]))} ({source["year"]}). '
                     f'<a href="{esc(source["url"], quote=True)}">{esc(source["title"])}</a>.</li>')
    return "<ol>" + "".join(items) + "</ol>"
