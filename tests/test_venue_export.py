from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from research_harness.publishing import venue_export as ve


def _kit(tmp_path: Path) -> Path:
    source = tmp_path / "kit"
    source.mkdir()
    (source / "iclr2026_conference.sty").write_text(
        "\\ProvidesPackage{iclr2026_conference}\n",
        encoding="utf-8",
    )
    (source / "iclr2026_conference.bst").write_text(
        Path("/usr/share/texlive/texmf-dist/bibtex/bst/base/plain.bst").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    archive = tmp_path / "kit.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in source.iterdir():
            zf.write(path, path.name)
    return archive


def _profiles(archive: Path) -> dict[str, dict[str, object]]:
    return {
        "iclr-2026-main": {
            "venue": "iclr",
            "year": 2026,
            "track": "main",
            "supported": True,
            "template_url": "https://example.test/iclr2026.zip",
            "template_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "rules_source_url": "https://iclr.cc/Conferences/2026/AuthorGuide",
            "main_page_limit": 9,
            "document_family": "iclr2026",
            "anonymous": True,
            "requires_impact_statement": False,
            "requires_checklist": False,
            "appendix_policy": "appendices after bibliography",
        },
        "neurips-2026-main": {
            "venue": "neurips",
            "year": 2026,
            "track": "main",
            "supported": False,
            "unsupported_reason": "No direct official kit URL pinned.",
        },
    }


def _export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: Path):
    monkeypatch.setattr(ve, "load_venue_profiles", lambda: _profiles(archive))
    return ve.export_venue_package(
        target=ve.VenueTarget("iclr", 2026, "main"),
        title="Anonymous Learning System With Verified Evidence",
        abstract="<p>This paper studies a small verified export path.</p>",
        sections=[
            {
                "title": "Introduction",
                "prose_html": "<p>The result is grounded in a compiled package.</p>",
            },
        ],
        appendix_sections=[{"title": "Appendix", "latex": "Additional proof detail."}],
        bibliography=[
            {
                "id": "smith2026",
                "title": "Verified Research Packages",
                "authors": "Smith, Alex",
                "year": 2026,
                "venue": "Test Proceedings",
            }
        ],
        output_dir=tmp_path / "out",
        template_zip_path=archive,
    )


def test_export_compiles_pinned_template_and_records_receipt(tmp_path, monkeypatch):
    archive = _kit(tmp_path)
    receipt = _export(tmp_path, monkeypatch, archive)
    out = tmp_path / "out"
    assert receipt["kind"] == "venue_latex_pdf_export"
    assert receipt["submission_ready"] is False
    assert receipt["target"] == {"venue": "iclr", "year": 2026, "track": "main"}
    assert receipt["template_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert receipt["pdf_pages"] >= 1
    assert (out / "paper.tex").exists()
    assert (out / "references.bib").exists()
    assert (out / "paper.pdf").stat().st_size > 0
    assert (out / "venue_export_receipt.json").exists()
    first_command = receipt["compile"]["commands"][0]["command"]
    assert "-no-shell-escape" in first_command


def test_unsupported_target_rejects_without_unofficial_template(tmp_path, monkeypatch):
    archive = _kit(tmp_path)
    monkeypatch.setattr(ve, "load_venue_profiles", lambda: _profiles(archive))
    with pytest.raises(ve.UnsupportedVenueError, match="No direct official kit"):
        ve.export_venue_package(
            target=ve.VenueTarget("neurips", 2026, "main"),
            title="Anonymous Learning System With Verified Evidence",
            abstract="<p>Abstract.</p>",
            sections=[],
            bibliography=[{"id": "x", "title": "X"}],
            output_dir=tmp_path / "out",
            template_zip_path=archive,
        )


def test_math_in_html_raises_and_authored_latex_is_allowed(tmp_path, monkeypatch):
    archive = _kit(tmp_path)
    monkeypatch.setattr(ve, "load_venue_profiles", lambda: _profiles(archive))
    with pytest.raises(ve.UnsupportedManuscriptContent, match="math"):
        ve.export_venue_package(
            target=ve.VenueTarget("iclr", 2026, "main"),
            title="Anonymous Learning System With Verified Evidence",
            abstract="<p>Abstract.</p>",
            sections=[{"title": "Theory", "prose_html": "<p>Use $x$.</p>"}],
            bibliography=[{"id": "x", "title": "X"}],
            output_dir=tmp_path / "bad",
            template_zip_path=archive,
        )
    receipt = ve.export_venue_package(
        target=ve.VenueTarget("iclr", 2026, "main"),
        title="Anonymous Learning System With Verified Evidence",
        abstract="<p>Abstract.</p>",
        sections=[{"title": "Theory", "latex": "Use $x$ without conversion loss."}],
        bibliography=[{"id": "x", "title": "X"}],
        output_dir=tmp_path / "good",
        template_zip_path=archive,
    )
    assert receipt["pdf_pages"] >= 1


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
            bibliography=[{"id": "x", "title": "X"}],
            output_dir=tmp_path / "out",
            template_zip_path=archive,
        )
