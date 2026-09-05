from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from research_harness.publishing import venue_export as ve


def _kit(tmp_path: Path, venue: str = "iclr") -> Path:
    source = tmp_path / f"kit-{venue}"
    source.mkdir()
    if venue == "iclr":
        (source / "iclr2026_conference.sty").write_text("\\ProvidesPackage{iclr2026_conference}\n", encoding="utf-8")
        (source / "iclr2026_conference.bst").write_text(
            Path("/usr/share/texlive/texmf-dist/bibtex/bst/base/plain.bst").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    else:
        (source / "cvpr.sty").write_text("\\NeedsTeXFormat{LaTeX2e}\n\\ProvidesPackage{cvpr}\n\\DeclareOption*{}\n\\ProcessOptions\n", encoding="utf-8")
        (source / "ieeenat_fullname.bst").write_text(
            Path("/usr/share/texlive/texmf-dist/bibtex/bst/base/plain.bst").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    archive = tmp_path / f"{venue}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in source.iterdir():
            zf.write(path, path.name)
    return archive


def _profiles(archive: Path, venue: str = "iclr") -> dict[str, dict[str, object]]:
    base = {
        f"{venue}-2026-main": {
            "venue": venue,
            "year": 2026,
            "track": "main",
            "supported": True,
            "template_url": f"https://example.test/{venue}.zip",
            "template_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "rules_source_url": f"https://example.test/{venue}",
            "main_page_limit": 9,
            "document_family": f"{venue}2026",
            "anonymous": True,
            "requires_impact_statement": False,
            "requires_checklist": False,
            "appendix_policy": "appendices after bibliography",
        }
    }
    return base


def _png(path: Path) -> None:
    path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
            "0000000c4944415408d763f8ffff3f0005fe02fea73581f50000000049454e44ae426082"
        )
    )


def _export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: Path):
    monkeypatch.setattr(ve, "load_venue_profiles", lambda: _profiles(archive))
    figure = tmp_path / "curve.png"
    _png(figure)
    return ve.export_venue_package(
        target=ve.VenueTarget("iclr", 2026, "main"),
        title="Anonymous Learning System With Verified Evidence",
        abstract="<p>This paper cites <a href='#ref_smith2026'>Smith</a>.</p>",
        sections=[
            {
                "title": "Introduction",
                "prose_html": "<p>The result is grounded in a compiled package.</p><img src='curve.png' alt='Measured curve'><table id='t_results'></table>",
            },
        ],
        appendix_sections=[{"title": "Appendix", "latex": "Additional proof detail.", "latex_verified": True}],
        bibliography=[
            {
                "id": "smith2026",
                "title": "Verified Research Packages",
                "authors": ["Smith, Alex", "Jones, Sam"],
                "year": 2026,
                "venue": "Test Proceedings",
            }
        ],
        figure_files={"curve": figure},
        tables={"t_results": {"latex": "\\begin{table}[t]\\centering\\caption{Verified table}\\begin{tabular}{ll}A&B\\\\\\end{tabular}\\end{table}", "source_digest": "abc123"}},
        output_dir=tmp_path / "out",
        template_zip_path=archive,
    )


def test_export_compiles_pinned_template_and_records_artifact_contract(tmp_path, monkeypatch):
    archive = _kit(tmp_path)
    receipt = _export(tmp_path, monkeypatch, archive)
    out = tmp_path / "out"
    paper = (out / "paper.tex").read_text(encoding="utf-8")
    bib = (out / "references.bib").read_text(encoding="utf-8")
    assert receipt["kind"] == "venue_latex_pdf_export"
    assert receipt["submission_ready"] is False
    assert receipt["target"] == {"venue": "iclr", "year": 2026, "track": "main"}
    assert receipt["template_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert receipt["main_pages"] >= 1
    assert "\\cite{smith2026}" in paper
    assert "figures/curve.png" in paper
    assert "Verified table" in paper
    assert "Smith, Alex and Jones, Sam" in bib
    assert (out / "figures" / "curve.png").exists()
    assert (out / "paper.pdf").stat().st_size > 0
    assert (out / "venue_export_receipt.json").exists()
    first_command = receipt["compile"]["commands"][0]["command"]
    assert "-no-shell-escape" in first_command


def test_missing_bibliography_fields_are_rejected(tmp_path, monkeypatch):
    archive = _kit(tmp_path)
    monkeypatch.setattr(ve, "load_venue_profiles", lambda: _profiles(archive))
    with pytest.raises(ve.VenueExportError, match="missing authors"):
        ve.export_venue_package(
            target=ve.VenueTarget("iclr", 2026, "main"),
            title="Anonymous Learning System With Verified Evidence",
            abstract="<p>Abstract.</p>",
            sections=[],
            bibliography=[{"id": "x", "title": "X", "year": 2026, "url": "https://example.test/x"}],
            output_dir=tmp_path / "out",
            template_zip_path=archive,
        )


def test_arxiv_without_venue_renders_misc_not_inproceedings():
    bib = ve._render_bibtex([
        {"id": "arxivx", "title": "X", "authors": ["Doe, Jane"], "year": 2026, "arxiv_id": "2601.00001"}
    ])
    assert bib.startswith("@misc{arxivx")
    assert "archivePrefix = {arXiv}" in bib


def test_math_in_html_raises_and_authored_latex_requires_verified_marker(tmp_path, monkeypatch):
    archive = _kit(tmp_path)
    monkeypatch.setattr(ve, "load_venue_profiles", lambda: _profiles(archive))
    with pytest.raises(ve.UnsupportedManuscriptContent, match="math"):
        ve.export_venue_package(
            target=ve.VenueTarget("iclr", 2026, "main"),
            title="Anonymous Learning System With Verified Evidence",
            abstract="<p>Abstract.</p>",
            sections=[{"title": "Theory", "prose_html": "<p>Use $x$.</p>"}],
            bibliography=[{"id": "x", "title": "X", "authors": "Doe, Jane", "year": 2026, "url": "https://example.test/x"}],
            output_dir=tmp_path / "bad",
            template_zip_path=archive,
        )
    with pytest.raises(ve.UnsupportedManuscriptContent, match="latex_verified"):
        ve.export_venue_package(
            target=ve.VenueTarget("iclr", 2026, "main"),
            title="Anonymous Learning System With Verified Evidence",
            abstract="<p>Abstract.</p>",
            sections=[{"title": "Theory", "latex": "Use $x$ without conversion loss."}],
            bibliography=[{"id": "x", "title": "X", "authors": "Doe, Jane", "year": 2026, "url": "https://example.test/x"}],
            output_dir=tmp_path / "unverified",
            template_zip_path=archive,
        )
    receipt = ve.export_venue_package(
        target=ve.VenueTarget("iclr", 2026, "main"),
        title="Anonymous Learning System With Verified Evidence",
        abstract="<p>Abstract cites <a href='#ref_x'>X</a>.</p>",
        sections=[{"title": "Theory", "latex": "Use $x$ without conversion loss.", "latex_verified": True}],
        bibliography=[{"id": "x", "title": "X", "authors": "Doe, Jane", "year": 2026, "url": "https://example.test/x"}],
        output_dir=tmp_path / "good",
        template_zip_path=archive,
    )
    assert receipt["pdf_pages"] >= 1


def test_cvpr_writes_appendix_only_to_supplement(tmp_path, monkeypatch):
    archive = _kit(tmp_path, "cvpr")
    monkeypatch.setattr(ve, "load_venue_profiles", lambda: _profiles(archive, "cvpr"))
    receipt = ve.export_venue_package(
        target=ve.VenueTarget("cvpr", 2026, "main"),
        title="Anonymous Learning System With Verified Evidence",
        abstract="<p>Abstract cites <a href='#ref_x'>X</a>.</p>",
        sections=[{"title": "Body", "prose_html": "<p>Body.</p>"}],
        appendix_sections=[{"title": "Proof", "latex": "Supplement only.", "latex_verified": True}],
        bibliography=[{"id": "x", "title": "X", "authors": "Doe, Jane", "year": 2026, "url": "https://example.test/x"}],
        output_dir=tmp_path / "cvpr-out",
        template_zip_path=archive,
    )
    assert "\\appendix" not in (tmp_path / "cvpr-out" / "paper.tex").read_text(encoding="utf-8")
    assert "Supplement only." in (tmp_path / "cvpr-out" / "supplement.tex").read_text(encoding="utf-8")
    assert receipt["supplemental_pdf_pages"] >= 1


def test_digest_mismatch_rejects_template(tmp_path, monkeypatch):
    archive = _kit(tmp_path)
    profiles = _profiles(archive)
    profiles["iclr-2026-main"]["template_sha256"] = "0" * 64
    monkeypatch.setattr(ve, "load_venue_profiles", lambda: profiles)
    with pytest.raises(ve.VenueExportError, match="template digest mismatch"):
        ve.export_venue_package(
            target=ve.VenueTarget("iclr", 2026, "main"),
            title="Anonymous Learning System With Verified Evidence",
            abstract="<p>Abstract.</p>",
            sections=[{"title": "Introduction", "prose_html": "<p>Body.</p>"}],
            bibliography=[{"id": "x", "title": "X", "authors": "Doe, Jane", "year": 2026, "url": "https://example.test/x"}],
            output_dir=tmp_path / "out",
            template_zip_path=archive,
        )
