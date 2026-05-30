"""
src/sources/base.py — Shared helpers for source fetchers.

Why this exists
---------------
Every source faces the same problems: HTTP with retries, polite
User-Agent, timeout, gentle backoff on rate-limit, and converting
source-specific date formats to UTC datetimes. Keeping these in one
place means each source module can stay focused on its own quirks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

# Polite identifier so site operators know who's hitting them. Include a
# contact channel — best practice for being a good free-API citizen.
USER_AGENT = (
    "JobHuntAgent/1.0 (+https://github.com/jha-sk; "
    "personal job-search tool for codewithsourabhjha@gmail.com)"
)

# Default timeout — short enough to fail fast, long enough that slow
# overseas APIs (Adzuna India) don't trip on transient lag.
DEFAULT_TIMEOUT_SECONDS = 20


class SourceError(Exception):
    """A source-level failure that should be logged and continue the run."""


# ---------------------------------------------------------------------------
# HTTP wrapper with retry. Wrapped in tenacity so transient network errors
# and 429s recover automatically without us writing per-source retry loops.
# ---------------------------------------------------------------------------
@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type((requests.RequestException,)),
)
def http_get(
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> requests.Response:
    """
    GET with retries and a polite UA. Raises on 4xx/5xx after retries
    are exhausted; callers should catch SourceError around the *batch*
    of calls, not each one (we want one bad page to skip, not crash).
    """
    merged_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        merged_headers.update(headers)

    log.debug("GET %s params=%s", url, params)
    response = requests.get(url, params=params, headers=merged_headers, timeout=timeout)

    # 429 = rate-limited. Let tenacity retry with exponential backoff.
    if response.status_code == 429:
        raise requests.HTTPError(f"429 rate limited from {url}", response=response)

    response.raise_for_status()
    return response


# ---------------------------------------------------------------------------
# Date helpers — every source uses a different format. Centralised here so
# we can never have a "datetime aware vs naive" mismatch downstream.
# ---------------------------------------------------------------------------
def epoch_to_utc(epoch_seconds: int | float) -> datetime:
    """Convert a Unix epoch (seconds) to a tz-aware UTC datetime."""
    return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)


def iso_to_utc(iso_string: str) -> datetime:
    """
    Parse an ISO-8601 string and return as UTC. Handles 'Z' suffix and
    timezone-naive inputs (assumed UTC).
    """
    s = iso_string.strip()
    # Python's fromisoformat does not accept 'Z' until 3.11+ — we target
    # 3.11+ so this works, but normalise just in case.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def strip_html(text: str) -> str:
    """
    Drop HTML tags and collapse whitespace. Used because Himalayas and
    JSearch return HTML-laden descriptions; the scorer prompt wants
    plain text to stay cheap on tokens and avoid prompt-injection vectors
    hidden in <script> tags.
    """
    if not text:
        return ""
    import re
    # Strip <script> and <style> blocks WITH their contents.
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    # Strip all remaining tags.
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    # Decode common HTML entities the cheap way.
    replacements = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
                    "&#39;": "'", "&nbsp;": " ", "&rsquo;": "’", "&lsquo;": "‘",
                    "&ndash;": "–", "&mdash;": "—"}
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text
