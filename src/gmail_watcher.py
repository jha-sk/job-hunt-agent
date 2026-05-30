r"""
src/gmail_watcher.py — Job Hunt Agent · Phase 7 Gmail watcher.

WHAT IT DOES
------------
Every run:
1. Reads the applications table to know which companies to watch.
2. Pulls recent Gmail messages (last GMAIL_LOOKBACK_DAYS).
3. Matches each message to an application via sender domain / subject /
   body containing the company name (companies + ATS senders both).
4. Sends each matched message + a small candidate-profile blurb to the
   LLM. LLM returns one of: shortlisted, rejected, interview_scheduled,
   follow_up_required, ghosted_check, unknown.
5. Writes an `email_events` row for every classified message.
6. Updates the related `applications.status` (and `jobs.status`) to
   reflect any new signal (shortlisted/rejected/interviewing/offer).
7. Sends Sourabh a HIGH-PRIORITY alert email when a message classifies
   as shortlisted or interview_scheduled.
8. Scans the applications table for items applied > GHOSTED_THRESHOLD_DAYS
   ago with no inbound emails — flags those as 'ghosted' status.

HOW TO RUN
----------
    .\.venv\Scripts\python.exe -m src.gmail_watcher                # full scan + classify + alert
    .\.venv\Scripts\python.exe -m src.gmail_watcher --dry-run      # no DB writes, no email sends
    .\.venv\Scripts\python.exe -m src.gmail_watcher --no-alerts    # skip the alert-send step
    .\.venv\Scripts\python.exe -m src.gmail_watcher --skip-llm     # match emails but don't classify (free)
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    CANDIDATE_EMAIL,
    CANDIDATE_NAME,
    GHOSTED_THRESHOLD_DAYS,
    GMAIL_LOOKBACK_DAYS,
    MODEL_TAILOR,  # we reuse the tailor's flash-lite quota for gmail too
)
from src import db, gmail_client  # noqa: E402
from src.llm_client import LLMClient  # noqa: E402

log = logging.getLogger("gmail_watcher")


# =============================================================================
# LLM schema
# =============================================================================
EmailCategory = Literal[
    "shortlisted",
    "rejected",
    "interview_scheduled",
    "follow_up_required",
    "ghosted_check",
    "unknown",
]


class EmailClassification(BaseModel):
    """The LLM's verdict on one email."""
    category: EmailCategory = Field(
        description=(
            "shortlisted = moving forward / asking for next steps but not yet scheduled. "
            "rejected = thanks but no thanks. "
            "interview_scheduled = a specific interview time/slot proposed or confirmed. "
            "follow_up_required = recruiter asked Sourabh for info (availability, links, refs). "
            "ghosted_check = automated follow-up reminder Sourabh received about an open app. "
            "unknown = unclear or unrelated to a job application."
        ),
    )
    confidence: Literal["High", "Medium", "Low"]
    short_reason: str = Field(
        max_length=300,
        description="One sentence quoting/paraphrasing the key signal.",
    )


SYSTEM_PROMPT = f"""\
You classify recruiter/ATS emails arriving in {CANDIDATE_NAME}'s inbox.

CONTEXT:
- {CANDIDATE_NAME} is a software engineer (Associate at Accenture) applying to backend / AI engineer roles.
- He applies to companies; companies / ATS systems write back. You classify the LATEST reply.
- Many emails come from ATS senders (greenhouse.io, lever.co, workday, ashbyhq.com, etc.)
  rather than the company's own domain. Use the body content, not just the sender.

CATEGORIES (pick exactly one):
- shortlisted: company moving forward — phrases like "we'd love to chat", "we'd like to move to the next round", "shortlisted", "passed the screen".
- interview_scheduled: a specific interview slot has been booked or proposed with a time/date. Calendar invites count.
- rejected: "we won't be moving forward", "unfortunately", "pursue other candidates", "after careful consideration".
- follow_up_required: recruiter is asking Sourabh for something — availability, portfolio link, references, a take-home submission.
- ghosted_check: an automated "still interested?" or "no update yet" reminder he received, not the company itself moving.
- unknown: noise, marketing, generic newsletter, or genuinely ambiguous.

Be conservative — if the email doesn't clearly fit one of the action categories, return 'unknown'.

Output strictly the JSON schema given.
"""


USER_PROMPT_TEMPLATE = """\
Email to classify:

From: {sender}
Subject: {subject}
Date: {date}

Body (first 2000 chars):
{body}

Return the JSON.
"""


# =============================================================================
# Matching: email -> application
# =============================================================================
# When a message hits the inbox, we need to figure out WHICH application
# it's about. We try three signals in order of confidence.
def _normalize(text: str) -> str:
    """Lowercase, strip non-alpha. 'Capital Numbers Pvt Ltd' -> 'capitalnumbers'."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _match_email_to_application(
    email_fields: dict,
    applications: list[dict],
) -> Optional[dict]:
    """
    Try sender-domain → company-name-in-subject → company-name-in-body, in
    order. Returns the matching application row (with .title/.company) or None.

    `applications` is the list from db.list_applications() — each row
    carries job_id, company, title (joined from jobs).
    """
    sender_email = (email_fields.get("from_email") or "").lower()
    sender_domain = sender_email.split("@", 1)[-1] if "@" in sender_email else ""
    subject_norm  = _normalize(email_fields.get("subject"))
    body_norm     = _normalize(email_fields.get("body_text", ""))[:5000]

    best: Optional[dict] = None
    for app in applications:
        company_norm = _normalize(app.get("company") or "")
        if not company_norm:
            continue
        # Strongest signal: sender domain contains the company name.
        if sender_domain and company_norm in _normalize(sender_domain):
            return app
        # Subject contains company name.
        if company_norm in subject_norm:
            best = best or app
        # Body contains company name (weakest, only as fallback).
        elif company_norm in body_norm and best is None:
            best = app
    return best


# =============================================================================
# Classification
# =============================================================================
def _build_user_prompt(email_fields: dict) -> str:
    body = (email_fields.get("body_text") or "")[:2000]
    return USER_PROMPT_TEMPLATE.format(
        sender=email_fields.get("from") or "(unknown)",
        subject=email_fields.get("subject") or "(no subject)",
        date=email_fields.get("date") or "",
        body=body,
    )


def classify_email(client: LLMClient, email_fields: dict) -> Optional[EmailClassification]:
    """Run the LLM. Returns None on failure; caller logs and skips."""
    try:
        result, _usage = client.complete_json(
            system=SYSTEM_PROMPT,
            user=_build_user_prompt(email_fields),
            schema=EmailClassification,
            max_output_tokens=512,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("classify_email: failed id=%s err=%s",
                  email_fields.get("id"), exc)
        return None


# =============================================================================
# Status transitions — what each classification means for the application
# =============================================================================
# When the LLM tells us a category, this map says what to set the
# applications row's status to. Categories that don't change status
# (follow_up_required, ghosted_check, unknown) aren't in the map.
_CATEGORY_TO_STATUS: dict[str, str] = {
    "shortlisted":         "shortlisted",
    "interview_scheduled": "interviewing",
    "rejected":            "rejected",
}


def _apply_status_transition(application: dict, category: str) -> None:
    """Update applications + jobs status if the category signals one."""
    new_status = _CATEGORY_TO_STATUS.get(category)
    if not new_status:
        return
    db.update_application_status(
        application["application_id"], new_status,
        notes=f"auto-updated by gmail_watcher ({category})",
    )
    log.info("status: app %s -> %s (was %s)",
             application["application_id"], new_status, application["status"])

    # Phase 9: enqueue a feedback prompt so memory_engine can ask Sourabh
    # WHY this happened next time he runs `memory_engine feedback`.
    # Only for terminal-ish transitions (shortlisted, rejected).
    if new_status in ("shortlisted", "rejected"):
        try:
            # Lazy import to avoid circular deps at module-load time.
            from src import memory_engine
            memory_engine.enqueue_feedback_prompt(
                application_id=application["application_id"],
                company=application.get("company") or "(unknown)",
                new_status=new_status,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("memory: failed to enqueue feedback prompt: %s", exc)


# =============================================================================
# Alerts — fire when a category demands Sourabh's immediate attention
# =============================================================================
_ALERT_CATEGORIES = {"shortlisted", "interview_scheduled"}


def _send_alert(application: dict, email_fields: dict, classification: EmailClassification) -> None:
    """High-priority email to Sourabh when something good lands."""
    subject = f"[JobHunt ALERT — {classification.category.upper()}] {application['company']}"
    body = (
        f"Good news on the {application.get('title') or 'role'} at {application['company']}.\n\n"
        f"Classification: {classification.category}  ({classification.confidence} confidence)\n"
        f"Signal: {classification.short_reason}\n\n"
        f"Source email:\n"
        f"  From:    {email_fields.get('from')}\n"
        f"  Subject: {email_fields.get('subject')}\n"
        f"  Date:    {email_fields.get('date')}\n"
        f"  Snippet: {email_fields.get('snippet')[:400]}\n\n"
        f"---\n"
        f"Sent automatically by your Job Hunt Agent.\n"
        f"Open Gmail to read + reply: https://mail.google.com/\n"
    )
    try:
        gmail_client.send_email(CANDIDATE_EMAIL, subject, body)
    except Exception as exc:  # noqa: BLE001
        log.error("alert send failed for app=%s: %s", application["application_id"], exc)


# =============================================================================
# Ghosted detection
# =============================================================================
def _flag_ghosted_applications(threshold_days: int = GHOSTED_THRESHOLD_DAYS) -> int:
    """
    Find applications applied > threshold_days ago with status='applied'
    and no email_events. Set their status to 'ghosted' and log an event.
    Returns the count of newly-flagged ghosts.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=threshold_days)
    cutoff_iso = cutoff.isoformat()
    apps = db.list_applications(status="applied", limit=10_000)
    flagged = 0
    for app in apps:
        # Skip apps that haven't aged into the ghost window yet.
        if app["applied_at"] >= cutoff_iso:
            continue
        events = db.list_email_events(limit=10_000)
        has_event = any(e.get("job_id") == app["job_id"] for e in events)
        if has_event:
            continue
        db.update_application_status(
            app["application_id"], "ghosted",
            notes=f"auto-ghosted after {threshold_days}d of no reply",
        )
        db.insert_email_event(
            job_id=app["job_id"],
            email_subject="(no reply — auto-ghosted)",
            email_from="(system)",
            received_at=datetime.now(timezone.utc).isoformat(),
            classified_as="ghosted_check",
            raw_snippet=f"No inbound email within {threshold_days} days of application.",
        )
        flagged += 1
        log.info("ghosted: app %s (%s) flagged after %dd",
                 app["application_id"], app["company"], threshold_days)
    return flagged


# =============================================================================
# Pipeline
# =============================================================================
def _build_gmail_query(lookback_days: int) -> str:
    """
    Gmail search query: messages from the last N days, IN INBOX, not from me.
    We don't filter by labels — recruiter emails land in different categories
    on different accounts (Primary vs Updates), so cast a wide net and let
    the matcher narrow it down.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y/%m/%d")
    return f"in:inbox -from:{CANDIDATE_EMAIL} after:{since}"


def run(
    *,
    dry_run: bool = False,
    no_alerts: bool = False,
    skip_llm: bool = False,
    lookback_days: int = GMAIL_LOOKBACK_DAYS,
) -> dict:
    """
    Full Phase 7 pass. Returns a stats dict. Safe to call multiple times
    per day — emails already classified (matched by message id in the
    email_events table) are skipped on re-runs.
    """
    log.info("====== Gmail watcher run starting (lookback %dd) ======", lookback_days)

    applications = db.list_applications(limit=10_000)
    log.info("apps in DB: %d", len(applications))

    if not applications:
        log.warning(
            "No applications in DB — nothing to match against. "
            "Mark a job as applied with: python -m src.db_viewer apply <job_id>"
        )

    # --- Already-seen guard so we don't re-classify on re-runs ---
    already_classified_msg_ids: set[str] = set()
    for ev in db.list_email_events(limit=10_000):
        # We stash the gmail message id in raw_snippet for re-run dedup.
        # See _record_event() below.
        if ev.get("raw_snippet", "").startswith("[msgid:"):
            already_classified_msg_ids.add(
                ev["raw_snippet"].split("[msgid:", 1)[1].split("]", 1)[0]
            )

    # --- Pull messages from Gmail ---
    try:
        message_refs = gmail_client.list_messages(
            query=_build_gmail_query(lookback_days),
            max_results=200,
        )
    except FileNotFoundError as exc:
        # credentials.json missing — gmail_client raises a clear message.
        log.error(str(exc))
        return {"error": "credentials.json missing", "messages_scanned": 0}
    except Exception as exc:  # noqa: BLE001
        log.exception("gmail: list_messages failed")
        return {"error": str(exc), "messages_scanned": 0}

    log.info("gmail: %d candidate messages in window", len(message_refs))

    # --- Classify + record matches ---
    classifier: Optional[LLMClient] = None
    if not skip_llm and applications:
        classifier = LLMClient(phase="gmail", model=MODEL_TAILOR)

    stats = {
        "messages_scanned":   0,
        "messages_matched":   0,
        "classified":         0,
        "alerts_sent":        0,
        "ghosted_flagged":    0,
        "skipped_already":    0,
    }

    for ref in message_refs:
        stats["messages_scanned"] += 1
        if ref["id"] in already_classified_msg_ids:
            stats["skipped_already"] += 1
            continue

        # Fetching full message body is the slow part — only do it for
        # candidates we might actually classify.
        try:
            msg = gmail_client.get_message(ref["id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("gmail: get_message failed id=%s err=%s", ref["id"], exc)
            continue
        fields = gmail_client.extract_message_fields(msg)

        # Match to an application. If no apps OR no match, skip (we won't
        # waste LLM tokens on unrelated emails).
        application = _match_email_to_application(fields, applications) if applications else None
        if not application:
            continue
        stats["messages_matched"] += 1

        if skip_llm or classifier is None:
            log.info(
                "match (no classify): app=%s subject=%r",
                application["application_id"], fields.get("subject"),
            )
            continue

        classification = classify_email(classifier, fields)
        if classification is None:
            continue
        stats["classified"] += 1

        if not dry_run:
            _record_event(application, fields, classification)
            _apply_status_transition(application, classification.category)
            if (
                classification.category in _ALERT_CATEGORIES
                and not no_alerts
                and classification.confidence != "Low"
            ):
                _send_alert(application, fields, classification)
                stats["alerts_sent"] += 1

        log.info(
            "classified: app=%s category=%s conf=%s subj=%r",
            application["application_id"], classification.category,
            classification.confidence, (fields.get("subject") or "")[:60],
        )

    # --- Ghost flagging ---
    if not dry_run and applications:
        stats["ghosted_flagged"] = _flag_ghosted_applications()

    log.info("====== Gmail watcher complete: %s ======", stats)
    _print_summary(stats)
    return stats


def _record_event(application: dict, fields: dict, classification: EmailClassification) -> None:
    """Persist one classified email to email_events."""
    snippet = (
        f"[msgid:{fields.get('id', '')}] "
        f"{(fields.get('snippet') or '')[:500]} | reason: {classification.short_reason}"
    )
    received_iso = datetime.fromtimestamp(
        (fields.get("internal_date_ms") or 0) / 1000, tz=timezone.utc,
    ).isoformat()
    db.insert_email_event(
        job_id=application["job_id"],
        email_subject=fields.get("subject", "")[:500],
        email_from=fields.get("from", "")[:200],
        received_at=received_iso,
        classified_as=classification.category,
        raw_snippet=snippet,
    )


def _print_summary(stats: dict) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return
    console = Console()
    table = Table(title="Gmail watcher run", show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for k, v in stats.items():
        table.add_row(k.replace("_", " "), str(v))
    console.print(table)


# =============================================================================
# CLI
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan Gmail for application replies, classify, alert.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify but don't write DB or send alerts.")
    parser.add_argument("--no-alerts", action="store_true",
                        help="Write DB but don't send the alert emails.")
    parser.add_argument("--skip-llm", action="store_true",
                        help="Match to apps but don't classify (zero LLM cost).")
    parser.add_argument("--lookback", type=int, default=GMAIL_LOOKBACK_DAYS,
                        help=f"Days of Gmail history to scan (default {GMAIL_LOOKBACK_DAYS}).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("googleapiclient", "google_auth_httplib2", "httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    run(
        dry_run=args.dry_run,
        no_alerts=args.no_alerts,
        skip_llm=args.skip_llm,
        lookback_days=args.lookback,
    )


if __name__ == "__main__":
    main()
