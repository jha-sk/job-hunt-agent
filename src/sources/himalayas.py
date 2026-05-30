"""
src/sources/himalayas.py — Fetch jobs from Himalayas.

API: https://himalayas.app/jobs/api
- No auth required.
- Paginated: ?limit=<n>&offset=<n>. Default limit=20.
- ~108K jobs total — we can't pull them all. Sorted newest-first by
  `pubDate`, so we paginate UNTIL pubDate falls outside our 24h window.
- Server-side filter params (search/categories/country/seniority) are
  silently ignored — verified during Phase 2 probe — so we filter
  client-side downstream.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from config import MAX_JOB_AGE_DAYS
from src.models import Job
from src.sources.base import SourceError, epoch_to_utc, http_get, strip_html

log = logging.getLogger(__name__)

HIMALAYAS_URL = "https://himalayas.app/jobs/api"
PAGE_LIMIT = 100              # max per request — server seems to cap silently
MAX_PAGES_PER_RUN = 30        # safety brake — 30 × 100 = 3000 jobs/day max


def fetch() -> list[Job]:
    """
    Paginate Himalayas newest-first; stop when we cross the age cutoff or
    hit MAX_PAGES_PER_RUN (whichever first). Returns the full set fetched;
    the keyword/location/etc. filtering happens later in src/filters.py.
    """
    age_cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_JOB_AGE_DAYS)
    log.info("Himalayas: fetching since %s", age_cutoff.isoformat())

    jobs: list[Job] = []
    seen_ids: set[str] = set()    # paranoia — guards against API double-paging

    for page in range(MAX_PAGES_PER_RUN):
        offset = page * PAGE_LIMIT
        try:
            response = http_get(
                HIMALAYAS_URL,
                params={"limit": PAGE_LIMIT, "offset": offset},
            )
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"Himalayas fetch failed at page {page}: {exc}") from exc

        payload = response.json()
        page_jobs_raw = payload.get("jobs") or []
        if not page_jobs_raw:
            log.info("Himalayas: empty page at offset %d — stopping", offset)
            break

        # Track the oldest item we saw on this page. If it's still inside
        # the window, keep paginating; otherwise stop AFTER processing it.
        oldest_on_page: datetime | None = None
        stop_after_page = False

        for item in page_jobs_raw:
            try:
                job = _to_job(item)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Himalayas: skipping malformed record guid=%s err=%s",
                    item.get("guid"), exc,
                )
                continue

            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)

            if oldest_on_page is None or job.posted_at < oldest_on_page:
                oldest_on_page = job.posted_at

            # Don't keep jobs older than the cutoff (the filter would drop
            # them anyway, but skipping here means we never even send them
            # downstream — saves memory on the 108K-job-corpus days).
            if job.posted_at >= age_cutoff:
                jobs.append(job)
            else:
                stop_after_page = True   # past the cliff; one more page is enough

        log.info(
            "Himalayas: page %d offset=%d returned=%d kept_running_total=%d oldest_on_page=%s",
            page, offset, len(page_jobs_raw), len(jobs),
            oldest_on_page.isoformat() if oldest_on_page else "—",
        )

        if stop_after_page:
            log.info("Himalayas: crossed age cutoff — stopping pagination")
            break

    log.info("Himalayas: fetched %d jobs within window", len(jobs))
    return jobs


def _to_job(item: dict[str, Any]) -> Job:
    """
    Map Himalayas record onto our normalized Job model.

    Sample Himalayas keys (verified in Phase 2 probe):
        applicationLink, categories, companyLogo, companyName, companySlug,
        currency, description, employmentType, excerpt, expiryDate, guid,
        locationRestrictions, maxSalary, minSalary, parentCategories,
        pubDate, seniority, timezoneRestrictions, title
    """
    posted = epoch_to_utc(item["pubDate"])

    # Apply URL: 'applicationLink' is the real external URL when present;
    # otherwise fall back to 'guid' (the himalayas.app job page).
    apply_url = item.get("applicationLink") or item.get("guid") or ""
    if not apply_url:
        raise ValueError("missing applicationLink/guid")

    # Location: Himalayas uses 'locationRestrictions' (list of countries) +
    # 'timezoneRestrictions' (list of UTC offsets). We flatten the country
    # list; "Worldwide" if both are empty.
    location_list = item.get("locationRestrictions") or []
    if location_list:
        location = ", ".join(str(loc) for loc in location_list)
    else:
        location = "Worldwide / Remote"

    # Seniority is a list like ['Senior'] — we keep the first as a hint.
    seniority_list = item.get("seniority") or []
    seniority = str(seniority_list[0]) if seniority_list else None

    # Stable per-source ID. Himalayas doesn't expose a numeric ID — we use
    # the guid (URL) which is permanent for the life of the posting.
    job_id = f"himalayas-{item['guid'].rstrip('/').rsplit('/', 1)[-1]}"

    return Job(
        job_id=job_id,
        title=item.get("title", "").strip(),
        company=item.get("companyName", "").strip(),
        location=location,
        jd_text=strip_html(item.get("description") or item.get("excerpt") or ""),
        apply_url=apply_url,
        source="himalayas",
        posted_at=posted,
        salary_min=_to_float(item.get("minSalary")),
        salary_max=_to_float(item.get("maxSalary")),
        salary_currency=item.get("currency"),
        seniority_hint=seniority,
        employment_type=item.get("employmentType"),
        tags=[str(c) for c in (item.get("categories") or [])],
        raw=item,
    )


def _to_float(value: Any) -> float | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
