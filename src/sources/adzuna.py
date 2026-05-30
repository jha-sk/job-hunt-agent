"""
src/sources/adzuna.py — Fetch jobs from Adzuna.

API: https://developer.adzuna.com/docs/search
- Free tier: 1000 calls/month. Budget per run set in config.ADZUNA_MAX_CALLS_PER_RUN.
- Per-country endpoints (in / us / gb / de / au / etc).
- Keyword search via ?what=, location filter via ?where=.
- Sorted by date (newest first) when sort_by=date.

Why we call once per (country, keyword) pair
--------------------------------------------
Adzuna's `what=` does OR-search across words within ONE call, which makes
the relevance ranking lousy when the words are heterogeneous (Go vs RAG
vs MLOps). Calling once per keyword gives cleaner per-topic results and
fits inside the per-run budget. We also keep the keyword set tighter than
JOB_KEYWORDS — only the top-leverage terms (otherwise we'd blow 1000/mo).
"""

from __future__ import annotations

import logging
from typing import Any

from config import (
    ADZUNA_APP_ID,
    ADZUNA_APP_KEY,
    ADZUNA_COUNTRIES,
    ADZUNA_MAX_CALLS_PER_RUN,
)
from src.models import Job
from src.sources.base import SourceError, iso_to_utc, http_get, strip_html

log = logging.getLogger(__name__)

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"

# Keywords to spend the per-run budget on, ordered by leverage. We send
# enough to cover backend + AI, but NOT all of JOB_KEYWORDS (that would
# spend the entire monthly cap in two days). The orchestrator clips this
# list × countries to ADZUNA_MAX_CALLS_PER_RUN.
PRIORITY_KEYWORDS = [
    "golang backend",
    "ai engineer",
    "ml engineer",
    "llm engineer",
    "backend engineer remote",
    "genai engineer",
]


def fetch() -> list[Job]:
    """
    Returns jobs from Adzuna. Skips gracefully if no API key configured.
    """
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        log.warning(
            "Adzuna: ADZUNA_APP_ID/ADZUNA_APP_KEY not set in .env — skipping. "
            "Sign up free at https://developer.adzuna.com/signup"
        )
        return []

    # Build the (country, keyword) call list, clipped to the budget.
    call_plan: list[tuple[str, str]] = []
    for country in ADZUNA_COUNTRIES:
        for keyword in PRIORITY_KEYWORDS:
            call_plan.append((country, keyword))
            if len(call_plan) >= ADZUNA_MAX_CALLS_PER_RUN:
                break
        if len(call_plan) >= ADZUNA_MAX_CALLS_PER_RUN:
            break

    log.info(
        "Adzuna: planning %d API calls (budget=%d, countries=%s)",
        len(call_plan), ADZUNA_MAX_CALLS_PER_RUN, ADZUNA_COUNTRIES,
    )

    jobs: list[Job] = []
    seen_ids: set[str] = set()

    for country, keyword in call_plan:
        url = ADZUNA_BASE.format(country=country)
        params = {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "what": keyword,
            "results_per_page": 50,    # max for free tier
            "sort_by": "date",          # newest first
            "max_days_old": 1,          # respects our 24h window upstream
        }
        try:
            response = http_get(url, params=params)
        except Exception as exc:  # noqa: BLE001
            # Don't abort the whole source — a single failed country/keyword
            # shouldn't kill Adzuna entirely.
            log.warning(
                "Adzuna: call failed country=%s keyword=%r err=%s",
                country, keyword, exc,
            )
            continue

        payload = response.json()
        results = payload.get("results") or []

        for item in results:
            try:
                job = _to_job(item, country)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Adzuna: skipping malformed record id=%s err=%s",
                    item.get("id"), exc,
                )
                continue

            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)

        log.info(
            "Adzuna: country=%s keyword=%r returned=%d running_total=%d",
            country, keyword, len(results), len(jobs),
        )

    if not jobs and (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        raise SourceError("Adzuna: all calls returned 0 results — check key validity")

    log.info("Adzuna: fetched %d jobs total", len(jobs))
    return jobs


def _to_job(item: dict[str, Any], country: str) -> Job:
    """
    Map Adzuna record onto our normalized Job model.

    Sample Adzuna keys (https://developer.adzuna.com/activedocs):
        id, created, title, description, company:{display_name},
        location:{display_name, area}, redirect_url, salary_min, salary_max,
        salary_is_predicted, contract_time, contract_type, category
    """
    posted = iso_to_utc(item["created"])
    apply_url = item.get("redirect_url") or ""
    if not apply_url:
        raise ValueError("missing redirect_url")

    company = (item.get("company") or {}).get("display_name", "").strip()
    location = (item.get("location") or {}).get("display_name", "").strip()

    salary_min = _to_float(item.get("salary_min"))
    salary_max = _to_float(item.get("salary_max"))
    # Adzuna doesn't return currency in the result — infer from country.
    salary_currency = _currency_for_country(country) if (salary_min or salary_max) else None

    return Job(
        job_id=f"adzuna-{country}-{item['id']}",
        title=item.get("title", "").strip(),
        company=company,
        location=location,
        jd_text=strip_html(item.get("description", "")),
        apply_url=apply_url,
        source="adzuna",
        posted_at=posted,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        seniority_hint=None,
        employment_type=item.get("contract_time"),
        tags=[item["category"]["label"]] if item.get("category") else [],
        raw=item,
    )


def _to_float(value: Any) -> float | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _currency_for_country(country_code: str) -> str:
    """Map Adzuna country code to ISO currency. Best-effort."""
    mapping = {
        "in": "INR", "us": "USD", "gb": "GBP", "de": "EUR",
        "fr": "EUR", "au": "AUD", "ca": "CAD", "nl": "EUR",
        "it": "EUR", "es": "EUR", "br": "BRL", "mx": "MXN",
        "nz": "NZD", "pl": "PLN", "ru": "RUB", "sg": "SGD",
        "za": "ZAR", "at": "EUR", "ch": "CHF", "be": "EUR",
    }
    return mapping.get(country_code.lower(), "USD")
