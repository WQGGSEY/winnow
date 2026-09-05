"""Phase F tests: Sakana ICML paper assembler invariants
(mental model required, figures required, sections present, sanitizer works)."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from research_harness.publishing.figures import render_figure
from research_harness.publishing.sakana_paper import (
    SakanaPaperError,
    render_sakana_paper,
)


def _build_inputs(tmp_path):
    figs_dir = tmp_path / "figures"
    figs_dir.mkdir()
    wr = {
        "metrics": {"auc_overall": 0.968, "lift_over_naive_auc": 0.435,
                    "lift_over_naive_ci95_low": 0.343, "lift_over_naive_ci95_high": 0.523},
        "baselines": {"current_best_known": 0.70, "naive": 0.533, "random_or_null": 0.510},
    }
    ac = {
        "decision": "accept", "confidence": "medium",
        "score_summary": {"validity": 8, "necessity": 7, "novelty": 7,
                           "clarity": 7, "reproducibility": 7, "taste_alignment": 8},
        "camera_ready_directives": [{
            "directive": "Add canary in method.",
            "origin_critic_ids": ["c1"],
            "must_appear_in_section": "method",
            "rationale": "needed",
        }],
        "advisor_message_to_professor": "x",
        "rebuttal_synthesis": {
            "strongest_supporting_evidence": ["w"],
            "load_bearing_objections": [], "minority_dissent": [],
            "methodology_assessment": {
                "aggregate_verdict": "partial",
                "methodology_for_user": "Use as calibration filter.",
                "remaining_gap": "harness recipe",
            },
        },
    }
    revision = {
        "mental_model_statement": ("The classifier acts as a pre-OOS calibration filter "
                                    "inside IS-Sharpe band-matched cohorts."),
        "responses_to_directives": [{"directive_index": 0, "status": "accepted_fully",
                                      "section_changes": [{"section": "method", "change_summary": "added"}],
                                      "professor_reply": "Done"}],
    }
    render_figure("f_baseline", "baseline_bars", "Baselines", {}, wr, ac, figs_dir)
    figs_registry = {"f_baseline": {"figure_id": "f_baseline", "figure_type": "baseline_bars",
                                     "caption": "Baselines", "artifact_path": str(figs_dir / "f_baseline.png")}}
    outline = {
        "title": "Pre-OOS Alpha Calibration via Meta-Feature Discrimination",
        "running_title": "Alpha calibration",
        "mental_model_statement": revision["mental_model_statement"],
        "abstract_seed": "We show a calibration filter.",
        "section_outline": [
            {"section_id": "introduction", "title": "Introduction", "purpose": "p", "key_claims": ["x"]},
            {"section_id": "method", "title": "Method", "purpose": "p", "key_claims": ["x"]},
            {"section_id": "experiments", "title": "Experiments", "purpose": "p", "key_claims": ["x"]},
            {"section_id": "discussion", "title": "Discussion", "purpose": "p", "key_claims": ["x"]},
            {"section_id": "conclusion", "title": "Conclusion", "purpose": "p", "key_claims": ["x"]},
        ],
        "figure_specs": [{"figure_id": "f_baseline", "figure_type": "baseline_bars",
                           "caption": "Baselines", "data_spec": {}}],
        "table_specs": [],
    }
    sections = {
        sid: {
            "thread_id": "t", "section_id": sid,
            "prose_html": f"<p>Section {sid} body with <img src='figures/f_baseline.png'>.</p>",
            "mental_model_link": "Connects to mental model.",
            "evidence_anchors": ["worker_report.metrics.auc_overall=0.968"],
            "embedded_figure_ids": ["f_baseline"] if sid == "method" else [],
        }
        for sid in ["introduction", "method", "experiments", "discussion", "conclusion"]
    }
    return wr, ac, revision, figs_registry, outline, sections


def test_paper_assembles_with_mental_model(tmp_path):
    wr, ac, rev, figs, outline, sections = _build_inputs(tmp_path)
    dispatch = render_sakana_paper(
        outline=outline, sections=sections, figures_registry=figs,
        ac_decision=ac, camera_ready_revision=rev, orchestrator_reduction={},
        rebuttal_reviews=[], node={"id": "n_root"}, worker_report=wr, output_dir=tmp_path,
    )
    assert {a["output"] for a in dispatch["rendered_artifacts"]} == {"paper_html", "interactive_html", "slides_html"}
    html = (tmp_path / "paper.html").read_text(encoding="utf-8")
    summary = (tmp_path / "interactive_summary.html").read_text(encoding="utf-8")
    assert "Add canary in method." not in html
    assert "Add canary in method." in summary
    assert "Research Harness — Practitioner-Reviewed Pipeline" not in html
    assert "n_root" not in html
    assert "n_root" in summary


def test_paper_rejects_empty_mental_model(tmp_path):
    wr, ac, rev, figs, outline, sections = _build_inputs(tmp_path)
    rev["mental_model_statement"] = "too short"
    outline["mental_model_statement"] = "too short"
    with pytest.raises(SakanaPaperError, match="mental_model"):
        render_sakana_paper(
            outline=outline, sections=sections, figures_registry=figs,
            ac_decision=ac, camera_ready_revision=rev, orchestrator_reduction={},
            rebuttal_reviews=[], node={}, worker_report=wr, output_dir=tmp_path,
        )


def test_paper_allows_text_only_manuscript(tmp_path):
    wr, ac, rev, figs, outline, sections = _build_inputs(tmp_path)
    outline["figure_specs"] = []
    for section in sections.values():
        section["embedded_figure_ids"] = []
        section["prose_html"] = "<p>A proof without figures.</p>"
    render_sakana_paper(
        outline=outline, sections=sections, figures_registry={},
        ac_decision=ac, camera_ready_revision=rev, orchestrator_reduction={},
        rebuttal_reviews=[], node={}, worker_report=wr, output_dir=tmp_path,
    )
    assert "A proof without figures." in (tmp_path / "paper.html").read_text()


def test_paper_rejects_unregistered_figure_reference(tmp_path):
    wr, ac, rev, figs, outline, sections = _build_inputs(tmp_path)
    sections["method"]["embedded_figure_ids"] = ["f_baseline", "f_ghost"]
    with pytest.raises(SakanaPaperError, match="unregistered"):
        render_sakana_paper(
            outline=outline, sections=sections, figures_registry=figs,
            ac_decision=ac, camera_ready_revision=rev, orchestrator_reduction={},
            rebuttal_reviews=[], node={}, worker_report=wr, output_dir=tmp_path,
        )


def test_paper_sanitizer_strips_script_tags(tmp_path):
    wr, ac, rev, figs, outline, sections = _build_inputs(tmp_path)
    sections["introduction"]["prose_html"] = "<p>Hello</p><script>alert(1)</script>"
    render_sakana_paper(
        outline=outline, sections=sections, figures_registry=figs,
        ac_decision=ac, camera_ready_revision=rev, orchestrator_reduction={},
        rebuttal_reviews=[], node={}, worker_report=wr, output_dir=tmp_path,
    )
    html = (tmp_path / "paper.html").read_text(encoding="utf-8")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_paper_preserves_figure_and_citation_urls(tmp_path):
    wr, ac, rev, figs, outline, sections = _build_inputs(tmp_path)
    sections["method"]["prose_html"] += (
        '<a href="https://example.org/paper?a=1&amp;b=2">Reference</a>'
    )
    render_sakana_paper(
        outline=outline, sections=sections, figures_registry=figs,
        ac_decision=ac, camera_ready_revision=rev, orchestrator_reduction={},
        rebuttal_reviews=[], node={}, worker_report=wr, output_dir=tmp_path,
    )

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.images = []
            self.links = []

        def handle_starttag(self, tag, attrs):
            if tag == "img":
                self.images.append(dict(attrs).get("src"))
            if tag == "a":
                self.links.append(dict(attrs).get("href"))

    parsed = Links()
    parsed.feed((tmp_path / "paper.html").read_text(encoding="utf-8"))
    assert parsed.images and all(src == "figures/f_baseline.png" for src in parsed.images)
    assert "https://example.org/paper?a=1&b=2" in parsed.links


def test_paper_preserves_authored_appendix_and_numbers_body_from_one(tmp_path):
    wr, ac, rev, figs, outline, sections = _build_inputs(tmp_path)
    outline["section_outline"].insert(0, {"section_id": "abstract", "title": "Abstract"})
    outline["section_outline"].append({"section_id": "supplementary", "title": "Proof details"})
    sections["abstract"] = {"prose_html": "<p>Authored abstract.</p>"}
    sections["supplementary"] = {"prose_html": "<p>The full proof is retained here.</p>"}
    render_sakana_paper(
        outline=outline, sections=sections, figures_registry=figs,
        ac_decision=ac, camera_ready_revision=rev, orchestrator_reduction={},
        rebuttal_reviews=[], node={}, worker_report=wr, output_dir=tmp_path,
    )
    paper = (tmp_path / "paper.html").read_text(encoding="utf-8")
    assert "1. Introduction" in paper
    assert "The full proof is retained here." in paper
