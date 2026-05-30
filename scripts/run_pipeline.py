r"""
scripts/run_pipeline.py — The single daily orchestrator.

WHAT IT DOES
------------
Runs every Job Hunt Agent phase in dependency order, in one process,
collecting per-phase status. Used by:

  - GitHub Actions cron (.github/workflows/daily_job_hunt.yml) — 8 AM IST
  - Manual local trigger anytime:  python scripts/run_pipeline.py

PHASE ORDER (and what happens on failure)
-----------------------------------------
  1. fetcher           — REQUIRED. If it fails, abort. No jobs = nothing to score.
  2. scorer            — REQUIRED. If it fails, abort. No scores = nothing to tailor.
  3. tailor            — best effort. Failure means no tailored resumes today.
  4. pdf_generator     — best effort. Failure means no PDFs.
  5. gmail_watcher     — best effort.
  6. quiz_generator    — best effort.
  7. memory analyzers  — best effort.
  8. digest            — RUNS ALWAYS, even on partial failure (send what we have).
  9. db backup         — last. Snapshot whatever state we ended in.

FAILURE BEHAVIOUR
-----------------
- Required-phase failure → email alert to candidate + exit code 1.
- Best-effort failure   → log full traceback, continue.
- Digest always tries to send so Sourabh at least sees "today was partial".

HOW TO RUN
----------
    python scripts/run_pipeline.py                # full run
    python scripts/run_pipeline.py --dry-run      # propagate dry-run to all phases
    python scripts/run_pipeline.py --no-email     # skip the actual digest send
    python scripts/run_pipeline.py --skip <phase> # skip one phase by name
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Project root importable so `from src.X import ...` works no matter where
# we're invoked from (matters for GitHub Actions vs local shell).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CANDIDATE_EMAIL  # noqa: E402

log = logging.getLogger("pipeline")


# =============================================================================
# Phase registry — order matters. Each entry: (name, callable, opts).
# Imports are LAZY inside _build_phases() so an import error in one module
# (e.g. weasyprint mid-day) doesn't kill the orchestrator's import.
# =============================================================================
def _build_phases(dry_run: bool, no_email: bool) -> list[tuple[str, Callable, dict]]:
    """Build the ordered phase list with the per-phase callable + options."""
    from src import (
        fetcher, scorer, tailor, pdf_generator,
        gmail_watcher, quiz_generator, memory_engine, digest, db,
    )

    return [
        # name              callable                              opts
        ("fetcher",         lambda: fetcher.run(dry_run=dry_run), {"required": True}),
        ("scorer",          lambda: scorer.run(dry_run=dry_run),  {"required": True}),
        ("tailor",          lambda: tailor.run(dry_run=dry_run),  {"required": False}),
        ("pdf_generator",   lambda: pdf_generator.run(),          {"required": False}),
        ("gmail_watcher",   lambda: gmail_watcher.run(dry_run=dry_run),
                                                                  {"required": False}),
        ("quiz_generator",  lambda: quiz_generator.run(dry_run=dry_run),
                                                                  {"required": False}),
        ("memory_analyze",  lambda: memory_engine.run_all_analyzers(),
                                                                  {"required": False}),
        ("digest",          lambda: digest.run(
                                dry_run=dry_run, no_email=no_email,
                            ),                                    {"required": False}),
        ("db_backup",       lambda: db.backup_db(),               {"required": False}),
    ]


# =============================================================================
# Failure alert — minimal email so Sourabh learns about a broken cron
# =============================================================================
def _send_failure_alert(phase: str, exc: Exception, tb: str) -> None:
    """
    Try hard to email Sourabh that the cron broke. Best-effort: if Gmail
    is also down (auth expired, etc.) this is a noisy log line and that's it.
    """
    try:
        from src import gmail_client
        subject = f"[JobHunt ALERT — PIPELINE FAILED] phase={phase}"
        body = (
            f"Today's run aborted on phase '{phase}'.\n\n"
            f"Error: {exc}\n\n"
            f"Traceback:\n{tb}\n\n"
            f"---\n"
            f"Check the GitHub Actions log (if running in CI) or `logs/`\n"
            f"locally. Re-run with:\n\n"
            f"  python scripts/run_pipeline.py --dry-run\n"
        )
        gmail_client.send_email(to=CANDIDATE_EMAIL, subject=subject, body=body)
        log.info("pipeline: failure alert email sent")
    except Exception as alert_exc:  # noqa: BLE001
        log.error("pipeline: also failed to send failure alert: %s", alert_exc)


# =============================================================================
# Main loop
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run every Job Hunt Agent phase in order. The cron entry-point.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Propagate --dry-run to every phase (no LLM, no email, no writes).")
    parser.add_argument("--no-email", action="store_true",
                        help="Run normally but skip the digest send step.")
    parser.add_argument("--skip", action="append", default=[],
                        help="Skip a phase by name. Repeatable: --skip pdf_generator --skip gmail_watcher")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-15s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down third-party noise that doesn't matter to the user.
    for noisy in (
        "urllib3", "httpx", "requests", "google_genai", "googleapiclient",
        "google_auth_httplib2", "google_auth_oauthlib",
        "weasyprint", "fontTools", "cssselect2", "tinycss2",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    started_overall = time.monotonic()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info("╔══════════════════════════════════════════════╗")
    log.info("║  Job Hunt Agent · daily pipeline · %s   ║", today)
    log.info("╚══════════════════════════════════════════════╝")

    phases = _build_phases(dry_run=args.dry_run, no_email=args.no_email)
    per_phase: list[dict[str, Any]] = []

    for name, fn, opts in phases:
        if name in args.skip:
            log.info("--- Phase %s: SKIPPED by --skip flag ---", name)
            per_phase.append({"name": name, "status": "skipped", "duration_s": 0})
            continue

        log.info("─── Phase: %s ───", name)
        started = time.monotonic()
        try:
            fn()
            elapsed = time.monotonic() - started
            per_phase.append({"name": name, "status": "ok", "duration_s": round(elapsed, 1)})
            log.info("✓ %s completed in %.1fs", name, elapsed)
        except Exception as exc:  # noqa: BLE001 — orchestrator IS the top-level handler
            elapsed = time.monotonic() - started
            tb = traceback.format_exc()
            per_phase.append({
                "name": name, "status": "failed",
                "duration_s": round(elapsed, 1), "error": str(exc),
            })
            log.error("✗ %s FAILED in %.1fs: %s", name, elapsed, exc)
            log.error(tb)

            if opts.get("required"):
                log.error("Required phase failed — aborting the rest of the pipeline.")
                _send_failure_alert(name, exc, tb)
                _write_run_summary(today, per_phase, fatal_phase=name)
                sys.exit(1)

    total_elapsed = time.monotonic() - started_overall
    log.info("════════════════════════════════════════════════")
    log.info("Pipeline complete in %.1fs", total_elapsed)
    for p in per_phase:
        icon = {"ok": "✓", "failed": "✗", "skipped": "·"}.get(p["status"], "?")
        line = f"  {icon} {p['name']:18} {p['status']:8} ({p['duration_s']}s)"
        if p["status"] == "failed":
            line += f"  — {p.get('error', '')[:60]}"
        log.info(line)

    _write_run_summary(today, per_phase, fatal_phase=None)

    # Exit code: 0 if no required phase failed; the loop above already
    # exits 1 in that case. If we reach here we're good.
    any_failures = any(p["status"] == "failed" for p in per_phase)
    if any_failures:
        log.warning("Pipeline finished with non-fatal failures. Check logs above.")
        # We don't exit 1 here — non-required failures shouldn't fail the
        # GitHub Actions run; the digest still went out and Sourabh sees
        # the issue in his digest's error section (planned for future).
    sys.exit(0)


def _write_run_summary(today: str, per_phase: list[dict], fatal_phase: str | None) -> None:
    """
    Persist the run summary to daily_runs.notes as JSON so the db_viewer
    can show what happened. Idempotent — overwrites today's row's notes.
    """
    try:
        from src import db
        import json as _json
        summary = {
            "phases": per_phase,
            "fatal_phase": fatal_phase,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        notes_str = _json.dumps(summary, ensure_ascii=False)
        db.upsert_daily_run(
            today,
            notes=notes_str,
            errors=(f"FATAL on phase={fatal_phase}" if fatal_phase else None),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("pipeline: failed to write run summary to db: %s", exc)


if __name__ == "__main__":
    main()
