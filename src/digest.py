r"""
src/digest.py — Job Hunt Agent · Phase 10 daily digest.

WHAT IT DOES
------------
Pulls the day's pipeline output from SQLite and emits ONE morning summary:
  1. TODAY'S TOP-N jobs (title, score, fit reasons, gap, apply URL, PDF path)
  2. APPLICATION STATUS UPDATES (anything that moved in the last 24h:
     shortlisted, interview_scheduled, ghosted, rejected)
  3. TODAY'S QUIZ (filename + topic list parsed from the .md)
  4. MEMORY ENGINE UPDATE (top recommendation from last analyze run)
  5. TOKEN USAGE (today / month-to-date)

Sends the plain-text version via Gmail to `DIGEST_RECIPIENT_EMAIL` and
mirrors it to terminal when run locally (per Sourabh's Phase 1 choice).
Also drafts polite follow-up emails for ghosted apps (saved to Gmail
Drafts; never auto-sent — Sourabh edits + sends manually).

WHY PLAIN TEXT, NOT HTML
------------------------
Plain text renders identically on Gmail web, iOS, Android, Outlook
desktop, K-9 Mail, command-line clients — everywhere. HTML breaks on
~10% of clients in subtle ways and is overkill for a daily checklist.
If you ever want HTML, swap `_render_plain()` for `_render_html()`.

HOW TO RUN
----------
    .\.venv\Scripts\python.exe -m src.digest                # send + mirror + drafts
    .\.venv\Scripts\python.exe -m src.digest --dry-run      # render + print only, NO send/drafts
    .\.venv\Scripts\python.exe -m src.digest --no-drafts    # send but skip follow-up drafts
    .\.venv\Scripts\python.exe -m src.digest --no-email     # print to terminal only
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    CANDIDATE_EMAIL,
    CANDIDATE_NAME,
    DIGEST_TERMINAL_MIRROR,
    GHOSTED_THRESHOLD_DAYS,
    MIN_MATCH_SCORE,
    QUIZ_EMAIL_INCLUDED,
    QUIZZES_DIR,
    TOP_JOBS_PER_DAY,
)
from src import db, token_log  # noqa: E402

log = logging.getLogger("digest")


# =============================================================================
# Recipient
# =============================================================================
# Where the digest goes (and where the watcher reads from). Same address by
# default — see DIGEST_RECIPIENT_EMAIL in .env. Falls back to CANDIDATE_EMAIL.
import os
DIGEST_TO = os.getenv("DIGEST_RECIPIENT_EMAIL", CANDIDATE_EMAIL)


# =============================================================================
# Helpers
# =============================================================================
SEP_HEAVY = "━" * 60
SEP_LIGHT = "─" * 60


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _short_ts(iso_ts: str | None) -> str:
    if not iso_ts:
        return ""
    try:
        return datetime.fromisoformat(iso_ts).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso_ts[:16]


# =============================================================================
# Section 1 — Today's top jobs
# =============================================================================
def _section_top_jobs() -> tuple[str, list[dict]]:
    """Returns (text, top_jobs_list). list is used downstream for digest stats."""
    top = db.list_jobs(min_score=MIN_MATCH_SCORE, limit=TOP_JOBS_PER_DAY)
    lines: list[str] = []
    lines.append(f"TODAY'S TOP {len(top)} JOB(S) (score ≥ {MIN_MATCH_SCORE})")
    lines.append(SEP_HEAVY)
    if not top:
        lines.append("")
        lines.append("No jobs cleared the threshold today.")
        lines.append("Check the raw pool with:  python -m src.db_viewer jobs")
        lines.append("")
        return "\n".join(lines), top

    for i, j in enumerate(top, 1):
        lines.append("")
        lines.append(f"{i}. {j['title']} at {j['company']} — Score: {j['match_score']}/100")
        lines.append(f"   Action: {j.get('recommended_action') or '?'}  ·  "
                     f"Confidence: {j.get('confidence') or '?'}  ·  "
                     f"Location: {j.get('location') or '?'}")
        reasons = j.get("reasons_for_fit") or []
        if reasons:
            lines.append("   Why you fit:")
            for r in reasons[:3]:
                lines.append(f"     - {r}")
        gaps = j.get("gaps") or []
        if gaps:
            lines.append(f"   Gap: {gaps[0]}")
        lines.append(f"   Apply here: {j.get('apply_url') or '(no URL)'}")
        if j.get("resume_version_used"):
            lines.append(f"   Tailored resume: {j['resume_version_used']}")
        if j.get("pdf_path"):
            lines.append(f"   PDF: {j['pdf_path']}")
        lines.append(f"   Mark applied:  python -m src.db_viewer apply {j['job_id']}")
    lines.append("")
    return "\n".join(lines), top


# =============================================================================
# Section 2 — Application status updates (only stuff that moved in the last 24h)
# =============================================================================
def _section_status_updates() -> str:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    apps = db.list_applications(limit=10_000)
    moved = [a for a in apps if (a.get("last_updated") or "") >= cutoff]

    # Bucket by status, ordering by importance.
    bucket_order = ["shortlisted", "interviewing", "offer", "rejected", "ghosted"]
    by_bucket: dict[str, list[dict]] = {b: [] for b in bucket_order}
    for a in moved:
        if a["status"] in by_bucket:
            by_bucket[a["status"]].append(a)

    lines: list[str] = []
    lines.append("APPLICATION STATUS UPDATE (last 24h)")
    lines.append(SEP_HEAVY)

    any_movement = False
    for bucket in bucket_order:
        items = by_bucket[bucket]
        if not items:
            continue
        any_movement = True
        lines.append("")
        emoji = {"shortlisted": "🟢", "interviewing": "🟢", "offer": "🎉",
                 "rejected": "🔴", "ghosted": "⚠️"}.get(bucket, "•")
        flag = " — ACTION REQUIRED" if bucket in {"shortlisted", "interviewing"} else ""
        lines.append(f"  {emoji} {bucket.upper()}{flag}")
        for a in items:
            company = a.get("company") or "(unknown)"
            title = a.get("title") or "(unknown role)"
            lines.append(f"     - {company} · {title}")
            if a.get("notes"):
                lines.append(f"       notes: {a['notes']}")

    if not any_movement:
        lines.append("")
        lines.append("No status changes in the last 24h.")
        # Highlight the total open pipeline anyway so it's not invisible.
        open_apps = [a for a in apps if a["status"] in {"applied", "shortlisted", "interviewing"}]
        if open_apps:
            lines.append(f"Open applications in pipeline: {len(open_apps)}")
        else:
            lines.append("(0 applications recorded yet — run `db_viewer apply <job_id>` after applying)")

    lines.append("")
    return "\n".join(lines)


# =============================================================================
# Section 3 — Today's quiz
# =============================================================================
def _section_quiz() -> str:
    today = _today()
    quiz_path = QUIZZES_DIR / f"quiz_{today}.md"
    lines: list[str] = []
    lines.append("TODAY'S QUIZ")
    lines.append(SEP_HEAVY)
    if not quiz_path.exists():
        lines.append("")
        lines.append("(no quiz generated yet — run `python -m src.quiz_generator`)")
        lines.append("")
        return "\n".join(lines)

    md = quiz_path.read_text(encoding="utf-8")
    topics: list[str] = []
    for line in md.splitlines():
        if line.startswith("## Q") and " — " in line:
            # "## Q1. [Mid] 🛠 Technical — Go: Goroutines vs Threads"
            topic = line.split(" — ", 1)[1].strip()
            topics.append(topic)

    lines.append("")
    lines.append(f"Saved to: {quiz_path}")
    if topics:
        lines.append(f"Topics ({len(topics)}):")
        for t in topics:
            lines.append(f"  - {t}")
    lines.append("")
    lines.append("Work through under a timer, then mark each:")
    lines.append("  python -m src.quiz_generator mark <n> <easy|struggled|nailed>")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# Section 4 — Memory engine recommendation (top 1)
# =============================================================================
def _section_memory_insight() -> str:
    """Pull the single most actionable recommendation from memory analyzers."""
    from src import memory_engine

    lines: list[str] = []
    lines.append("MEMORY ENGINE UPDATE")
    lines.append(SEP_HEAVY)
    lines.append("")

    # Reuse the same recommendation builder the weekly report uses.
    sq = db.memory_list(memory_engine.CAT_SOURCE_QUALITY)
    cr = db.memory_list(memory_engine.CAT_COMPANY_RESPONSE)
    sg = sorted(
        db.memory_list(memory_engine.CAT_SKILLS_GAP), key=lambda x: x.get("key", ""),
    )
    sg_nonempty = [r for r in sg if (r.get("value") or {}).get("gap")]
    qs = [r for r in db.memory_list("quiz_question_result")
          if (r.get("value") or {}).get("result") == "struggled"]

    recs = memory_engine._build_recommendations(sq, cr, sg_nonempty, qs)
    if recs:
        for r in recs[:2]:    # top 2 in the daily digest
            lines.append(f"  • {r}")
    else:
        lines.append("  (not enough data yet — engine will start surfacing patterns "
                     "after ~1-2 weeks of activity)")
    lines.append("")
    lines.append("Full weekly report:  python -m src.memory_engine weekly-report")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# Section 5 — Token usage
# =============================================================================
def _section_tokens() -> str:
    """Today's tokens + naive month-to-date sum from the token log."""
    today = token_log.todays_usage()
    month_total = _month_to_date_tokens()
    lines: list[str] = []
    lines.append("TOKEN USAGE")
    lines.append(SEP_HEAVY)
    lines.append("")
    lines.append(
        f"  Today:   in={today['input_tokens']:,}  out={today['output_tokens']:,}  "
        f"calls={today['calls']}  cost=${today['cost_usd']:.4f}"
    )
    lines.append(
        f"  Month:   in={month_total['input_tokens']:,}  out={month_total['output_tokens']:,}  "
        f"calls={month_total['calls']}  cost=${month_total['cost_usd']:.4f}"
    )
    lines.append("")
    return "\n".join(lines)


def _month_to_date_tokens() -> dict:
    """Sum tokens since the 1st of the current UTC month."""
    import json as _json
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    totals = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
    if not token_log.TOKEN_USAGE_LOG.exists():
        return totals
    with token_log.TOKEN_USAGE_LOG.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if not e.get("ts", "").startswith(month_prefix):
                continue
            totals["input_tokens"]  += int(e.get("input_tokens", 0))
            totals["output_tokens"] += int(e.get("output_tokens", 0))
            totals["cost_usd"]      += float(e.get("cost_usd", 0.0))
            totals["calls"]         += 1
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return totals


# =============================================================================
# Top-level render
# =============================================================================
def _render_plain() -> tuple[str, dict]:
    """Build the full digest text. Returns (text, stats_for_subject)."""
    today_str = _today()
    header = [
        f"GOOD MORNING — JOB HUNT DAILY DIGEST  ·  {today_str}",
        f"Hi {CANDIDATE_NAME.split()[0]} — here's where things stand.",
        "",
    ]

    jobs_text, top_jobs = _section_top_jobs()
    status_text         = _section_status_updates()
    quiz_text           = _section_quiz()        if QUIZ_EMAIL_INCLUDED else ""
    memory_text         = _section_memory_insight()
    tokens_text         = _section_tokens()

    footer = [
        SEP_LIGHT,
        "Sent automatically by your Job Hunt Agent.",
        "Run `python -m src.db_viewer status` for the full pipeline picture.",
    ]

    body = "\n".join([
        *header,
        jobs_text,
        status_text,
        *( [quiz_text] if quiz_text else [] ),
        memory_text,
        tokens_text,
        *footer,
    ])

    stats = {"top_count": len(top_jobs), "date": today_str}
    return body, stats


def _build_subject(stats: dict) -> str:
    n = stats["top_count"]
    suffix = (f"{n} top match" + ("" if n == 1 else "es")) if n else "no qualifying matches"
    return f"[JobHunt] Daily Digest — {stats['date']} · {suffix}"


# =============================================================================
# Ghosted follow-up drafts
# =============================================================================
def _draft_ghosted_followups() -> int:
    """
    For every application currently in 'ghosted' status, save ONE follow-up
    draft to Gmail Drafts. Sourabh edits To: + sends manually. Idempotent:
    we memo each drafted application_id so re-runs don't keep drafting.
    """
    from src import gmail_client
    DRAFT_MEMO_CAT = "ghosted_followup_drafted"

    ghosted = db.list_applications(status="ghosted", limit=10_000)
    if not ghosted:
        return 0

    already_drafted = {r["key"] for r in db.memory_list(DRAFT_MEMO_CAT)}

    drafted_count = 0
    for app in ghosted:
        memo_key = str(app["application_id"])
        if memo_key in already_drafted:
            continue
        body = _ghosted_followup_body(app)
        subject = f"Following up on my {app.get('title') or 'application'} — {app.get('company') or ''}"
        try:
            gmail_client.create_draft(
                to=CANDIDATE_EMAIL,  # placeholder — Sourabh edits before sending
                subject=subject.strip(),
                body=body,
            )
        except Exception as exc:  # noqa: BLE001 — one bad draft shouldn't block the digest
            log.warning("digest: draft failed for app %s: %s", app["application_id"], exc)
            continue
        db.memory_set(DRAFT_MEMO_CAT, memo_key,
                      {"drafted_at": datetime.now(timezone.utc).isoformat(),
                       "company": app.get("company"), "title": app.get("title")},
                      source="digest")
        drafted_count += 1
    return drafted_count


def _ghosted_followup_body(app: dict) -> str:
    """Politely-templated follow-up. No LLM cost; deterministic, short, professional."""
    return (
        f"[REPLACE TO: with recruiter or careers@ contact for {app.get('company', '')}]\n\n"
        f"Hi,\n\n"
        f"I wanted to follow up on my application for the "
        f"{app.get('title') or 'position'} role at {app.get('company') or 'your company'}, "
        f"submitted around {_short_ts(app.get('applied_at'))}. "
        f"I remain very interested in the opportunity and would love to learn about the next "
        f"steps in your hiring process.\n\n"
        f"Please let me know if there's any additional information I can provide.\n\n"
        f"Best regards,\n"
        f"{CANDIDATE_NAME}\n"
        f"{CANDIDATE_EMAIL}\n"
    )


# =============================================================================
# Pipeline
# =============================================================================
def run(
    *,
    dry_run: bool = False,
    no_email: bool = False,
    no_drafts: bool = False,
) -> dict:
    """
    Render the digest, optionally send + draft. Returns stats dict.
    """
    log.info("====== Daily digest starting ======")
    body, stats = _render_plain()
    subject = _build_subject(stats)
    drafted = 0

    if DIGEST_TERMINAL_MIRROR:
        # Terminal print so local runs are useful (e.g. before pushing
        # the Phase 11 cron live, or after manually triggering).
        print()
        print(body)
        print()

    if dry_run:
        log.info("--dry-run: no email, no drafts")
        return {"subject": subject, "drafted": 0, "sent": False, **stats}

    if not no_drafts:
        try:
            drafted = _draft_ghosted_followups()
            if drafted:
                log.info("digest: drafted %d ghosted follow-up email(s)", drafted)
        except Exception as exc:  # noqa: BLE001
            log.warning("digest: ghosted-followup drafting failed: %s", exc)

    sent = False
    if not no_email:
        try:
            from src import gmail_client
            gmail_client.send_email(to=DIGEST_TO, subject=subject, body=body)
            sent = True
            log.info("digest: sent to %s", DIGEST_TO)
        except Exception as exc:  # noqa: BLE001
            log.error("digest: send failed: %s", exc)

    # Update today's daily_runs row.
    try:
        db.upsert_daily_run(_today(), digest_sent=sent)
    except Exception as exc:  # noqa: BLE001
        log.warning("digest: db daily_runs update failed: %s", exc)

    log.info("====== Daily digest complete (sent=%s, drafted=%d) ======", sent, drafted)
    return {"subject": subject, "drafted": drafted, "sent": sent, **stats}


# =============================================================================
# CLI
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Render + send the morning digest.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print only — no email, no drafts, no DB writes.")
    parser.add_argument("--no-email", action="store_true",
                        help="Skip the email send (but DO still draft + print).")
    parser.add_argument("--no-drafts", action="store_true",
                        help="Skip generating ghosted follow-up drafts.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("googleapiclient", "urllib3", "httpx", "google_auth_httplib2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    run(dry_run=args.dry_run, no_email=args.no_email, no_drafts=args.no_drafts)


if __name__ == "__main__":
    main()
