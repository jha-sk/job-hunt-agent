"""
src/resume_parser.py — Convert the source resume PDF into structured forms.

WHAT IT DOES
------------
Reads `data/Latest_Resume.pdf` and produces:
  - `data/resume.md`   — clean markdown, human-readable, used in LLM prompts
  - `data/resume.json` — structured object, used by the tailor to know what
                         it's allowed to rewrite vs. preserve verbatim

WHY BOTH
--------
The markdown version is what we feed to Claude (LLMs handle markdown well
and it preserves layout cues). The JSON version is what the tailor mutates
field-by-field so we can enforce LOCKED_RESUME_FIELDS from config.

WHEN TO RE-RUN
--------------
Any time you update your resume PDF. Just drop the new file in as
`data/Latest_Resume.pdf` and run:

    python -m src.resume_parser

USAGE
-----
    python -m src.resume_parser              # parse + write both files
    python -m src.resume_parser --dry-run    # parse + print, don't write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# pdfplumber is the PDF text extractor. If it's not installed we fall back
# to a friendly error rather than a stack trace.
try:
    import pdfplumber
except ImportError:
    sys.exit(
        "pdfplumber is not installed. Run:  python -m pip install -r requirements.txt"
    )

# Project root import — keep this file runnable as `python -m src.resume_parser`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    CANDIDATE_EMAIL,
    CANDIDATE_GITHUB,
    CANDIDATE_LINKEDIN,
    CANDIDATE_LOCATION,
    CANDIDATE_NAME,
    CANDIDATE_PHONE,
    RESUME_JSON,
    RESUME_MD,
    RESUME_PDF_SOURCE,
)


# =============================================================================
# PDF → raw text
# =============================================================================
# Why this is more complicated than `page.extract_text()`:
# Sourabh's resume PDF uses kerning that makes pdfplumber's default text
# extraction lose inter-word spaces (e.g. "TECHNICAL SKILLS" → "TECHNICALSKILLS").
# So instead we pull WORDS (with x/y coordinates) and reconstruct lines
# ourselves, inserting a single space between adjacent words and a TAB-STOP
# marker (two spaces) when there is a large x-gap — that gap is how the
# resume signals right-aligned dates ("Associate Software Engineer ... Nov 2024 – Present").
# Downstream parsers split on `\s{2,}` to recover the title/date pair.
LINE_Y_TOLERANCE: float = 3.0     # pixels — words within this Δy are same line
BIG_X_GAP: float = 30.0           # pixels — bigger gap = right-aligned tab-stop
WORD_X_TOLERANCE: float = 1.0     # pixels — empirically right for Sourabh's
                                  # LaTeX-rendered PDF; default 3 was too loose
                                  # and merged words like "AssociateSoftwareEngineer".


def extract_text(pdf_path: Path) -> str:
    """
    Pull text out of the PDF as visually-correct lines, preserving spacing.

    Returns one big string with `\\n` between lines and `\\n\\n` between pages.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(
            f"Resume PDF not found at {pdf_path}. "
            "Place your latest resume there and re-run."
        )

    pages_text: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(
                use_text_flow=False,
                x_tolerance=WORD_X_TOLERANCE,
            )

            # 1) Group words into visual lines by y-coordinate.
            lines: list[list[dict]] = []
            current_line: list[dict] = []
            current_top: float | None = None
            for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
                if current_top is None or abs(w["top"] - current_top) <= LINE_Y_TOLERANCE:
                    current_line.append(w)
                    if current_top is None:
                        current_top = w["top"]
                else:
                    lines.append(current_line)
                    current_line = [w]
                    current_top = w["top"]
            if current_line:
                lines.append(current_line)

            # 2) Reconstruct each line: single space between close words,
            #    double space between wide-gap words (right-aligned content).
            page_lines: list[str] = []
            for line in lines:
                line_sorted = sorted(line, key=lambda w: w["x0"])
                parts: list[str] = [line_sorted[0]["text"]]
                for prev, curr in zip(line_sorted, line_sorted[1:]):
                    gap = curr["x0"] - prev["x1"]
                    if gap >= BIG_X_GAP:
                        parts.append("  " + curr["text"])  # tab-stop marker
                    else:
                        parts.append(" " + curr["text"])
                page_lines.append("".join(parts))

            pages_text.append("\n".join(page_lines))

    return "\n\n".join(pages_text).strip()


# =============================================================================
# Raw text → structured JSON
# =============================================================================
# This parser is deliberately hand-rolled rather than LLM-based — it runs
# offline, costs $0, and the structure of Sourabh's resume is stable enough
# that section-header detection works reliably. If you redesign the resume
# layout, this function is the only thing that needs updating.
#
# Section headers we look for (case-insensitive, exact match per line):
SECTION_HEADERS = {
    "technical skills": "skills",
    "skills": "skills",
    "certifications": "certifications",
    "experience": "experience",
    "work experience": "experience",
    "projects": "projects",
    "education": "education",
    "summary": "summary",
    "professional summary": "summary",
}


def _split_into_sections(text: str) -> dict[str, list[str]]:
    """
    Split the raw resume text into a dict of {section_name: [lines]}.

    Walks line-by-line; when it sees a known section header, starts
    accumulating subsequent lines under that section's key.
    """
    sections: dict[str, list[str]] = {"header": []}
    current = "header"

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        header_key = line.strip().lower()
        if header_key in SECTION_HEADERS:
            current = SECTION_HEADERS[header_key]
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)

    return sections


def _parse_skills_block(lines: list[str]) -> dict[str, list[str]]:
    """
    Parse the Technical Skills section, which has the shape:

        Languages: Go, Java, Python, ...
        Backend & APIs: REST APIs, Microservices, ...
        ...

    Returns a dict of {category: [skills]}.
    """
    skills: dict[str, list[str]] = {}
    for line in lines:
        line = line.strip()
        if not line or ":" not in line:
            continue
        category, items = line.split(":", 1)
        # Normalise category name to snake_case-ish key.
        key = (
            category.strip().lower()
            .replace(" & ", "_")
            .replace(" ", "_")
            .replace("-", "_")
        )
        skills[key] = [s.strip() for s in items.split(",") if s.strip()]
    return skills


def _parse_bullet_block(lines: list[str]) -> list[str]:
    """
    Pull bullet lines from a block of text. Treats anything starting
    with '-', '–', '•', or '*' as a bullet; joins continuation lines.
    """
    bullets: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            bullets.append(" ".join(s.strip() for s in buffer).strip())
            buffer.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith(("-", "–", "•", "*")):
            flush()
            buffer.append(stripped.lstrip("-–•* ").strip())
        else:
            buffer.append(stripped)
    flush()
    return [b for b in bullets if b]


def parse_resume(text: str) -> dict[str, Any]:
    """
    Build the structured resume dict from raw PDF text.

    The output schema is stable — the tailor and scorer depend on these
    exact field names. If you rename a field here, you must also update
    config.LOCKED_RESUME_FIELDS and the prompts in tailor.py / scorer.py.
    """
    sections = _split_into_sections(text)

    # Skills section.
    skills = _parse_skills_block(sections.get("skills", []))

    # Certifications: each non-empty line is one certification.
    certifications = [
        line.strip().lstrip("•-–* ").strip()
        for line in sections.get("certifications", [])
        if line.strip()
    ]

    # Experience: Sourabh has a single role. We hardcode the parser to
    # extract the company/title/dates from the first two non-empty lines
    # and the rest as bullets. If you add a second job, generalise this.
    exp_lines = [ln for ln in sections.get("experience", []) if ln.strip()]
    experience: list[dict[str, Any]] = []
    if exp_lines:
        # Line 1: "Associate Software Engineer        Nov 2024 – Present"
        # Line 2: "Accenture Solutions Pvt Ltd"
        first = exp_lines[0]
        # Split title from dates on the rightmost run of 2+ spaces.
        import re
        m = re.split(r"\s{2,}", first.strip(), maxsplit=1)
        title = m[0].strip() if m else first.strip()
        dates = m[1].strip() if len(m) > 1 else ""
        company = exp_lines[1].strip() if len(exp_lines) > 1 else ""
        bullets = _parse_bullet_block(exp_lines[2:])
        experience.append({
            "title": title,
            "company": company,
            "dates": dates,
            "bullets": bullets,
        })

    # Projects: each project block starts with "<Name> | Stack: ... | <link>"
    # and is followed by bullets.
    project_lines = sections.get("projects", [])
    projects: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in project_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if "|" in stripped and "Stack:" in stripped:
            if current:
                projects.append(current)
            parts = [p.strip() for p in stripped.split("|")]
            name = parts[0]
            stack = ""
            link = ""
            for p in parts[1:]:
                if p.lower().startswith("stack:"):
                    stack = p.split(":", 1)[1].strip()
                else:
                    link = p
            current = {"name": name, "stack": stack, "link": link, "bullets": []}
        elif current is not None and stripped.startswith(("-", "–", "•", "*")):
            current["bullets"].append(stripped.lstrip("-–•* ").strip())
        elif current is not None:
            if current["bullets"]:
                current["bullets"][-1] += " " + stripped
    if current:
        projects.append(current)

    # Education: simplest of all. Sourabh has one degree.
    edu_lines = [ln for ln in sections.get("education", []) if ln.strip()]
    education: list[dict[str, Any]] = []
    if edu_lines:
        # Line 1: degree (+ maybe right-aligned "Graduated")
        # Line 2: institution (+ maybe CGPA)
        import re
        m1 = re.split(r"\s{2,}", edu_lines[0].strip(), maxsplit=1)
        degree = m1[0].strip()
        status = m1[1].strip() if len(m1) > 1 else ""
        m2 = (
            re.split(r"\s{2,}", edu_lines[1].strip(), maxsplit=1)
            if len(edu_lines) > 1 else [""]
        )
        institution = m2[0].strip() if m2 else ""
        cgpa = m2[1].strip() if len(m2) > 1 else ""
        education.append({
            "degree": degree,
            "institution": institution,
            "status": status,
            "cgpa": cgpa,
        })

    return {
        "name": CANDIDATE_NAME,
        "contact": {
            "email": CANDIDATE_EMAIL,
            "phone": CANDIDATE_PHONE,
            "linkedin": CANDIDATE_LINKEDIN,
            "github": CANDIDATE_GITHUB,
            "location": CANDIDATE_LOCATION,
        },
        # Sourabh's current resume has no professional summary. Leaving
        # this empty signals to the tailor (Phase 4) that it should
        # generate one fresh per job rather than rewrite an existing one.
        "summary": "",
        "skills": skills,
        "certifications": certifications,
        "experience": experience,
        "projects": projects,
        "education": education,
    }


# =============================================================================
# Structured → markdown
# =============================================================================
# Skill-category keys are normalised to snake_case in _parse_skills_block
# ("Backend & APIs" → "backend_apis"). For display we reverse the mapping
# using a small lookup table of words that .title() would mangle.
_CATEGORY_WORD_FIXES: dict[str, str] = {
    "Apis": "APIs",
    "Ci": "CI",
    "Cd": "CD",
    "Devops": "DevOps",
    "Ai": "AI",
    "Llm": "LLM",
    "Gcp": "GCP",
    "Aws": "AWS",
}


def _humanize_category(key: str) -> str:
    """
    Turn 'backend_apis' → 'Backend & APIs', 'ai_augmented_engineering' →
    'AI-Augmented Engineering'. Heuristic; if you add new skill categories
    with weird casing, extend _CATEGORY_WORD_FIXES.
    """
    # Two-word categories that originally had '&' in them get '&' back.
    two_word_amp = {"backend_apis", "cloud_devops"}
    sep = " & " if key in two_word_amp else " "
    # The 'ai_augmented_engineering' style keeps a hyphen between the first
    # two words (matches the source resume's "AI-Augmented Engineering").
    parts = key.split("_")
    titled = [_CATEGORY_WORD_FIXES.get(p.title(), p.title()) for p in parts]
    if key == "ai_augmented_engineering":
        return f"{titled[0]}-{titled[1]} {titled[2]}"
    return sep.join(titled) if len(titled) <= 2 else " ".join(titled)


def to_markdown(resume: dict[str, Any]) -> str:
    """
    Render the structured resume back to clean markdown for LLM prompts.

    This is NOT the format that gets PDF'd in Phase 5 — that uses a Jinja
    HTML template. This output is purely for feeding to Claude as context.
    """
    lines: list[str] = []
    c = resume["contact"]

    lines.append(f"# {resume['name']}")
    lines.append(
        f"{c['phone']} · {c['email']} · "
        f"[LinkedIn]({c['linkedin']}) · [GitHub]({c['github']}) · {c['location']}"
    )
    lines.append("")

    if resume.get("summary"):
        lines.append("## Summary")
        lines.append(resume["summary"])
        lines.append("")

    if resume.get("skills"):
        lines.append("## Technical Skills")
        for category, items in resume["skills"].items():
            pretty = _humanize_category(category)
            lines.append(f"- **{pretty}**: {', '.join(items)}")
        lines.append("")

    if resume.get("certifications"):
        lines.append("## Certifications")
        for cert in resume["certifications"]:
            lines.append(f"- {cert}")
        lines.append("")

    if resume.get("experience"):
        lines.append("## Experience")
        for exp in resume["experience"]:
            lines.append(f"### {exp['title']} — {exp['company']}")
            if exp.get("dates"):
                lines.append(f"*{exp['dates']}*")
            for b in exp.get("bullets", []):
                lines.append(f"- {b}")
            lines.append("")

    if resume.get("projects"):
        lines.append("## Projects")
        for p in resume["projects"]:
            header = f"### {p['name']}"
            if p.get("stack"):
                header += f" — *{p['stack']}*"
            lines.append(header)
            if p.get("link"):
                # In the source PDF, "GitHub" is a hyperlink to Sourabh's
                # GitHub profile but the visible text is just "GitHub".
                # Substitute the real URL so the markdown link is actually
                # clickable when fed into LLM prompts or rendered.
                from config import CANDIDATE_GITHUB
                url = CANDIDATE_GITHUB if p["link"].lower() == "github" else p["link"]
                lines.append(f"[{p['link']}]({url})")
            for b in p.get("bullets", []):
                lines.append(f"- {b}")
            lines.append("")

    if resume.get("education"):
        lines.append("## Education")
        for e in resume["education"]:
            line = f"**{e['degree']}** — {e['institution']}"
            if e.get("status"):
                line += f" ({e['status']})"
            if e.get("cgpa"):
                line += f" · {e['cgpa']}"
            lines.append(line)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# =============================================================================
# CLI
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse the source resume PDF into resume.md + resume.json."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print parsed JSON to stdout instead of writing files.",
    )
    args = parser.parse_args()

    print(f"[resume_parser] Reading {RESUME_PDF_SOURCE} ...")
    raw_text = extract_text(RESUME_PDF_SOURCE)

    print("[resume_parser] Parsing sections ...")
    resume = parse_resume(raw_text)

    print("[resume_parser] Rendering markdown ...")
    md = to_markdown(resume)

    if args.dry_run:
        print("\n--- resume.json (dry-run) ---")
        print(json.dumps(resume, indent=2, ensure_ascii=False))
        print("\n--- resume.md (dry-run) ---")
        print(md)
        return

    RESUME_JSON.write_text(json.dumps(resume, indent=2, ensure_ascii=False), encoding="utf-8")
    RESUME_MD.write_text(md, encoding="utf-8")
    print(f"[resume_parser] Wrote {RESUME_JSON}")
    print(f"[resume_parser] Wrote {RESUME_MD}")


if __name__ == "__main__":
    main()
