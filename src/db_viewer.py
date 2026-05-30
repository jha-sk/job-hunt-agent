r"""
src/db_viewer.py — Job Hunt Agent · Phase 6 read-only CLI.

WHAT IT DOES
------------
Lets Sourabh inspect his job pipeline without writing SQL. Every command
prints a rich-formatted table.

HOW TO RUN
----------
    .\.venv\Scripts\python.exe -m src.db_viewer status
    .\.venv\Scripts\python.exe -m src.db_viewer jobs                # all jobs
    .\.venv\Scripts\python.exe -m src.db_viewer jobs --status new
    .\.venv\Scripts\python.exe -m src.db_viewer jobs --score-min 70
    .\.venv\Scripts\python.exe -m src.db_viewer jobs --limit 25
    .\.venv\Scripts\python.exe -m src.db_viewer apps
    .\.venv\Scripts\python.exe -m src.db_viewer apps --status shortlisted
    .\.venv\Scripts\python.exe -m src.db_viewer emails
    .\.venv\Scripts\python.exe -m src.db_viewer runs
    .\.venv\Scripts\python.exe -m src.db_viewer memory
    .\.venv\Scripts\python.exe -m src.db_viewer memory --category source_quality
    .\.venv\Scripts\python.exe -m src.db_viewer backup           # snapshot + prune
    .\.venv\Scripts\python.exe -m src.db_viewer init             # create schema if missing
    .\.venv\Scripts\python.exe -m src.db_viewer import-today     # backfill today's JSON files
    .\.venv\Scripts\python.exe -m src.db_viewer job <job_id>     # full record for one job

This module is READ-ONLY except for `backup`, `init`, and `import-today`.
It will never alter row data — for that, the pipeline modules write directly
via src/db.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DATA_DIR  # noqa: E402
from src import db  # noqa: E402

log = logging.getLogger("db_viewer")


# =============================================================================
# Helpers
# =============================================================================
def _console():
    """Return a rich Console, lazily."""
    from rich.console import Console
    return Console()


def _truncate(text: str | None, n: int) -> str:
    if not text:
        return ""
    return text if len(text) <= n else text[: n - 1] + "…"


def _short_ts(iso_ts: str | None) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM'. Empty if None."""
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_ts[:16]


# =============================================================================
# `status` — single-page pipeline overview
# =============================================================================
def cmd_status(args: argparse.Namespace) -> None:
    from rich.panel import Panel
    from rich.table import Table

    console = _console()
    jobs = db.list_jobs(limit=10_000)
    apps = db.list_applications(limit=10_000)
    emails = db.list_email_events(limit=10_000)
    runs = db.list_daily_runs(limit=10)

    # ---- Jobs by status ----
    job_status_counts: dict[str, int] = {}
    scored_count = 0
    for j in jobs:
        job_status_counts[j["status"]] = job_status_counts.get(j["status"], 0) + 1
        if j.get("match_score") is not None:
            scored_count += 1

    jobs_table = Table(title="Jobs by status", show_header=True, header_style="bold")
    jobs_table.add_column("Status")
    jobs_table.add_column("Count", justify="right")
    for status, count in sorted(job_status_counts.items()):
        jobs_table.add_row(status, str(count))
    jobs_table.add_row("[bold]TOTAL[/bold]", f"[bold]{len(jobs)}[/bold]")

    # ---- Applications by status ----
    app_status_counts: dict[str, int] = {}
    for a in apps:
        app_status_counts[a["status"]] = app_status_counts.get(a["status"], 0) + 1

    apps_table = Table(title="Applications by status", show_header=True, header_style="bold")
    apps_table.add_column("Status")
    apps_table.add_column("Count", justify="right")
    for status, count in sorted(app_status_counts.items()):
        apps_table.add_row(status, str(count))
    apps_table.add_row("[bold]TOTAL[/bold]", f"[bold]{len(apps)}[/bold]")

    # ---- Recent daily runs ----
    runs_table = Table(title=f"Recent daily runs (latest {len(runs)})",
                       show_header=True, header_style="bold")
    runs_table.add_column("Date")
    runs_table.add_column("Fetched", justify="right")
    runs_table.add_column("Scored", justify="right")
    runs_table.add_column("Top-N", justify="right")
    runs_table.add_column("PDFs", justify="right")
    runs_table.add_column("Digest")
    runs_table.add_column("Errors")
    for r in runs:
        runs_table.add_row(
            r["run_date"],
            str(r.get("jobs_fetched") or 0),
            str(r.get("jobs_scored") or 0),
            str(r.get("top_jobs_count") or 0),
            str(r.get("pdfs_generated") or 0),
            "✓" if r.get("digest_sent") else "—",
            _truncate(r.get("errors") or "", 40),
        )

    # ---- Header panel ----
    summary = (
        f"[bold]Database:[/bold] {db.DB_PATH}\n"
        f"[bold]Jobs:[/bold] {len(jobs)} total, {scored_count} scored\n"
        f"[bold]Applications:[/bold] {len(apps)}\n"
        f"[bold]Email events:[/bold] {len(emails)}\n"
        f"[bold]Daily runs recorded:[/bold] {len(runs)}"
    )
    console.print(Panel(summary, title="Job Hunt Agent — pipeline status", border_style="cyan"))
    console.print(jobs_table)
    console.print(apps_table)
    console.print(runs_table)


# =============================================================================
# `jobs` — table of all (filtered) jobs
# =============================================================================
def cmd_jobs(args: argparse.Namespace) -> None:
    from rich.table import Table
    console = _console()
    jobs = db.list_jobs(
        status=args.status, min_score=args.score_min, limit=args.limit,
    )
    title = f"Jobs ({len(jobs)} shown)"
    if args.status:
        title += f" — status={args.status}"
    if args.score_min is not None:
        title += f" — score ≥ {args.score_min}"

    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column("Score", justify="right")
    table.add_column("Status")
    table.add_column("Action")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Source", style="dim")
    table.add_column("Posted", style="dim")
    for j in jobs:
        score = j.get("match_score")
        score_str = ""
        if score is not None:
            color = "green" if score >= 80 else "yellow" if score >= 60 else "red"
            score_str = f"[{color}]{score}[/{color}]"
        table.add_row(
            score_str,
            j["status"],
            j.get("recommended_action") or "",
            _truncate(j["title"], 55),
            _truncate(j["company"], 25),
            j["source"],
            _short_ts(j["posted_at"]),
        )
    console.print(table)


# =============================================================================
# `job <job_id>` — one full record
# =============================================================================
def cmd_job(args: argparse.Namespace) -> None:
    from rich.panel import Panel
    console = _console()
    j = db.get_job(args.job_id)
    if not j:
        console.print(f"[red]No job with job_id={args.job_id!r}[/red]")
        return

    body_lines = []
    body_lines.append(f"[bold]Title:[/bold] {j['title']}")
    body_lines.append(f"[bold]Company:[/bold] {j['company']}")
    body_lines.append(f"[bold]Location:[/bold] {j['location'] or '(none)'}")
    body_lines.append(f"[bold]Source:[/bold] {j['source']}")
    body_lines.append(f"[bold]Apply URL:[/bold] {j['apply_url']}")
    body_lines.append(f"[bold]Posted:[/bold] {_short_ts(j['posted_at'])}    "
                      f"[bold]Fetched:[/bold] {_short_ts(j['fetched_at'])}")
    if j.get("match_score") is not None:
        body_lines.append(f"\n[bold]Score:[/bold] {j['match_score']}/100 "
                          f"({j['recommended_action']}, {j['confidence']} conf)")
        body_lines.append(f"[bold]Reasoning:[/bold] {j.get('score_reasoning') or ''}")
        reasons = j.get("reasons_for_fit") or []
        if reasons:
            body_lines.append("[bold]Fit reasons:[/bold]")
            for r in reasons:
                body_lines.append(f"  - {r}")
        gaps = j.get("gaps") or []
        if gaps:
            body_lines.append("[bold]Gaps:[/bold]")
            for g in gaps:
                body_lines.append(f"  - {g}")
    body_lines.append(f"\n[bold]Status:[/bold] {j['status']}")
    if j.get("resume_version_used"):
        body_lines.append(f"[bold]Tailored resume:[/bold] {j['resume_version_used']}")
    if j.get("pdf_path"):
        body_lines.append(f"[bold]PDF:[/bold] {j['pdf_path']}")
    body_lines.append(f"\n[dim]JD preview:[/dim]\n{_truncate(j.get('jd_text') or '', 400)}")

    console.print(Panel("\n".join(body_lines),
                        title=f"job_id = {j['job_id']}",
                        border_style="cyan"))


# =============================================================================
# `apps` — applications
# =============================================================================
def cmd_apps(args: argparse.Namespace) -> None:
    from rich.table import Table
    console = _console()
    apps = db.list_applications(status=args.status, limit=args.limit)
    table = Table(title=f"Applications ({len(apps)} shown)",
                  show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("Status")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Applied")
    table.add_column("Updated")
    table.add_column("Notes")
    for a in apps:
        table.add_row(
            str(a["application_id"]),
            a["status"],
            _truncate(a.get("title") or "", 45),
            _truncate(a.get("company") or "", 25),
            _short_ts(a["applied_at"]),
            _short_ts(a["last_updated"]),
            _truncate(a.get("notes") or "", 30),
        )
    console.print(table)


# =============================================================================
# `emails`
# =============================================================================
def cmd_emails(args: argparse.Namespace) -> None:
    from rich.table import Table
    console = _console()
    events = db.list_email_events(limit=args.limit)
    table = Table(title=f"Email events (latest {len(events)})",
                  show_header=True, header_style="bold")
    table.add_column("Received")
    table.add_column("Classified")
    table.add_column("From")
    table.add_column("Subject")
    table.add_column("Job ID", style="dim")
    for e in events:
        table.add_row(
            _short_ts(e["received_at"]),
            e.get("classified_as") or "",
            _truncate(e.get("email_from") or "", 35),
            _truncate(e.get("email_subject") or "", 60),
            _truncate(e.get("job_id") or "", 30),
        )
    console.print(table)


# =============================================================================
# `runs`
# =============================================================================
def cmd_runs(args: argparse.Namespace) -> None:
    from rich.table import Table
    console = _console()
    runs = db.list_daily_runs(limit=args.limit)
    table = Table(title=f"Daily runs (latest {len(runs)})",
                  show_header=True, header_style="bold")
    table.add_column("Date")
    table.add_column("Fetched", justify="right")
    table.add_column("Scored", justify="right")
    table.add_column("Top-N", justify="right")
    table.add_column("Tailored", justify="right")
    table.add_column("PDFs", justify="right")
    table.add_column("Quiz")
    table.add_column("Digest")
    table.add_column("Errors", style="red")
    for r in runs:
        table.add_row(
            r["run_date"],
            str(r.get("jobs_fetched") or 0),
            str(r.get("jobs_scored") or 0),
            str(r.get("top_jobs_count") or 0),
            str(r.get("resumes_tailored") or 0),
            str(r.get("pdfs_generated") or 0),
            "✓" if r.get("quiz_generated") else "—",
            "✓" if r.get("digest_sent") else "—",
            _truncate(r.get("errors") or "", 40),
        )
    console.print(table)


# =============================================================================
# `memory`
# =============================================================================
def cmd_memory(args: argparse.Namespace) -> None:
    from rich.table import Table
    console = _console()
    entries = db.memory_list(category=args.category)
    table = Table(title=f"Memory entries ({len(entries)} shown)",
                  show_header=True, header_style="bold")
    table.add_column("Category")
    table.add_column("Key")
    table.add_column("Value")
    table.add_column("Updated")
    table.add_column("Source", style="dim")
    for e in entries:
        val = e["value"]
        if isinstance(val, (dict, list)):
            val_str = _truncate(json.dumps(val, ensure_ascii=False), 60)
        else:
            val_str = _truncate(str(val) if val is not None else "", 60)
        table.add_row(
            e["category"], e["key"], val_str,
            _short_ts(e["updated_at"]),
            e.get("source") or "",
        )
    console.print(table)


# =============================================================================
# `backup`
# =============================================================================
def cmd_backup(args: argparse.Namespace) -> None:
    path = db.backup_db()
    pruned = db.prune_old_backups(keep_days=args.keep_days)
    _console().print(
        f"[green]Backup written:[/green] {path}\n"
        f"[dim]Pruned {pruned} backup(s) older than {args.keep_days} days[/dim]"
    )


# =============================================================================
# `init`
# =============================================================================
def cmd_init(args: argparse.Namespace) -> None:
    db.init_db()
    _console().print(f"[green]Database initialised:[/green] {db.DB_PATH}")


# =============================================================================
# `apply <job_id>` — record that Sourabh applied to a job.
# This is what gives Phase 7's Gmail watcher something to match against.
# =============================================================================
def cmd_apply(args: argparse.Namespace) -> None:
    console = _console()
    job = db.get_job(args.job_id)
    if not job:
        console.print(f"[red]No job with job_id={args.job_id!r}. Use `db_viewer jobs` to list.[/red]")
        return
    # Prefer the tailored resume that the pipeline already produced for
    # this job; falls back to None which Phase 7 still handles fine.
    resume_path = job.get("resume_version_used")
    app_id = db.record_application(
        args.job_id,
        resume_path=resume_path,
        notes=args.notes,
    )
    console.print(
        f"[green]Recorded application:[/green] application_id={app_id}\n"
        f"  Job:     {job['title']} @ {job['company']}\n"
        f"  Resume:  {resume_path or '(none — Phase 4 not run for this job)'}\n"
        f"  Status:  applied  (gmail_watcher will track replies from now on)"
    )


# =============================================================================
# `import-today` — backfill the DB with today's JSON files (raw + scored)
# =============================================================================
def cmd_import_today(args: argparse.Namespace) -> None:
    console = _console()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_path = DATA_DIR / f"raw_jobs_{today}.json"
    scored_path = DATA_DIR / f"scored_jobs_{today}.json"

    if raw_path.exists():
        inserted, skipped = db.import_raw_jobs_json(raw_path)
        console.print(f"[green]Imported raw_jobs:[/green] inserted={inserted} skipped={skipped}")
    else:
        console.print(f"[yellow]No raw_jobs file for {today}[/yellow]")

    if scored_path.exists():
        n = db.import_scored_jobs_json(scored_path)
        console.print(f"[green]Imported scored_jobs:[/green] {n} job scores applied")
    else:
        console.print(f"[yellow]No scored_jobs file for {today}[/yellow]")


# =============================================================================
# CLI plumbing
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only viewer for the Job Hunt Agent SQLite database.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Pipeline overview (jobs, apps, recent runs).")

    p_jobs = sub.add_parser("jobs", help="List jobs.")
    p_jobs.add_argument("--status", choices=[
        "new", "applied", "shortlisted", "interviewing", "rejected", "ghosted", "offer",
    ])
    p_jobs.add_argument("--score-min", type=int, help="Minimum match_score.")
    p_jobs.add_argument("--limit", type=int, default=50)

    p_job = sub.add_parser("job", help="Show one job's full record.")
    p_job.add_argument("job_id")

    p_apps = sub.add_parser("apps", help="List applications.")
    p_apps.add_argument("--status", choices=[
        "applied", "shortlisted", "interviewing", "rejected", "ghosted", "offer",
    ])
    p_apps.add_argument("--limit", type=int, default=50)

    p_emails = sub.add_parser("emails", help="List recent email events.")
    p_emails.add_argument("--limit", type=int, default=20)

    p_runs = sub.add_parser("runs", help="List daily run summaries.")
    p_runs.add_argument("--limit", type=int, default=20)

    p_memory = sub.add_parser("memory", help="List memory entries.")
    p_memory.add_argument("--category", help="Filter to one category.")

    p_backup = sub.add_parser("backup", help="Snapshot DB + prune old backups.")
    p_backup.add_argument("--keep-days", type=int, default=30)

    sub.add_parser("init", help="Create schema if missing.")
    sub.add_parser("import-today", help="Backfill DB with today's raw + scored JSON files.")

    p_apply = sub.add_parser("apply", help="Record that you applied to a job (so gmail_watcher tracks replies).")
    p_apply.add_argument("job_id", help="Job ID from `db_viewer jobs` listing.")
    p_apply.add_argument("--notes", help="Optional free-form note about this application.")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )

    handlers = {
        "status": cmd_status, "jobs": cmd_jobs, "job": cmd_job, "apps": cmd_apps,
        "emails": cmd_emails, "runs": cmd_runs, "memory": cmd_memory,
        "backup": cmd_backup, "init": cmd_init,
        "import-today": cmd_import_today,
        "apply": cmd_apply,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
