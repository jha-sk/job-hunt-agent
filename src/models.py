"""
src/models.py — Pydantic data models shared across the pipeline.

The Job model is the spine of the system. Every source fetcher must
return list[Job], and every downstream stage (filter, dedupe, scorer,
tailor, db) accepts and emits Job objects. Adding a field here ripples
through everything, so think before extending.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


# Source names we currently support. Add to this Literal when you add a
# new fetcher under src/sources/, so type-checkers catch typos.
SourceName = Literal["remoteok", "himalayas", "adzuna", "jsearch"]


class Job(BaseModel):
    """
    A single normalized job posting. All source-specific quirks (different
    field names, salary currency formats, date formats) are flattened into
    this shape by the source fetchers before they hand jobs upstream.

    Required fields are exactly the ones the master prompt's Phase 2 spec
    lists: title, company, location, jd_text, apply_url, source, posted_at,
    job_id. Everything else is optional metadata that improves scoring,
    tailoring, and the daily digest later.
    """

    # --- Required (master prompt Phase 2 contract) -------------------------
    job_id: str = Field(..., description="Stable per-source ID. Used for dedup.")
    title: str
    company: str
    location: str = Field(
        default="",
        description="Free-form. May be 'Remote', 'Bangalore, India', etc.",
    )
    jd_text: str = Field(
        default="",
        description="Full or excerpt JD. May include HTML tags from some sources.",
    )
    apply_url: str = Field(
        ...,
        description="Where the user goes to apply. Direct ATS link when possible.",
    )
    source: SourceName
    posted_at: datetime = Field(
        ...,
        description="When the JOB was published (NOT when we fetched it). UTC.",
    )

    # --- Optional metadata --------------------------------------------------
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None  # ISO code: USD, INR, EUR, GBP.

    # Seniority hint from the source if it provides one (e.g. Himalayas
    # gives 'Junior'/'Senior'/'Lead'). We use it as a SOFT hint in the
    # experience filter, not a hard rule.
    seniority_hint: Optional[str] = None

    employment_type: Optional[str] = None  # 'Full Time', 'Part Time', etc.
    tags: list[str] = Field(default_factory=list)

    # When OUR pipeline pulled this job. Set automatically by fetcher.
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Original raw record from the source — kept for debugging and for the
    # scorer (Phase 3), which sometimes wants source-specific signals that
    # didn't fit into a normalized field. Not serialized into the digest.
    raw: dict[str, Any] = Field(default_factory=dict, exclude=False)

    # ----------------------------------------------------------------------
    # Convenience derived properties — kept out of __init__ so we never
    # have to remember to set them.
    # ----------------------------------------------------------------------
    @property
    def dedup_key(self) -> str:
        """
        Cross-source identity. The same job posted to RemoteOK and to
        Adzuna will (usually) have the same (company, title) pair. We
        lowercase and strip to be tolerant of minor whitespace/case
        differences across sources.
        """
        return f"{self.company.strip().lower()}|{self.title.strip().lower()}"

    @property
    def age_hours(self) -> float:
        """Hours since the job was posted, using UTC."""
        now = datetime.now(timezone.utc)
        posted = self.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (now - posted).total_seconds() / 3600.0


class FetchRunSummary(BaseModel):
    """
    Per-source diagnostics emitted by fetcher.py at the end of a run.
    Stored alongside raw_jobs_YYYY-MM-DD.json so we can debug "why so few
    jobs today" without re-running the pipeline.
    """
    source: SourceName
    fetched_count: int
    filtered_count: int   # how many survived the per-source pre-filter
    error: Optional[str] = None
    duration_seconds: float = 0.0
