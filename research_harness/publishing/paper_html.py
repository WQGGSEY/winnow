"""Conference-paper-style HTML renderer.

Mirrors Sakana v2's ICML LaTeX template
(blank_icml_latex/template.tex) section-for-section, but emits HTML so the
operator can read it like a paper without a TeX toolchain:

    Title
    Abstract
    1. Introduction
    2. Related Work
    3. Background
    4. Method
    5. Experimental Setup
    6. Experiments
    7. Conclusion
    Impact Statement
    Supplementary Material

Content is composed deterministically from the research_state_bundle the
production pipeline already builds (node + worker_report + reduction +
critic_reviews + ac_decision + refined plan if present). No LLM call;
publication remains a read-only renderer.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def render_paper_html(state: dict[str, Any], output_path: Path) -> None:
    node = state["node"]
    worker_report = state.get("worker_report", {}) or {}
    reduction = state.get("orchestrator_reduction", {}) or {}
    critic_reviews = state.get("critic_reviews", []) or []
    rebuttal_reviews = state.get("rebuttal_critic_reviews", []) or []
    ac_decision = state.get("ac_decision", {}) or {}
    failure_branch_prior = state.get("failure_branch_prior", {}) or {}

    contract = node["claim_contract"]
    title = contract["claim_under_test"]
    abstract = _build_abstract(node, worker_report, reduction, ac_decision)
    figures = _collect_figures(worker_report, output_path.parent)
    sections = [
        ("1. Introduction", _build_introduction(node, contract, reduction)),
        ("2. Related Work", _build_related_work(node, failure_branch_prior)),
        ("3. Background", _build_background(node)),
        ("4. Method", _build_method(node, contract)),
        ("5. Experimental Setup", _build_experimental_setup(node, worker_report)),
        (
            "6. Experiments",
            _build_experiments(worker_report, reduction, critic_reviews, figures),
        ),
        (
            "7. Conclusion",
            _build_conclusion(reduction, rebuttal_reviews, ac_decision),
        ),
    ]
    impact = _build_impact_statement(node, ac_decision)
    references = _build_references(node, failure_branch_prior)
    supplementary = _build_supplementary(worker_report, reduction, ac_decision)

    rendered = _render(
        title=title,
        authors="Research Harness — Claim-First Sakana-v2 Mirror",
        affiliation="Operator-Gated Subscription Worker Pool",
        abstract=abstract,
        sections=sections,
        impact=impact,
        references=references,
        supplementary=supplementary,
        metadata={
            "node_id": node["id"],
            "node_type": node["type"],
            "domain": node.get("domain", "n/a"),
            "ac_decision": ac_decision.get("decision", "n/a"),
            "confidence": ac_decision.get("confidence", "n/a"),
            "rendered_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")


def _build_abstract(
    node: dict[str, Any],
    worker_report: dict[str, Any],
    reduction: dict[str, Any],
    ac_decision: dict[str, Any],
) -> str:
    contract = node["claim_contract"]
    verdict = reduction.get("final_verdict", worker_report.get("claim_verdict_candidate", "n/a"))
    decision = ac_decision.get("decision", "n/a")
    metrics = worker_report.get("metrics") or {}
    baselines = worker_report.get("baselines") or {}
    parts = [
        f"We test the claim: \"{contract['claim_under_test']}\".",
        "The contract pins {b} mandatory baselines, {s} success criteria, and {d} disproof conditions.".format(
            b=len(contract["mandatory_baselines"]),
            s=len(contract["success_criteria"]),
            d=len(contract["disproof_conditions"]),
        ),
    ]
    if metrics:
        m_summary = ", ".join(f"{k}={v}" for k, v in list(metrics.items())[:3])
        parts.append(f"Headline metrics: {m_summary}.")
    if baselines:
        b_summary = ", ".join(f"{k}={v}" for k, v in list(baselines.items())[:3])
        parts.append(f"Baselines: {b_summary}.")
    parts.append(
        f"The orchestrator's final verdict was \"{verdict}\" and the area-chair "
        f"decision was \"{decision}\"."
    )
    return " ".join(parts)


def _build_introduction(
    node: dict[str, Any],
    contract: dict[str, Any],
    reduction: dict[str, Any],
) -> str:
    lineage = node.get("lineage", {}) or {}
    facets = lineage.get("covers_goal_facets", []) or []
    facets_html = (
        "<ul>" + "".join(f"<li>{html.escape(str(f))}</li>" for f in facets) + "</ul>"
        if facets
        else "<p><em>No explicit goal facets recorded.</em></p>"
    )
    intro = (
        "<p>This work investigates a single claim under a fixed claim-contract, "
        "rather than chasing improvements to a numeric metric. The contract "
        "pins what would count as evidence in favor of the claim, what would "
        "count as evidence against, and which baselines are mandatory.</p>"
        f"<p><strong>Claim under test:</strong> {html.escape(contract['claim_under_test'])}</p>"
        "<p><strong>Goal facets this claim covers:</strong></p>"
        f"{facets_html}"
    )
    if reduction:
        intro += (
            "<p><strong>Research status after orchestrator reduction:</strong> "
            f"{html.escape(str(reduction.get('research_status', 'n/a')))}.</p>"
        )
    return intro


def _build_related_work(
    node: dict[str, Any],
    failure_branch_prior: dict[str, Any],
) -> str:
    inherited = node.get("lineage", {}).get("inherited_assumptions", []) or []
    selected_failures = failure_branch_prior.get("selected_failure_files", []) or []
    risk_controls = failure_branch_prior.get("risk_controls", []) or []
    lines = [
        "<p>Prior internal results retrieved via the failure-memory index that "
        "informed this run:</p>"
    ]
    if not selected_failures and not risk_controls:
        lines.append("<p><em>No prior failure memory was selected as a control.</em></p>")
    else:
        lines.append("<ul>")
        for control in risk_controls:
            lines.append(
                "<li><strong>{cat}</strong> — {lesson}".format(
                    cat=html.escape(str(control.get("category", "unknown"))),
                    lesson=html.escape(str(control.get("lesson", ""))),
                )
                + (
                    f" <em>(controlled by: {html.escape(str(control.get('required_control', '')))})</em>"
                    if control.get("required_control")
                    else ""
                )
                + "</li>"
            )
        lines.append("</ul>")
    if inherited:
        lines.append("<p><strong>Inherited assumptions from earlier nodes:</strong></p><ul>")
        for assumption in inherited:
            lines.append(f"<li>{html.escape(str(assumption))}</li>")
        lines.append("</ul>")
    return "".join(lines)


def _build_background(node: dict[str, Any]) -> str:
    baseline_refs = node.get("baseline_refs", []) or []
    lines = [
        "<p>The harness fixes a three-role baseline triad for every claim node: "
        "<em>current_best_known</em>, <em>naive</em>, and <em>random_or_null</em>. "
        "Each role attaches to a dossier-tracked candidate; the orchestrator "
        "does not let the worker replace these.</p>"
    ]
    if baseline_refs:
        lines.append("<p><strong>Baseline references for this node:</strong></p><ul>")
        for ref in baseline_refs:
            ds = ref.get("baseline_dossier_id", "?")
            roles = ", ".join(ref.get("roles", []))
            candidates = ", ".join(ref.get("candidate_ids", []))
            lines.append(
                f"<li>dossier <code>{html.escape(str(ds))}</code> — roles: {html.escape(roles)} — "
                f"candidates: {html.escape(candidates)}</li>"
            )
        lines.append("</ul>")
    return "".join(lines)


def _build_method(node: dict[str, Any], contract: dict[str, Any]) -> str:
    runtime = node.get("runtime_profile", {}) or {}
    parts = [
        "<p>The harness routes the claim through a four-stage Sakana-v2-shaped "
        "tree search where stages are typed by <em>claim axis</em>, not by metric "
        "improvement:</p>",
        "<ol>"
        "<li><strong>scope_pinning</strong> — admits validity / taste / operational claims</li>"
        "<li><strong>baseline_evidence</strong> — admits capability claims and runs the baseline triad</li>"
        "<li><strong>mechanism_or_necessity</strong> — admits mechanism and necessity children</li>"
        "<li><strong>boundary_ablation</strong> — admits boundary and constraint children</li>"
        "</ol>",
        "<p>Workers are bounded tools: they execute one job-manifest in a sandboxed "
        "workspace under <em>no_self_expansion</em> scope policy, may suggest but "
        "may not create branches, and cannot write shared memory or repo paths. "
        "Critics are read-only and selected deterministically by folder routing.</p>",
        f"<p><strong>Worker runtime profile:</strong> {html.escape(str(runtime.get('worker_type', 'n/a')))} "
        f"with turn budget {html.escape(str(runtime.get('turn_budget', 'n/a')))} and "
        f"timeout policy <em>{html.escape(str(runtime.get('timeout_policy', 'n/a')))}</em>.</p>",
        "<h3>Mandatory Baselines</h3><ul>",
    ]
    for baseline in contract["mandatory_baselines"]:
        parts.append(f"<li>{html.escape(baseline)}</li>")
    parts.append("</ul><h3>Success Criteria</h3><ul>")
    for crit in contract["success_criteria"]:
        parts.append(f"<li>{html.escape(crit)}</li>")
    parts.append("</ul><h3>Disproof Conditions</h3><ul>")
    for cond in contract["disproof_conditions"]:
        parts.append(f"<li>{html.escape(cond)}</li>")
    parts.append("</ul>")
    return "".join(parts)


def _build_experimental_setup(
    node: dict[str, Any], worker_report: dict[str, Any]
) -> str:
    artifacts = worker_report.get("artifacts", []) or []
    lines = [
        "<p>The experiment plan was deterministically materialized from the "
        "domain template at <code>experiment_plan_templates/" +
        html.escape(node.get("domain", "fallback")) + "/</code>, "
        "validated against <code>experiment_plan.schema.json</code>, and executed "
        "by the harness's LocalRunner under the job-manifest contract.</p>",
        "<p><strong>Artifacts captured by the runner:</strong></p>",
    ]
    if artifacts:
        lines.append("<ul>")
        for artifact in artifacts:
            lines.append(f"<li><code>{html.escape(str(artifact))}</code></li>")
        lines.append("</ul>")
    else:
        lines.append("<p><em>No artifacts recorded.</em></p>")
    return "".join(lines)


def _collect_figures(worker_report: dict[str, Any], paper_dir: Path) -> list[dict[str, str]]:
    """Find image artifacts referenced by the worker_report and copy them next
    to the paper for embedding. Mirrors Sakana's `figures/` convention.
    """
    figures: list[dict[str, str]] = []
    figure_exts = (".png", ".jpg", ".jpeg", ".svg", ".gif")
    paper_figures_dir = paper_dir / "figures"
    for index, artifact in enumerate(worker_report.get("artifacts", []) or [], start=1):
        if not isinstance(artifact, str):
            continue
        if not artifact.lower().endswith(figure_exts):
            continue
        src = Path(artifact)
        if not src.is_absolute():
            # Try resolving relative to a few likely roots.
            for root in (paper_dir, paper_dir.parent, paper_dir.parent.parent):
                candidate = (root / artifact).resolve()
                if candidate.is_file():
                    src = candidate
                    break
        if not src.is_file():
            continue
        paper_figures_dir.mkdir(parents=True, exist_ok=True)
        dst = paper_figures_dir / f"figure_{index:02d}{src.suffix.lower()}"
        try:
            dst.write_bytes(src.read_bytes())
        except OSError:
            continue
        figures.append(
            {
                "src": f"figures/{dst.name}",
                "caption": f"Artifact {index}: {src.name}",
            }
        )
    return figures


def _build_experiments(
    worker_report: dict[str, Any],
    reduction: dict[str, Any],
    critic_reviews: list[dict[str, Any]],
    figures: list[dict[str, str]] | None = None,
) -> str:
    metrics = worker_report.get("metrics") or {}
    baselines = worker_report.get("baselines") or {}
    baseline_status = worker_report.get("baseline_evidence_status") or {}
    lines = ["<h3>Metrics</h3>", _kv_table(metrics, "Metric", "Value")]
    lines.extend(["<h3>Baseline Comparison</h3>", _kv_table(baselines, "Baseline", "Value")])
    overall = baseline_status.get("overall", "n/a")
    lines.append(
        f"<p><strong>Overall baseline-evidence status:</strong> <span class='verdict-{html.escape(str(overall))}'>"
        f"{html.escape(str(overall))}</span></p>"
    )
    results = baseline_status.get("results", []) or []
    if results:
        lines.append(
            "<table><thead><tr><th>role</th><th>metric_key</th><th>baseline_key</th>"
            "<th>operator</th><th>status</th><th>reason</th></tr></thead><tbody>"
        )
        for row in results:
            lines.append("<tr>")
            for col in ("role", "metric_key", "baseline_key", "operator", "status", "reason"):
                lines.append(f"<td>{html.escape(str(row.get(col, '')))}</td>")
            lines.append("</tr>")
        lines.append("</tbody></table>")
    lines.append("<h3>Critic Reviews</h3>")
    if not critic_reviews:
        lines.append("<p><em>No critic reviews recorded.</em></p>")
    else:
        lines.append(
            "<table><thead><tr><th>critic_id</th><th>verdict</th><th>blocking</th>"
            "<th>validity</th><th>necessity</th><th>reproducibility</th>"
            "<th>taste_alignment</th></tr></thead><tbody>"
        )
        for review in critic_reviews:
            scores = review.get("scores", {})
            lines.append("<tr>")
            lines.append(f"<td><code>{html.escape(str(review.get('critic_id', '')))}</code></td>")
            lines.append(f"<td>{html.escape(str(review.get('verdict_candidate', '')))}</td>")
            lines.append(f"<td>{'yes' if review.get('blocking') else 'no'}</td>")
            for key in ("validity", "necessity", "reproducibility", "taste_alignment"):
                lines.append(f"<td>{html.escape(str(scores.get(key, '')))}</td>")
            lines.append("</tr>")
        lines.append("</tbody></table>")
    objections = [
        objection
        for review in critic_reviews
        if review.get("blocking")
        for objection in review.get("objections", [])
    ]
    if objections:
        lines.append("<h3>Blocking Objections</h3><ul>")
        for obj in objections:
            lines.append(
                f"<li><strong>{html.escape(str(obj.get('objection', '')))}</strong> "
                f"<em>→ resolution: {html.escape(str(obj.get('required_resolution', '')))}</em></li>"
            )
        lines.append("</ul>")
    lines.append(
        f"<p><strong>Orchestrator final verdict:</strong> "
        f"<span class='verdict-{html.escape(str(reduction.get('final_verdict', 'n/a')))}'>"
        f"{html.escape(str(reduction.get('final_verdict', 'n/a')))}</span></p>"
    )
    for index, fig in enumerate(figures or [], start=1):
        lines.append(
            "<figure class='paper-figure'>"
            f"<img src='{html.escape(fig['src'])}' alt='{html.escape(fig['caption'])}'>"
            f"<figcaption>Figure {index}. {html.escape(fig['caption'])}</figcaption>"
            "</figure>"
        )
    return "".join(lines)


def _build_conclusion(
    reduction: dict[str, Any],
    rebuttal_reviews: list[dict[str, Any]],
    ac_decision: dict[str, Any],
) -> str:
    decision = ac_decision.get("decision", "n/a")
    scores = ac_decision.get("score_summary", {}) or {}
    lines = [
        f"<p><strong>Area-chair decision:</strong> <span class='decision-{html.escape(decision)}'>"
        f"{html.escape(decision)}</span> "
        f"(confidence {html.escape(str(ac_decision.get('confidence', 'n/a')))}).</p>",
        "<h3>Score Summary</h3>",
        _kv_table(scores, "Axis", "Score"),
    ]
    blocking = ac_decision.get("blocking_reasons") or []
    if blocking:
        lines.append("<h3>Blocking Reasons</h3><ul>")
        for reason in blocking:
            lines.append(f"<li>{html.escape(str(reason))}</li>")
        lines.append("</ul>")
    cr = ac_decision.get("camera_ready_conditions") or []
    if cr:
        lines.append("<h3>Camera-Ready Conditions</h3><ul>")
        for cond in cr:
            lines.append(f"<li>{html.escape(str(cond))}</li>")
        lines.append("</ul>")
    rn = ac_decision.get("required_next_search_nodes") or []
    if rn:
        lines.append("<h3>Required Next Search Nodes</h3><ul>")
        for nxt in rn:
            lines.append(
                f"<li><code>{html.escape(str(nxt.get('type', '')))}</code> — "
                f"{html.escape(str(nxt.get('claim', '')))}</li>"
            )
        lines.append("</ul>")
    lessons = reduction.get("accepted_lesson_candidates") or []
    if lessons:
        lines.append("<h3>Lessons Distilled</h3><ul>")
        for lesson in lessons:
            lines.append(f"<li>{html.escape(str(lesson))}</li>")
        lines.append("</ul>")
    return "".join(lines)


def _build_references(
    node: dict[str, Any],
    failure_branch_prior: dict[str, Any],
) -> str:
    """References section — Sakana parity for \\bibliography{references}.

    Pulls from baseline_refs (dossier ids + candidates) and from failure
    branch_prior's selected_failure_files. These are the project-internal
    citations the harness actually relied on for this claim.
    """
    refs: list[str] = []
    for ref in node.get("baseline_refs", []) or []:
        dossier_id = ref.get("baseline_dossier_id", "n/a")
        roles = ", ".join(ref.get("roles", []))
        candidates = ", ".join(ref.get("candidate_ids", []))
        refs.append(
            f"Baseline dossier <code>{html.escape(str(dossier_id))}</code> "
            f"(roles: {html.escape(roles)}; candidates: {html.escape(candidates)})."
        )
    inherited = node.get("lineage", {}).get("inherited_assumptions", []) or []
    for assumption in inherited:
        text = str(assumption)
        if any(token in text for token in ("path:", "://", ".md", ".pdf", ".json")):
            refs.append(html.escape(text))
    for failure_file in failure_branch_prior.get("selected_failure_files", []) or []:
        refs.append(
            f"Internal failure record: <code>{html.escape(str(failure_file))}</code>"
        )
    if not refs:
        return "<p><em>No external references attached to this run.</em></p>"
    lines = ["<ol class='references'>"]
    for ref in refs:
        lines.append(f"<li>{ref}</li>")
    lines.append("</ol>")
    return "".join(lines)


def _build_impact_statement(node: dict[str, Any], ac_decision: dict[str, Any]) -> str:
    return (
        "<p>This research was conducted by an AI harness with bounded workers, "
        "deterministic critics, and an operator-gated live path. Outputs are "
        "auditable: every artifact path, transition, score, and AC decision "
        "is persisted under the run directory. The harness refuses to publish "
        "when evidence comes from the fallback demo plan, so all results in "
        "this paper are tied to a real domain template under "
        "<code>experiment_plan_templates/</code>.</p>"
    )


def _build_supplementary(
    worker_report: dict[str, Any],
    reduction: dict[str, Any],
    ac_decision: dict[str, Any],
) -> str:
    payload = {
        "worker_report": worker_report,
        "orchestrator_reduction": reduction,
        "ac_decision": ac_decision,
    }
    return (
        "<p>Full machine-readable state for this paper:</p>"
        f"<pre>{html.escape(json.dumps(payload, indent=2, sort_keys=True, default=str))}</pre>"
    )


def _kv_table(d: dict[str, Any], key_label: str, value_label: str) -> str:
    if not d:
        return f"<p><em>No {value_label.lower()} recorded.</em></p>"
    rows = "".join(
        f"<tr><td><code>{html.escape(str(k))}</code></td>"
        f"<td>{html.escape(str(v))}</td></tr>"
        for k, v in d.items()
    )
    return (
        f"<table><thead><tr><th>{html.escape(key_label)}</th>"
        f"<th>{html.escape(value_label)}</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _render(
    *,
    title: str,
    authors: str,
    affiliation: str,
    abstract: str,
    sections: list[tuple[str, str]],
    impact: str,
    references: str,
    supplementary: str,
    metadata: dict[str, str],
) -> str:
    section_html = "".join(
        f"<section><h2>{html.escape(name)}</h2>{body}</section>"
        for name, body in sections
    )
    metadata_html = "".join(
        f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>"
        for k, v in metadata.items()
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  :root {{
    --fg: #1a1a1a; --muted: #5a5a5a; --bg: #fdfdfd;
    --accent: #2845a8; --rule: #c8cbe0; --soft: #f4f5fb;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: 'Times New Roman', Times, Georgia, serif;
    max-width: 920px; margin: 2.5em auto; padding: 0 1.4em;
    color: var(--fg); background: var(--bg); line-height: 1.55;
    text-align: justify;
  }}
  /* ICML-style two-column title block on top, single-column body below
     except the .twocolumn class which mimics LaTeX \\twocolumn. */
  header.titleblock {{
    text-align: center; border-bottom: 1px solid var(--rule);
    padding-bottom: 1.2em; margin-bottom: 1.8em;
  }}
  header.titleblock h1 {{
    font-size: 1.7em; margin: 0 0 0.35em 0; font-weight: bold;
  }}
  header.titleblock .authors {{ color: var(--fg); font-size: 1.0em; }}
  header.titleblock .affiliation {{ color: var(--muted); font-style: italic; font-size: 0.9em; }}
  .abstract {{
    border: 1px solid var(--rule); padding: 1em 1.2em; margin: 1.6em 0;
    background: var(--soft); font-size: 0.95em;
  }}
  .abstract h2 {{ font-size: 1.0em; margin: 0 0 0.4em 0; text-transform: uppercase; letter-spacing: 0.05em; }}
  /* Two-column body for sections 1-7, like ICML \\twocolumn. */
  main.twocolumn {{ column-count: 2; column-gap: 2em; }}
  main.twocolumn section {{ break-inside: avoid-column; }}
  section h2 {{
    font-size: 1.1em; margin-top: 1.4em; margin-bottom: 0.5em;
    font-weight: bold; break-after: avoid;
  }}
  section h3 {{
    font-size: 0.98em; margin-top: 1em; color: var(--accent);
    font-weight: bold; break-after: avoid;
  }}
  /* Single-column tail (Impact, References, Supplementary, metadata) */
  .singlecolumn {{ column-count: 1; }}
  table {{
    border-collapse: collapse; margin: 0.6em 0; width: 100%; font-size: 0.85em;
    break-inside: avoid;
  }}
  th, td {{ border: 1px solid var(--rule); padding: 0.32em 0.55em; text-align: left; vertical-align: top; }}
  th {{ background: var(--soft); }}
  code {{
    font-family: 'SF Mono', Menlo, Consolas, monospace;
    background: var(--soft); padding: 0 0.25em; border-radius: 2px; font-size: 0.83em;
  }}
  pre {{
    background: var(--soft); border: 1px solid var(--rule); padding: 0.7em;
    overflow-x: auto; font-size: 0.75em; line-height: 1.35;
  }}
  ul, ol {{ padding-left: 1.3em; margin: 0.4em 0; }}
  ol.references {{ font-size: 0.9em; }}
  ol.references li {{ margin-bottom: 0.35em; }}
  dl.metadata {{
    display: grid; grid-template-columns: max-content 1fr; gap: 0.25em 1em;
    font-size: 0.82em; color: var(--muted); margin-top: 1.5em;
    border-top: 1px solid var(--rule); padding-top: 0.8em;
  }}
  dl.metadata dt {{ font-weight: bold; }}
  figure.paper-figure {{ margin: 1em 0; text-align: center; break-inside: avoid; }}
  figure.paper-figure img {{ max-width: 100%; border: 1px solid var(--rule); }}
  figure.paper-figure figcaption {{ font-size: 0.85em; color: var(--muted); margin-top: 0.4em; }}
  .decision-accept {{ color: #1f7a4d; font-weight: bold; }}
  .decision-revise {{ color: #b6850b; font-weight: bold; }}
  .decision-reject {{ color: #a82828; font-weight: bold; }}
  .verdict-supported, .verdict-supported_with_scope_narrowing {{ color: #1f7a4d; font-weight: bold; }}
  .verdict-contradicted, .verdict-confounded_or_not_evaluable {{ color: #a82828; font-weight: bold; }}
  .verdict-passed {{ color: #1f7a4d; font-weight: bold; }}
  .verdict-failed {{ color: #a82828; font-weight: bold; }}
  .verdict-not_evaluable {{ color: #b6850b; font-weight: bold; }}
  @media (max-width: 720px) {{ main.twocolumn {{ column-count: 1; }} }}
</style>
</head>
<body>
<header class="titleblock">
<h1>{html.escape(title)}</h1>
<div class="authors">{html.escape(authors)}</div>
<div class="affiliation">{html.escape(affiliation)}</div>
</header>
<section class="abstract"><h2>Abstract</h2><p>{abstract}</p></section>
<main class="twocolumn">
{section_html}
</main>
<section class="singlecolumn"><h2>Impact Statement</h2>{impact}</section>
<section class="singlecolumn"><h2>References</h2>{references}</section>
<hr>
<section class="singlecolumn"><h2>Supplementary Material</h2>{supplementary}</section>
<footer class="singlecolumn"><dl class="metadata">{metadata_html}</dl></footer>
</body>
</html>
"""
