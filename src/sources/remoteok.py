"""
src/sources/remoteok.py — Fetch jobs from RemoteOK.

API: https://remoteok.com/api
- No auth required.
- Single GET returns ~100 newest jobs (their site's index page in JSON).
- First element is a "legal" notice object, NOT a job — we filter it.
- Sorted newest-first by `epoch`.
"""

from __future__ import annotations

import logging
from typing import Any

from src.models import Job
from src.sources.base import SourceError, epoch_to_utc, http_get, strip_html

log = logging.getLogger(__name__)

REMOTEOK_URL = "https://remoteok.com/api"


def fetch() -> list[Job]:
    """
    Returns all jobs RemoteOK exposes (~100 newest). No filtering here —
    filtering happens later in src/filters.py against the full job pool.
    """
    log.info("RemoteOK: fetching %s", REMOTEOK_URL)
    try:
        response = http_get(REMOTEOK_URL)
    except Exception as exc:  # noqa: BLE001 — top-level source isolation
        raise SourceError(f"RemoteOK fetch failed: {exc}") from exc

    raw = response.json()
    if not isinstance(raw, list):
        raise SourceError(f"RemoteOK returned unexpected payload type: {type(raw)}")

    jobs: list[Job] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # The first record is a metadata/legal object without 'position'.
        if "position" not in item or "id" not in item:
            continue
        try:
            jobs.append(_to_job(item))
        except Exception as exc:  # noqa: BLE001 — never let one bad record kill the source
            log.warning("RemoteOK: skipping malformed record id=%s err=%s", item.get("id"), exc)
            continue

    log.info("RemoteOK: fetched %d jobs", len(jobs))
    return jobs


def _to_job(item: dict[str, Any]) -> Job:
    """
    Map RemoteOK's record shape onto our normalized Job model.

    Sample RemoteOK keys (verified in Phase 2 probe):
        slug, id, epoch, date, company, company_logo, position, tags,
        description, location, apply_url, salary_min, salary_max, logo, url
    """
    # Posted time. RemoteOK gives 'epoch' (Unix seconds) and 'date' (ISO).
    # Prefer epoch — it's already UTC and unambiguous.
    posted = epoch_to_utc(item["epoch"])

    # Apply URL: prefer 'apply_url' (direct), fall back to 'url' (the job
    # page on remoteok.com, which has an "Apply" button).
    apply_url = item.get("apply_url") or item.get("url") or ""
    if not apply_url:
        # No apply URL = no point in this job. Skip.
        raise ValueError("missing apply_url")

    # Salary: RemoteOK numbers are USD/year and may be missing.
    salary_min = _to_float(item.get("salary_min"))
    salary_max = _to_float(item.get("salary_max"))
    salary_currency = "USD" if (salary_min or salary_max) else None

    return Job(
        job_id=f"remoteok-{item['id']}",
        title=item["position"],
        company=item.get("company") or "",
        location=item.get("location") or "Remote",   # RemoteOK = all remote.
        jd_text=strip_html(item.get("description", "")),
        apply_url=apply_url,
        source="remoteok",
        posted_at=posted,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        tags=list(item.get("tags") or []),
        employment_type=None,   # RemoteOK doesn't expose this.
        seniority_hint=None,
        raw=item,
    )


def _to_float(value: Any) -> float | None:
    """RemoteOK sometimes returns salary as int, sometimes str, sometimes 0."""
    if value in (None, "", 0, "0"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
