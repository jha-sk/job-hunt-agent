"""
src/token_log.py — Append-only LLM usage log + daily cap enforcement.

WHY THIS EXISTS
---------------
Every LLM call across the pipeline (scorer, tailor, quiz, gmail watcher)
records its token usage here. The log answers three questions:
  1. How many tokens did this run cost?
  2. Are we approaching the DAILY_TOKEN_CAP safety brake?
  3. What did we spend this month in $?

FORMAT
------
JSONL — one line per LLM call. Easy to grep, easy to load into pandas/sqlite,
survives partial writes (corrupted line = drop one record, not the whole log).

Example line:
{"ts":"2026-05-30T10:00:00+00:00","phase":"scorer","provider":"gemini",
 "model":"gemini-2.0-flash","job_id":"adzuna-in-1234","input_tokens":1450,
 "output_tokens":210,"cost_usd":0.0,"duration_s":1.3}
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from config import DAILY_TOKEN_CAP, LLM_PRICING_USD_PER_M_TOKENS, TOKEN_USAGE_LOG

log = logging.getLogger(__name__)


# A module-level lock so concurrent LLM calls (if we ever parallelise)
# don't interleave bytes in the JSONL file.
_WRITE_LOCK = threading.Lock()


class DailyTokenCapExceeded(RuntimeError):
    """Raised when today's cumulative tokens would exceed DAILY_TOKEN_CAP."""


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Cost estimate in USD using LLM_PRICING_USD_PER_M_TOKENS from config.
    Returns 0.0 for free-tier models (Gemini Flash) or unknown models —
    we'd rather understate than alarm Sourabh with a fake number.
    """
    rates = LLM_PRICING_USD_PER_M_TOKENS.get(model)
    if not rates:
        log.warning("token_log: no pricing for model %r — cost estimate = 0.0", model)
        return 0.0
    return (input_tokens / 1_000_000) * rates["input"] + (output_tokens / 1_000_000) * rates["output"]


def record(
    *,
    phase: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_s: float,
    job_id: str | None = None,
    extra: dict | None = None,
) -> None:
    """
    Append one line to the token usage log. Atomically (under the lock).

    Phase = which pipeline stage made this call (scorer / tailor / quiz /
    gmail / digest). Useful for cost attribution.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "provider": provider,
        "model": model,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cost_usd": round(cost_usd(model, input_tokens, output_tokens), 6),
        "duration_s": round(duration_s, 2),
    }
    if job_id:
        entry["job_id"] = job_id
    if extra:
        entry.update(extra)

    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with _WRITE_LOCK:
        TOKEN_USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TOKEN_USAGE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)


def todays_usage() -> dict[str, int | float]:
    """
    Sum today's tokens + cost across all phases. Used both for the daily
    safety-cap check and for the Phase 10 digest's "X tokens, $Y used today".
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}

    if not TOKEN_USAGE_LOG.exists():
        return totals

    for entry in _iter_log_safely(TOKEN_USAGE_LOG):
        if not entry.get("ts", "").startswith(today):
            continue
        totals["input_tokens"]  += int(entry.get("input_tokens", 0))
        totals["output_tokens"] += int(entry.get("output_tokens", 0))
        totals["cost_usd"]      += float(entry.get("cost_usd", 0.0))
        totals["calls"]         += 1

    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return totals


def enforce_daily_cap(projected_extra_tokens: int = 0) -> None:
    """
    Raise DailyTokenCapExceeded if today's usage (plus a projected next-call
    estimate) would cross the cap. Caller should invoke this BEFORE the
    actual LLM call. The cap is a safety brake against runaway loops, NOT
    a budget — set generously in config.DAILY_TOKEN_CAP.
    """
    used = todays_usage()
    total = used["input_tokens"] + used["output_tokens"] + projected_extra_tokens
    if total > DAILY_TOKEN_CAP:
        raise DailyTokenCapExceeded(
            f"Today's token usage ({total:,}) would exceed DAILY_TOKEN_CAP "
            f"({DAILY_TOKEN_CAP:,}). Halting to prevent runaway costs. "
            f"If this is legitimate, raise DAILY_TOKEN_CAP in .env."
        )


def _iter_log_safely(path: Path) -> Iterable[dict]:
    """
    Yield each JSON line as a dict, silently skipping malformed lines.
    Defensive against partial writes from a crashed run.
    """
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
