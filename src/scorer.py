r"""
src/scorer.py — Job Hunt Agent · Phase 3 LLM scorer.

WHAT IT DOES
------------
Loads the latest data/raw_jobs_YYYY-MM-DD.json, sends each job + resume.md
to the configured LLM (Gemini Free or Anthropic Hybrid), gets back a
structured JobScore (0-100 match + reasons + gaps + recommended action +
confidence), sorts descending by score, and writes the top-N (≥ threshold)
to data/scored_jobs_YYYY-MM-DD.json.

RUN
---
    .\.venv\Scripts\python.exe -m src.scorer                  # standard run
    .\.venv\Scripts\python.exe -m src.scorer --dry-run        # don't write file
    .\.venv\Scripts\python.exe -m src.scorer --input <path>   # score a specific file
    .\.venv\Scripts\python.exe -m src.scorer --verbose

WHAT THIS MODULE DOES NOT DO
----------------------------
- Tailor resumes — that's Phase 4.
- Persist to SQLite — that's Phase 6.
It just produces a ranked list of scored jobs.

PROMPT DESIGN NOTES
-------------------
- Temperature = 0.0 (set in config.SCORER_TEMPERATURE) for determinism.
- System prompt loads candidate facts explicitly so the LLM never
  penalises Sourabh for "not having 5+ years".
- Skill weighting (Go/Python/AI high, Java low) is encoded in the system
  prompt so the LLM consistently downranks Java-heavy postings.
- Output schema is strict Pydantic — the LLM SDKs enforce it server-side.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

# Project root importable for `python -m src.scorer`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    DATA_DIR,
    LLM_PROVIDER,
    MIN_MATCH_SCORE,
    MODEL_SCORER,
    RESUME_MD,
    TOP_JOBS_PER_DAY,
)
from src import db                    # noqa: E402
from src.llm_client import LLMClient  # noqa: E402
from src.models import Job             # noqa: E402
from src import token_log              # noqa: E402

log = logging.getLogger("scorer")


# =============================================================================
# Output schema — what the LLM must produce per job.
# =============================================================================
class JobScore(BaseModel):
    """
    Strict scoring contract returned by the LLM for one job.

    Field names and types are SENT TO THE LLM as the response schema.
    Both Gemini (response_schema=) and Anthropic (tool input_schema=)
    enforce this server-side, so a malformed response can't reach us.
    """

    match_score: int = Field(
        ge=0, le=100,
        description=(
            "0-100 fit score. 0=irrelevant. 40=stretch but possible. "
            "60=acceptable. 80=strong match. 95+=excellent."
        ),
    )
    reasons_for_fit: list[str] = Field(
        min_length=1, max_length=3,
        description="Up to 3 specific reasons Sourabh fits THIS job. Concrete, not generic.",
    )
    gaps: list[str] = Field(
        default_factory=list, max_length=3,
        description="1-2 things the JD asks for that Sourabh doesn't clearly have. Empty list if none.",
    )
    recommended_action: Literal["Apply", "Skip", "Apply with note"] = Field(
        description=(
            "'Apply' if score ≥ 70, 'Apply with note' if 55-69 and gap is addressable, "
            "'Skip' if < 55 or fundamentally mismatched."
        ),
    )
    confidence: Literal["High", "Medium", "Low"] = Field(
        description="How confident you are in this score. Low if JD is vague or missing key info.",
    )
    score_reasoning: str = Field(
        description="One short paragraph (2-3 sentences) explaining the score.",
    )


# =============================================================================
# Scored output container — saved to data/scored_jobs_<date>.json
# =============================================================================
class ScoredJob(BaseModel):
    """One job + its LLM score. What we persist + hand to Phase 4."""
    job: Job
    score: JobScore


# =============================================================================
# Prompt templates
# =============================================================================
SYSTEM_PROMPT = """\
You are a senior technical recruiter scoring jobs for ONE specific candidate, Sourabh Jha.

CANDIDATE LOCKED FACTS (do not contradict):
- Associate Software Engineer at Accenture (Nov 2024 - Present, ~1.5 years experience).
- B.Tech CSE, SRM University Sonepat, 2024 graduate, CGPA 7.72.
- Located in Gurugram, India. Open to remote globally.
- Salary floor: 12 LPA INR / ~$30K USD post-tax equivalent for remote roles.
- Primary skills: Go (strongest), Python, backend systems, cloud (Docker, Kubernetes, Terraform, AWS/Azure/GCP), CI/CD, observability (Prometheus, Grafana, ELK).
- Secondary skills: Java (used at Accenture but not preferred), C++, TypeScript, Node.js.
- Building toward: AI / LLM / RAG / vector DBs / agentic AI / MLOps (stretch area).

SCORING RULES (apply strictly):
1. Score 0-100 based on ROLE FIT, not years of experience.
2. NEVER penalise for "5+ years experience" requirements. Sourabh is 1.5y but a strong engineer; if the role is otherwise a great match, score it high and note the years gap as a single bullet under "gaps".
3. Skill weighting:
   - HIGH WEIGHT (+15 to +25 each): Go/Golang, Python, AI/LLM/RAG/MLOps, backend engineering, distributed systems, cloud/devops, K8s, Terraform, observability.
   - MEDIUM (+5 to +10): TypeScript, Node.js, C++, security.
   - LOW (0): Java-heavy roles (he has it but doesn't prefer it).
   - NEGATIVE (-15 to -30): Roles requiring deep specialisation he lacks (PhD-ML, 5+ years specific framework, mobile native, deep frontend like Vue/React-only).
4. Bonuses (additive, max +10 total):
   - Startup (any seed/Series A/B) → +5
   - AI-first or LLM-shop company → +5
   - Remote-friendly + pays USD above $30K equivalent → +5
   - India-based with clearly published salary above 12 LPA → +5
5. Penalties (subtractive):
   - Service-based shop (TCS/Infy/Wipro/etc) with no salary signal → -20
   - Sales / marketing / non-engineering role → -50
   - QA-only role (he is dev, not QA) → -30
6. Recommended action mapping:
   - score ≥ 70 → "Apply"
   - score 55-69 AND gap is addressable in a cover-letter line → "Apply with note"
   - else → "Skip"
7. Confidence: "Low" if the JD is short/vague or missing critical info.
8. reasons_for_fit must be SPECIFIC. Bad: "good backend experience". Good: "JD requires Go microservices on K8s — Sourabh has 1.5y of Go + Docker/K8s/Terraform production work."
9. Be honest about gaps. If the company is a wrong fit, score low and say so.

OUTPUT: Return strictly the JSON schema provided. No prose outside it.
"""

USER_PROMPT_TEMPLATE = """\
RESUME (markdown):
================
{resume_md}

================

JOB TO SCORE:
Title:    {title}
Company:  {company}
Location: {location}
Salary:   {salary}
Source:   {source}

JOB DESCRIPTION:
================
{jd_text}
================

Score this job for Sourabh per the system rules. Return the JSON.
"""


def _format_salary(job: Job) -> str:
    """Pretty 'min-max CCY' or 'unstated'."""
    if job.salary_min is None and job.salary_max is None:
        return "unstated"
    parts = []
    if job.salary_min is not None:
        parts.append(f"{int(job.salary_min):,}")
    if job.salary_max is not None and job.salary_max != job.salary_min:
        parts.append(f"{int(job.salary_max):,}")
    range_str = " - ".join(parts)
    return f"{range_str} {job.salary_currency or ''}".strip()


def _build_user_prompt(job: Job, resume_md: str) -> str:
    # Truncate very long JDs to save tokens. 4000 chars ≈ ~1000 tokens.
    # Long JDs are common from Adzuna (full HTML stripped); the tail is
    # usually company boilerplate, so head-truncation is safe.
    jd_text = (job.jd_text or "")[:4000]
    return USER_PROMPT_TEMPLATE.format(
        resume_md=resume_md,
        title=job.title,
        company=job.company,
        location=job.location or "unstated",
        salary=_format_salary(job),
        source=job.source,
        jd_text=jd_text,
    )


# =============================================================================
# Pipeline
# =============================================================================
def _latest_raw_jobs_path() -> Path:
    """Newest data/raw_jobs_*.json by filename (which is date-sorted)."""
    candidates = sorted(DATA_DIR.glob("raw_jobs_*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(
            f"No raw_jobs_*.json in {DATA_DIR}. Run `python -m src.fetcher` first."
        )
    return candidates[0]


def _load_jobs(path: Path) -> list[Job]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Job.model_validate(j) for j in payload.get("jobs", [])]


def score_one_job(client: LLMClient, job: Job, resume_md: str) -> Optional[ScoredJob]:
    """
    Score a single job. Returns None on irrecoverable failure (logged) so
    the caller can skip and continue rather than abort the whole run.
    """
    try:
        score, _usage = client.complete_json(
            system=SYSTEM_PROMPT,
            user=_build_user_prompt(job, resume_md),
            schema=JobScore,
            job_id=job.job_id,
        )
        scored = ScoredJob(job=job, score=score)
        # Persist immediately — if a later job fails, we still keep this one
        # in the DB rather than only in the eventual scored_jobs.json.
        try:
            db.update_job_score(
                job.job_id,
                match_score=score.match_score,
                recommended_action=score.recommended_action,
                confidence=score.confidence,
                score_reasoning=score.score_reasoning,
                reasons_for_fit=score.reasons_for_fit,
                gaps=score.gaps,
            )
        except Exception as db_exc:  # noqa: BLE001 — log but don't lose the score
            log.warning("scorer: db persist failed for %s: %s", job.job_id, db_exc)
        return scored
    except Exception as exc:  # noqa: BLE001 — one bad job shouldn't kill the run
        log.error("scorer: failed on %s (%s @ %s): %s",
                  job.job_id, job.title, job.company, exc)
        return None


def run(
    input_path: Path | None = None,
    dry_run: bool = False,
) -> tuple[list[ScoredJob], list[ScoredJob]]:
    """
    Score all jobs in `input_path` (defaults to today's raw_jobs file).
    Returns (all_scored, top_n_selected).
    """
    in_path = input_path or _latest_raw_jobs_path()
    log.info("====== Scorer run starting — input=%s ======", in_path.name)

    jobs = _load_jobs(in_path)
    log.info("Loaded %d jobs to score", len(jobs))

    if not RESUME_MD.exists():
        raise FileNotFoundError(
            f"Resume not found at {RESUME_MD}. Run `python -m src.resume_parser` first."
        )
    resume_md = RESUME_MD.read_text(encoding="utf-8")

    log.info("Using LLM provider=%s model=%s", LLM_PROVIDER, MODEL_SCORER)
    client = LLMClient(phase="scorer", model=MODEL_SCORER)

    started = time.monotonic()
    scored: list[ScoredJob] = []
    for i, job in enumerate(jobs, 1):
        result = score_one_job(client, job, resume_md)
        if result is None:
            continue
        scored.append(result)
        log.info(
            "scored %d/%d: %d/100 [%s] %s @ %s",
            i, len(jobs),
            result.score.match_score,
            result.score.recommended_action,
            (result.job.title[:48] + "…") if len(result.job.title) > 48 else result.job.title,
            (result.job.company[:30] + "…") if len(result.job.company) > 30 else result.job.company,
        )

    scored.sort(key=lambda s: s.score.match_score, reverse=True)

    # Select top N at or above the threshold.
    eligible = [s for s in scored if s.score.match_score >= MIN_MATCH_SCORE]
    top_n = eligible[:TOP_JOBS_PER_DAY]

    if len(eligible) < TOP_JOBS_PER_DAY:
        log.warning(
            "Only %d jobs scored ≥ %d (wanted %d). Lower MIN_MATCH_SCORE in .env "
            "or accept a smaller top list today.",
            len(eligible), MIN_MATCH_SCORE, TOP_JOBS_PER_DAY,
        )

    duration = time.monotonic() - started
    log.info(
        "Scored %d jobs in %.1fs; %d ≥ %d threshold; top-%d selected",
        len(scored), duration, len(eligible), MIN_MATCH_SCORE, len(top_n),
    )

    if not dry_run:
        _write_output(scored, top_n, in_path)
        # Update today's daily_runs row with scorer counts.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.upsert_daily_run(
            today,
            jobs_scored=len(scored),
            top_jobs_count=len(top_n),
            token_usage=token_log.todays_usage(),
        )

    _print_summary_table(scored, top_n)
    return scored, top_n


# =============================================================================
# Output — atomic write to data/scored_jobs_YYYY-MM-DD.json
# =============================================================================
def _write_output(all_scored: list[ScoredJob], top_n: list[ScoredJob], input_path: Path) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"scored_jobs_{today}.json"
    tmp_path = out_path.with_suffix(".json.tmp")

    today_usage = token_log.todays_usage()

    payload = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_date": today,
        "input_file": input_path.name,
        "provider": LLM_PROVIDER,
        "model": MODEL_SCORER,
        "min_match_score": MIN_MATCH_SCORE,
        "scored_count": len(all_scored),
        "top_n_count": len(top_n),
        "token_usage_today": today_usage,
        "top_n_job_ids": [s.job.job_id for s in top_n],
        # Sort: top_n first (already sorted), then the rest by score desc.
        "all_scored": [s.model_dump(mode="json") for s in all_scored],
        "top_n": [s.model_dump(mode="json") for s in top_n],
    }
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(out_path)
    log.info("wrote %s (%d bytes)", out_path, out_path.stat().st_size)


def _print_summary_table(all_scored: list[ScoredJob], top_n: list[ScoredJob]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for s in top_n:
            print(f"  [{s.score.match_score:3}] {s.score.recommended_action:18}"
                  f"  {s.job.title[:50]:50}  @ {s.job.company[:25]}")
        return

    console = Console()
    usage = token_log.todays_usage()

    console.print(
        f"\n[bold]Scored:[/bold] {len(all_scored)}  "
        f"[bold]Top-N (≥{MIN_MATCH_SCORE}):[/bold] {len(top_n)}  "
        f"[bold]Tokens today:[/bold] {usage['input_tokens'] + usage['output_tokens']:,}  "
        f"[bold]Cost:[/bold] ${usage['cost_usd']:.4f}",
    )

    table = Table(
        title=f"Top {len(top_n)} scored jobs",
        show_header=True, header_style="bold",
    )
    table.add_column("Score", justify="right", style="bold")
    table.add_column("Action")
    table.add_column("Conf")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Source", style="dim")
    for s in top_n:
        score_str = f"[green]{s.score.match_score}[/green]" if s.score.match_score >= 80 \
                    else f"[yellow]{s.score.match_score}[/yellow]" if s.score.match_score >= 60 \
                    else f"[red]{s.score.match_score}[/red]"
        table.add_row(
            score_str,
            s.score.recommended_action,
            s.score.confidence,
            (s.job.title[:55] + "…") if len(s.job.title) > 55 else s.job.title,
            (s.job.company[:25] + "…") if len(s.job.company) > 25 else s.job.company,
            s.job.source,
        )
    console.print(table)


# =============================================================================
# CLI
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Score today's fetched jobs with the LLM.")
    parser.add_argument("--input", type=Path, help="Path to a raw_jobs_*.json. Defaults to newest.")
    parser.add_argument("--dry-run", action="store_true", help="Don't write scored_jobs file.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)

    run(input_path=args.input, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
