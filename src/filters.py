"""
src/filters.py — Pre-scorer filters.

Why a pre-filter exists at all
------------------------------
Phase 3 (scorer) sends each job to the LLM. LLM calls cost time + money.
Anything we can KNOW is irrelevant before scoring saves us tokens. The
filters here are deliberately CONSERVATIVE — we prefer to let a marginal
job through (and let the scorer judge it) rather than risk dropping a
hidden gem. Filtering removes ~70-80% of the raw fetched jobs but should
have near-zero false-negatives.

Filter order (cheapest first → most expensive last)
---------------------------------------------------
1. age              — drop if posted_at older than MAX_JOB_AGE_DAYS
2. excluded_type    — drop interns/contracts/freelance/part-time
3. location         — drop if not Remote/India/Gurugram/Worldwide
4. keyword          — drop if no relevant keyword in title or description
5. experience       — drop if JD explicitly demands more than MAX_REQUIRED_YEARS_EXP
6. service_based    — drop if company in SERVICE_BASED_COMPANIES AND salary < ₹12 LPA

Each filter logs its drop count to the orchestrator's run summary so you
can see WHY today's pool shrank from 500 → 47 jobs.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from config import (
    ACCEPTED_LOCATIONS,
    EXCLUDED_JOB_TYPES,
    JOB_KEYWORDS,
    MAX_JOB_AGE_DAYS,
    MAX_REQUIRED_YEARS_EXP,
    MIN_SALARY_INR_LPA,
    SERVICE_BASED_COMPANIES,
)
from src.models import Job

log = logging.getLogger(__name__)


# =============================================================================
# Individual filter predicates — each returns True if the job should be KEPT.
# =============================================================================

def passes_age(job: Job) -> bool:
    """Within the last MAX_JOB_AGE_DAYS days?"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_JOB_AGE_DAYS)
    posted = job.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted >= cutoff


def passes_excluded_type(job: Job) -> bool:
    """
    Drop if the title or employment_type mentions an excluded job type
    (intern, contract, freelance, part-time). We check both because
    different sources put this signal in different fields.
    """
    haystack = (job.title + " " + (job.employment_type or "")).lower()
    for needle in EXCLUDED_JOB_TYPES:
        # Word-boundary match so "internal" doesn't trip "intern".
        if re.search(rf"\b{re.escape(needle.lower())}\b", haystack):
            return False
    return True


def passes_location(job: Job) -> bool:
    """
    Keep if the job is Remote, India, Gurugram, or worldwide. Done by
    case-insensitive substring match against ACCEPTED_LOCATIONS.

    Also keep when location is empty — some sources (e.g. RemoteOK) leave
    it blank for fully-remote postings. Erring permissive here is fine;
    the scorer will judge fit.
    """
    if not job.location:
        return True
    location_lower = job.location.lower()
    return any(loc.lower() in location_lower for loc in ACCEPTED_LOCATIONS)


# Pre-compile keyword regexes once at import time — saves CPU when filtering
# hundreds of jobs per run.
_KEYWORD_RES = [
    re.compile(rf"\b{re.escape(kw.lower())}\b") for kw in JOB_KEYWORDS
]


def passes_keyword(job: Job) -> bool:
    """
    At least one JOB_KEYWORD must appear in the title OR description.
    Word-boundary match (so 'Go' doesn't match 'Google' — important).
    """
    haystack = (job.title + " " + job.jd_text).lower()
    return any(pattern.search(haystack) for pattern in _KEYWORD_RES)


# Detect "X+ years experience", "5-7 years experience", "Minimum 5 years
# of relevant experience" etc. Conservative — must mention 'experience'
# or 'exp' within 4 words of the number to count, so unrelated mentions
# of years don't false-positive.
_YEARS_EXP_RE = re.compile(
    r"\b(\d+)\s*\+?\s*(?:[-–to]+\s*\d+\s*)?years?"
    r"\s+(?:of\s+)?(?:relevant\s+|professional\s+|hands[- ]on\s+|work\s+)?"
    r"(?:experience|exp)\b",
    re.IGNORECASE,
)


def passes_experience(job: Job) -> bool:
    """
    Drop if the JD explicitly demands more years than MAX_REQUIRED_YEARS_EXP.
    The first numeric match wins (it's typically the floor — '5+', '5-7').

    False-negative bias: when the regex doesn't match, KEEP the job. Most
    JDs don't have an explicit experience floor, and we'd rather let the
    scorer reject them than blanket-drop here.
    """
    match = _YEARS_EXP_RE.search(job.jd_text)
    if not match:
        return True
    try:
        years = int(match.group(1))
    except ValueError:
        return True
    return years <= MAX_REQUIRED_YEARS_EXP


def passes_service_based_filter(job: Job) -> bool:
    """
    For companies in SERVICE_BASED_COMPANIES (TCS, Infy, Accenture etc):
    keep ONLY if the JD's stated salary clears MIN_SALARY_INR_LPA.

    If the salary is unstated, treat it as below the floor (these shops
    routinely pay below 12 LPA for ASE-level roles). If the salary is in
    a non-INR currency (e.g. USD on a remote posting), keep the job —
    overseas-paying gigs at "service-based" company names are usually
    different business units worth letting through.
    """
    company_lower = (job.company or "").lower()
    is_service_based = any(s in company_lower for s in SERVICE_BASED_COMPANIES)
    if not is_service_based:
        return True

    # Service-based + no salary listed → drop.
    if job.salary_min is None and job.salary_max is None:
        return False

    # Service-based + non-INR salary → keep (probably a remote/overseas role).
    if job.salary_currency and job.salary_currency.upper() != "INR":
        return True

    # Service-based + INR salary → check floor. salary_min is the floor.
    # Convert "annual rupees" to LPA: divide by 100,000.
    salary_floor_lpa = (job.salary_min or 0) / 100_000
    return salary_floor_lpa >= MIN_SALARY_INR_LPA


# =============================================================================
# Pipeline — apply filters in order and emit a drop-count summary.
# =============================================================================

# Each tuple = (filter_name, predicate). Order matters: cheapest first,
# most expensive (regex-heavy) last.
_FILTER_CHAIN: list[tuple[str, Callable[[Job], bool]]] = [
    ("age",            passes_age),
    ("excluded_type",  passes_excluded_type),
    ("location",       passes_location),
    ("keyword",        passes_keyword),
    ("experience",     passes_experience),
    ("service_based",  passes_service_based_filter),
]


def apply_filters(jobs: list[Job]) -> tuple[list[Job], dict[str, int]]:
    """
    Run the filter chain. Returns (kept_jobs, drop_counts_by_filter).

    drop_counts_by_filter tracks the FIRST filter to reject each job —
    useful for diagnostics ("today we lost 312 jobs to keyword filter
    and 47 to age").
    """
    kept: list[Job] = []
    drops: dict[str, int] = {name: 0 for name, _ in _FILTER_CHAIN}

    for job in jobs:
        dropped_by: str | None = None
        for name, predicate in _FILTER_CHAIN:
            if not predicate(job):
                dropped_by = name
                break
        if dropped_by is None:
            kept.append(job)
        else:
            drops[dropped_by] += 1

    log.info(
        "filters: input=%d kept=%d drops=%s",
        len(jobs), len(kept), {k: v for k, v in drops.items() if v},
    )
    return kept, drops
