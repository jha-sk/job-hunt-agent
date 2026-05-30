"""
src/db.py — Job Hunt Agent · Phase 6 SQLite persistence.

WHY SQLITE, WHY RAW SQL
-----------------------
- SQLite: zero-config, file-based, ACID, perfect for single-process
  daily-cron + occasional local-tool reads. No server to run.
- Raw `sqlite3` stdlib (no SQLAlchemy/peewee): the schema is small and
  fixed, queries are simple CRUD, an ORM would add complexity without
  saving lines.

WHAT'S HERE
-----------
- Schema (5 tables: jobs, applications, email_events, memory, daily_runs)
- A connection helper with WAL mode + foreign keys ON
- CRUD per table — typed where helpful, returning dicts elsewhere
- Import-from-JSON helpers so we can backfill today's raw_jobs +
  scored_jobs without re-running the LLM pipeline
- Backup helper — atomic online backup via sqlite3's .backup() API

THREAD SAFETY
-------------
sqlite3.Connection objects are NOT shareable across threads. We open
a fresh connection per public function and close it. WAL mode lets
the digest viewer read while the cron writes without locking.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from config import BACKUPS_DIR, DB_PATH

log = logging.getLogger(__name__)


# =============================================================================
# Schema
# =============================================================================
# All timestamps are ISO-8601 UTC strings (e.g. "2026-05-30T08:00:00+00:00").
# We use TEXT for them rather than relying on SQLite's date functions because
# Python's datetime round-trips reliably and we can format-compare lexically.
SCHEMA_SQL = """
-- ===============  jobs  ==============================================
-- One row per (source, job_id). Fetcher INSERT OR IGNOREs; scorer/tailor
-- UPDATEs subsequent fields. The 'raw' column stores the original source
-- record as JSON, for debugging and for future memory-engine signals.
CREATE TABLE IF NOT EXISTS jobs (
    job_id              TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    company             TEXT NOT NULL,
    location            TEXT,
    jd_text             TEXT,
    apply_url           TEXT NOT NULL,
    source              TEXT NOT NULL,
    posted_at           TEXT NOT NULL,
    fetched_at          TEXT NOT NULL,

    salary_min          REAL,
    salary_max          REAL,
    salary_currency     TEXT,
    employment_type     TEXT,
    seniority_hint      TEXT,
    tags                TEXT,            -- JSON list
    raw                 TEXT,            -- JSON of source's original record

    -- Phase 3 (scorer) fields. NULL until scored.
    match_score         INTEGER CHECK (match_score IS NULL OR (match_score BETWEEN 0 AND 100)),
    recommended_action  TEXT    CHECK (recommended_action IS NULL OR recommended_action IN ('Apply','Skip','Apply with note')),
    confidence          TEXT    CHECK (confidence IS NULL OR confidence IN ('High','Medium','Low')),
    score_reasoning     TEXT,
    reasons_for_fit     TEXT,            -- JSON list
    gaps                TEXT,            -- JSON list
    scored_at           TEXT,

    -- Phase 4/5 (tailor + pdf) fields.
    resume_version_used TEXT,            -- path to tailored .md
    pdf_path            TEXT,            -- path to PDF
    tailored_at         TEXT,

    -- Phase 7 (Gmail watcher) maintains these.
    status              TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new','applied','shortlisted','interviewing','rejected','ghosted','offer')),
    applied_at          TEXT,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score    ON jobs(match_score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company  ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_fetched  ON jobs(fetched_at DESC);


-- ===============  applications  =======================================
-- One row per APPLICATION attempt. A job can have multiple application
-- records over time (e.g. you re-apply after a year).
CREATE TABLE IF NOT EXISTS applications (
    application_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              TEXT NOT NULL,
    applied_at          TEXT NOT NULL,
    resume_path         TEXT,
    cover_letter_path   TEXT,
    status              TEXT NOT NULL DEFAULT 'applied'
        CHECK (status IN ('applied','shortlisted','interviewing','rejected','ghosted','offer')),
    last_updated        TEXT NOT NULL,
    notes               TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_apps_status   ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_job      ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_apps_updated  ON applications(last_updated DESC);


-- ===============  email_events  =======================================
-- Every classified email — wins, losses, ghost-check pings. Job-linked
-- when we can match company/subject; otherwise job_id stays NULL.
CREATE TABLE IF NOT EXISTS email_events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              TEXT,
    email_subject       TEXT,
    email_from          TEXT,
    received_at         TEXT NOT NULL,
    classified_as       TEXT
        CHECK (classified_as IN ('shortlisted','rejected','interview_scheduled','follow_up_required','ghosted_check','unknown')),
    raw_snippet         TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_email_received ON email_events(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_class    ON email_events(classified_as);


-- ===============  memory  =============================================
-- The learning engine's store. (category, key) is unique; value is
-- typically JSON. category examples: 'source_quality', 'company_response',
-- 'quiz_question_seen', 'feedback_shortlisted', 'pattern_keyword_match'.
CREATE TABLE IF NOT EXISTS memory (
    memory_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category            TEXT NOT NULL,
    key                 TEXT NOT NULL,
    value               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    source              TEXT,
    UNIQUE(category, key)
);

CREATE INDEX IF NOT EXISTS idx_memory_cat ON memory(category);


-- ===============  daily_runs  =========================================
-- One row per calendar day. Updated incrementally as each phase
-- completes during the morning cron run.
CREATE TABLE IF NOT EXISTS daily_runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date            TEXT NOT NULL UNIQUE,
    jobs_fetched        INTEGER DEFAULT 0,
    jobs_scored         INTEGER DEFAULT 0,
    top_jobs_count      INTEGER DEFAULT 0,
    resumes_tailored    INTEGER DEFAULT 0,
    pdfs_generated      INTEGER DEFAULT 0,
    quiz_generated      INTEGER NOT NULL DEFAULT 0,
    digest_sent         INTEGER NOT NULL DEFAULT 0,
    errors              TEXT,
    token_usage         TEXT,
    notes               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
"""


# =============================================================================
# Connection
# =============================================================================
def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """
    Open a connection with the project's standard pragmas:
      - row_factory: rows behave like dicts (row['col_name'])
      - WAL mode: readers don't block writers (digest can read while cron writes)
      - foreign_keys ON: enforce FK constraints (sqlite off by default)
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe, faster than FULL
    return conn


@contextlib.contextmanager
def transaction(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    """
    Wrap a block in a single transaction. Commits on success, rolls
    back on exception. Used for multi-statement updates.
    """
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """Create the schema (idempotent). Safe to call repeatedly."""
    with contextlib.closing(get_conn(db_path)) as conn:
        conn.executescript(SCHEMA_SQL)
    log.info("db: schema initialised at %s", db_path)


# =============================================================================
# Small helpers
# =============================================================================
def _utc_now() -> str:
    """Canonical timestamp string used everywhere."""
    return datetime.now(timezone.utc).isoformat()


def _to_json(value: Any) -> Optional[str]:
    """JSON-encode for TEXT columns; None passes through unchanged."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _from_json(value: Optional[str]) -> Any:
    """JSON-decode a TEXT column. Returns None on null/empty/invalid."""
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    """sqlite3.Row → plain dict. Returns None if row is None."""
    return dict(row) if row is not None else None


# =============================================================================
# jobs CRUD
# =============================================================================
_JOB_INSERT_COLUMNS = (
    "job_id, title, company, location, jd_text, apply_url, source, "
    "posted_at, fetched_at, salary_min, salary_max, salary_currency, "
    "employment_type, seniority_hint, tags, raw"
)
_JOB_INSERT_PLACEHOLDERS = ", ".join(["?"] * 16)


def upsert_job(job: dict, db_path: Path = DB_PATH) -> bool:
    """
    Insert if new, else preserve existing row (we KEEP scoring/tailoring
    fields across re-fetches). Returns True if inserted, False if existing.

    `job` is the dict shape produced by src.models.Job.model_dump(mode='json').
    """
    sql = (
        f"INSERT OR IGNORE INTO jobs ({_JOB_INSERT_COLUMNS}) "
        f"VALUES ({_JOB_INSERT_PLACEHOLDERS})"
    )
    params = (
        job["job_id"], job["title"], job["company"], job.get("location") or "",
        job.get("jd_text") or "", job["apply_url"], job["source"],
        job["posted_at"], job.get("fetched_at") or _utc_now(),
        job.get("salary_min"), job.get("salary_max"), job.get("salary_currency"),
        job.get("employment_type"), job.get("seniority_hint"),
        _to_json(job.get("tags") or []), _to_json(job.get("raw") or {}),
    )
    with contextlib.closing(get_conn(db_path)) as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount > 0


def upsert_jobs_bulk(jobs: list[dict], db_path: Path = DB_PATH) -> tuple[int, int]:
    """
    Bulk-insert a list of jobs in one transaction. Returns (inserted, skipped).
    """
    inserted = skipped = 0
    sql = (
        f"INSERT OR IGNORE INTO jobs ({_JOB_INSERT_COLUMNS}) "
        f"VALUES ({_JOB_INSERT_PLACEHOLDERS})"
    )
    with transaction(db_path) as conn:
        for job in jobs:
            params = (
                job["job_id"], job["title"], job["company"], job.get("location") or "",
                job.get("jd_text") or "", job["apply_url"], job["source"],
                job["posted_at"], job.get("fetched_at") or _utc_now(),
                job.get("salary_min"), job.get("salary_max"), job.get("salary_currency"),
                job.get("employment_type"), job.get("seniority_hint"),
                _to_json(job.get("tags") or []), _to_json(job.get("raw") or {}),
            )
            cur = conn.execute(sql, params)
            if cur.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
    return inserted, skipped


def update_job_score(
    job_id: str,
    *,
    match_score: int,
    recommended_action: str,
    confidence: str,
    score_reasoning: str,
    reasons_for_fit: list[str],
    gaps: list[str],
    db_path: Path = DB_PATH,
) -> None:
    """Save Phase 3 scorer output onto an existing job row."""
    sql = """
        UPDATE jobs SET
            match_score = ?, recommended_action = ?, confidence = ?,
            score_reasoning = ?, reasons_for_fit = ?, gaps = ?, scored_at = ?
        WHERE job_id = ?
    """
    params = (
        match_score, recommended_action, confidence, score_reasoning,
        _to_json(reasons_for_fit), _to_json(gaps), _utc_now(), job_id,
    )
    with contextlib.closing(get_conn(db_path)) as conn:
        conn.execute(sql, params)


def set_resume_paths(
    job_id: str,
    *,
    resume_md_path: Optional[str] = None,
    pdf_path: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> None:
    """
    Record the per-job artifact paths from Phase 4 / Phase 5. Pass only
    the field(s) being updated; the other stays at its current value.
    """
    fields: list[str] = []
    params: list[Any] = []
    if resume_md_path is not None:
        fields.append("resume_version_used = ?")
        fields.append("tailored_at = ?")
        params.extend([resume_md_path, _utc_now()])
    if pdf_path is not None:
        fields.append("pdf_path = ?")
        params.append(pdf_path)
    if not fields:
        return
    params.append(job_id)
    sql = f"UPDATE jobs SET {', '.join(fields)} WHERE job_id = ?"
    with contextlib.closing(get_conn(db_path)) as conn:
        conn.execute(sql, params)


def set_pdf_path_by_md(
    md_path: str,
    pdf_path: str,
    db_path: Path = DB_PATH,
) -> int:
    """
    Set jobs.pdf_path for the row whose resume_version_used == md_path.
    Used by pdf_generator.py which has the .md and .pdf paths but not the
    job_id directly. Returns the affected row count (0 if no match, e.g.
    PDF was generated before the tailor row was written).
    """
    with contextlib.closing(get_conn(db_path)) as conn:
        cur = conn.execute(
            "UPDATE jobs SET pdf_path = ? WHERE resume_version_used = ?",
            (pdf_path, md_path),
        )
        return cur.rowcount


def get_job(job_id: str, db_path: Path = DB_PATH) -> Optional[dict]:
    with contextlib.closing(get_conn(db_path)) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    d = _row_to_dict(row)
    if d:
        # Decode JSON columns for caller convenience.
        for col in ("tags", "raw", "reasons_for_fit", "gaps"):
            d[col] = _from_json(d.get(col))
    return d


def list_jobs(
    *,
    status: Optional[str] = None,
    min_score: Optional[int] = None,
    since: Optional[str] = None,    # ISO date prefix, e.g. "2026-05"
    limit: int = 100,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """List jobs with optional filters. Sorted by score desc, then fetched_at desc."""
    where = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if min_score is not None:
        where.append("match_score >= ?")
        params.append(min_score)
    if since:
        where.append("fetched_at >= ?")
        params.append(since)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT * FROM jobs {where_sql}
        ORDER BY COALESCE(match_score, -1) DESC, fetched_at DESC
        LIMIT ?
    """
    params.append(limit)
    with contextlib.closing(get_conn(db_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
    # Decode JSON-text columns so callers get real list/dict values, not
    # raw JSON strings. (Same as get_job — kept in sync deliberately.)
    out = []
    for r in rows:
        d = _row_to_dict(r)
        if d:
            for col in ("tags", "raw", "reasons_for_fit", "gaps"):
                d[col] = _from_json(d.get(col))
        out.append(d)
    return out


# =============================================================================
# applications CRUD
# =============================================================================
def record_application(
    job_id: str,
    *,
    resume_path: Optional[str] = None,
    cover_letter_path: Optional[str] = None,
    notes: Optional[str] = None,
    applied_at: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> int:
    """Add an application row + flip jobs.status='applied'. Returns application_id."""
    applied_at = applied_at or _utc_now()
    with transaction(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO applications
                (job_id, applied_at, resume_path, cover_letter_path,
                 status, last_updated, notes)
            VALUES (?, ?, ?, ?, 'applied', ?, ?)
            """,
            (job_id, applied_at, resume_path, cover_letter_path, applied_at, notes),
        )
        new_id = cur.lastrowid
        conn.execute(
            "UPDATE jobs SET status='applied', applied_at=? WHERE job_id=?",
            (applied_at, job_id),
        )
    return new_id


def update_application_status(
    application_id: int,
    new_status: str,
    notes: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> None:
    """Move an application through its lifecycle (shortlisted/rejected/etc)."""
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET status=?, last_updated=?, notes=COALESCE(?, notes) "
            "WHERE application_id=?",
            (new_status, _utc_now(), notes, application_id),
        )
        # Mirror onto jobs.status so summary queries on the jobs table stay accurate.
        conn.execute(
            "UPDATE jobs SET status=? "
            "WHERE job_id = (SELECT job_id FROM applications WHERE application_id=?)",
            (new_status, application_id),
        )


def list_applications(
    *, status: Optional[str] = None, limit: int = 200, db_path: Path = DB_PATH,
) -> list[dict]:
    where = "WHERE a.status = ?" if status else ""
    params: list[Any] = [status] if status else []
    sql = f"""
        SELECT a.*, j.title, j.company
        FROM applications a
        LEFT JOIN jobs j ON j.job_id = a.job_id
        {where}
        ORDER BY a.last_updated DESC LIMIT ?
    """
    params.append(limit)
    with contextlib.closing(get_conn(db_path)) as conn:
        return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


# =============================================================================
# email_events CRUD
# =============================================================================
def insert_email_event(
    *,
    job_id: Optional[str],
    email_subject: str,
    email_from: str,
    received_at: str,
    classified_as: str,
    raw_snippet: str,
    db_path: Path = DB_PATH,
) -> int:
    with contextlib.closing(get_conn(db_path)) as conn:
        cur = conn.execute(
            """
            INSERT INTO email_events
                (job_id, email_subject, email_from, received_at,
                 classified_as, raw_snippet)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, email_subject, email_from, received_at, classified_as, raw_snippet),
        )
        return cur.lastrowid


def list_email_events(limit: int = 50, db_path: Path = DB_PATH) -> list[dict]:
    with contextlib.closing(get_conn(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM email_events ORDER BY received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# =============================================================================
# memory CRUD — Phase 9 (learning engine) is the heavy user, but the table
# is wired up now so other phases can write to it as they go.
# =============================================================================
def memory_set(
    category: str, key: str, value: Any,
    *, source: Optional[str] = None, db_path: Path = DB_PATH,
) -> None:
    """Upsert one (category, key, value) entry. value is JSON-encoded."""
    now = _utc_now()
    with contextlib.closing(get_conn(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO memory (category, key, value, created_at, updated_at, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(category, key) DO UPDATE SET
                value      = excluded.value,
                updated_at = excluded.updated_at,
                source     = COALESCE(excluded.source, memory.source)
            """,
            (category, key, _to_json(value), now, now, source),
        )


def memory_get(category: str, key: str, db_path: Path = DB_PATH) -> Any:
    with contextlib.closing(get_conn(db_path)) as conn:
        row = conn.execute(
            "SELECT value FROM memory WHERE category=? AND key=?",
            (category, key),
        ).fetchone()
    return _from_json(row["value"]) if row else None


def memory_list(category: Optional[str] = None, db_path: Path = DB_PATH) -> list[dict]:
    where = "WHERE category = ?" if category else ""
    params: list[Any] = [category] if category else []
    sql = f"SELECT * FROM memory {where} ORDER BY category, key"
    with contextlib.closing(get_conn(db_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
    out = [_row_to_dict(r) for r in rows]
    for d in out:
        d["value"] = _from_json(d["value"])
    return out


# =============================================================================
# daily_runs CRUD
# =============================================================================
def upsert_daily_run(
    run_date: str,
    *,
    jobs_fetched: Optional[int] = None,
    jobs_scored: Optional[int] = None,
    top_jobs_count: Optional[int] = None,
    resumes_tailored: Optional[int] = None,
    pdfs_generated: Optional[int] = None,
    quiz_generated: Optional[bool] = None,
    digest_sent: Optional[bool] = None,
    errors: Optional[str] = None,
    token_usage: Optional[dict] = None,
    notes: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> None:
    """
    Upsert today's run summary. Pass only the fields you're updating —
    everything else is preserved. Each phase calls this as it completes.
    """
    now = _utc_now()
    fields_to_update = {
        "jobs_fetched": jobs_fetched,
        "jobs_scored": jobs_scored,
        "top_jobs_count": top_jobs_count,
        "resumes_tailored": resumes_tailored,
        "pdfs_generated": pdfs_generated,
        "quiz_generated": int(quiz_generated) if quiz_generated is not None else None,
        "digest_sent": int(digest_sent) if digest_sent is not None else None,
        "errors": errors,
        "token_usage": _to_json(token_usage) if token_usage is not None else None,
        "notes": notes,
    }
    # Drop fields the caller didn't set so the UPSERT preserves prior values.
    update_fields = {k: v for k, v in fields_to_update.items() if v is not None}

    insert_cols = list(update_fields.keys()) + ["run_date", "created_at", "updated_at"]
    insert_vals = list(update_fields.values()) + [run_date, now, now]
    placeholders = ", ".join(["?"] * len(insert_vals))
    cols_sql = ", ".join(insert_cols)

    # UPSERT: insert; on conflict on run_date, overwrite only the supplied cols.
    update_sql = ", ".join(f"{k} = excluded.{k}" for k in update_fields)
    update_sql += ", updated_at = excluded.updated_at"

    sql = (
        f"INSERT INTO daily_runs ({cols_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT(run_date) DO UPDATE SET {update_sql}"
    )
    with contextlib.closing(get_conn(db_path)) as conn:
        conn.execute(sql, insert_vals)


def get_daily_run(run_date: str, db_path: Path = DB_PATH) -> Optional[dict]:
    with contextlib.closing(get_conn(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM daily_runs WHERE run_date=?", (run_date,)
        ).fetchone()
    d = _row_to_dict(row)
    if d and d.get("token_usage"):
        d["token_usage"] = _from_json(d["token_usage"])
    return d


def list_daily_runs(limit: int = 30, db_path: Path = DB_PATH) -> list[dict]:
    with contextlib.closing(get_conn(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM daily_runs ORDER BY run_date DESC LIMIT ?", (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# =============================================================================
# Import-from-JSON — backfill the DB without re-running the pipeline.
# =============================================================================
def import_raw_jobs_json(path: Path, db_path: Path = DB_PATH) -> tuple[int, int]:
    """Ingest a `data/raw_jobs_*.json` file. Returns (inserted, skipped)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    jobs = payload.get("jobs") or []
    inserted, skipped = upsert_jobs_bulk(jobs, db_path)
    log.info("db: imported %s — inserted=%d skipped=%d", path.name, inserted, skipped)
    return inserted, skipped


def import_scored_jobs_json(path: Path, db_path: Path = DB_PATH) -> int:
    """
    Ingest a `data/scored_jobs_*.json` file, applying scores to existing
    rows. Returns count of jobs updated. Job must already exist (run
    import_raw_jobs_json first for the same date).
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    scored = payload.get("all_scored") or payload.get("top_n") or []
    updated = 0
    for s in scored:
        job = s["job"]
        score = s["score"]
        # Make sure the underlying job row exists before scoring.
        upsert_job(job, db_path)
        update_job_score(
            job["job_id"],
            match_score=score["match_score"],
            recommended_action=score["recommended_action"],
            confidence=score["confidence"],
            score_reasoning=score["score_reasoning"],
            reasons_for_fit=score.get("reasons_for_fit") or [],
            gaps=score.get("gaps") or [],
            db_path=db_path,
        )
        updated += 1
    log.info("db: imported %s — scores updated=%d", path.name, updated)
    return updated


# =============================================================================
# Backup — atomic, online, uses sqlite3's .backup() API. Safe to run
# while the DB is in use (WAL keeps reads/writes flowing).
# =============================================================================
def backup_db(db_path: Path = DB_PATH, backup_dir: Path = BACKUPS_DIR) -> Path:
    """Snapshot the DB to data/backups/job_hunt_<YYYY-MM-DD>.db. Returns the path."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backup_dir.mkdir(parents=True, exist_ok=True)
    out_path = backup_dir / f"job_hunt_{today}.db"

    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(out_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    log.info("db: backup written to %s (%.1f KB)",
             out_path, out_path.stat().st_size / 1024)
    return out_path


def prune_old_backups(keep_days: int = 30, backup_dir: Path = BACKUPS_DIR) -> int:
    """Delete backups older than keep_days. Returns count deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    deleted = 0
    for p in backup_dir.glob("job_hunt_*.db"):
        try:
            # Parse date from filename: job_hunt_YYYY-MM-DD.db
            date_str = p.stem.replace("job_hunt_", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if file_date < cutoff:
            p.unlink()
            deleted += 1
    if deleted:
        log.info("db: pruned %d backup(s) older than %d days", deleted, keep_days)
    return deleted
