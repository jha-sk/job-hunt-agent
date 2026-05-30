"""
src/dedupe.py — Cross-source job deduplication.

Why this matters
----------------
The same job is regularly posted to multiple boards (a company posts to
RemoteOK, Adzuna scrapes it from LinkedIn, JSearch picks it up from Indeed
— now we have 3 copies). If we don't dedupe, the scorer pays 3× the token
cost to evaluate the same JD, and the digest shows the user the same
posting 3 times.

Identity definition
-------------------
Two jobs are "the same posting" if (company.lower(), title.lower()) match
after whitespace normalization. This is the cheap heuristic — it would
false-merge "Backend Engineer" at "Acme" if Acme genuinely had two
distinct Backend Engineer postings (different team/seniority). That's
rare and the cost is negligible (we'd show the user one of them; they
miss one duplicate listing).

Source preference when merging
------------------------------
When duplicates exist we keep the version from the highest-quality
source — defined here as: the one with the longest jd_text (LinkedIn-via-
JSearch usually has the fullest description, then Adzuna, then Himalayas
excerpts, then RemoteOK's HTML strip). Ties broken by preferred-source
order (configurable below).
"""

from __future__ import annotations

import logging
from collections import defaultdict

from src.models import Job

log = logging.getLogger(__name__)


# When two jobs tie on jd_text length, prefer earlier in this list. Roughly
# ordered by "directness of the apply link" — RemoteOK posts go direct to
# the company; aggregators sometimes redirect through their own URL.
_SOURCE_PREFERENCE = ["remoteok", "himalayas", "adzuna", "jsearch"]


def _source_rank(source: str) -> int:
    try:
        return _SOURCE_PREFERENCE.index(source)
    except ValueError:
        return len(_SOURCE_PREFERENCE)


def _quality_score(job: Job) -> tuple[int, int]:
    """
    Sort key for picking the best version of a duplicated job.
    Higher = better. Used as `max(group, key=_quality_score)`.

    Returns (jd_text_length, -source_rank). Negative source rank means
    that lower-ranked sources tie-break to the preferred ones above.
    """
    return (len(job.jd_text or ""), -_source_rank(job.source))


def dedupe(jobs: list[Job]) -> list[Job]:
    """
    Collapse jobs sharing the same (company, title) into one — the
    highest-quality version. Logs the dedup count for diagnostics.
    """
    groups: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        groups[job.dedup_key].append(job)

    winners: list[Job] = []
    duplicate_count = 0
    for key, candidates in groups.items():
        if len(candidates) == 1:
            winners.append(candidates[0])
            continue
        # Multiple postings for the same (company, title).
        chosen = max(candidates, key=_quality_score)
        winners.append(chosen)
        duplicate_count += len(candidates) - 1
        log.debug(
            "dedup: %d copies of %r → kept source=%s",
            len(candidates), key, chosen.source,
        )

    log.info(
        "dedupe: input=%d unique=%d collapsed=%d",
        len(jobs), len(winners), duplicate_count,
    )
    return winners
