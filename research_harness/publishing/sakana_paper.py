"""Assemble a manuscript preview and a separate internal research report.

HTML rendering does not establish scientific or conference submission readiness.
"""

from __future__ import annotations

import html as html_lib
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class SakanaPaperError(ValueError):
    pass


PAPER_WRITING_REQUIREMENTS = (
    "Write a self-contained research manuscript for an external reviewer. "
    "The original problem, contribution, and closest prior work must be explicit.",
    "Include authored abstract and references sections. Verify each cited paper's "
    "metadata and what it supports. Internal reviews and lessons are not publications.",
    "Explain the actual method using definitions, assumptions, algorithm details, "
    "and equations as needed. A runner invocation is not a description of the method.",
    "For empirical claims, describe data, splits, model selection, baselines and "
    "matched budgets. Report measured results, uncertainty, and relevant ablations. "
    "If evidence is absent, request experiments; do not invent it or pad the prose.",
    "For theoretical claims, state precise assumptions and results, provide complete "
    "proofs, and distinguish proven statements from conjectures. Figures are optional.",
    "Trace quantitative claims and figure data to the supplied execution artifacts. "
    "An evidence anchor must resolve to actual evidence, not just look like a path.",
    "State limitations and the scope of the claim. Write an actual impact statement "
    "when the target venue requires it; pipeline provenance is not an impact analysis.",
    "Put complete proofs, implementation details and additional results in authored "
    "supplementary sections. Internal AC scores, revision directives, local paths and "
    "state JSON belong in the internal report, not in the submission manuscript.",
    "HTML is a manuscript preview. Conference readiness also requires a chosen "
    "venue, year and track, the official template, compiled PDF, verified bibliography, "
    "anonymized supplementary artifacts and the required scientific review.",
)


# Disallowed tags are escaped. Only the listed attributes survive parsing.
_ALLOWED_TAGS = {
    "p", "h3", "h4", "ul", "ol", "li", "strong", "em", "code", "pre",
    "table", "thead", "tbody", "tr", "th", "td", "figure", "figcaption",
    "img", "a", "br", "blockquote", "hr",
}
_ALLOWED_ATTRS = {
    "img": {"src", "alt", "title"},
    "a": {"href", "title"},
    "li": {"id"},
    "table": {"id"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
    "figure": {"id"},
}


class _SafeHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALLOWED_TAGS:
            self.parts.append(html_lib.escape(self.get_starttag_text()))
            return
        allowed_attrs = _ALLOWED_ATTRS.get(tag, set())
        clean_attrs: list[str] = []
        seen: set[str] = set()
        for name, value in attrs:
            if name not in allowed_attrs or value is None or name in seen:
                continue
            seen.add(name)
            if name in {"href", "src"}:
                normalized = re.sub(r"[\x00-\x20\x7f]", "", value)
                try:
                    scheme = urlsplit(normalized).scheme.lower()
                except ValueError:
                    continue
                if scheme not in {"", "http", "https", "mailto"}:
                    continue
                if name == "src" and (scheme or normalized.startswith("//")):
                    continue
            clean_attrs.append(f'{name}="{html_lib.escape(value, quote=True)}"')
        joined = (" " + " ".join(clean_attrs)) if clean_attrs else ""
        suffix = " />" if tag in {"img", "br", "hr"} else ">"
        self.parts.append(f"<{tag}{joined}{suffix}")

    def handle_endtag(self, tag: str) -> None:
        closing = f"</{tag}>"
        if tag not in {"img", "br", "hr"}:
            self.parts.append(closing if tag in _ALLOWED_TAGS else html_lib.escape(closing))

    def handle_data(self, data: str) -> None:
        self.parts.append(html_lib.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")


def _sanitize_html(prose: str) -> str:
    parser = _SafeHTML()
    parser.feed(prose)
    parser.close()
    return "".join(parser.parts)


def _esc(s: str) -> str:
    return html_lib.escape(str(s) if s is not None else "")


def _table_from_dict(data: dict[str, Any], col_a: str = "key", col_b: str = "value") -> str:
    rows = "".join(
        f"<tr><td><code>{_esc(k)}</code></td><td>{_esc(v)}</td></tr>" for k, v in data.items()
    )
    return (
        f"<table><thead><tr><th>{_esc(col_a)}</th><th>{_esc(col_b)}</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _EmbedScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.figure_ids: list[str] = []
        self.table_ids: list[str] = []
        self.citation_source_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "img":
            src = values.get("src") or ""
            match = re.fullmatch(r"figures/(f_[a-z0-9_]+)\.png", src)
            if match:
                self.figure_ids.append(match.group(1))
        if tag == "table":
            table_id = values.get("id") or ""
            if re.fullmatch(r"t_[a-z0-9_]+", table_id):
                self.table_ids.append(table_id)
        if tag == "a":
            href = values.get("href") or ""
            match = re.fullmatch(r"#ref_([-A-Za-z0-9_:.]+)", href)
            if match:
                self.citation_source_ids.append(match.group(1))


def _scan_embeds(prose_html: str) -> tuple[set[str], set[str], set[str]]:
    scanner = _EmbedScanner()
    scanner.feed(prose_html)
    scanner.close()
    return (
        set(scanner.figure_ids),
        set(scanner.table_ids),
        set(scanner.citation_source_ids),
    )


def _dotpath(state: dict[str, Any], path: str) -> Any:
    if not isinstance(path, str) or not path.startswith("worker_report."):
        raise SakanaPaperError(f"table data_source must be a worker_report dot-path: {path!r}")
    cur: Any = state
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
            continue
        raise SakanaPaperError(f"table data_source path not found: {path!r}")
    return cur


def _render_data_table(table_spec: dict[str, Any], worker_report: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    table_id = table_spec["table_id"]
    data_source = table_spec["data_source"]
    value = _dotpath({"worker_report": worker_report}, data_source)
    requested_columns = [str(c) for c in table_spec.get("columns") or []]

    if isinstance(value, dict) and all(not isinstance(v, dict) for v in value.values()):
        columns = requested_columns or ["metric", "value"]
        if len(columns) != 2:
            raise SakanaPaperError(f"table {table_id!r} scalar dict tables require exactly two columns")
        rows = [[key, _fmt_table_value(val)] for key, val in value.items()]
    elif isinstance(value, dict) and all(isinstance(v, dict) for v in value.values()):
        nested_columns = requested_columns or sorted(
            {str(k) for row in value.values() for k in row.keys()}
        )
        columns = ["item", *nested_columns]
        rows = [
            [key, *[_fmt_table_value(row.get(col, "")) for col in nested_columns]]
            for key, row in value.items()
        ]
    elif isinstance(value, list) and all(isinstance(row, dict) for row in value):
        columns = requested_columns or sorted({str(k) for row in value for k in row.keys()})
        rows = [[_fmt_table_value(row.get(col, "")) for col in columns] for row in value]
    else:
        columns = requested_columns or ["value"]
        if len(columns) != 1:
            raise SakanaPaperError(f"table {table_id!r} scalar values require one column")
        rows = [[_fmt_table_value(value)]]

    head = "".join(f"<th>{_esc(col)}</th>" for col in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    html = (
        f"<table id=\"{_esc(table_id)}\"><caption>{_esc(table_spec.get('title', table_id))}</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )
    receipt = {
        "table_id": table_id,
        "data_source": data_source,
        "source_digest": _json_digest(value),
        "row_count": len(rows),
        "columns": columns,
    }
    return html, receipt


def _fmt_table_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _replace_table_placeholders(prose: str, table_html: dict[str, str]) -> str:
    for table_id, rendered in table_html.items():
        pattern = re.compile(rf"<table id=\"{re.escape(table_id)}\">\s*</table>")
        prose, count = pattern.subn(rendered, prose)
        if count == 0:
            raise SakanaPaperError(f"table {table_id!r} must appear as an empty table placeholder")
    return prose


def _render_directives_table(directives: list[dict[str, Any]]) -> str:
    if not directives:
        return "<p><em>(no camera-ready directives)</em></p>"
    rows = []
    for i, d in enumerate(directives):
        origin = ", ".join(d.get("origin_critic_ids", []))
        rows.append(
            f"<tr><td>{i + 1}</td>"
            f"<td>{_esc(d.get('directive', ''))}</td>"
            f"<td><code>{_esc(d.get('must_appear_in_section', ''))}</code></td>"
            f"<td><code>{_esc(origin)}</code></td>"
            f"<td>{_esc(d.get('rationale', ''))}</td></tr>"
        )
    body = "".join(rows)
    return (
        "<table class='directives'><thead><tr><th>#</th><th>Directive</th>"
        "<th>Section</th><th>Origin critics</th><th>Rationale</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _render_critic_summary(reviews: list[dict[str, Any]]) -> str:
    if not reviews:
        return ""
    rows = []
    for r in reviews:
        scores = r.get("scores", {})
        methodology = r.get("direct_methodology_for_user", {}) or {}
        rows.append(
            "<tr>"
            f"<td><code>{_esc(r.get('critic_id'))}</code></td>"
            f"<td>{_esc(r.get('verdict_candidate'))}</td>"
            f"<td>{_esc(scores.get('validity'))}</td>"
            f"<td>{_esc(scores.get('necessity'))}</td>"
            f"<td>{_esc(scores.get('reproducibility'))}</td>"
            f"<td>{_esc(scores.get('taste_alignment'))}</td>"
            f"<td>{_esc(methodology.get('verdict', '—'))}</td>"
            f"<td>{_esc(r.get('so_what', '')[:200])}</td>"
            "</tr>"
        )
    return (
        "<h3>Reviewer summary (practitioner take)</h3>"
        "<table class='reviewer-summary'><thead><tr>"
        "<th>critic_id</th><th>verdict</th><th>validity</th><th>necessity</th>"
        "<th>reproducibility</th><th>taste</th><th>methodology for user</th><th>so_what</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_next_actions(reviews: list[dict[str, Any]]) -> str:
    """Pull all next_actions from every review into a single consolidated list."""
    items: list[str] = []
    for r in reviews:
        for action in r.get("next_actions", []):
            items.append(
                f"<li><strong>{_esc(action.get('action'))}</strong> "
                f"<span class='muted'>(owner: <code>{_esc(action.get('owner_role'))}</code>, "
                f"ETA: {_esc(action.get('eta_weeks'))}w, "
                f"from <code>{_esc(r.get('critic_id'))}</code>)</span></li>"
            )
    if not items:
        return ""
    return (
        "<h3>Consolidated next actions (from every reviewer)</h3>"
        "<ul class='next-actions'>" + "".join(items) + "</ul>"
    )


def render_sakana_paper(
    outline: dict[str, Any],
    sections: dict[str, dict[str, Any]],
    figures_registry: dict[str, dict[str, Any]],
    ac_decision: dict[str, Any],
    camera_ready_revision: dict[str, Any],
    orchestrator_reduction: dict[str, Any],
    rebuttal_reviews: list[dict[str, Any]],
    node: dict[str, Any],
    worker_report: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """Assemble a manuscript preview from submitted sections.

    Returns a publication_dispatch shape so the frontend's existing
    `Published artifacts` section can list the outputs without changes.
    """
    mental_model = camera_ready_revision.get("mental_model_statement") or outline.get("mental_model_statement")
    if not mental_model or len(mental_model.strip()) < 40:
        raise SakanaPaperError(
            "mental_model_statement is missing or trivially short. A paper without a mental model is a list of results."
        )

    # Check every referenced figure exists; flag missing ones loudly.
    referenced_ids: set[str] = set()
    for section in sections.values():
        for fid in section.get("embedded_figure_ids", []) or []:
            referenced_ids.add(fid)
    missing_fids = sorted(referenced_ids - set(figures_registry.keys()))
    if missing_fids:
        raise SakanaPaperError(f"sections reference unregistered figures: {missing_fids}")
    actual_figure_ids: set[str] = set()
    actual_table_ids: set[str] = set()
    for section_id, section in sections.items():
        section_figures, section_tables, section_citations = _scan_embeds(
            section.get("prose_html") or ""
        )
        declared_figures = set(section.get("embedded_figure_ids") or [])
        declared_tables = set(section.get("embedded_table_ids") or [])
        declared_citations = set(section.get("citation_source_ids") or [])
        if section_figures != declared_figures:
            raise SakanaPaperError(
                f"section {section_id!r} embedded_figure_ids do not match manuscript images"
            )
        if section_tables != declared_tables:
            raise SakanaPaperError(
                f"section {section_id!r} embedded_table_ids do not match manuscript table placeholders"
            )
        if section_citations != declared_citations:
            raise SakanaPaperError(
                f"section {section_id!r} citation_source_ids do not match manuscript reference links"
            )
        actual_figure_ids.update(section_figures)
        actual_table_ids.update(section_tables)

    figure_specs = {
        spec.get("figure_id"): spec for spec in outline.get("figure_specs", [])
    }
    for figure_id in actual_figure_ids:
        meta = figures_registry.get(figure_id) or {}
        figure_type = meta.get("figure_type") or (figure_specs.get(figure_id) or {}).get("figure_type")
        if figure_type in {"score_radar", "claim_tree_status"}:
            raise SakanaPaperError(
                f"figure {figure_id!r} is internal review material and cannot be embedded in the manuscript"
            )
        artifact_path = Path(str(meta.get("artifact_path") or ""))
        if not artifact_path.exists() or not artifact_path.is_file():
            raise SakanaPaperError(f"figure {figure_id!r} artifact file is missing")

    table_specs = {
        spec.get("table_id"): spec for spec in outline.get("table_specs", [])
    }
    unknown_tables = sorted(actual_table_ids - set(table_specs))
    if unknown_tables:
        raise SakanaPaperError(f"sections reference undeclared tables: {unknown_tables}")
    unused_tables = sorted(set(table_specs) - actual_table_ids)
    if unused_tables:
        raise SakanaPaperError(f"declared tables were not embedded: {unused_tables}")
    rendered_tables: dict[str, str] = {}
    table_receipts: list[dict[str, Any]] = []
    for table_id in sorted(actual_table_ids):
        table_html, receipt = _render_data_table(table_specs[table_id], worker_report)
        rendered_tables[table_id] = table_html
        table_receipts.append(receipt)

    title = outline.get("title") or node.get("claim_contract", {}).get("claim_under_test", "Untitled")
    abstract = sections.get("abstract", {}).get("prose_html") or outline.get("abstract_seed", "")
    abstract_sanitized = _sanitize_html(abstract)

    # Body sections follow the outline; meta sections render separately.
    META_SECTIONS = {"abstract", "impact_statement", "references", "supplementary"}
    body_sections_html: list[str] = []
    contents: list[str] = []
    for sec_spec in outline["section_outline"]:
        sid = sec_spec["section_id"]
        if sid in META_SECTIONS:
            continue
        section = sections.get(sid)
        if not section:
            raise SakanaPaperError(f"section {sid!r} was in outline but never submitted")
        sec_title = sec_spec.get("title") or sid.replace("_", " ").title()
        prose = _replace_table_placeholders(
            _sanitize_html(section["prose_html"]),
            {table_id: rendered_tables[table_id] for table_id in section.get("embedded_table_ids", []) or []},
        )
        heading = f"{len(body_sections_html) + 1}. {_esc(sec_title)}"
        contents.append(f"<a href='#{_esc(sid)}'>{heading}</a>")
        body_sections_html.append(
            f"<section id='{_esc(sid)}'><h2>{heading}</h2>{prose}</section>"
        )

    # Meta sections rendered separately (single-column blocks).
    impact_section = sections.get("impact_statement")
    references_section = sections.get("references")
    supplementary_section = sections.get("supplementary")
    supplementary_html = (
        "<section class='singlecolumn' id='supplementary'><h2>Appendix</h2>"
        f"{_sanitize_html(supplementary_section['prose_html'])}</section>"
        if supplementary_section else ""
    )

    # Internal review material belongs in the companion report.
    directives_html = _render_directives_table(ac_decision.get("camera_ready_directives", []))
    reviewer_summary = _render_critic_summary(rebuttal_reviews)
    next_actions_block = _render_next_actions(rebuttal_reviews)
    methodology_assessment = (
        ac_decision.get("rebuttal_synthesis", {}) or {}
    ).get("methodology_assessment", {}) or {}

    methodology_html = ""
    if methodology_assessment:
        methodology_html = (
            "<section class='methodology-card'><h2>Methodology for the original user</h2>"
            f"<p><strong>AC aggregate verdict:</strong> "
            f"<span class='verdict-{_esc(methodology_assessment.get('aggregate_verdict'))}'>"
            f"{_esc(methodology_assessment.get('aggregate_verdict'))}</span></p>"
            f"<p><strong>Methodology the user can apply:</strong> "
            f"{_esc(methodology_assessment.get('methodology_for_user'))}</p>"
            + (
                f"<p><strong>Remaining gap:</strong> {_esc(methodology_assessment.get('remaining_gap'))}</p>"
                if methodology_assessment.get("remaining_gap") else ""
            )
            + "</section>"
        )

    css = _ICML_CSS

    # State bundle for supplementary (JSON pretty-printed).
    state_bundle = {
        "node": node,
        "worker_report": worker_report,
        "orchestrator_reduction": orchestrator_reduction,
        "ac_decision": ac_decision,
        "camera_ready_revision": camera_ready_revision,
        "rebuttal_reviews": rebuttal_reviews,
    }
    supp_json = html_lib.escape(json.dumps(state_bundle, indent=2, ensure_ascii=False))

    impact_html = (
        f"<section class='singlecolumn'><h2>Impact Statement</h2>"
        f"{_sanitize_html(impact_section['prose_html'])}</section>"
        if impact_section else ""
    )
    references_html = (
        f"<section class='singlecolumn'><h2>References</h2>"
        f"{_sanitize_html(references_section['prose_html'])}</section>"
        if references_section else ""
    )

    paper_html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{css}</style>
</head><body class="icml-paper">
<nav class="preview-notice" aria-label="Preview information">
  <span>Manuscript preview · venue formatting and submission checks pending</span>
  <a href="interactive_summary.html">Internal research report</a>
</nav>
<header class="paper-header">
  <h1>{_esc(title)}</h1>
</header>

<section class="abstract">
  <h2>Abstract</h2>
  <div class="abstract-body">{abstract_sanitized}</div>
</section>

<nav class="contents" aria-label="Contents">{''.join(contents)}</nav>

<main class="twocolumn">
{''.join(body_sections_html)}
</main>

{impact_html}

{references_html}

{supplementary_html}

</body></html>
"""
    paper_path = output_dir / "paper.html"
    paper_path.write_text(paper_html, encoding="utf-8")

    # Also write a minimal interactive_summary.html (slides shown separately).
    interactive_path = output_dir / "interactive_summary.html"
    interactive_path.write_text(
        _render_interactive_summary(
            title, mental_model, ac_decision, rebuttal_reviews, figures_registry,
            audit_html=(
                methodology_html
                + "<section class='camera-ready-card'><h2>Internal revision directives</h2>"
                + directives_html + "</section>" + reviewer_summary + next_actions_block
                + "<details><summary>Internal evidence and review record</summary>"
                + f"<pre class='state-bundle'>{supp_json}</pre></details>"
            ),
        ),
        encoding="utf-8",
    )
    slides_path = output_dir / "slides_summary.html"
    slides_path.write_text(
        _render_slides(title, mental_model, ac_decision, rebuttal_reviews, figures_registry),
        encoding="utf-8",
    )

    dispatch = {
        "type": "publication_dispatch",
        "decision": ac_decision.get("decision"),
        "evidence_is_fake": False,
        "requested_outputs": ["paper_html", "interactive_html", "slides_html"],
        "rendered_artifacts": [
            {"output": "paper_html", "artifact_path": str(paper_path)},
            {"output": "interactive_html", "artifact_path": str(interactive_path)},
            {"output": "slides_html", "artifact_path": str(slides_path)},
        ],
        "rendered_tables": table_receipts,
        "skipped_outputs": [],
        "blocked_reason": None,
        "dispatch_path": str(output_dir / "publication_dispatch.json"),
    }
    (output_dir / "publication_dispatch.json").write_text(
        json.dumps(dispatch, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return dispatch


# --- minimal companion renderers ---------------------------------------- #


def _render_interactive_summary(
    title: str,
    mental_model: str,
    ac_decision: dict[str, Any],
    reviews: list[dict[str, Any]],
    figures_registry: dict[str, dict[str, Any]],
    *,
    audit_html: str = "",
) -> str:
    figs = "".join(
        f"<figure><img src='figures/{fid}.png' alt='{_esc(meta.get('caption', ''))}'>"
        f"<figcaption>{_esc(meta.get('caption', ''))}</figcaption></figure>"
        for fid, meta in figures_registry.items()
    )
    reviews_html = "".join(
        f"<details><summary><code>{_esc(r['critic_id'])}</code> — "
        f"{_esc((r.get('direct_methodology_for_user') or {}).get('verdict','?'))}</summary>"
        f"<p><strong>so what:</strong> {_esc(r.get('so_what'))}</p>"
        f"<p><strong>practitioner take:</strong> {_esc(r.get('practitioner_take'))}</p>"
        "</details>"
        for r in reviews
    )
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_esc(title)} — internal report</title>"
        f"<style>{_ICML_CSS}</style></head><body class='interactive-page'>"
        "<p><a href='paper.html'>Manuscript preview</a> · Internal review, not an external acceptance</p>"
        f"<h1>{_esc(title)}</h1>"
        f"<section class='mental-model-card'><h2>Mental model</h2><p>{_esc(mental_model)}</p></section>"
        f"<section><h2>Figures</h2>{figs}</section>"
        f"<section><h2>Reviewer take</h2>{reviews_html}</section>"
        f"{audit_html}"
        "</body></html>"
    )


def _render_slides(
    title: str,
    mental_model: str,
    ac_decision: dict[str, Any],
    reviews: list[dict[str, Any]],
    figures_registry: dict[str, dict[str, Any]],
) -> str:
    fig_items = list(figures_registry.items())
    fig_slides = "".join(
        f"<section class='slide'><h2>Figure: {_esc(fid)}</h2>"
        f"<img src='figures/{fid}.png' alt='{_esc(meta.get('caption', ''))}'>"
        f"<p>{_esc(meta.get('caption', ''))}</p></section>"
        for fid, meta in fig_items
    )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(title)} — slides</title>"
        f"<style>{_SLIDES_CSS}</style></head><body class='slides-deck'>"
        f"<section class='slide'><h1>{_esc(title)}</h1></section>"
        f"<section class='slide mental-model'><h2>Mental model</h2><p>{_esc(mental_model)}</p></section>"
        f"<section class='slide'><h2>AC decision</h2><p>{_esc(ac_decision.get('decision'))} "
        f"({_esc(ac_decision.get('confidence'))})</p></section>"
        f"{fig_slides}"
        "</body></html>"
    )


# --- inline CSS ---------------------------------------------------------- #


_ICML_CSS = """
:root { --ink:#111; --muted:#666; --accent:#2845a8; --hl-bg:#eef3ff; --warn:#c25450; }
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body.icml-paper, body.interactive-page { font-family: 'Georgia', serif; color: var(--ink); max-width: 850px;
  margin: 0 auto; padding: 28px 48px 80px; line-height: 1.65; font-size: 16px; overflow-wrap: anywhere; }
body.icml-paper h1 { font-size: 2em; line-height: 1.25; text-align: center; margin: 1.3em 0 0.7em; }
body.icml-paper h2 { font-size: 1.15em; margin: 1.4em 0 0.6em 0; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
body.icml-paper h3 { font-size: 1.0em; margin: 1.2em 0 0.5em 0; }
.paper-header { text-align: center; margin-bottom: 1.2em; }
.paper-header .authors { color: var(--muted); font-size: 0.95em; }
.paper-header .affiliation { color: var(--muted); font-size: 0.9em; }
.abstract { background:#fafafa; border-left: 3px solid var(--accent); padding: 12px 18px;
  margin: 1.5em 0; }
.abstract h2 { font-size: 0.95em; text-transform: uppercase; letter-spacing: 0.06em;
  margin: 0 0 0.4em 0; border: none; }
.mental-model-card { background: var(--hl-bg); border: 2px solid var(--accent); border-radius: 6px;
  padding: 18px 22px; margin: 1.4em 0 2em 0; }
.mental-model-card h2 { color: var(--accent); margin: 0 0 0.4em 0; border: none; }
.mental-model-card p { font-size: 1.05em; }
.methodology-card { background:#f6fff6; border:1px solid #cde8cd; border-radius:6px;
  padding: 14px 18px; margin: 0 0 1.6em 0; }
.methodology-card .verdict-provides { color:#2e7d2e; font-weight: 600; }
.methodology-card .verdict-partial { color:#b88a00; font-weight: 600; }
.methodology-card .verdict-absent { color: var(--warn); font-weight: 600; }
main.twocolumn { column-count: 1; }
main.twocolumn h2 { break-after: avoid; }
.preview-notice { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px;
  font: 12px/1.5 system-ui, sans-serif; color: var(--muted); border-bottom: 1px solid #ddd; padding-bottom: 12px; }
.contents { display: flex; flex-wrap: wrap; gap: 8px 20px; margin: 24px 0;
  font: 13px/1.5 system-ui, sans-serif; border-bottom: 1px solid #ddd; padding-bottom: 18px; }
a { color: var(--accent); text-underline-offset: 3px; overflow-wrap: anywhere; }
p { margin: 0.7em 0; }
section { scroll-margin-top: 20px; }
.singlecolumn { column-count: 1; margin-top: 2em; }
.mental-model-link { color: var(--accent); font-size: 0.95em; margin: 0 0 0.5em 0; }
.camera-ready-card table.directives th, .camera-ready-card table.directives td {
  border:1px solid #ddd; padding: 6px 8px; font-size: 0.9em; vertical-align: top; }
.reviewer-summary th, .reviewer-summary td { border: 1px solid #eee; padding: 4px 8px;
  font-size: 0.85em; }
.next-actions li { margin: 4px 0; }
.muted { color: var(--muted); }
figure { margin: 14px auto; text-align: center; }
figure img { max-width: 100%; border:1px solid #eee; }
figcaption { font-size: 0.88em; color: var(--muted); padding: 4px 0; }
pre.state-bundle { font-size: 0.78em; background:#fafafa; padding: 12px; overflow:auto; }
table { border-collapse: collapse; margin: 16px 0; width: 100%; font-size: 0.9em; }
table th { border-top: 2px solid #333; border-bottom: 1px solid #aaa; text-align: left; }
table th, table td { padding: 7px 10px; }
table tbody tr:last-child td { border-bottom: 2px solid #333; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 640px) {
  body.icml-paper, body.interactive-page { padding: 20px; font-size: 15px; }
  body.icml-paper h1 { font-size: 1.65em; }
  table { display: block; overflow-x: auto; }
}
@media print {
  @page { margin: 20mm; }
  body.icml-paper { max-width: none; padding: 0; font-size: 10pt; line-height: 1.4; }
  .preview-notice, .contents { display: none; }
  figure, table { break-inside: avoid; }
  a { color: inherit; }
}
"""


_SLIDES_CSS = """
body.slides-deck { font-family: 'Georgia', serif; margin: 0; padding: 0; background:#222; color:#eee; }
.slide { min-height: 90vh; padding: 60px 80px; border-bottom: 1px solid #444;
  display: flex; flex-direction: column; justify-content: center; }
.slide h1 { font-size: 2.4em; }
.slide h2 { font-size: 1.6em; color:#9ab8ff; }
.slide.mental-model { background:#1c2e6b; }
.slide img { max-width: 80%; max-height: 60vh; margin: 16px auto; display:block; }
"""
