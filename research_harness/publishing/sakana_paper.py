"""Sakana-v2-style ICML paper assembler.

The deterministic publishing/paper_html.py renderer was a state-bundle
dump. This assembler is different: it takes LLM-written sections (via
submit_paper_section), server-rendered figures (via register_paper_figure),
the mental_model_statement from the camera-ready revision, and the AC's
camera-ready directives, and stitches them into a real two-column ICML
HTML paper.

Key invariants enforced:
- The mental_model_statement appears as a highlighted card immediately
  after the abstract. A paper without a mental model is rejected.
- At least one figure must be embedded somewhere in the body.
- Sections that reference figure_ids must reference figures that exist
  in the figures_registry.
- HTML in submitted prose is sanitized to a whitelist of safe tags.

Layout: ICML \\twocolumn body for sections 1..N; single-column blocks
for abstract, mental_model card, impact statement, references, and
supplementary state bundle.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from pathlib import Path
from typing import Any


class SakanaPaperError(ValueError):
    pass


# Whitelist of tags we accept inside submitted prose. Anything else is
# stripped at sanitize time — we keep paragraphs/tables/figures/links
# but drop scripts/styles/iframes/etc.
_ALLOWED_TAGS = {
    "p", "h3", "h4", "ul", "ol", "li", "strong", "em", "code", "pre",
    "table", "thead", "tbody", "tr", "th", "td", "figure", "figcaption",
    "img", "a", "br", "blockquote", "hr",
}
_ALLOWED_ATTRS = {
    "img": {"src", "alt", "title"},
    "a": {"href", "title"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
    "figure": {"id"},
}


def _sanitize_html(prose: str) -> str:
    """Conservative HTML sanitizer for submitted section prose.

    We accept a small whitelist of tags and attributes. Disallowed tags
    have their angle brackets escaped so they render as visible text
    rather than executing. Attributes outside the whitelist are dropped.
    """
    def replace_tag(match: re.Match[str]) -> str:
        is_close = bool(match.group(1))
        tag_name = match.group(2).lower()
        attrs_str = match.group(3) or ""
        if tag_name not in _ALLOWED_TAGS:
            return html_lib.escape(match.group(0))
        if is_close:
            return f"</{tag_name}>"
        allowed_attrs = _ALLOWED_ATTRS.get(tag_name, set())
        clean_attrs: list[str] = []
        for attr_match in re.finditer(
            r'([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*"([^"]*)"', attrs_str
        ):
            name = attr_match.group(1).lower()
            value = attr_match.group(2)
            if name in allowed_attrs:
                # Disallow javascript: URLs in href/src
                if name in {"href", "src"} and value.strip().lower().startswith("javascript:"):
                    continue
                clean_attrs.append(f'{name}="{html_lib.escape(value, quote=True)}"')
        joined = (" " + " ".join(clean_attrs)) if clean_attrs else ""
        is_void = tag_name in {"img", "br", "hr"}
        suffix = " />" if is_void else ">"
        return f"<{tag_name}{joined}{suffix}"

    return re.sub(r"<(/?)\s*([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>", replace_tag, prose)


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
    """Assemble the ICML two-column HTML paper from LLM submissions.

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
    if not figures_registry:
        raise SakanaPaperError(
            "at least one figure must be registered — a Sakana-v2-style paper without figures is a memo"
        )

    title = outline.get("title") or node.get("claim_contract", {}).get("claim_under_test", "Untitled")
    abstract = sections.get("abstract", {}).get("prose_html") or outline.get("abstract_seed", "")
    abstract_sanitized = _sanitize_html(abstract)

    # Two-column body sections in outline order (excluding meta sections).
    META_SECTIONS = {"abstract", "impact_statement", "references", "supplementary"}
    body_sections_html: list[str] = []
    for i, sec_spec in enumerate(outline["section_outline"], start=1):
        sid = sec_spec["section_id"]
        if sid in META_SECTIONS:
            continue
        section = sections.get(sid)
        if not section:
            raise SakanaPaperError(f"section {sid!r} was in outline but never submitted")
        sec_title = sec_spec.get("title") or sid.replace("_", " ").title()
        prose = _sanitize_html(section["prose_html"])
        mml = section.get("mental_model_link", "")
        mml_html = (
            f"<p class='mental-model-link'><em>How this connects to the mental model:</em> "
            f"{_esc(mml)}</p>" if mml else ""
        )
        body_sections_html.append(
            f"<section><h2>{i}. {_esc(sec_title)}</h2>{mml_html}{prose}</section>"
        )

    # Meta sections rendered separately (single-column blocks).
    impact_section = sections.get("impact_statement")
    references_section = sections.get("references")

    # AC + reviewer panel (always shown, single-column).
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

    # Mental model card — highlighted block between abstract and intro.
    mental_model_card = (
        "<section class='mental-model-card'>"
        "<h2>Mental model this paper gives you</h2>"
        f"<p>{_esc(mental_model)}</p></section>"
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
        if impact_section else
        "<section class='singlecolumn'><h2>Impact Statement</h2>"
        "<p>This research was generated under the research_harness MCP pipeline, "
        "with deterministic critic routing, LLM-driven practitioner reviewers, "
        "an AC that emits camera-ready directives even on accept, and a paper writer "
        "that requires an explicit mental model. Every claim, score, directive, "
        "and revision is persisted under the run directory for full auditability.</p></section>"
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
<header class="paper-header">
  <h1>{_esc(title)}</h1>
  <div class="authors">Research Harness — Practitioner-Reviewed Pipeline</div>
  <div class="affiliation">Sakana-v2 mirror under MCP-driven reasoning</div>
</header>

<section class="abstract">
  <h2>Abstract</h2>
  <div class="abstract-body">{abstract_sanitized}</div>
</section>

{mental_model_card}

{methodology_html}

<main class="twocolumn">
{''.join(body_sections_html)}
</main>

<section class="singlecolumn camera-ready-card">
  <h2>Area-Chair camera-ready directives</h2>
  <p class="muted">Directives the Professor folded into this revision. Even on accept,
    the AC names rebuttal-surfaced insights that must shape camera-ready.</p>
  {directives_html}
</section>

<section class="singlecolumn">
  {reviewer_summary}
  {next_actions_block}
</section>

{impact_html}

{references_html}

<section class="singlecolumn supplementary">
  <h2>Supplementary — full state bundle</h2>
  <details><summary>Click to expand JSON</summary>
    <pre class='state-bundle'>{supp_json}</pre>
  </details>
</section>

</body></html>
"""
    paper_path = output_dir / "paper.html"
    paper_path.write_text(paper_html, encoding="utf-8")

    # Also write a minimal interactive_summary.html (slides shown separately).
    interactive_path = output_dir / "interactive_summary.html"
    interactive_path.write_text(
        _render_interactive_summary(title, mental_model, ac_decision, rebuttal_reviews, figures_registry),
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
        f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(title)} — interactive</title>"
        f"<style>{_ICML_CSS}</style></head><body class='interactive-page'>"
        f"<h1>{_esc(title)}</h1>"
        f"<section class='mental-model-card'><h2>Mental model</h2><p>{_esc(mental_model)}</p></section>"
        f"<section><h2>Figures</h2>{figs}</section>"
        f"<section><h2>Reviewer take</h2>{reviews_html}</section>"
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
body.icml-paper { font-family: 'Georgia', serif; color: var(--ink); max-width: 1100px;
  margin: 0 auto; padding: 32px 48px 80px; line-height: 1.5; font-size: 13.5px; }
body.icml-paper h1 { font-size: 1.7em; line-height: 1.25; text-align: center; margin: 0 0 0.4em 0; }
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
main.twocolumn { column-count: 2; column-gap: 28px; }
main.twocolumn section { break-inside: avoid-column; }
main.twocolumn h2 { break-after: avoid-column; }
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
table { border-collapse: collapse; margin: 8px 0; }
table th { background:#f3f3f3; }
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
