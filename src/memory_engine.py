r"""
src/memory_engine.py — Job Hunt Agent · Phase 9 learning engine.

WHAT IT DOES
------------
Three responsibilities, in increasing order of cleverness:

  1. ANALYZERS (read-only of DB → write derived insights to `memory`):
     - source_quality:           which sources yield highest scores
     - company_response_rate:    apps vs replies per company
     - apply_time_pattern:       when Sourabh tends to apply
     - skills_gap_top:           gaps that recur across tailored resumes
     - winning_resume_versions:  resume paths that reached shortlisted/interview

  2. FEEDBACK CLI (capture context-rich signal):
     - Interactive prompts: "you got shortlisted at X — what made the difference?"
     - Driven by `pending_feedback_prompt` entries enqueued by gmail_watcher
       whenever an application transitions to shortlisted / rejected.

  3. WEEKLY INTELLIGENCE REPORT (markdown, saved to reports/):
     - Numbers (apps this week, response rate, sources hit)
     - Learned insights from the analyzers
     - Skill-gap analysis
     - Recommended adjustments for the week ahead

WHAT IT DOES NOT DO
-------------------
- It does NOT directly adjust scorer/fetcher weights at runtime.
  Auto-tuning needs a lot of data before it's trustworthy. For now,
  insights are written to the `memory` table where other phases can
  optionally CONSULT them (quiz already does this for struggle topics).
  Future iterations can promote individual insights to hard knobs.

HOW TO RUN
----------
    python -m src.memory_engine analyze         # refresh all derived insights
    python -m src.memory_engine feedback        # interactive feedback for pending events
    python -m src.memory_engine weekly-report   # generate this week's report
    python -m src.memory_engine pending         # list outstanding feedback prompts
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CANDIDATE_NAME, REPORTS_DIR, RESUMES_TAILORED_DIR  # noqa: E402
from src import db  # noqa: E402

log = logging.getLogger("memory_engine")


# =============================================================================
# Memory category names — defined once so analyzers, hooks, and readers
# never drift apart on spelling.
# =============================================================================
CAT_SOURCE_QUALITY        = "analysis_source_quality"
CAT_COMPANY_RESPONSE      = "analysis_company_response_rate"
CAT_APPLY_TIME            = "analysis_apply_time_pattern"
CAT_SKILLS_GAP            = "analysis_skills_gap_top"
CAT_WINNING_RESUMES       = "analysis_winning_resume_versions"

CAT_FEEDBACK_SHORTLISTED  = "feedback_shortlisted_reason"
CAT_FEEDBACK_REJECTED     = "feedback_rejected_reason"

# Pending queue: rows in this category mean "ask Sourabh about this next
# time he runs `memory_engine feedback`". gmail_watcher enqueues these
# when an application's status flips to shortlisted or rejected.
CAT_PENDING_FEEDBACK      = "pending_feedback_prompt"

# Per-job gap log written by tailor.py. Each row is a single gap; the
# analyzer below counts frequencies across all of them.
CAT_TAILOR_GAP            = "tailor_gap"


# =============================================================================
# Tiny helpers
# =============================================================================
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# =============================================================================
# Analyzers — each reads from DB, writes to memory table.
# Idempotent: rerunning overwrites the previous insight.
# =============================================================================
def analyze_source_quality() -> dict:
    """
    Per source, compute: jobs_scored, avg_match_score, top_match_score.
    Stores as memory[CAT_SOURCE_QUALITY][<source>] = {...}.
    Returns the full dict for caller inspection.
    """
    jobs = db.list_jobs(limit=10_000)
    by_source: dict[str, list[int]] = {}
    for j in jobs:
        if j.get("match_score") is None:
            continue
        by_source.setdefault(j["source"], []).append(j["match_score"])

    out: dict[str, dict[str, float | int]] = {}
    for source, scores in by_source.items():
        if not scores:
            continue
        out[source] = {
            "jobs_scored":     len(scores),
            "avg_match_score": round(sum(scores) / len(scores), 1),
            "top_match_score": max(scores),
            "median_score":    sorted(scores)[len(scores) // 2],
        }
        db.memory_set(CAT_SOURCE_QUALITY, source, out[source], source="memory_engine.analyzer")
    return out


def analyze_company_response_rate() -> dict:
    """
    Per company, compute: applications, email_events received,
    response_rate (events/apps), and the distribution of resulting statuses.
    A company that ghosted you 5 times is one we should consider deprioritising.
    """
    apps = db.list_applications(limit=10_000)
    events = db.list_email_events(limit=10_000)
    events_by_job = {}
    for e in events:
        if e.get("job_id"):
            events_by_job.setdefault(e["job_id"], []).append(e)

    per_company: dict[str, dict[str, Any]] = {}
    for app in apps:
        company = (app.get("company") or "").strip()
        if not company:
            continue
        per_company.setdefault(company, {
            "applications": 0, "events": 0, "statuses": [],
        })
        per_company[company]["applications"] += 1
        per_company[company]["events"] += len(events_by_job.get(app["job_id"], []))
        per_company[company]["statuses"].append(app["status"])

    out: dict[str, dict[str, Any]] = {}
    for company, stats in per_company.items():
        apps_n = stats["applications"]
        stats["response_rate"] = round(stats["events"] / apps_n, 2) if apps_n else 0
        stats["status_dist"]   = dict(Counter(stats["statuses"]))
        out[company] = stats
        db.memory_set(CAT_COMPANY_RESPONSE, company, stats,
                      source="memory_engine.analyzer")
    return out


def analyze_apply_time_pattern() -> dict:
    """
    When does Sourabh tend to apply? hour-of-day + day-of-week distributions.
    Surface this so the digest can suggest, e.g. "you do best on Tue mornings".
    """
    apps = db.list_applications(limit=10_000)
    by_hour: Counter[int] = Counter()
    by_dow: Counter[str] = Counter()
    DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for app in apps:
        ts = app.get("applied_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        by_hour[dt.hour] += 1
        by_dow[DOW[dt.weekday()]] += 1

    payload = {
        "by_hour": dict(sorted(by_hour.items())),
        "by_dow":  {d: by_dow[d] for d in DOW if by_dow[d]},
        "total_applications": len(apps),
    }
    db.memory_set(CAT_APPLY_TIME, "overall", payload, source="memory_engine.analyzer")
    return payload


def analyze_skills_gap_top(top_n: int = 10) -> list[dict]:
    """
    Count the most frequent gap items across all tailored resumes
    (tailor.py writes each gap as one memory row of category CAT_TAILOR_GAP).
    The top N become memory[CAT_SKILLS_GAP][rank-<n>].
    """
    rows = db.memory_list(CAT_TAILOR_GAP)
    gap_counts: Counter[str] = Counter()
    for row in rows:
        val = row.get("value") or {}
        gap = (val.get("gap") or "").strip()
        if gap:
            # Truncate to first 100 chars to merge near-duplicates.
            gap_counts[gap[:100]] += 1

    top = gap_counts.most_common(top_n)
    out = [{"gap": g, "frequency": n} for g, n in top]

    # Wipe old "rank-*" rows and write the new top-N.
    existing = db.memory_list(CAT_SKILLS_GAP)
    for e in existing:
        if e["key"].startswith("rank-"):
            db.memory_set(CAT_SKILLS_GAP, e["key"], {"_archived": True},
                          source="memory_engine.analyzer")
    for i, item in enumerate(out, 1):
        db.memory_set(CAT_SKILLS_GAP, f"rank-{i:02d}", item,
                      source="memory_engine.analyzer")
    return out


def analyze_winning_resume_versions() -> list[dict]:
    """
    Resume versions that took an application to shortlisted/interviewing/offer.
    These are the phrasings tailor should ideally reuse.
    """
    apps = db.list_applications(limit=10_000)
    winning_statuses = {"shortlisted", "interviewing", "offer"}
    winners: list[dict] = []
    for app in apps:
        if app["status"] not in winning_statuses:
            continue
        if not app.get("resume_path"):
            continue
        winners.append({
            "resume_path":    app["resume_path"],
            "job_id":         app["job_id"],
            "company":        app.get("company"),
            "title":          app.get("title"),
            "ended_at_status": app["status"],
            "last_updated":    app["last_updated"],
        })

    # Replace the stored "winners" list (one row per winner, keyed by app id).
    for w in winners:
        db.memory_set(CAT_WINNING_RESUMES, str(w["job_id"]), w,
                      source="memory_engine.analyzer")
    return winners


def run_all_analyzers() -> dict:
    """Run every analyzer in one shot. Used by the daily cron + weekly report."""
    log.info("memory: running all analyzers")
    out = {
        "source_quality":         analyze_source_quality(),
        "company_response_rate":  analyze_company_response_rate(),
        "apply_time_pattern":     analyze_apply_time_pattern(),
        "skills_gap_top":         analyze_skills_gap_top(),
        "winning_resume_versions": analyze_winning_resume_versions(),
    }
    log.info("memory: analyzers done — wrote insights to %d categories",
             len([c for c, v in out.items() if v]))
    return out


# =============================================================================
# Feedback queue — gmail_watcher enqueues, this CLI drains.
# =============================================================================
def enqueue_feedback_prompt(
    *, application_id: int, company: str, new_status: str,
) -> None:
    """
    Called from gmail_watcher when an application transitions to shortlisted
    or rejected. Stores a row in `pending_feedback_prompt` so Sourabh can
    answer when he runs `memory_engine feedback`.
    """
    if new_status not in ("shortlisted", "rejected"):
        return
    prompt_text = {
        "shortlisted": (
            f"You got SHORTLISTED at {company}. "
            "What do you think made the difference? "
            "(Anything specific about your resume, cover letter, or your "
            "background that the role asked for?)"
        ),
        "rejected": (
            f"You got REJECTED at {company}. "
            "Do you know why? Any signal from the email or post-interview "
            "vibe — skills gap, timing, fit, salary?"
        ),
    }[new_status]
    db.memory_set(
        CAT_PENDING_FEEDBACK,
        str(application_id),
        {
            "application_id": application_id,
            "company":        company,
            "new_status":     new_status,
            "prompt":         prompt_text,
            "created_at":     _utc_now(),
            "answered":       False,
        },
        source="gmail_watcher",
    )


def cmd_pending(_args) -> None:
    """List outstanding feedback prompts without answering them."""
    pending = [
        r for r in db.memory_list(CAT_PENDING_FEEDBACK)
        if not (r.get("value") or {}).get("answered")
    ]
    if not pending:
        print("No pending feedback prompts. Nothing to answer right now.")
        return
    try:
        from rich.console import Console
        from rich.table import Table
        c = Console()
        t = Table(title=f"{len(pending)} pending feedback prompt(s)",
                  show_header=True, header_style="bold")
        t.add_column("App ID", justify="right")
        t.add_column("Company")
        t.add_column("Status")
        t.add_column("Created")
        for r in pending:
            v = r["value"]
            t.add_row(str(v["application_id"]), v["company"],
                      v["new_status"], v["created_at"][:16])
        c.print(t)
    except ImportError:
        for r in pending:
            v = r["value"]
            print(f"  app={v['application_id']:>4} {v['company']:25} "
                  f"{v['new_status']:12} created={v['created_at'][:16]}")


def cmd_feedback(_args) -> None:
    """
    Interactive walkthrough of pending feedback prompts.
    Sourabh answers each, answers go into shortlisted/rejected categories,
    pending prompt is marked answered=True so it doesn't reappear.
    """
    pending = [
        r for r in db.memory_list(CAT_PENDING_FEEDBACK)
        if not (r.get("value") or {}).get("answered")
    ]
    if not pending:
        print("No pending feedback prompts. Nothing to answer right now.")
        return

    print(f"\n{len(pending)} pending feedback prompt(s). "
          "Press Enter on empty line to skip a question.\n")
    for r in pending:
        v = r["value"]
        print(f"\n=== App #{v['application_id']} — {v['company']} ({v['new_status']}) ===")
        print(v["prompt"])
        answer = input("\nYour answer (Enter to skip): ").strip()
        if not answer:
            print("  skipped.")
            continue
        target_cat = {
            "shortlisted": CAT_FEEDBACK_SHORTLISTED,
            "rejected":    CAT_FEEDBACK_REJECTED,
        }[v["new_status"]]
        db.memory_set(
            target_cat,
            str(v["application_id"]),
            {
                "application_id": v["application_id"],
                "company":        v["company"],
                "reason":         answer,
                "recorded_at":    _utc_now(),
            },
            source="memory_engine.feedback",
        )
        v["answered"] = True
        db.memory_set(CAT_PENDING_FEEDBACK, str(v["application_id"]), v,
                      source="memory_engine.feedback")
        print("  recorded.")
    print("\nDone. Future scorer/digest may surface these insights.")


# =============================================================================
# Weekly Intelligence Report
# =============================================================================
def _date_n_days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def generate_weekly_report() -> Path:
    """
    Build reports/weekly_<YYYY-MM-DD>.md summarising the last 7 days.
    Always overwrites the file for the current Sunday → makes it idempotent
    for the weekly cron.
    """
    # First refresh all analyzers so the report sees up-to-date insights.
    run_all_analyzers()

    today = _utc_today()
    week_ago_iso = _date_n_days_ago(7)
    out_path = REPORTS_DIR / f"weekly_{today}.md"

    apps = db.list_applications(limit=10_000)
    events = db.list_email_events(limit=10_000)
    runs = db.list_daily_runs(limit=14)   # last 2 weeks for trend

    # --- Numbers ---
    apps_this_week = [a for a in apps if (a.get("applied_at") or "") >= week_ago_iso]
    events_this_week = [e for e in events if (e.get("received_at") or "") >= week_ago_iso]
    runs_this_week = [r for r in runs if r["run_date"] >= week_ago_iso[:10]]
    total_jobs_fetched = sum((r.get("jobs_fetched") or 0) for r in runs_this_week)
    total_jobs_scored  = sum((r.get("jobs_scored")  or 0) for r in runs_this_week)
    total_top          = sum((r.get("top_jobs_count") or 0) for r in runs_this_week)
    total_resumes      = sum((r.get("resumes_tailored") or 0) for r in runs_this_week)

    response_rate = (
        round(len(events_this_week) / len(apps_this_week), 2)
        if apps_this_week else None
    )

    # --- Build markdown ---
    lines: list[str] = []
    lines.append(f"# Weekly Intelligence Report — {today}")
    lines.append("")
    lines.append(f"_{CANDIDATE_NAME} · covering the 7 days through {today} (UTC)_")
    lines.append("")

    lines.append("## TL;DR")
    lines.append("")
    lines.append(f"- **Jobs fetched (raw):** {total_jobs_fetched}")
    lines.append(f"- **Jobs scored:** {total_jobs_scored}")
    lines.append(f"- **Top-N selected:** {total_top}")
    lines.append(f"- **Tailored resumes:** {total_resumes}")
    lines.append(f"- **Applications submitted:** {len(apps_this_week)}")
    lines.append(f"- **Email events received:** {len(events_this_week)}")
    lines.append(
        f"- **Response rate:** "
        f"{response_rate if response_rate is not None else 'n/a — no apps yet'}"
    )
    lines.append("")

    # --- Status distribution ---
    statuses = Counter(a["status"] for a in apps_this_week)
    if statuses:
        lines.append("## Application status (this week)")
        lines.append("")
        for status, count in statuses.most_common():
            lines.append(f"- {status}: {count}")
        lines.append("")

    # --- Source quality ---
    sq = db.memory_list(CAT_SOURCE_QUALITY)
    if sq:
        lines.append("## Source quality (cumulative, all-time)")
        lines.append("")
        lines.append("| Source | Jobs scored | Avg score | Top score |")
        lines.append("|---|---:|---:|---:|")
        for r in sorted(sq, key=lambda x: -(x.get("value", {}).get("avg_match_score") or 0)):
            v = r["value"] or {}
            lines.append(
                f"| {r['key']} | {v.get('jobs_scored', 0)} | "
                f"{v.get('avg_match_score', 0)} | {v.get('top_match_score', 0)} |"
            )
        lines.append("")

    # --- Company response rate ---
    cr = db.memory_list(CAT_COMPANY_RESPONSE)
    if cr:
        lines.append("## Company response rate (cumulative)")
        lines.append("")
        lines.append("| Company | Apps | Events | Response rate | Status mix |")
        lines.append("|---|---:|---:|---:|---|")
        for r in sorted(cr, key=lambda x: -(x.get("value", {}).get("response_rate") or 0))[:15]:
            v = r["value"] or {}
            sd = v.get("status_dist", {})
            mix = ", ".join(f"{k}:{n}" for k, n in sd.items())
            lines.append(
                f"| {r['key']} | {v.get('applications', 0)} | {v.get('events', 0)} | "
                f"{v.get('response_rate', 0)} | {mix} |"
            )
        lines.append("")

    # --- Apply time pattern ---
    atp = db.memory_get(CAT_APPLY_TIME, "overall")
    if atp and atp.get("total_applications"):
        lines.append("## When you apply")
        lines.append("")
        if atp.get("by_dow"):
            lines.append("**By day of week:** " +
                         ", ".join(f"{k}={n}" for k, n in atp["by_dow"].items()))
        if atp.get("by_hour"):
            top_hours = sorted(atp["by_hour"].items(), key=lambda x: -x[1])[:3]
            lines.append("**Top hours:** " +
                         ", ".join(f"{h:02d}:00 ({n})" for h, n in top_hours))
        lines.append("")

    # --- Skill gaps ---
    sg = sorted(db.memory_list(CAT_SKILLS_GAP),
                key=lambda x: x.get("key", ""))[:10]
    sg_nonempty = [r for r in sg if (r.get("value") or {}).get("gap")]
    if sg_nonempty:
        lines.append("## Top skill gaps (across tailored resumes)")
        lines.append("")
        lines.append("These keep appearing in JDs you target. Worth either")
        lines.append("learning them or filtering out roles that demand them.")
        lines.append("")
        for r in sg_nonempty:
            v = r["value"]
            lines.append(f"- ×{v['frequency']}: {v['gap']}")
        lines.append("")

    # --- Quiz struggles ---
    quiz_results = db.memory_list("quiz_question_result")
    struggled = [r for r in quiz_results
                 if (r.get("value") or {}).get("result") == "struggled"]
    if struggled:
        lines.append("## Quiz topics you've struggled on")
        lines.append("")
        topic_counts = Counter()
        for r in struggled:
            t = (r["value"] or {}).get("topic")
            if t:
                topic_counts[t] += 1
        for topic, n in topic_counts.most_common(10):
            lines.append(f"- ×{n}: {topic}")
        lines.append("")

    # --- Feedback insights (shortlisted / rejected reasons) ---
    sl = db.memory_list(CAT_FEEDBACK_SHORTLISTED)
    if sl:
        lines.append("## What you told us made shortlists happen")
        lines.append("")
        for r in sl[-5:]:
            v = r["value"] or {}
            lines.append(f"- **{v.get('company', '?')}**: {v.get('reason', '?')}")
        lines.append("")
    rj = db.memory_list(CAT_FEEDBACK_REJECTED)
    if rj:
        lines.append("## What you told us about rejections")
        lines.append("")
        for r in rj[-5:]:
            v = r["value"] or {}
            lines.append(f"- **{v.get('company', '?')}**: {v.get('reason', '?')}")
        lines.append("")

    # --- Recommended adjustments ---
    lines.append("## Recommended adjustments for next week")
    lines.append("")
    recs = _build_recommendations(sq, cr, sg_nonempty, struggled)
    if recs:
        for r in recs:
            lines.append(f"- {r}")
    else:
        lines.append("- (not enough data yet — keep applying and the engine will learn)")
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    log.info("memory: weekly report written to %s (%.1f KB)",
             out_path, out_path.stat().st_size / 1024)
    return out_path


def _build_recommendations(
    source_quality: list[dict],
    company_response: list[dict],
    skill_gaps: list[dict],
    quiz_struggles: list[dict],
) -> list[str]:
    """Convert raw insights into a short list of concrete suggestions."""
    recs: list[str] = []

    # Source recommendation: prefer the source with the highest avg score.
    if source_quality:
        ranked = sorted(
            source_quality,
            key=lambda x: -(x.get("value", {}).get("avg_match_score") or 0),
        )
        if ranked:
            top = ranked[0]
            v = top.get("value") or {}
            if v.get("jobs_scored", 0) >= 10:
                recs.append(
                    f"`{top['key']}` is yielding your highest-scoring matches "
                    f"(avg {v.get('avg_match_score')}). Consider raising its "
                    f"priority or checking it more often manually."
                )

    # Company ghoster recommendation.
    ghosters = [
        r for r in company_response
        if ((r.get("value") or {}).get("response_rate") == 0
            and (r.get("value") or {}).get("applications", 0) >= 2)
    ]
    if ghosters:
        names = ", ".join(r["key"] for r in ghosters[:3])
        recs.append(
            f"Repeat ghosters (2+ apps, zero replies): {names}. Add these "
            f"to `SERVICE_BASED_COMPANIES` or revisit your match criteria."
        )

    # Skill gap recommendation.
    if skill_gaps:
        topgap = skill_gaps[0]["value"]["gap"]
        recs.append(
            f"Most common gap across tailored resumes: \"{topgap[:80]}\". "
            f"Either pick this up (course/project) or filter roles that need it."
        )

    # Quiz struggle recommendation.
    if quiz_struggles:
        topics = Counter(
            (r.get("value") or {}).get("topic", "")
            for r in quiz_struggles
        )
        top_topic = topics.most_common(1)[0][0] if topics else ""
        if top_topic:
            recs.append(
                f"Quiz topic you struggle with most: \"{top_topic}\". The "
                f"next quizzes will drill this — also worth proactively studying."
            )

    return recs


# =============================================================================
# CLI
# =============================================================================
def cmd_analyze(_args) -> None:
    result = run_all_analyzers()
    try:
        from rich.console import Console
        from rich.table import Table
        c = Console()
        t = Table(title="Memory engine — analyzers run", show_header=True, header_style="bold")
        t.add_column("Category")
        t.add_column("Items written", justify="right")
        for name, payload in result.items():
            n = len(payload) if isinstance(payload, (dict, list)) else (1 if payload else 0)
            t.add_row(name, str(n))
        c.print(t)
    except ImportError:
        print(json.dumps({k: len(v) if hasattr(v, "__len__") else 0 for k, v in result.items()}, indent=2))


def cmd_weekly_report(_args) -> None:
    path = generate_weekly_report()
    try:
        from rich.console import Console
        Console().print(f"[green]Weekly report written:[/green] {path}")
    except ImportError:
        print(f"Wrote {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Memory & learning engine: analyze, capture feedback, report weekly.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("analyze", help="Run all analyzers; refresh derived insights in memory.")
    sub.add_parser("feedback", help="Interactive walkthrough of pending feedback prompts.")
    sub.add_parser("pending",  help="List outstanding feedback prompts without answering them.")
    sub.add_parser("weekly-report", help="Generate this week's intelligence report (markdown).")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-15s %(message)s",
        datefmt="%H:%M:%S",
    )

    handlers = {
        "analyze":       cmd_analyze,
        "feedback":      cmd_feedback,
        "pending":       cmd_pending,
        "weekly-report": cmd_weekly_report,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
