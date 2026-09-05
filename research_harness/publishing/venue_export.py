"""Export structured manuscript content to pinned 2026 venue LaTeX packages."""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.request import Request, urlopen


class VenueExportError(ValueError):
    pass


class UnsupportedVenueError(VenueExportError):
    pass


class UnsupportedManuscriptContent(VenueExportError):
    pass


PROFILE_PATH = Path(__file__).with_name("venue_templates") / "profiles.json"
_MAIN_END_LABEL = "rhMainEnd"


@dataclass(frozen=True, slots=True)
class VenueTarget:
    venue: str
    year: int
    track: str = "main"

    @property
    def profile_key(self) -> str:
        return f"{self.venue}-{self.year}-{self.track}"


@dataclass(frozen=True, slots=True)
class RenderAssets:
    figures: Mapping[str, str]
    tables: Mapping[str, str]


def load_venue_profiles() -> dict[str, dict[str, Any]]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def supported_targets() -> tuple[str, ...]:
    profiles = load_venue_profiles()
    return tuple(sorted(key for key, value in profiles.items() if value.get("supported")))


def export_venue_package(
    *,
    target: VenueTarget,
    title: str,
    abstract: str,
    sections: list[dict[str, Any]],
    bibliography: list[dict[str, Any]],
    output_dir: Path,
    appendix_sections: list[dict[str, Any]] | None = None,
    impact_statement: str | None = None,
    checklist_tex: str | None = None,
    figure_files: Mapping[str, Path | str] | None = None,
    tables: Mapping[str, Mapping[str, Any]] | None = None,
    template_cache_dir: Path | None = None,
    template_zip_path: Path | None = None,
) -> dict[str, Any]:
    """Build a venue package and compile a PDF.

    HTML sections may cite bibliography entries with ``<a href="#ref_id">`` and
    embed explicitly supplied artifacts with ``<img src="fig_id">`` or
    ``<table id="table_id"></table>``. Raw LaTeX sections are accepted only when
    the caller marks that field as already verified, so this exporter is not a
    silent evidence bypass.
    """

    profile = _profile_for(target)
    output_dir.mkdir(parents=True, exist_ok=True)
    template_root, template_digest = _prepare_template(
        profile,
        output_dir=output_dir,
        cache_dir=template_cache_dir,
        template_zip_path=template_zip_path,
    )
    _copy_template_files(template_root, output_dir)
    assets = RenderAssets(
        figures=_copy_figure_files(figure_files or {}, output_dir),
        tables=_normalize_tables(tables or {}),
    )
    tex = _render_latex(
        profile=profile,
        title=title,
        abstract=abstract,
        sections=sections,
        appendix_sections=[] if profile["venue"] == "cvpr" else appendix_sections or [],
        impact_statement=impact_statement,
        checklist_tex=checklist_tex,
        assets=assets,
    )
    bib = _render_bibtex(bibliography)
    (output_dir / "paper.tex").write_text(tex, encoding="utf-8")
    (output_dir / "references.bib").write_text(bib, encoding="utf-8")
    compile_receipt = _compile(output_dir, "paper", main_page_limit=profile["main_page_limit"])
    supplemental_receipt = None
    if profile["venue"] == "cvpr" and appendix_sections:
        supplemental_tex = _render_supplemental_latex(
            profile=profile,
            title=title,
            appendix_sections=appendix_sections,
            assets=assets,
        )
        (output_dir / "supplement.tex").write_text(supplemental_tex, encoding="utf-8")
        supplemental_receipt = _compile(output_dir, "supplement", main_page_limit=None)
    receipt = {
        "version": 2,
        "kind": "venue_latex_pdf_export",
        "submission_ready": False,
        "target": {
            "venue": profile["venue"],
            "year": profile["year"],
            "track": profile["track"],
        },
        "rules_source_url": profile["rules_source_url"],
        "template_url": profile["template_url"],
        "template_sha256": template_digest,
        "expected_template_sha256": profile["template_sha256"],
        "main_page_limit": profile["main_page_limit"],
        "main_pages": compile_receipt["main_pages"],
        "anonymous_author_default": profile["anonymous"],
        "required_statements": _required_statements(profile),
        "appendix_policy": profile["appendix_policy"],
        "figures": dict(assets.figures),
        "tables": sorted(assets.tables),
        "tex_sha256": _file_sha256(output_dir / "paper.tex"),
        "bib_sha256": _file_sha256(output_dir / "references.bib"),
        "pdf_sha256": _file_sha256(output_dir / "paper.pdf"),
        "pdf_pages": compile_receipt["pdf_pages"],
        "compile": compile_receipt,
        "limitations": [
            "The export compiles a pinned official-template PDF, but does not run external venue format checkers.",
            "The author list is anonymized by construction; final author metadata must be supplied outside this preview.",
        ],
    }
    if supplemental_receipt:
        receipt["supplemental_pdf_sha256"] = _file_sha256(output_dir / "supplement.pdf")
        receipt["supplemental_pdf_pages"] = supplemental_receipt["pdf_pages"]
        receipt["supplemental_compile"] = supplemental_receipt
    (output_dir / "venue_export_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def _profile_for(target: VenueTarget) -> dict[str, Any]:
    profiles = load_venue_profiles()
    profile = profiles.get(target.profile_key)
    if profile is None:
        raise UnsupportedVenueError(
            f"unsupported venue target {target.profile_key!r}; supported targets: {supported_targets()}"
        )
    if not profile.get("supported"):
        raise UnsupportedVenueError(profile.get("unsupported_reason") or f"{target.profile_key} is not supported")
    return profile


def _prepare_template(
    profile: dict[str, Any],
    *,
    output_dir: Path,
    cache_dir: Path | None,
    template_zip_path: Path | None,
) -> tuple[Path, str]:
    if template_zip_path is None:
        cache = cache_dir or output_dir / "_template_cache"
        cache.mkdir(parents=True, exist_ok=True)
        template_zip_path = cache / f"{profile['document_family']}.zip"
        if not template_zip_path.exists():
            request = Request(profile["template_url"], headers={"User-Agent": "research-harness/venue-export"})
            template_zip_path.write_bytes(urlopen(request, timeout=60).read())
    digest = _file_sha256(template_zip_path)
    expected = profile.get("template_sha256")
    if expected and digest != expected:
        raise VenueExportError(
            f"template digest mismatch for {profile['venue']} {profile['year']}: {digest} != {expected}"
        )
    extract_dir = output_dir / "_official_template"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)
    with zipfile.ZipFile(template_zip_path) as archive:
        archive.extractall(extract_dir)
    return extract_dir, digest


def _copy_template_files(template_root: Path, output_dir: Path) -> None:
    for source in template_root.rglob("*"):
        if not source.is_file():
            continue
        if "__MACOSX" in source.parts or source.suffix.lower() in {".pdf", ".log", ".aux", ".out"}:
            continue
        relative = source.relative_to(template_root)
        parts = relative.parts[1:] if len(relative.parts) > 1 else relative.parts
        destination = output_dir.joinpath(*parts)
        if destination.name in {"main.tex", "example_paper.tex", "iclr2026_conference.tex", "colt2026-sample.tex", "neurips_2026.tex"}:
            continue
        if destination.name.endswith(".bib"):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _copy_figure_files(figures: Mapping[str, Path | str], output_dir: Path) -> dict[str, str]:
    copied: dict[str, str] = {}
    figure_dir = output_dir / "figures"
    for figure_id, source_value in figures.items():
        key = _clean_asset_id(figure_id, "figure")
        source = Path(source_value)
        if not source.is_file():
            raise VenueExportError(f"figure {key!r} file does not exist: {source}")
        if source.suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg"}:
            raise UnsupportedManuscriptContent(f"figure {key!r} has unsupported extension {source.suffix!r}")
        figure_dir.mkdir(exist_ok=True)
        destination = figure_dir / f"{key}{source.suffix.lower()}"
        shutil.copy2(source, destination)
        copied[key] = destination.relative_to(output_dir).as_posix()
    return copied


def _normalize_tables(tables: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for table_id, table_value in tables.items():
        key = _clean_asset_id(table_id, "table")
        if not table_value.get("source_digest"):
            raise VenueExportError(f"table {key!r} requires source_digest provenance")
        latex = str(table_value.get("latex") or "")
        if not latex.strip():
            raise VenueExportError(f"table {key!r} requires latex")
        normalized[key] = latex.strip()
    return normalized


def _clean_asset_id(value: str, kind: str) -> str:
    key = re.sub(r"[^A-Za-z0-9:_-]", "", str(value or ""))
    if not key:
        raise VenueExportError(f"{kind} id is required")
    return key


def _render_latex(
    *,
    profile: dict[str, Any],
    title: str,
    abstract: str,
    sections: list[dict[str, Any]],
    appendix_sections: list[dict[str, Any]],
    impact_statement: str | None,
    checklist_tex: str | None,
    assets: RenderAssets,
) -> str:
    family = profile["document_family"]
    body = "\n\n".join(_section_to_latex(section, assets) for section in sections)
    appendix = "\n\n".join(_section_to_latex(section, assets) for section in appendix_sections)
    impact = ""
    if profile.get("requires_impact_statement"):
        if not impact_statement or not impact_statement.strip():
            raise VenueExportError("target requires an impact_statement")
        impact = "\\section*{Impact Statement}\n" + _html_to_latex(impact_statement, assets)
    checklist = ""
    if profile.get("requires_checklist"):
        if not checklist_tex or not checklist_tex.strip():
            raise VenueExportError("target requires checklist_tex")
        checklist = "\n\\section*{Checklist}\n" + checklist_tex
    main_end = f"\n\\label{{{_MAIN_END_LABEL}}}\n"
    if profile["venue"] == "icml":
        preamble = "\\documentclass{article}\n\\usepackage{graphicx}\n\\usepackage{booktabs}\n\\usepackage{hyperref}\n\\usepackage{icml2026}\n\\icmltitlerunning{" + _latex_text(title[:80]) + "}\n"
        author = "\\begin{icmlauthorlist}\\icmlauthor{Anonymous Authors}{anon}\\end{icmlauthorlist}\\icmlaffiliation{anon}{Anonymous Institution}\\icmlcorrespondingauthor{Anonymous}{anonymous@example.com}\\printAffiliationsAndNotice{}"
        return f"{preamble}\\begin{{document}}\n\\twocolumn[\\icmltitle{{{_latex_text(title)}}}\n{author}\n\\icmlkeywords{{Anonymous Submission}}\n\\vskip 0.3in]\n\\begin{{abstract}}\n{_html_to_latex(abstract, assets)}\n\\end{{abstract}}\n{body}\n{impact}{main_end}\n\\bibliography{{references}}\n\\bibliographystyle{{icml2026}}\n\\appendix\n{appendix}\n\\end{{document}}\n"
    if profile["venue"] == "iclr":
        preamble = "\\documentclass{article}\n\\usepackage{iclr2026_conference,times}\n\\usepackage{hyperref}\n\\usepackage{booktabs}\n\\usepackage{graphicx}\n\\title{" + _latex_text(title) + "}\n\\author{Anonymous Authors\\\\Anonymous Institution}\n"
        return f"{preamble}\\begin{{document}}\n\\maketitle\n\\begin{{abstract}}\n{_html_to_latex(abstract, assets)}\n\\end{{abstract}}\n{body}\n{impact}{main_end}\n\\bibliography{{references}}\n\\bibliographystyle{{iclr2026_conference}}\n\\appendix\n{appendix}\n{checklist}\n\\end{{document}}\n"
    if profile["venue"] == "cvpr":
        preamble = "\\documentclass[10pt,twocolumn,letterpaper]{article}\n\\usepackage[review]{cvpr}\n\\usepackage{times}\n\\usepackage{epsfig}\n\\usepackage{graphicx}\n\\usepackage{amsmath}\n\\usepackage{amssymb}\n\\usepackage[pagebackref,breaklinks,colorlinks]{hyperref}\n\\def\\paperID{0000}\n\\def\\confName{CVPR}\n\\def\\confYear{2026}\n\\title{" + _latex_text(title) + "}\n\\author{Anonymous Authors}\n"
        return f"{preamble}\\begin{{document}}\n\\maketitle\n\\begin{{abstract}}\n{_html_to_latex(abstract, assets)}\n\\end{{abstract}}\n{body}\n{impact}{main_end}\n{{\\small\\bibliographystyle{{ieeenat_fullname}}\\bibliography{{references}}}}\n\\end{{document}}\n"
    if profile["venue"] == "colt":
        preamble = "\\documentclass[anon,12pt]{colt2026}\n\\usepackage{times}\n\\usepackage{graphicx}\n\\usepackage{booktabs}\n\\usepackage{hyperref}\n\\title[" + _latex_text(title[:40]) + "]{" + _latex_text(title) + "}\n"
        return f"{preamble}\\begin{{document}}\n\\maketitle\n\\begin{{abstract}}\n{_html_to_latex(abstract, assets)}\n\\end{{abstract}}\n{body}\n{impact}{main_end}\n\\bibliography{{references}}\n\\appendix\n{appendix}\n\\end{{document}}\n"
    if profile["venue"] == "neurips":
        preamble = "\\documentclass{article}\n\\usepackage{neurips_2026}\n\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n\\usepackage{hyperref}\n\\usepackage{url}\n\\usepackage{booktabs}\n\\usepackage{graphicx}\n\\title{" + _latex_text(title) + "}\n\\author{Anonymous Authors}"
        return f"{preamble}\n\\begin{{document}}\n\\maketitle\n\\begin{{abstract}}\n{_html_to_latex(abstract, assets)}\n\\end{{abstract}}\n{body}\n{impact}{main_end}\n{{\\small\\bibliographystyle{{plainnat}}\\bibliography{{references}}}}\n\\appendix\n{appendix}\n{checklist}\n\\end{{document}}\n"
    raise UnsupportedVenueError(f"unsupported document family {family}")


def _render_supplemental_latex(
    *,
    profile: dict[str, Any],
    title: str,
    appendix_sections: list[dict[str, Any]],
    assets: RenderAssets,
) -> str:
    appendix = "\n\n".join(_section_to_latex(section, assets) for section in appendix_sections)
    if profile["venue"] != "cvpr":
        raise UnsupportedVenueError("separate supplemental export is implemented for CVPR only")
    preamble = "\\documentclass[10pt,twocolumn,letterpaper]{article}\n\\usepackage[review]{cvpr}\n\\usepackage{times}\n\\usepackage{epsfig}\n\\usepackage{graphicx}\n\\usepackage{amsmath}\n\\usepackage{amssymb}\n\\usepackage[pagebackref,breaklinks,colorlinks]{hyperref}\n\\def\\paperID{0000}\n\\def\\confName{CVPR}\n\\def\\confYear{2026}\n\\title{Supplemental Material: " + _latex_text(title) + "}\n\\author{Anonymous Authors}\n"
    return f"{preamble}\\begin{{document}}\n\\maketitle\n\\appendix\n{appendix}\n\\end{{document}}\n"


def _required_statements(profile: dict[str, Any]) -> list[str]:
    out = []
    if profile.get("requires_impact_statement"):
        out.append("impact_statement")
    if profile.get("requires_checklist"):
        out.append("checklist")
    if profile["venue"] == "iclr":
        out.extend(["ethics_statement_recommended", "reproducibility_statement_recommended"])
    if profile["venue"] == "colt":
        out.append("main_text_proof_detail_required_for_theory")
    return out


def _section_to_latex(section: dict[str, Any], assets: RenderAssets) -> str:
    title = _latex_text(str(section.get("title") or section.get("section_id") or "Section"))
    if section.get("latex"):
        if section.get("latex_verified") is not True:
            raise UnsupportedManuscriptContent("section['latex'] requires latex_verified=true provenance")
        return f"\\section{{{title}}}\n{section['latex']}"
    return f"\\section{{{title}}}\n{_html_to_latex(str(section.get('prose_html') or ''), assets)}"


class _LatexHTML(HTMLParser):
    allowed = {"p", "strong", "em", "code", "ul", "ol", "li", "br", "a", "img", "table"}

    def __init__(self, assets: RenderAssets) -> None:
        super().__init__(convert_charrefs=True)
        self.assets = assets
        self.parts: list[str] = []
        self._cite_depth = 0
        self._skip_table_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.allowed:
            raise UnsupportedManuscriptContent(f"unsupported HTML tag <{tag}> for LaTeX export")
        attr = {name: value for name, value in attrs}
        if tag == "p":
            self.parts.append("\n\n")
        elif tag == "strong":
            self.parts.append("\\textbf{")
        elif tag == "em":
            self.parts.append("\\emph{")
        elif tag == "code":
            self.parts.append("\\texttt{")
        elif tag in {"ul", "ol"}:
            self.parts.append("\\begin{itemize}\n")
        elif tag == "li":
            self.parts.append("\\item ")
        elif tag == "br":
            self.parts.append("\\\\\n")
        elif tag == "a":
            href = attr.get("href") or ""
            if not href.startswith("#ref_"):
                raise UnsupportedManuscriptContent("LaTeX export supports links only as bibliography anchors href='#ref_id'")
            key = _clean_asset_id(href[len("#ref_") :], "citation")
            self.parts.append(f"\\cite{{{key}}}")
            self._cite_depth += 1
        elif tag == "img":
            figure_id = _figure_id_from_src(attr.get("src") or attr.get("data-figure-id") or "")
            path = self.assets.figures.get(figure_id)
            if path is None:
                raise VenueExportError(f"figure {figure_id!r} is referenced but no figure_files mapping was supplied")
            alt = attr.get("alt") or figure_id
            self.parts.append(
                "\n\\begin{figure}[t]\n\\centering\n"
                f"\\includegraphics[width=0.95\\linewidth]{{{path}}}\n"
                f"\\caption{{{_latex_text(alt)}}}\n"
                f"\\label{{fig:{figure_id}}}\n\\end{{figure}}\n"
            )
        elif tag == "table":
            table_id = _clean_asset_id(attr.get("id") or attr.get("data-table-id") or "", "table")
            latex = self.assets.tables.get(table_id)
            if latex is None:
                raise VenueExportError(f"table {table_id!r} is referenced but no tables mapping was supplied")
            self.parts.append("\n" + latex + "\n")
            self._skip_table_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"strong", "em", "code"}:
            self.parts.append("}")
        elif tag in {"ul", "ol"}:
            self.parts.append("\\end{itemize}\n")
        elif tag == "a" and self._cite_depth:
            self._cite_depth -= 1
        elif tag == "table" and self._skip_table_depth:
            self._skip_table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._cite_depth:
            return
        if self._skip_table_depth:
            if data.strip():
                raise UnsupportedManuscriptContent("HTML tables must be empty placeholders resolved from the verified tables mapping")
            return
        if any(marker in data for marker in ("$", "\\(", "\\[", "\\begin{")):
            raise UnsupportedManuscriptContent(
                "math or raw TeX was found in prose_html; provide verified section['latex'] so math is not stripped or escaped"
            )
        self.parts.append(_latex_text(data))


def _figure_id_from_src(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise VenueExportError("figure src is required")
    stem = Path(raw).stem if any(raw.lower().endswith(ext) for ext in (".pdf", ".png", ".jpg", ".jpeg")) else raw
    return _clean_asset_id(stem, "figure")


def _html_to_latex(value: str, assets: RenderAssets | None = None) -> str:
    parser = _LatexHTML(assets or RenderAssets(figures={}, tables={}))
    parser.feed(value)
    parser.close()
    return "".join(parser.parts).strip()


def _latex_text(value: str) -> str:
    text = html.unescape(str(value))
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def _render_bibtex(entries: list[dict[str, Any]]) -> str:
    if not entries:
        raise VenueExportError("at least one bibliography entry is required")
    rendered = []
    for entry in entries:
        key = re.sub(r"[^A-Za-z0-9:_-]", "", str(entry.get("id") or ""))
        if not key:
            raise VenueExportError("bibliography entry id is required")
        title = str(entry.get("title") or "").strip()
        authors = _bib_authors(entry.get("authors"))
        year = str(entry.get("year") or "").strip()
        if not title:
            raise VenueExportError(f"bibliography entry {key!r} missing title")
        if not authors:
            raise VenueExportError(f"bibliography entry {key!r} missing authors")
        if not year:
            raise VenueExportError(f"bibliography entry {key!r} missing year")
        fields = {"title": title, "author": authors, "year": year}
        entry_type = _bib_entry_type(entry)
        if entry_type == "article":
            journal = str(entry.get("journal") or "").strip()
            if not journal:
                raise VenueExportError(f"bibliography article {key!r} missing journal")
            fields["journal"] = journal
        elif entry_type == "inproceedings":
            booktitle = str(entry.get("booktitle") or entry.get("venue") or "").strip()
            if not booktitle:
                raise VenueExportError(f"bibliography inproceedings {key!r} missing venue/booktitle")
            fields["booktitle"] = booktitle
        if entry.get("arxiv_id"):
            fields["eprint"] = str(entry["arxiv_id"])
            fields["archivePrefix"] = "arXiv"
        if entry.get("url"):
            fields["url"] = str(entry["url"])
        body = ",\n".join(f"  {name} = {{{_bib_value(value)}}}" for name, value in fields.items())
        rendered.append(f"@{entry_type}{{{key},\n{body}\n}}\n")
    return "\n".join(rendered)


def _bib_authors(value: Any) -> str:
    if isinstance(value, list):
        return " and ".join(str(author).strip() for author in value if str(author).strip())
    return str(value or "").strip()


def _bib_entry_type(entry: Mapping[str, Any]) -> str:
    explicit = str(entry.get("entry_type") or "").strip().lower()
    if explicit:
        if explicit not in {"article", "inproceedings", "misc"}:
            raise VenueExportError(f"unsupported bibliography entry_type {explicit!r}")
        return explicit
    if entry.get("journal"):
        return "article"
    if entry.get("venue") or entry.get("booktitle"):
        return "inproceedings"
    if entry.get("arxiv_id") or entry.get("url"):
        return "misc"
    raise VenueExportError("bibliography entry requires journal, venue/booktitle, arxiv_id, url, or explicit entry_type")


def _bib_value(value: str) -> str:
    return str(value).replace("\\", "").replace("{", "").replace("}", "")


def _compile(output_dir: Path, jobname: str, *, main_page_limit: int | None) -> dict[str, Any]:
    tex_file = f"{jobname}.tex"
    commands = [["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", tex_file]]
    commands.append(["bibtex", jobname])
    commands.extend([
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", tex_file],
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", tex_file],
    ])
    logs = []
    for command in commands:
        if command[0] == "bibtex" and not _aux_has_bibliography(output_dir / f"{jobname}.aux"):
            logs.append({"command": command, "returncode": None, "output_tail": "skipped: no bibliography in aux"})
            continue
        result = subprocess.run(command, cwd=output_dir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        logs.append({"command": command, "returncode": result.returncode, "output_tail": result.stdout[-4000:]})
        if result.returncode != 0:
            raise VenueExportError(f"LaTeX command failed: {' '.join(command)}\n{result.stdout[-2000:]}")
    pdf = output_dir / f"{jobname}.pdf"
    if not pdf.exists() or pdf.stat().st_size == 0:
        raise VenueExportError(f"pdflatex did not produce {jobname}.pdf")
    pages = _pdf_pages(pdf)
    main_pages = _main_pages_from_aux(output_dir / f"{jobname}.aux") if main_page_limit is not None else None
    if main_page_limit is not None:
        if main_pages is None:
            raise VenueExportError("could not determine main-body page count from LaTeX label")
        if main_pages > main_page_limit:
            raise VenueExportError(f"main body is {main_pages} pages; target limit is {main_page_limit}")
    return {"commands": logs, "pdf_pages": pages, "main_pages": main_pages}


def _aux_has_bibliography(aux_path: Path) -> bool:
    if not aux_path.exists():
        return False
    text = aux_path.read_text(encoding="utf-8", errors="replace")
    return "\\bibdata{" in text


def _pdf_pages(pdf: Path) -> int:
    info = subprocess.run(["pdfinfo", str(pdf)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    for line in info.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise VenueExportError(f"could not read PDF page count\n{info.stdout[-1000:]}")


def _main_pages_from_aux(aux_path: Path) -> int | None:
    if not aux_path.exists():
        return None
    pattern = re.compile(r"\\newlabel\{" + re.escape(_MAIN_END_LABEL) + r"\}\{\{.*?\}\{(\d+)\}")
    for line in aux_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.search(line)
        if match:
            return int(match.group(1))
    return None


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
