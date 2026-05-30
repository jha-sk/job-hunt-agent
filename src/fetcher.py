r"""
src/fetcher.py — Job Hunt Agent · Phase 2 orchestrator.

WHAT IT DOES
------------
Runs every source enabled in config.SOURCES, normalizes their results
into Job objects, applies pre-filters, dedupes across sources, and
writes the output to data/raw_jobs_YYYY-MM-DD.json plus a per-source
diagnostic summary.

HOW TO RUN
----------
    .\.venv\Scripts\python.exe -m src.fetcher              # full run
    .\.venv\Scripts\python.exe -m src.fetcher --dry-run    # don't write file
    .\.venv\Scripts\python.exe -m src.fetcher --verbose    # debug logging

WHAT THIS MODULE DOES NOT DO
----------------------------
- Score jobs — that's Phase 3.
- Tailor resumes — that's Phase 4.
- Send anything anywhere — that's Phase 10.
It just produces a clean, deduped, pre-filtered list of jobs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make the project root importable so `from config import ...` works whether
# this module is launched as `python -m src.fetcher` (recommended) or
# directly as `python src/fetcher.py` (still supported via the path hack).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DATA_DIR, SOURCES  # noqa: E402
from src import db                    # noqa: E402
from src.dedupe import dedupe         # noqa: E402
from src.filters import apply_filters # noqa: E402
from src.models import FetchRunSummary, Job, SourceName  # noqa: E402
from src.sources import adzuna, himalayas, jsearch, remoteok  # noqa: E402
from src.sources.base import SourceError  # noqa: E402

log = logging.getLogger("fetcher")


# Registry of source-name → fetch callable. Add a new source by
# importing it above and adding it here. Order doesn't matter; the
# orchestrator just iterates.
_SOURCE_FETCHERS: dict[SourceName, callable] = {
    "remoteok":  remoteok.fetch,
    "himalayas": himalayas.fetch,
    "adzuna":    adzuna.fetch,
    "jsearch":   jsearch.fetch,
}


# =============================================================================
# Per-source runner — isolates failures so one source can't break the run.
# =============================================================================
def _run_one_source(name: SourceName) -> tuple[list[Job], FetchRunSummary]:
    """
    Fetch + per-source filter pass. Returns the kept jobs and a summary.
    A SourceError (or any unexpected exception) is caught here so the
    rest of the pipeline still runs with whatever sources DID work.
    """
    started = time.monotonic()
    try:
        raw_jobs = _SOURCE_FETCHERS[name]()
        kept, _drops = apply_filters(raw_jobs)
        return kept, FetchRunSummary(
            source=name,
            fetched_count=len(raw_jobs),
            filtered_count=len(kept),
            duration_seconds=round(time.monotonic() - started, 2),
        )
    except SourceError as exc:
        log.error("source %s: %s", name, exc)
        return [], FetchRunSummary(
            source=name,
            fetched_count=0,
            filtered_count=0,
            error=str(exc),
            duration_seconds=round(time.monotonic() - started, 2),
        )
    except Exception as exc:  # noqa: BLE001 — top-level isolation
        log.exception("source %s: unexpected error", name)
        return [], FetchRunSummary(
            source=name,
            fetched_count=0,
            filtered_count=0,
            error=f"unexpected: {exc!r}",
            duration_seconds=round(time.monotonic() - started, 2),
        )


# =============================================================================
# Main pipeline
# =============================================================================
def run(dry_run: bool = False) -> tuple[list[Job], list[FetchRunSummary]]:
    """
    Execute the Phase 2 pipeline. Returns (final_jobs, per_source_summaries).
    If dry_run=True, doesn't write any files.
    """
    log.info("====== Job Hunt Agent · Fetcher run starting ======")
    log.info("Enabled sources: %s", [s for s, on in SOURCES.items() if on])

    all_jobs: list[Job] = []
    summaries: list[FetchRunSummary] = []

    for source_name, enabled in SOURCES.items():
        if not enabled:
            log.info("source %s: DISABLED in config — skipping", source_name)
            continue
        if source_name not in _SOURCE_FETCHERS:
            log.warning("source %s: no fetcher registered — skipping", source_name)
            continue
        kept, summary = _run_one_source(source_name)
        all_jobs.extend(kept)
        summaries.append(summary)

    log.info("=== cross-source dedupe ===")
    final_jobs = dedupe(all_jobs)

    if not dry_run:
        _write_output(final_jobs, summaries)
        _persist_to_db(final_jobs, summaries)

    _print_summary_table(final_jobs, summaries)
    log.info("====== Fetcher run complete: %d unique jobs ======", len(final_jobs))
    return final_jobs, summaries


def _persist_to_db(jobs: list[Job], summaries: list[FetchRunSummary]) -> None:
    """
    Mirror the fetched jobs into SQLite and update today's daily_runs row.
    Uses upsert (INSERT OR IGNORE) so subsequent scorer/tailor fields
    aren't clobbered when fetcher runs twice in one day.
    """
    db.init_db()   # idempotent; safe to call every run
    inserted, skipped = db.upsert_jobs_bulk(
        [j.model_dump(mode="json") for j in jobs],
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_fetched = sum(s.fetched_count for s in summaries)
    db.upsert_daily_run(
        today,
        jobs_fetched=total_fetched,
        notes=f"sources: {','.join(s.source for s in summaries if s.fetched_count > 0)}",
    )
    log.info("db: jobs upserted=%d (new=%d skipped=%d)",
             len(jobs), inserted, skipped)


# =============================================================================
# Output — atomic write to data/raw_jobs_YYYY-MM-DD.json
# =============================================================================
def _write_output(jobs: list[Job], summaries: list[FetchRunSummary]) -> None:
    """
    Write the daily output file. Includes per-source diagnostics so we
    can investigate "why so few jobs today" without re-running.

    Atomic write pattern: write to .tmp file, then rename. Prevents a
    Ctrl-C in the middle from leaving a half-written JSON.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"raw_jobs_{today}.json"
    tmp_path = out_path.with_suffix(".json.tmp")

    payload = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_date": today,
        "job_count": len(jobs),
        "summaries": [s.model_dump() for s in summaries],
        # Pydantic .model_dump(mode="json") converts datetime → ISO strings
        # and HttpUrl → str so the result is plain JSON-serializable.
        "jobs": [j.model_dump(mode="json") for j in jobs],
    }

    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(out_path)
    log.info("wrote %s (%d bytes)", out_path, out_path.stat().st_size)


# =============================================================================
# Terminal output — per master prompt: must print jobs to terminal when run.
# =============================================================================
def _print_summary_table(jobs: list[Job], summaries: list[FetchRunSummary]) -> None:
    """
    Pretty terminal output using rich. Falls back to plain print if rich
    isn't available for any reason (it's in requirements.txt though).
    """
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        # Plain text fallback — should not normally hit this branch.
        print("\n=== Sources ===")
        for s in summaries:
            print(f"  {s.source:10} fetched={s.fetched_count:4} kept={s.filtered_count:4} "
                  f"error={s.error or '-'} t={s.duration_seconds}s")
        print(f"\n=== Final: {len(jobs)} unique jobs ===")
        for j in jobs[:20]:
            print(f"  [{j.source:8}] {j.title:50} @ {j.company:30} ({j.location})")
        return

    console = Console()

    src_table = Table(title="Per-source diagnostics", show_header=True, header_style="bold")
    src_table.add_column("Source")
    src_table.add_column("Fetched", justify="right")
    src_table.add_column("Kept after filters", justify="right")
    src_table.add_column("Time", justify="right")
    src_table.add_column("Error")
    for s in summaries:
        src_table.add_row(
            s.source,
            str(s.fetched_count),
            str(s.filtered_count),
            f"{s.duration_seconds}s",
            s.error or "-",
        )
    console.print(src_table)

    jobs_table = Table(
        title=f"Final unique jobs after dedup: {len(jobs)} (showing first 25)",
        show_header=True, header_style="bold",
    )
    jobs_table.add_column("Source", style="cyan")
    jobs_table.add_column("Title", style="white")
    jobs_table.add_column("Company", style="green")
    jobs_table.add_column("Location", style="yellow")
    jobs_table.add_column("Posted (h)", justify="right")
    for j in jobs[:25]:
        jobs_table.add_row(
            j.source,
            (j.title[:60] + "…") if len(j.title) > 60 else j.title,
            (j.company[:30] + "…") if len(j.company) > 30 else j.company,
            (j.location[:30] + "…") if len(j.location) > 30 else j.location,
            f"{j.age_hours:.1f}",
        )
    console.print(jobs_table)


# =============================================================================
# CLI
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch + filter + dedupe today's jobs from all enabled sources.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't write the daily output file. Useful for testing.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show DEBUG-level logs from sources (very chatty).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence very noisy third-party libs even in verbose mode.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
