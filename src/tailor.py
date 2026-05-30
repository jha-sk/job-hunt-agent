r"""
src/tailor.py — Job Hunt Agent · Phase 4 resume tailor.

WHAT IT DOES
------------
Reads today's `data/scored_jobs_YYYY-MM-DD.json`, takes the top-N (≥
MIN_MATCH_SCORE) jobs, and for each one asks the LLM to tailor the resume
to that specific JD. Each tailored resume is rendered to markdown and
saved to `resumes/tailored/resume_<co>_<role>_<date>.md` along with a
`.changes.md` file summarising what was changed and any honest gaps.

LOCKED-SECTION SAFETY
---------------------
The LLM is NEVER given the chance to mutate locked fields (company names,
employment dates, education, certifications, project NAMES). It only emits
the EDITABLE fields — summary, skills ordering, experience bullets, project
descriptions/bullets — as a strict Pydantic schema. The renderer then
plugs those edits into a markdown template that hard-codes the locked
parts from resume.json. Failure mode: even if the LLM hallucinates a fake
job title or date, it can't reach the file.

HOW TO RUN
----------
    .\.venv\Scripts\python.exe -m src.tailor               # tailor all top-N
    .\.venv\Scripts\python.exe -m src.tailor --top-only    # just the #1 job (testing)
    .\.venv\Scripts\python.exe -m src.tailor --dry-run     # build prompt, don't call LLM
    .\.venv\Scripts\python.exe -m src.tailor --input <file>
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    CANDIDATE_EMAIL,
    CANDIDATE_GITHUB,
    CANDIDATE_LINKEDIN,
    CANDIDATE_LOCATION,
    CANDIDATE_NAME,
    CANDIDATE_PHONE,
    DATA_DIR,
    LLM_PROVIDER,
    MODEL_TAILOR,
    RESUME_JSON,
    RESUMES_TAILORED_DIR,
)
from src import db                    # noqa: E402
from src.llm_client import DailyQuotaExhausted, LLMClient  # noqa: E402

log = logging.getLogger("tailor")


# =============================================================================
# LLM output schema — these are the ONLY fields the LLM may edit.
# Everything else (company, dates, education, project names) comes from
# resume.json verbatim. The renderer enforces this — see render_markdown().
#
# Schema-shape note: Gemini's Developer API rejects schemas that use
# `additionalProperties` (e.g. an arbitrary-key dict). Pydantic emits that
# for `dict[str, ...]`. So instead of `dict[str, list[str]]` for skills,
# we use a list of fixed-shape records with a Literal-typed category.
# =============================================================================
SkillCategoryName = Literal[
    "languages",
    "backend_apis",
    "cloud_devops",
    "databases",
    "tools",
    "security",
    "ai_augmented_engineering",
    "practices",
]


class SkillCategoryEdit(BaseModel):
    """One skill category in the tailored order. Items are reorderable
    and droppable (omit JD-irrelevant ones) but never inventable."""
    category: SkillCategoryName
    items: list[str] = Field(
        min_length=1,
        description="Skills in this category, ordered most-JD-relevant first.",
    )


class ProjectEdit(BaseModel):
    """
    Edits to ONE project. The 'name' field is a lookup key — the LLM
    must echo the project name verbatim from the input. The renderer
    rejects any project name not present in resume.json (LLM can't
    invent fake projects).
    """
    name: str = Field(
        description="EXACT project name from input. Echo verbatim. Used as a lookup key.",
    )
    new_stack: str = Field(
        description=(
            "Refreshed stack line, JD-relevant tech listed first. "
            "Comma-separated. Keep all tech that's actually in the original."
        ),
    )
    new_bullets: list[str] = Field(
        min_length=2, max_length=5,
        description="Reordered/reframed bullets. Lead with JD-relevant impact. Never invent metrics or features.",
    )


class TailoredResume(BaseModel):
    """
    The LLM's edits to Sourabh's resume for ONE specific job. The renderer
    will plug these into a markdown template that hard-codes locked fields
    from resume.json (name, contact, education, certifications, company
    names, dates, project names).
    """
    professional_summary: str = Field(
        min_length=60, max_length=800,
        description=(
            "2-4 sentence summary (target ~400 chars, hard max 800) tailored "
            "to THIS job. State Sourabh's strongest relevant background, then "
            "the value he'd bring. No first-person ('I'). No fabrications."
        ),
    )
    skills_in_order: list[SkillCategoryEdit] = Field(
        min_length=3,
        description=(
            "Skill categories in tailored ORDER (most JD-relevant first). "
            "You may drop irrelevant categories and items entirely; never "
            "invent skills Sourabh doesn't have. Allowed category names: "
            "languages, backend_apis, cloud_devops, databases, tools, "
            "security, ai_augmented_engineering, practices."
        ),
    )
    experience_bullets_in_order: list[str] = Field(
        min_length=4, max_length=8,
        description=(
            "Sourabh's Accenture experience bullets, REORDERED so the most "
            "JD-relevant lead. You MAY lightly reword for keyword alignment "
            "(e.g. add 'Go' or 'Python' if the original implied it). NEVER "
            "invent achievements or fabricate metrics. Keep the same FACTS."
        ),
    )
    projects: list[ProjectEdit] = Field(
        min_length=1, max_length=5,
        description=(
            "Per-project edits. The 'name' must match a project from the input. "
            "Reorder projects too — put the most JD-relevant FIRST in this list."
        ),
    )
    changes_summary: list[str] = Field(
        min_length=3, max_length=6,
        description="Plain-English bullets explaining what you changed and why.",
    )
    honest_gaps: list[str] = Field(
        default_factory=list, max_length=4,
        description=(
            "Things the JD asks for that Sourabh doesn't clearly have. "
            "Be honest. Empty list if none."
        ),
    )


# =============================================================================
# Prompt
# =============================================================================
SYSTEM_PROMPT = """\
You are an expert resume editor preparing Sourabh Jha's resume for ONE specific job.

YOUR JOB
- Reframe and reorder Sourabh's REAL experience so the most JD-relevant work leads.
- Rewrite the professional summary to match THIS job.
- Reorder the skills section to surface the JD's emphasised tech first.
- Reorder/reword the Accenture experience bullets so the most relevant lead.
- Reorder projects so JD-relevant projects are FIRST. Reframe each project's
  stack and bullets to highlight the relevant pieces.

HARD RULES — ANY VIOLATION IS A FAIL
1. NEVER FABRICATE. Don't invent achievements, metrics, tech he didn't use, or experience he doesn't have. If the JD asks for X and he doesn't have X, list X under "honest_gaps".
2. NEVER CHANGE THESE (the renderer will reject your output if you try):
   - His name, contact details
   - Accenture company name, role title, dates
   - Project NAMES (you may rewrite stack/bullets, but the name is locked)
   - Education (degree, institution, CGPA)
   - Certifications (verbatim list)
3. Skills you may REORDER and DROP irrelevant items. You may NOT add skills he doesn't have.
4. Light rewording is fine. Wholesale invention is not. Test: would Sourabh's current manager recognise every bullet as something he actually did at Accenture?
5. Keyword stuffing is detectable and gets rejected by recruiters. Use JD keywords naturally where they fit.

SOURABH'S LOCKED PROFILE
- Associate Software Engineer @ Accenture, Nov 2024 - Present, ~1.5 years experience
- B.Tech CSE, SRM University Sonepat, 2024, CGPA 7.72
- Primary skills: Go (strongest), Python, backend engineering, cloud (Docker, K8s, Terraform, AWS/Azure/GCP), CI/CD, observability
- Secondary: Java (used at Accenture but not preferred), C++, TypeScript, Node.js
- Stretch area: AI/LLM/RAG/MLOps — building this
- Projects (names LOCKED): Ash OS, Website Nativefier, Git Automation CLI
- Located Gurugram, India. Salary floor 12 LPA INR / ~$30K USD remote.

OUTPUT: Return strictly the JSON schema provided. No prose outside it.
"""

USER_PROMPT_TEMPLATE = """\
ORIGINAL RESUME (structured JSON — your reference for what's REAL):
{resume_json}

ORIGINAL RESUME (rendered markdown — for context):
{resume_md}

================
JOB TO TAILOR FOR:
Title:    {title}
Company:  {company}
Location: {location}
Salary:   {salary}
Source:   {source}

JOB DESCRIPTION:
{jd_text}
================

WHY THIS JOB SCORED HIGH (use this to inform tailoring):
Score: {score}/100
Action: {recommended_action}
Top fits:
{fits}
Gaps to address (or flag honestly):
{gaps}

Tailor the resume for THIS job. Return JSON per schema.
"""


def _format_salary(j: dict) -> str:
    if not (j.get("salary_min") or j.get("salary_max")):
        return "unstated"
    parts = []
    if j.get("salary_min"):
        parts.append(f"{int(j['salary_min']):,}")
    if j.get("salary_max") and j["salary_max"] != j.get("salary_min"):
        parts.append(f"{int(j['salary_max']):,}")
    return f"{' - '.join(parts)} {j.get('salary_currency') or ''}".strip()


def _build_user_prompt(
    resume_json: dict,
    resume_md: str,
    scored: dict,
) -> str:
    job, score = scored["job"], scored["score"]
    return USER_PROMPT_TEMPLATE.format(
        resume_json=json.dumps(resume_json, indent=2, ensure_ascii=False),
        resume_md=resume_md,
        title=job["title"],
        company=job["company"],
        location=job.get("location") or "unstated",
        salary=_format_salary(job),
        source=job["source"],
        jd_text=(job.get("jd_text") or "")[:5000],   # cap for token budget
        score=score["match_score"],
        recommended_action=score["recommended_action"],
        fits="\n".join(f"  - {f}" for f in score.get("reasons_for_fit", [])),
        gaps="\n".join(f"  - {g}" for g in score.get("gaps", [])) or "  (none)",
    )


# =============================================================================
# Renderer — produces final markdown from LLM edits + LOCKED resume.json data.
# This is the safety boundary. Locked fields come ONLY from resume_json,
# never from the LLM response.
# =============================================================================
def _humanize_category(key: str) -> str:
    """Same logic as src/resume_parser.py — kept in sync to avoid divergence."""
    fixes = {"Apis": "APIs", "Ci": "CI", "Cd": "CD", "Devops": "DevOps",
             "Ai": "AI", "Llm": "LLM", "Gcp": "GCP", "Aws": "AWS"}
    two_word_amp = {"backend_apis", "cloud_devops"}
    parts = key.split("_")
    titled = [fixes.get(p.title(), p.title()) for p in parts]
    if key == "ai_augmented_engineering":
        return f"{titled[0]}-{titled[1]} {titled[2]}"
    sep = " & " if key in two_word_amp else " "
    return sep.join(titled) if len(titled) <= 2 else " ".join(titled)


def _filter_skills_against_original(
    edits_skills: list[SkillCategoryEdit],
    original_skills: dict[str, list[str]],
) -> tuple[list[SkillCategoryEdit], list[str]]:
    """
    Safety net: drop any skill the LLM added that isn't anywhere in the
    original resume.json. Re-categorisation across the original categories
    is fine; outright invention is not.

    Returns (filtered_skill_categories, items_dropped_for_log).
    """
    # Build a case-insensitive set of EVERY skill from resume.json,
    # regardless of original category. This way Node.js moving from
    # backend_apis → languages stays allowed; ClickHouse appearing where
    # it never existed gets stripped.
    original_set: set[str] = set()
    for items in original_skills.values():
        for item in items:
            original_set.add(item.strip().lower())

    filtered: list[SkillCategoryEdit] = []
    dropped: list[str] = []
    for cat in edits_skills:
        kept: list[str] = []
        for item in cat.items:
            if item.strip().lower() in original_set:
                kept.append(item)
            else:
                dropped.append(f"{cat.category}/{item}")
        if kept:
            filtered.append(SkillCategoryEdit(category=cat.category, items=kept))
    return filtered, dropped


def render_markdown(resume_json: dict, edits: TailoredResume) -> str:
    """
    Build the final tailored .md by combining LLM edits with LOCKED data
    pulled directly from resume.json. The LLM cannot influence anything
    that's read here from resume_json (header, education, certifications,
    company names, dates, project names).
    """
    lines: list[str] = []

    # --- Header (LOCKED from config — same as original) ---
    lines.append(f"# {CANDIDATE_NAME}")
    lines.append(
        f"{CANDIDATE_PHONE} · {CANDIDATE_EMAIL} · "
        f"[LinkedIn]({CANDIDATE_LINKEDIN}) · [GitHub]({CANDIDATE_GITHUB}) · {CANDIDATE_LOCATION}"
    )
    lines.append("")

    # --- Professional Summary (LLM, validated for length) ---
    lines.append("## Summary")
    lines.append(edits.professional_summary.strip())
    lines.append("")

    # --- Technical Skills (LLM ORDER, fabrications filtered out) ---
    # The filter is the safety boundary against the LLM inventing skills
    # Sourabh doesn't have. Anything dropped is logged for review and
    # surfaced in the .changes.md file.
    filtered_skills, dropped = _filter_skills_against_original(
        edits.skills_in_order,
        resume_json.get("skills", {}),
    )
    if dropped:
        log.warning(
            "tailor: dropped %d fabricated skill(s) from LLM output: %s",
            len(dropped), dropped,
        )
        # Stash on the edits object so render_changes can surface it to
        # Sourabh in the .changes.md file. We mutate; this object is
        # short-lived (one job tailoring pass).
        edits.honest_gaps = list(edits.honest_gaps) + [
            f"LLM tried to add skill(s) not on your resume: {', '.join(dropped)} "
            f"— removed automatically. Consider whether to add any of these "
            f"truthfully to your master resume.json."
        ]
    lines.append("## Technical Skills")
    for cat_edit in filtered_skills:
        pretty = _humanize_category(cat_edit.category)
        items_str = ", ".join(cat_edit.items)
        lines.append(f"- **{pretty}**: {items_str}")
    lines.append("")

    # --- Certifications (LOCKED from resume.json) ---
    lines.append("## Certifications")
    for cert in resume_json.get("certifications", []):
        lines.append(f"- {cert}")
    lines.append("")

    # --- Experience (company/title/dates LOCKED; bullets from LLM) ---
    lines.append("## Experience")
    for exp in resume_json.get("experience", []):
        lines.append(f"### {exp['title']} — {exp['company']}")
        if exp.get("dates"):
            lines.append(f"*{exp['dates']}*")
        # Use LLM's reordered bullets. Only one experience entry on
        # Sourabh's current resume, so attach all of LLM's bullets here.
        for bullet in edits.experience_bullets_in_order:
            lines.append(f"- {bullet}")
        lines.append("")

    # --- Projects (NAMES locked from JSON; stack + bullets from LLM,
    #     in the order the LLM gave them) ---
    lines.append("## Projects")
    projects_by_name = {p["name"]: p for p in resume_json.get("projects", [])}
    for proj_edit in edits.projects:
        orig = projects_by_name.get(proj_edit.name)
        if orig is None:
            # LLM hallucinated a project name — skip with a warning
            # rather than letting fake project text reach the file.
            log.warning(
                "tailor: LLM emitted unknown project name %r — skipping. "
                "Known projects: %s",
                proj_edit.name, list(projects_by_name.keys()),
            )
            continue
        header = f"### {orig['name']}"  # NAME from resume_json, NOT LLM
        if proj_edit.new_stack:
            header += f" — *{proj_edit.new_stack}*"
        lines.append(header)
        if orig.get("link"):
            url = CANDIDATE_GITHUB if str(orig["link"]).lower() == "github" else orig["link"]
            lines.append(f"[{orig['link']}]({url})")
        for bullet in proj_edit.new_bullets:
            lines.append(f"- {bullet}")
        lines.append("")

    # --- Education (LOCKED from resume.json) ---
    lines.append("## Education")
    for edu in resume_json.get("education", []):
        line = f"**{edu['degree']}** — {edu['institution']}"
        if edu.get("status"):
            line += f" ({edu['status']})"
        if edu.get("cgpa"):
            line += f" · {edu['cgpa']}"
        lines.append(line)
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_changes(scored: dict, edits: TailoredResume) -> str:
    """The reviewer-friendly summary: what we changed, plus honest gaps."""
    job = scored["job"]
    lines: list[str] = []
    lines.append(f"# Tailoring notes — {job['title']} @ {job['company']}")
    lines.append("")
    lines.append(f"**Apply URL:** {job['apply_url']}")
    lines.append(f"**Match score:** {scored['score']['match_score']}/100 "
                 f"({scored['score']['recommended_action']}, "
                 f"{scored['score']['confidence']} confidence)")
    lines.append("")
    lines.append("## What changed and why")
    for c in edits.changes_summary:
        lines.append(f"- {c}")
    lines.append("")
    if edits.honest_gaps:
        lines.append("## Honest gaps to acknowledge / address")
        for g in edits.honest_gaps:
            lines.append(f"- {g}")
        lines.append("")
    lines.append("## Top reasons you fit (from scorer)")
    for f in scored["score"].get("reasons_for_fit", []):
        lines.append(f"- {f}")
    return "\n".join(lines).rstrip() + "\n"


# =============================================================================
# Filename sanitization — master prompt: resume_[company]_[role]_[date].md
# =============================================================================
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9]+")


def _slugify(text: str, max_len: int = 40) -> str:
    """company / role → safe, short slug for filenames."""
    s = _SAFE_CHARS.sub("-", text or "").strip("-").lower()
    return (s[:max_len].rstrip("-")) or "unknown"


def tailored_paths(job: dict, date: str) -> tuple[Path, Path]:
    """Return (resume_md_path, changes_md_path) for a given job + date."""
    co = _slugify(job["company"], 30)
    role = _slugify(job["title"], 40)
    base = f"resume_{co}_{role}_{date}"
    md_path = RESUMES_TAILORED_DIR / f"{base}.md"
    changes_path = RESUMES_TAILORED_DIR / f"{base}.changes.md"
    return md_path, changes_path


# =============================================================================
# Pipeline
# =============================================================================
def _latest_scored_jobs_path() -> Path:
    candidates = sorted(DATA_DIR.glob("scored_jobs_*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(
            f"No scored_jobs_*.json in {DATA_DIR}. Run `python -m src.scorer` first."
        )
    return candidates[0]


def tailor_one(
    client: LLMClient,
    resume_json: dict,
    resume_md: str,
    scored: dict,
) -> tuple[TailoredResume | None, str | None]:
    """
    Tailor ONE job. Returns (edits, error_msg). On success error_msg is None.
    Raises DailyQuotaExhausted so the caller stops the per-job loop.
    """
    try:
        edits, _usage = client.complete_json(
            system=SYSTEM_PROMPT,
            user=_build_user_prompt(resume_json, resume_md, scored),
            schema=TailoredResume,
            job_id=scored["job"]["job_id"],
            max_output_tokens=2048,    # tailor output is bigger than scorer
        )
        return edits, None
    except DailyQuotaExhausted:
        raise
    except Exception as exc:  # noqa: BLE001 — never let one bad tailoring kill the batch
        return None, str(exc)


def run(
    input_path: Path | None = None,
    top_only: bool = False,
    dry_run: bool = False,
) -> list[Path]:
    """
    Tailor today's top-N (or just #1 if top_only). Returns list of written files.
    """
    in_path = input_path or _latest_scored_jobs_path()
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    top_n_records = payload.get("top_n") or []
    if not top_n_records:
        log.warning("tailor: no jobs in top_n of %s — nothing to do", in_path.name)
        return []

    if top_only:
        top_n_records = top_n_records[:1]

    log.info("====== Tailor run — %d job(s) — input=%s ======",
             len(top_n_records), in_path.name)

    resume_json = json.loads(RESUME_JSON.read_text(encoding="utf-8"))
    from config import RESUME_MD
    resume_md = RESUME_MD.read_text(encoding="utf-8")

    if dry_run:
        log.info("--dry-run: skipping LLM calls. Would tailor:")
        for s in top_n_records:
            log.info("  - %s @ %s (score=%d)",
                     s["job"]["title"], s["job"]["company"], s["score"]["match_score"])
        return []

    log.info("Using provider=%s model=%s", LLM_PROVIDER, MODEL_TAILOR)
    client = LLMClient(phase="tailor", model=MODEL_TAILOR)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    written: list[Path] = []
    failures: list[tuple[str, str]] = []

    quota_hit = False
    for i, scored in enumerate(top_n_records, 1):
        job = scored["job"]
        log.info("tailoring %d/%d: %s @ %s",
                 i, len(top_n_records), job["title"], job["company"])

        started = time.monotonic()
        try:
            edits, err = tailor_one(client, resume_json, resume_md, scored)
        except DailyQuotaExhausted:
            log.warning(
                "tailor: daily quota exhausted after %d/%d resumes. Stopping early. "
                "Already-tailored resumes are saved; tomorrow's run resumes the rest.",
                i - 1, len(top_n_records),
            )
            quota_hit = True
            break
        elapsed = time.monotonic() - started

        if err:
            log.error("tailor: FAILED %s @ %s (%.1fs): %s",
                      job["title"], job["company"], elapsed, err)
            failures.append((f"{job['title']} @ {job['company']}", err))
            continue

        md = render_markdown(resume_json, edits)
        changes = render_changes(scored, edits)

        md_path, changes_path = tailored_paths(job, today)
        md_path.write_text(md, encoding="utf-8")
        changes_path.write_text(changes, encoding="utf-8")
        written.extend([md_path, changes_path])
        # Record the tailored-resume path against the job row so Phase 10
        # (digest) can find it without re-globbing the filesystem.
        try:
            db.set_resume_paths(job["job_id"], resume_md_path=str(md_path))
        except Exception as db_exc:  # noqa: BLE001 — log but don't lose the file
            log.warning("tailor: db persist failed for %s: %s", job["job_id"], db_exc)

        # Phase 9: log every honest_gap to the memory table so the
        # memory engine can compute "most common gaps across all tailored
        # resumes" without re-parsing .changes.md files.
        try:
            from datetime import datetime, timezone
            for gap in (edits.honest_gaps or []):
                gap_key = f"{job['job_id']}-{abs(hash(gap)) % 10_000_000}"
                db.memory_set(
                    "tailor_gap", gap_key,
                    {
                        "gap":     gap,
                        "job_id":  job["job_id"],
                        "company": job["company"],
                        "title":   job["title"],
                        "logged_at": datetime.now(timezone.utc).isoformat(),
                    },
                    source="tailor",
                )
        except Exception as mem_exc:  # noqa: BLE001
            log.warning("tailor: memory.gap log failed for %s: %s",
                        job["job_id"], mem_exc)
        log.info(
            "tailored %d/%d in %.1fs -> %s (and .changes.md)",
            i, len(top_n_records), elapsed, md_path.name,
        )

    log.info("====== Tailor run complete: %d resumes written, %d failed ======",
             len(written) // 2, len(failures))

    # Update today's daily_runs row with the count of resumes tailored.
    # `written` contains BOTH the .md and .changes.md per job, so divide by 2.
    try:
        db.upsert_daily_run(
            today,
            resumes_tailored=len(written) // 2,
            errors=(
                f"tailor: daily Gemini quota exhausted after "
                f"{len(written) // 2}/{len(top_n_records)} resumes"
            ) if quota_hit else None,
        )
    except Exception as db_exc:  # noqa: BLE001
        log.warning("tailor: db daily_runs update failed: %s", db_exc)

    _print_summary_table(written, failures)
    return written


def _print_summary_table(written: list[Path], failures: list[tuple[str, str]]) -> None:
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
    table = Table(title="Tailored resumes", show_header=True, header_style="bold")
    table.add_column("File", style="white")
    table.add_column("Size (KB)", justify="right")
    for p in written:
        if p.exists():
            table.add_row(p.name, f"{p.stat().st_size / 1024:.1f}")
    console.print(table)

    if failures:
        ftable = Table(title="Failures", show_header=True, header_style="bold red")
        ftable.add_column("Job")
        ftable.add_column("Error")
        for name, err in failures:
            ftable.add_row(name, err[:80])
        console.print(ftable)


# =============================================================================
# CLI
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Tailor today's top-N scored jobs.")
    parser.add_argument("--input", type=Path, help="Path to scored_jobs_*.json. Defaults to newest.")
    parser.add_argument("--top-only", action="store_true",
                        help="Only tailor the single highest-scored job (testing).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan but don't call the LLM.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "requests", "httpx", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    run(input_path=args.input, top_only=args.top_only, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
