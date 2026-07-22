# Job Hunt Agent

An autonomous, self-hosted job-search pipeline. Every morning at 08:00 IST it fetches
fresh job postings, scores them against your resume with an LLM, tailors a resume + PDF
for the best matches, watches your inbox for recruiter replies, generates an interview
quiz, learns from what worked, and emails you a single digest.

It runs on GitHub Actions (free tier) and a free LLM API key. Steady-state cost: **$0/month**.

```
fetch → filter → dedupe → score → tailor → PDF → gmail watch → quiz → learn → digest → backup
```

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Running it](#running-it)
- [The daily loop](#the-daily-loop-what-you-actually-do)
- [Data model](#data-model)
- [Project layout](#project-layout)
- [Cost and quotas](#cost-and-quotas)
- [Troubleshooting](#troubleshooting)
- [Extending](#extending)

---

## What it does

| # | Phase | Module | What happens |
|---|-------|--------|--------------|
| 1 | Resume parsing | [resume_parser.py](src/resume_parser.py) | Reads `data/Latest_Resume.pdf` → `resume.md` (for LLM prompts) + `resume.json` (structured, for safe tailoring). |
| 2 | Fetch | [fetcher.py](src/fetcher.py), [src/sources/](src/sources/) | Pulls postings from RemoteOK, Himalayas, Adzuna, and (optionally) JSearch. Normalizes everything to one `Job` model. |
| — | Filter + dedupe | [filters.py](src/filters.py), [dedupe.py](src/dedupe.py) | Drops stale/irrelevant/out-of-range postings before spending tokens; merges the same job posted to multiple boards. Typically culls 70–80% of the raw pool. |
| 3 | Score | [scorer.py](src/scorer.py) | Sends each surviving job + your resume to the LLM. Gets back a 0–100 match score, fit reasons, gaps, recommended action, and confidence. |
| 4 | Tailor | [tailor.py](src/tailor.py) | For the top-N jobs, rewrites the *editable* resume sections against that JD. Locked fields (company names, dates, education, project names) are structurally unreachable by the model. |
| 5 | PDF | [pdf_generator.py](src/pdf_generator.py) | Renders each tailored markdown resume to a single-column, ATS-friendly PDF via WeasyPrint. |
| 6 | Persist | [db.py](src/db.py), [db_viewer.py](src/db_viewer.py) | SQLite (5 tables, raw `sqlite3`, WAL mode). `db_viewer` is a read-only `rich` CLI over it. |
| 7 | Gmail watcher | [gmail_watcher.py](src/gmail_watcher.py), [gmail_client.py](src/gmail_client.py) | Reads recent mail, matches it to your applications, classifies each with the LLM (shortlisted / rejected / interview_scheduled / …), updates statuses, and sends a high-priority alert on good news. Flags silent applications as `ghosted`. |
| 8 | Quiz | [quiz_generator.py](src/quiz_generator.py) | Generates a mock-interview quiz (5 technical + 2 behavioural + 1 system design) from today's top-3 JDs, deduped against every question ever asked. |
| 9 | Learn | [memory_engine.py](src/memory_engine.py) | Analyzers over your history: which sources yield the best matches, per-company response rates, recurring skill gaps, which resume versions reached an interview. Plus a weekly markdown intelligence report. |
| 10 | Digest | [digest.py](src/digest.py) | One plain-text morning email: today's top jobs, status changes in the last 24h, the quiz, the top memory insight, token usage. Drafts (never auto-sends) polite follow-ups for ghosted applications. |
| 11 | Cron | [.github/workflows/daily_job_hunt.yml](.github/workflows/daily_job_hunt.yml) | Runs the whole thing on GitHub Actions at 02:30 UTC and commits the DB + reports back to the repo so state survives between runs. |

Everything is orchestrated by [scripts/run_pipeline.py](scripts/run_pipeline.py).

## Architecture

**Phase ordering and failure policy.** `fetcher` and `scorer` are *required* — if either
fails the run aborts, emails you a traceback, and exits 1. Everything else is best-effort:
a failure is logged with a full traceback and the pipeline continues. The digest always
attempts to send, so a partial day still reaches your inbox.

**Provider-agnostic LLM.** [llm_client.py](src/llm_client.py) hides the difference between
Gemini and Anthropic behind one `complete_json(system, user, schema)` call that returns a
validated Pydantic object. Switching providers is a one-line `.env` change — both SDKs are
already in `requirements.txt`, and both have first-class JSON-schema support (Gemini's
`response_schema`, Anthropic's tool-use).

**Token safety.** Every LLM call appends a JSONL record to `logs/token_usage.jsonl`
([token_log.py](src/token_log.py)). If a day exceeds `DAILY_TOKEN_CAP`, the pipeline halts
and emails you. It's a brake, not a budget.

**State.** SQLite at `data/job_hunt.db`, committed back to the repo by the workflow so
memory, applications, and email history persist across ephemeral CI runners. Snapshots
land in `data/backups/`.

## Quick start

Requires **Python 3.11 or 3.12** (3.14 mostly works; `weasyprint` and `pdfplumber` are the
likely wheel-build casualties).

```bash
git clone <your-repo-url> job-hunt-agent
cd job-hunt-agent

python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

WeasyPrint needs the GTK/Pango stack:

- **Linux:** `sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2`
- **macOS:** `brew install pango cairo`
- **Windows:** [GTK for Windows Runtime installer](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases)

Then:

```bash
cp .env.example .env               # then fill in your keys — every field has a "how to get it" comment
cp /path/to/your/resume.pdf data/Latest_Resume.pdf

python -m src.resume_parser        # → data/resume.md + data/resume.json
python -m src.db_viewer init       # create the SQLite schema
python scripts/run_pipeline.py --dry-run
```

The dry run makes no LLM calls, writes no files, and sends no email. If all nine phases
report `ok`, you're wired up correctly.

**Minimum viable secrets:** a `GEMINI_API_KEY` ([free, 1500 req/day](https://aistudio.google.com/app/apikey))
and Gmail OAuth credentials. RemoteOK and Himalayas need no keys at all, so you can start
fetching before signing up for anything else.

### Gmail OAuth (one time)

1. [Google Cloud Console](https://console.cloud.google.com/) → new project → enable the **Gmail API**.
2. OAuth consent screen → External → add yourself as a Test User → scopes `gmail.modify`.
3. Credentials → Create OAuth client ID → **Desktop app** → download → save as `credentials.json` in the project root.
4. Run `python -m src.gmail_watcher` once. A browser opens; consent. A `token.json` refresh token is cached.

`credentials.json`, `token.json`, and `.env` are all gitignored. Verify before your first push:

```bash
git check-ignore .env credentials.json token.json   # should print all three
```

### Deploying the cron

See **[SETUP.md](SETUP.md)** for the full walkthrough — private repo, Actions secrets,
manual test trigger, and the weekly `token.json` refresh chore. In short: push to a
**private** repo, add `GEMINI_API_KEY` / `GMAIL_CREDENTIALS_JSON` / `GMAIL_TOKEN_JSON` /
`DIGEST_RECIPIENT_EMAIL` as Actions secrets, and trigger the workflow manually with
`dry_run: true` before letting the schedule take over.

## Configuration

Two places, with a clean split:

- **[config.py](config.py)** — everything stable and repo-wide: paths, candidate profile,
  source toggles and per-source API budgets, filter rules, scoring weights, model
  selection, pricing table, locked resume fields, schedule. Committed to git. If you want
  to change *how the system behaves*, start here.
- **`.env`** — secrets and per-machine values (API keys, digest recipient) plus a handful
  of runtime overrides. Never committed. See [.env.example](.env.example), which documents
  how to obtain every key.

Knobs you're most likely to touch:

| Setting | Where | Default | Meaning |
|---|---|---|---|
| `LLM_PROVIDER` | `.env` | `gemini` | `gemini` (free) or `anthropic` (paid, better tailoring). |
| `MIN_MATCH_SCORE` | `.env` | `60` | Score floor for a job to reach the digest. |
| `TOP_JOBS_PER_DAY` | `.env` | `10` | How many resumes get tailored per day. |
| `GHOSTED_THRESHOLD_DAYS` | `.env` | `14` | Silence after applying before a job is flagged `ghosted`. |
| `DAILY_TOKEN_CAP` | `.env` | `500000` | Safety brake; pipeline halts and emails you if exceeded. |
| `SOURCES` | `config.py` | remoteok, himalayas, adzuna on; jsearch off | Per-source on/off without deleting integration code. |
| `CANDIDATE_*`, `PRIMARY_SKILLS`, `MIN_SALARY_*` | `config.py` | — | Your profile. Feeds the scorer prompt and the pre-filters. |
| `SCHEDULE_CRON_UTC` | `config.py` + workflow | `30 2 * * *` | 08:00 IST. Change both places if you move it. |

Importing `config` also creates every runtime directory it references, so downstream code
can assume paths exist. Call `config.validate_config()` to check for missing required keys
and a missing source resume — it raises `SystemExit` with the full list of problems.

## Running it

The full pipeline:

```bash
python scripts/run_pipeline.py                      # everything
python scripts/run_pipeline.py --dry-run            # no LLM, no email, no writes
python scripts/run_pipeline.py --no-email           # run for real, skip the digest send
python scripts/run_pipeline.py --skip pdf_generator --skip gmail_watcher
python scripts/run_pipeline.py -v                   # debug logging
```

Any phase also runs standalone — useful when iterating on one stage:

```bash
python -m src.fetcher --dry-run                     # fetch + filter + dedupe only
python -m src.scorer --input data/raw_jobs_2026-07-22.json
python -m src.tailor --top-only                     # tailor just the #1 job
python -m src.pdf_generator --date 2026-07-22
python -m src.gmail_watcher --skip-llm --lookback 7
python -m src.quiz_generator generate
python -m src.digest --dry-run
```

Inspecting state ([db_viewer.py](src/db_viewer.py)) — the query commands are read-only;
`init`, `backup`, `apply`, and `import-today` write:

```bash
python -m src.db_viewer status                      # one-screen overview
python -m src.db_viewer jobs --score-min 70
python -m src.db_viewer apps --status shortlisted
python -m src.db_viewer emails
python -m src.db_viewer runs                        # per-phase results of recent runs
python -m src.db_viewer memory --category source_quality
python -m src.db_viewer job <job_id>                # full record
python -m src.db_viewer backup                      # snapshot + prune old backups
```

Learning loop ([memory_engine.py](src/memory_engine.py)):

```bash
python -m src.memory_engine analyze                 # refresh derived insights
python -m src.memory_engine pending                 # outstanding feedback prompts
python -m src.memory_engine feedback                # answer them interactively
python -m src.memory_engine weekly-report           # → reports/weekly_*.md
```

## The daily loop (what you actually do)

1. **Read the digest email** that lands at ~08:00 IST.
2. For jobs worth pursuing: click through, apply, then record it so the watcher tracks replies:
   ```bash
   python -m src.db_viewer apply <job_id>
   ```
3. **Work the quiz** in `quizzes/quiz_<today>.md`, then mark each question:
   ```bash
   python -m src.quiz_generator list
   python -m src.quiz_generator mark 3 struggled     # easy | struggled | nailed
   ```
4. When a recruiter replies, the next run classifies it. Shortlist and interview signals
   trigger an immediate alert email rather than waiting for tomorrow's digest.
5. Answer feedback prompts (`memory_engine feedback`) after any status change — that's the
   signal the learning engine actually improves on.
6. Weekly, skim `reports/weekly_*.md`.

If you're running the cron, `git pull` before working locally — the bot commits the DB,
quizzes, and reports back to `main` after each successful run.

## Data model

The `Job` model in [models.py](src/models.py) is the spine: every source fetcher returns
`list[Job]`, and every downstream stage consumes and emits `Job`. Adding a field there
ripples through the whole pipeline.

SQLite schema ([db.py](src/db.py)):

| Table | Holds |
|---|---|
| `jobs` | Every fetched posting plus its score, reasons, gaps, and current status. |
| `applications` | Jobs you actually applied to, with status transitions and resume paths. |
| `email_events` | Each classified inbound message, linked to its application. |
| `memory` | Derived insights and captured feedback, keyed by category. |
| `daily_runs` | Per-run summary: which phases ran, how long, what failed. |

Intermediate artifacts land in `data/raw_jobs_<date>.json` and `data/scored_jobs_<date>.json`,
so you can re-run a later phase without paying for the earlier ones again.

## Project layout

```
config.py                  All tunable settings. Start here.
scripts/run_pipeline.py    The daily orchestrator / cron entry point.
src/
  models.py                Pydantic Job / Resume / ScoredJob models.
  llm_client.py            Provider-agnostic LLM wrapper (Gemini | Anthropic).
  token_log.py             JSONL usage log + daily cap enforcement.
  resume_parser.py         PDF → resume.md + resume.json.
  sources/                 One module per job board (base.py has shared HTTP/retry).
  fetcher.py               Runs sources, normalizes, filters, dedupes.
  filters.py, dedupe.py    Pre-scorer culling and cross-source merge.
  scorer.py                LLM match scoring.
  tailor.py                Per-JD resume rewriting with locked-field safety.
  pdf_generator.py         Markdown → ATS-friendly PDF (WeasyPrint).
  db.py, db_viewer.py      SQLite persistence + read-only CLI.
  gmail_client.py          Gmail API wrapper (OAuth, send, search).
  gmail_watcher.py         Classify replies, update statuses, alert, flag ghosted.
  quiz_generator.py        Daily mock-interview quiz + self-evaluation.
  memory_engine.py         Analyzers, feedback capture, weekly report.
  digest.py                The morning email.
data/       resume artifacts, raw/scored JSON, job_hunt.db, backups/
resumes/    tailored/*.md  and  pdf/*.pdf
quizzes/    quiz_<date>.md
reports/    weekly_<date>.md
logs/       token_usage.jsonl, run logs
```

## Cost and quotas

| Service | Free tier | How the pipeline stays inside it |
|---|---|---|
| Gemini 2.5 Flash | 1500 req/day, 5 RPM | Pre-filters cut 70–80% of jobs before scoring; requests are rate-limited to `GEMINI_RPM`. |
| RemoteOK / Himalayas | Unlimited, no auth | Himalayas pagination stops as soon as postings fall outside the age window. |
| Adzuna | 1000 calls/month | `ADZUNA_MAX_CALLS_PER_RUN` × `ADZUNA_COUNTRIES` — defaults to ~360/month. |
| JSearch (RapidAPI) | 150 calls/month | Disabled by default; when on, exactly one high-value query per day. |
| GitHub Actions | 2000 min/month private | A run takes ~10 min → ~300 min/month. |
| Gmail API | Generous | A few dozen calls per run. |

Switching to Anthropic (`LLM_PROVIDER=anthropic`) with the default Haiku-for-scoring /
Sonnet-for-tailoring split costs roughly $6–10/month and noticeably improves tailoring
quality. Actual spend is tracked per call in `logs/token_usage.jsonl` and summarized in
each digest.

## Troubleshooting

**`invalid_grant` / "Token has been expired"** — Gmail refresh tokens for unverified
Test-User OAuth apps expire about every 7 days. Re-run `python -m src.gmail_watcher`
locally to re-auth, then paste the new `token.json` into the `GMAIL_TOKEN_JSON` Actions
secret. [Submitting the app for verification](https://support.google.com/cloud/answer/13463073)
removes the expiry permanently.

**WeasyPrint import or render errors** — GTK/Pango isn't installed. See
[Quick start](#quick-start). To keep the pipeline running meanwhile:
`python scripts/run_pipeline.py --skip pdf_generator`.

**"GEMINI_API_KEY is missing" in the scorer phase** — the secret isn't set, or the workflow's
`.env` step didn't pick it up. Check the exact secret name against
[SETUP.md](SETUP.md#step-4--add-actions-secrets).

**Zero jobs after filtering** — normal on a quiet day, suspicious two days running. Run
`python -m src.fetcher --verbose`; each filter logs its own drop count, so you can see which
rule is over-culling. `MAX_JOB_AGE_DAYS` and the keyword list in `config.py` are the usual
suspects.

**A phase failed but the run went green** — by design; only `fetcher` and `scorer` are fatal.
`python -m src.db_viewer runs` shows exactly which phase failed and why.

**Rate-limit / 429 from a source** — sources retry with exponential backoff
([sources/base.py](src/sources/base.py)) and give up gracefully. One bad source doesn't
sink the run.

## Extending

**Add a job source:** create `src/sources/<name>.py` exporting `fetch() -> list[Job]`
(use the helpers in [base.py](src/sources/base.py) for HTTP, retries, and date
normalization), add the name to the `SourceName` literal in [models.py](src/models.py),
register it in `SOURCES` in [config.py](config.py), and import it in
[fetcher.py](src/fetcher.py).

**Add an LLM provider:** implement the `complete_json()` path in
[llm_client.py](src/llm_client.py) and add pricing to `LLM_PRICING_USD_PER_M_TOKENS`.

**Add a pipeline phase:** write `run(dry_run: bool = False)` in a new `src/` module and add
it to `_build_phases()` in [scripts/run_pipeline.py](scripts/run_pipeline.py) with
`{"required": False}`.

**Change what the tailor may rewrite:** `LOCKED_RESUME_FIELDS` in [config.py](config.py).
The safety property is structural — the LLM only ever emits editable fields, and the
renderer plugs them into a template that hard-codes the locked parts from `resume.json`.
Even a fully hallucinating model cannot alter a date or employer.

## License

MIT — see [LICENSE](LICENSE).
