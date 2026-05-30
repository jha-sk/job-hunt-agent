"""
src/sources/jsearch.py — Fetch jobs via JSearch (RapidAPI).

API: https://rapidapi.com/letscrape-6bRgm3GnNRZ/api/jsearch
- Free tier: 150 calls/MONTH. Very tight (~5/day). We do exactly
  config.JSEARCH_MAX_CALLS_PER_RUN per day (default 1).
- Aggregates LinkedIn, Indeed, ZipRecruiter, Glassdoor.
- One call = up to 10 results on a single page. We accept that small slice
  rather than paginating — pagination would burn the monthly budget in
  one day.

When LLM_PROVIDER=gemini, this is the ONLY way the pipeline sees
LinkedIn-aggregated postings. It's a small but high-leverage source.
"""

from __future__ import annotations

import logging
from typing import Any

from config import JSEARCH_DAILY_QUERY, JSEARCH_MAX_CALLS_PER_RUN, RAPIDAPI_KEY
from src.models import Job
from src.sources.base import SourceError, iso_to_utc, http_get, strip_html

log = logging.getLogger(__name__)

JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"
JSEARCH_HOST = "jsearch.p.rapidapi.com"


def fetch() -> list[Job]:
    """
    Returns jobs from JSearch. Skips gracefully if no RapidAPI key set.

    Spends JSEARCH_MAX_CALLS_PER_RUN calls per run (default 1) on a single
    daily query (JSEARCH_DAILY_QUERY). Picks the highest-value coverage —
    LinkedIn aggregation — without burning the monthly budget.
    """
    if not RAPIDAPI_KEY:
        log.warning(
            "JSearch: RAPIDAPI_KEY not set in .env — skipping. "
            "Sign up free at https://rapidapi.com/letscrape-6bRgm3GnNRZ/api/jsearch"
        )
        return []

    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": JSEARCH_HOST,
    }

    jobs: list[Job] = []
    seen_ids: set[str] = set()

    # We only ever spend JSEARCH_MAX_CALLS_PER_RUN calls per run. Default
    # 1 = the single most valuable query (JSEARCH_DAILY_QUERY from .env).
    for call_num in range(JSEARCH_MAX_CALLS_PER_RUN):
        params = {
            "query": JSEARCH_DAILY_QUERY,
            "page": "1",
            "num_pages": "1",
            "date_posted": "today",     # respects our 24h window upstream
        }
        try:
            response = http_get(JSEARCH_URL, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "JSearch: call %d failed query=%r err=%s",
                call_num, JSEARCH_DAILY_QUERY, exc,
            )
            continue

        payload = response.json()
        results = payload.get("data") or []
        log.info(
            "JSearch: call %d query=%r returned=%d",
            call_num, JSEARCH_DAILY_QUERY, len(results),
        )

        for item in results:
            try:
                job = _to_job(item)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "JSearch: skipping malformed record id=%s err=%s",
                    item.get("job_id"), exc,
                )
                continue
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)

    if not jobs and RAPIDAPI_KEY:
        # Don't escalate to SourceError — a 0-result day can legitimately
        # happen with this narrow daily query. Just log.
        log.warning("JSearch: 0 jobs returned today")

    log.info("JSearch: fetched %d jobs", len(jobs))
    return jobs


def _to_job(item: dict[str, Any]) -> Job:
    """
    Map JSearch record onto our normalized Job model.

    Sample JSearch keys (https://rapidapi.com/letscrape-6bRgm3GnNRZ/api/jsearch):
        job_id, employer_name, job_title, job_description, job_apply_link,
        job_city, job_country, job_is_remote, job_posted_at_datetime_utc,
        job_min_salary, job_max_salary, job_salary_currency,
        job_employment_type, job_publisher
    """
    posted_raw = item.get("job_posted_at_datetime_utc")
    if not posted_raw:
        raise ValueError("missing job_posted_at_datetime_utc")
    posted = iso_to_utc(posted_raw)

    apply_url = item.get("job_apply_link") or ""
    if not apply_url:
        raise ValueError("missing job_apply_link")

    # Location: "City, Country" with "Remote" appended if flagged.
    city = (item.get("job_city") or "").strip()
    country = (item.get("job_country") or "").strip()
    parts = [p for p in (city, country) if p]
    location = ", ".join(parts) if parts else ""
    if item.get("job_is_remote"):
        location = (location + " (Remote)").strip() if location else "Remote"

    return Job(
        job_id=f"jsearch-{item['job_id']}",
        title=(item.get("job_title") or "").strip(),
        company=(item.get("employer_name") or "").strip(),
        location=location,
        jd_text=strip_html(item.get("job_description") or ""),
        apply_url=apply_url,
        source="jsearch",
        posted_at=posted,
        salary_min=_to_float(item.get("job_min_salary")),
        salary_max=_to_float(item.get("job_max_salary")),
        salary_currency=item.get("job_salary_currency"),
        seniority_hint=None,
        employment_type=item.get("job_employment_type"),
        tags=[item.get("job_publisher")] if item.get("job_publisher") else [],
        raw=item,
    )


def _to_float(value: Any) -> float | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
