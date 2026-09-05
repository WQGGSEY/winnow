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
from typing import Any
from urllib.request import Request, urlopen


class VenueExportError(ValueError):
    pass


class UnsupportedVenueError(VenueExportError):
    pass


class UnsupportedManuscriptContent(VenueExportError):
    pass


PROFILE_PATH = Path(__file__).with_name("venue_templates") / "profiles.json"


@dataclass(frozen=True, slots=True)
class VenueTarget:
    venue: str
    year: int
    track: str = "main"

    @property
    def profile_key(self) -> str:
        return f"{self.venue}-{self.year}-{self.track}"


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
    template_cache_dir: Path | None = None,
    template_zip_path: Path | None = None,
) -> dict[str, Any]:
    """Build a venue package and compile a PDF.

    The returned receipt describes a compiled preview against a pinned venue
    profile. It does not certify final conference submission readiness.
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
    tex = _render_latex(
        profile=profile,
        title=title,
        abstract=abstract,
        sections=sections,
        appendix_sections=appendix_sections or [],
        impact_statement=impact_statement,
        checklist_tex=checklist_tex,
    )
    bib = _render_bibtex(bibliography)
    (output_dir / "paper.tex").write_text(tex, encoding="utf-8")
    (output_dir / "references.bib").write_text(bib, encoding="utf-8")
    compile_receipt = _compile(output_dir)
    receipt = {
        "version": 1,
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
        "anonymous_author_default": profile["anonymous"],
        "required_statements": _required_statements(profile),
        "appendix_policy": profile["appendix_policy"],
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
        if destination.name in {"main.tex", "example_paper.tex", "iclr2026_conference.tex", "colt2026-sample.tex"}:
            continue
        if destination.name.endswith(".bib"):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _render_latex(
    *,
    profile: dict[str, Any],
    title: str,
    abstract: str,
    sections: list[dict[str, Any]],
    appendix_sections: list[dict[str, Any]],
    impact_statement: str | None,
    checklist_tex: str | None,
) -> str:
    family = profile["document_family"]
    body = "\n\n".join(_section_to_latex(section) for section in sections)
    appendix = "\n\n".join(_section_to_latex(section) for section in appendix_sections)
    impact = ""
    if profile.get("requires_impact_statement"):
        if not impact_statement or not impact_statement.strip():
            raise VenueExportError("target requires an impact_statement")
        impact = "\\section*{Impact Statement}\n" + _html_to_latex(impact_statement)
    checklist = ""
    if profile.get("requires_checklist"):
        if not checklist_tex or not checklist_tex.strip():
            raise VenueExportError("target requires checklist_tex")
        checklist = "\n\\section*{Checklist}\n" + checklist_tex
    if profile["venue"] == "icml":
        preamble = "\\documentclass{article}\n\\usepackage{graphicx}\n\\usepackage{booktabs}\n\\usepackage{hyperref}\n\\usepackage{icml2026}\n\\icmltitlerunning{" + _latex_text(title[:80]) + "}\n"
        author = "\\begin{icmlauthorlist}\\icmlauthor{Anonymous Authors}{anon}\\end{icmlauthorlist}\\icmlaffiliation{anon}{Anonymous Institution}\\icmlcorrespondingauthor{Anonymous}{anonymous@example.com}\\printAffiliationsAndNotice{}"
        return f"{preamble}\\begin{{document}}\n\\twocolumn[\\icmltitle{{{_latex_text(title)}}}\n{author}\n\\icmlkeywords{{Anonymous Submission}}\n\\vskip 0.3in]\n\\begin{{abstract}}\n{_html_to_latex(abstract)}\n\\end{{abstract}}\n{body}\n{impact}\n\\nocite{{*}}\n\\bibliography{{references}}\n\\bibliographystyle{{icml2026}}\n\\appendix\n{appendix}\n\\end{{document}}\n"
    if profile["venue"] == "iclr":
        preamble = "\\documentclass{article}\n\\usepackage{iclr2026_conference,times}\n\\usepackage{hyperref}\n\\usepackage{booktabs}\n\\title{" + _latex_text(title) + "}\n\\author{Anonymous Authors\\\\Anonymous Institution}\n"
        return f"{preamble}\\begin{{document}}\n\\maketitle\n\\begin{{abstract}}\n{_html_to_latex(abstract)}\n\\end{{abstract}}\n{body}\n{impact}\n\\nocite{{*}}\n\\bibliography{{references}}\n\\bibliographystyle{{iclr2026_conference}}\n\\appendix\n{appendix}\n{checklist}\n\\end{{document}}\n"
    if profile["venue"] == "cvpr":
        preamble = "\\documentclass[10pt,twocolumn,letterpaper]{article}\n\\usepackage[review]{cvpr}\n\\usepackage{times}\n\\usepackage{epsfig}\n\\usepackage{graphicx}\n\\usepackage{amsmath}\n\\usepackage{amssymb}\n\\usepackage[pagebackref,breaklinks,colorlinks]{hyperref}\n\\def\\paperID{0000}\n\\def\\confName{CVPR}\n\\def\\confYear{2026}\n\\title{" + _latex_text(title) + "}\n\\author{Anonymous Authors}\n"
        return f"{preamble}\\begin{{document}}\n\\maketitle\n\\begin{{abstract}}\n{_html_to_latex(abstract)}\n\\end{{abstract}}\n{body}\n{impact}\n\\nocite{{*}}\n{{\\small\\bibliographystyle{{ieeenat_fullname}}\\bibliography{{references}}}}\n\\appendix\n{appendix}\n\\end{{document}}\n"
    if profile["venue"] == "colt":
        preamble = "\\documentclass[anon,12pt]{colt2026}\n\\usepackage{times}\n\\usepackage{hyperref}\n\\title[" + _latex_text(title[:40]) + "]{" + _latex_text(title) + "}\n"
        return f"{preamble}\\begin{{document}}\n\\maketitle\n\\begin{{abstract}}\n{_html_to_latex(abstract)}\n\\end{{abstract}}\n{body}\n{impact}\n\\nocite{{*}}\n\\bibliography{{references}}\n\\appendix\n{appendix}\n\\end{{document}}\n"
    raise UnsupportedVenueError(f"unsupported document family {family}")


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


def _section_to_latex(section: dict[str, Any]) -> str:
    title = _latex_text(str(section.get("title") or section.get("section_id") or "Section"))
    if section.get("latex"):
        return f"\\section{{{title}}}\n{section['latex']}"
    return f"\\section{{{title}}}\n{_html_to_latex(str(section.get('prose_html') or ''))}"


class _LatexHTML(HTMLParser):
    allowed = {"p", "strong", "em", "code", "ul", "ol", "li", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.allowed:
            raise UnsupportedManuscriptContent(
                f"unsupported HTML tag <{tag}> for LaTeX export; provide section['latex'] for math, tables, or custom markup"
            )
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

    def handle_endtag(self, tag: str) -> None:
        if tag in {"strong", "em", "code"}:
            self.parts.append("}")
        elif tag in {"ul", "ol"}:
            self.parts.append("\\end{itemize}\n")

    def handle_data(self, data: str) -> None:
        if any(marker in data for marker in ("$", "\\(", "\\[", "\\begin{")):
            raise UnsupportedManuscriptContent(
                "math or raw TeX was found in prose_html; provide section['latex'] so math is not stripped or escaped"
            )
        self.parts.append(_latex_text(data))


def _html_to_latex(value: str) -> str:
    parser = _LatexHTML()
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
        authors = str(entry.get("authors") or "Anonymous").strip()
        year = str(entry.get("year") or "2026").strip()
        if not title:
            raise VenueExportError(f"bibliography entry {key!r} missing title")
        fields = {
            "title": title,
            "author": authors,
            "year": year,
        }
        if entry.get("venue"):
            fields["booktitle"] = str(entry["venue"])
        if entry.get("url"):
            fields["url"] = str(entry["url"])
        body = ",\n".join(f"  {name} = {{{_bib_value(value)}}}" for name, value in fields.items())
        rendered.append(f"@inproceedings{{{key},\n{body}\n}}\n")
    return "\n".join(rendered)


def _bib_value(value: str) -> str:
    return str(value).replace("\\", "").replace("{", "").replace("}", "")


def _compile(output_dir: Path) -> dict[str, Any]:
    commands = [
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "paper.tex"],
        ["bibtex", "paper"],
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "paper.tex"],
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "paper.tex"],
    ]
    logs = []
    for command in commands:
        result = subprocess.run(command, cwd=output_dir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        logs.append({"command": command, "returncode": result.returncode, "output_tail": result.stdout[-4000:]})
        if result.returncode != 0:
            raise VenueExportError(f"LaTeX command failed: {' '.join(command)}\n{result.stdout[-2000:]}")
    pdf = output_dir / "paper.pdf"
    if not pdf.exists() or pdf.stat().st_size == 0:
        raise VenueExportError("pdflatex did not produce paper.pdf")
    info = subprocess.run(["pdfinfo", str(pdf)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    pages = None
    for line in info.stdout.splitlines():
        if line.startswith("Pages:"):
            pages = int(line.split(":", 1)[1].strip())
            break
    if pages is None:
        raise VenueExportError(f"could not read PDF page count\n{info.stdout[-1000:]}")
    return {"commands": logs, "pdf_pages": pages}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
