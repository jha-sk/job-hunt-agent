r"""
src/pdf_generator.py — Job Hunt Agent · Phase 5 PDF generator.

WHAT IT DOES
------------
Converts each tailored `resumes/tailored/resume_<co>_<role>_<date>.md`
into a clean, ATS-friendly PDF at `resumes/pdf/resume_<co>_<role>_<date>.pdf`.

DESIGN CHOICES (locked at Phase 5 wrap-up)
------------------------------------------
- Engine: WeasyPrint. Renders HTML+CSS → PDF. Highest fidelity, smallest
  output, fastest runtime once GTK is installed. NEEDS the GTK Runtime
  on Windows: https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases
  Linux/macOS have GTK out of the box.
- Layout: Single column. ATS-friendly (no tables, no columns, no images).
- Fonts: Helvetica/Arial fallback chain. Embeds DejaVu Sans as a backup so
  the PDF renders identically on any machine, regardless of system fonts.
- Header: Name (centered, large) + one-line contact strip (phone · email ·
  LinkedIn URL · GitHub URL · location). All four URLs visible so any ATS
  can extract them whether or not it follows hyperlinks.
- Page target: 1 page, max 2.

HOW TO RUN
----------
    .\.venv\Scripts\python.exe -m src.pdf_generator              # PDF every tailored .md for today
    .\.venv\Scripts\python.exe -m src.pdf_generator --top-only   # just the highest-scored one
    .\.venv\Scripts\python.exe -m src.pdf_generator --input <md>
    .\.venv\Scripts\python.exe -m src.pdf_generator --date 2026-05-30
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    CANDIDATE_EMAIL,
    CANDIDATE_GITHUB,
    CANDIDATE_LINKEDIN,
    CANDIDATE_LOCATION,
    CANDIDATE_NAME,
    CANDIDATE_PHONE,
    RESUMES_PDF_DIR,
    RESUMES_TAILORED_DIR,
)
from src import db  # noqa: E402

log = logging.getLogger("pdf_generator")


# =============================================================================
# CSS — embedded once, applied to every rendered resume.
# All choices here serve the ATS-friendly + single-column + 1-page-target
# goal. Don't add background colors, tables, or two-column layouts.
# =============================================================================
RESUME_CSS = """
@page {
    size: Letter;
    margin: 0.55in 0.65in 0.55in 0.65in;
}

/* Reset + base */
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: 'Helvetica', 'Arial', 'Liberation Sans', sans-serif;
    font-size: 10.5pt;
    line-height: 1.36;
    color: #111;
}

/* Name + contact header */
header.resume-header {
    text-align: center;
    margin-bottom: 10pt;
}
header.resume-header h1 {
    font-size: 22pt;
    font-weight: 700;
    letter-spacing: 0.02em;
    margin-bottom: 4pt;
}
header.resume-header .contact {
    font-size: 9.5pt;
    color: #333;
}
header.resume-header .contact a {
    color: #111;
    text-decoration: none;
}

/* Section headers — small-caps with a thin rule under, matches the
   original PDF's look while staying ATS-clean (no images/icons). */
h2 {
    font-size: 11pt;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
    color: #000;
    border-bottom: 0.6pt solid #444;
    padding-bottom: 1pt;
    margin-top: 9pt;
    margin-bottom: 4pt;
}

/* Sub-headings: job title, project name */
h3 {
    font-size: 10.5pt;
    font-weight: 700;
    margin-top: 5pt;
    margin-bottom: 1pt;
}

/* The italic date line under a job title (markdown emits a <p><em>…</em></p>) */
p em {
    font-style: italic;
    color: #444;
    font-size: 9.5pt;
}

/* Body paragraphs (summary text) */
p {
    margin-top: 2pt;
    margin-bottom: 2pt;
}

/* Bullet lists — kept tight to maximize one-page fit */
ul {
    padding-left: 16pt;
    margin-top: 2pt;
    margin-bottom: 4pt;
}
li {
    margin-bottom: 1.5pt;
}
li::marker {
    color: #444;
}

/* Inline link styling — visible URL, no underline color noise.
   ATS extracts both the visible text and the href. */
a { color: #1a3a72; text-decoration: none; }

/* Avoid page break mid-section / mid-bullet */
h2, h3 { page-break-after: avoid; }
li     { page-break-inside: avoid; }
"""


# =============================================================================
# HTML scaffold — name + contact rendered manually (NOT from markdown),
# so we control the layout exactly. The body comes from markdown_html.
# =============================================================================
HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{name} — Résumé</title>
  <style>{css}</style>
</head>
<body>
  <header class="resume-header">
    <h1>{name}</h1>
    <div class="contact">{contact}</div>
  </header>
  {body_html}
</body>
</html>
"""


def _build_contact_line() -> str:
    """
    One-line contact strip. URLs are visible as text AND clickable, so
    BOTH href-following and text-extracting ATS systems pick them up.

    Format:  phone · email · linkedin.com/in/x · github.com/x · location
    """
    # Strip protocol/scheme from URLs for visible display.
    def _short(url: str) -> str:
        return url.replace("https://", "").replace("http://", "").rstrip("/")

    parts = [
        CANDIDATE_PHONE,
        f'<a href="mailto:{CANDIDATE_EMAIL}">{CANDIDATE_EMAIL}</a>',
        f'<a href="{CANDIDATE_LINKEDIN}">{_short(CANDIDATE_LINKEDIN)}</a>',
        f'<a href="{CANDIDATE_GITHUB}">{_short(CANDIDATE_GITHUB)}</a>',
        CANDIDATE_LOCATION,
    ]
    return " &nbsp;·&nbsp; ".join(parts)


# =============================================================================
# Markdown body parsing
# =============================================================================
# The tailored .md files have this shape (produced by src/tailor.py):
#   # Name                       <- line 1
#   contact-line ...             <- line 2
#   (blank)
#   ## Summary
#   ...
# We strip the first 2 lines (we re-render the header ourselves) and
# feed the rest to markdown-it-py.
def _strip_header_lines(md: str) -> str:
    """Drop the name (#) and contact line — the HTML template replaces them."""
    lines = md.splitlines()
    if not lines or not lines[0].startswith("# "):
        # Defensive: if the format ever changes, render the whole thing
        # rather than crash. The header will appear twice — visible bug.
        return md
    # Skip line 0 (# Name), line 1 (contact), and any blank lines after.
    i = 2
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "\n".join(lines[i:])


def _markdown_to_html(md_body: str) -> str:
    """Render the resume body to HTML. markdown-it-py is in requirements.txt."""
    from markdown_it import MarkdownIt
    parser = MarkdownIt("commonmark", {"breaks": False, "html": False})
    return parser.render(md_body)


# =============================================================================
# Top-level rendering: .md -> .pdf
# =============================================================================
def render_pdf_from_markdown(md_path: Path, pdf_path: Path) -> None:
    """
    Read tailored markdown, render to single-column ATS-friendly PDF.
    Raises if WeasyPrint can't load (GTK missing on Windows is the usual cause).
    """
    md = md_path.read_text(encoding="utf-8")
    body_md = _strip_header_lines(md)
    body_html = _markdown_to_html(body_md)

    html_doc = HTML_TEMPLATE.format(
        name=CANDIDATE_NAME,
        contact=_build_contact_line(),
        css=RESUME_CSS,
        body_html=body_html,
    )

    # Lazy import — gives a clearer error message when GTK is missing.
    try:
        from weasyprint import HTML
    except OSError as exc:
        raise RuntimeError(
            "WeasyPrint couldn't load its native libraries. On Windows this "
            "means GTK is not installed. Install from:\n"
            "  https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases\n"
            f"Original error: {exc}"
        ) from exc

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_doc, base_url=str(md_path.parent)).write_pdf(str(pdf_path))


def pdf_path_for(md_path: Path) -> Path:
    """Mirror the tailored .md filename into resumes/pdf/, swap suffix."""
    return RESUMES_PDF_DIR / (md_path.stem + ".pdf")


# =============================================================================
# Batch — pick today's tailored files (or a specific one)
# =============================================================================
def _todays_tailored_files(date_str: str) -> list[Path]:
    """
    All `resume_<co>_<role>_<date>.md` files for date_str. Sorted by name
    so output order is deterministic. The `.changes.md` siblings are
    excluded (we only PDF the resume itself).
    """
    pattern = f"resume_*_{date_str}.md"
    found = sorted(RESUMES_TAILORED_DIR.glob(pattern))
    return [p for p in found if not p.name.endswith(".changes.md")]


def run(
    input_path: Path | None = None,
    date_str: str | None = None,
    top_only: bool = False,
) -> list[Path]:
    """
    PDF every tailored resume for today (or a specific date / single file).
    Returns the list of written PDFs.
    """
    if input_path:
        md_files = [input_path]
    else:
        date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        md_files = _todays_tailored_files(date_str)
        if not md_files:
            log.warning(
                "pdf: no tailored .md files for %s. Run `python -m src.tailor` first.",
                date_str,
            )
            return []

    if top_only:
        md_files = md_files[:1]

    log.info("====== PDF run — %d resume(s) ======", len(md_files))

    written: list[Path] = []
    failures: list[tuple[str, str]] = []
    for i, md in enumerate(md_files, 1):
        out = pdf_path_for(md)
        try:
            render_pdf_from_markdown(md, out)
            sz_kb = out.stat().st_size / 1024
            log.info("pdf %d/%d -> %s (%.1f KB)", i, len(md_files), out.name, sz_kb)
            written.append(out)
            # Record the PDF path against the job row (looked up by md path).
            try:
                rows = db.set_pdf_path_by_md(str(md), str(out))
                if rows == 0:
                    log.debug("pdf: no job row matched md=%s (tailor not yet persisted?)", md.name)
            except Exception as db_exc:  # noqa: BLE001 — log but don't lose the file
                log.warning("pdf: db persist failed for %s: %s", md.name, db_exc)
        except Exception as exc:  # noqa: BLE001 — one bad PDF shouldn't kill the batch
            log.error("pdf %d/%d FAILED for %s: %s", i, len(md_files), md.name, exc)
            failures.append((md.name, str(exc)))

    # Update today's daily_runs row with the count of PDFs generated.
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.upsert_daily_run(today, pdfs_generated=len(written))
    except Exception as db_exc:  # noqa: BLE001
        log.warning("pdf: db daily_runs update failed: %s", db_exc)

    _print_summary(written, failures)
    log.info("====== PDF run complete: %d written, %d failed ======",
             len(written), len(failures))
    return written


def _print_summary(written: list[Path], failures: list[tuple[str, str]]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for p in written:
            print(f"  wrote {p}")
        for name, err in failures:
            print(f"  FAILED {name}: {err}")
        return

    console = Console()
    if written:
        table = Table(title="PDFs written", show_header=True, header_style="bold")
        table.add_column("File", style="white")
        table.add_column("Size (KB)", justify="right")
        for p in written:
            table.add_row(p.name, f"{p.stat().st_size / 1024:.1f}")
        console.print(table)
    if failures:
        ftable = Table(title="Failures", show_header=True, header_style="bold red")
        ftable.add_column("File")
        ftable.add_column("Error")
        for name, err in failures:
            ftable.add_row(name, err[:120])
        console.print(ftable)


# =============================================================================
# CLI
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert tailored resume .md files into ATS-friendly PDFs.",
    )
    parser.add_argument("--input", type=Path,
                        help="Path to a single tailored .md file. Overrides --date.")
    parser.add_argument("--date", type=str,
                        help="Date string YYYY-MM-DD (default: today UTC).")
    parser.add_argument("--top-only", action="store_true",
                        help="Only PDF the first matching tailored file.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("weasyprint", "fontTools", "cssselect2", "tinycss2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    run(input_path=args.input, date_str=args.date, top_only=args.top_only)


if __name__ == "__main__":
    main()
