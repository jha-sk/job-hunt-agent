"""
config.py — Centralised configuration for the Job Hunt Agent.

WHY THIS FILE EXISTS
--------------------
Every tunable knob (paths, model names, scoring weights, filter rules, source
toggles, schedule, etc.) lives here. No magic numbers or hardcoded strings
should appear anywhere else in the codebase. If you want to change *anything*
about how the system behaves, start here.

WHAT GOES WHERE
---------------
- Secrets and per-machine values → .env (loaded via python-dotenv).
- Stable, repo-wide settings → this file (committed to git).

If a setting could differ between Sourabh's laptop and the GitHub Actions
runner, it belongs in .env. If it's the same everywhere, it belongs here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

# Load .env from project root. Silently no-ops if .env doesn't exist yet
# (e.g. on first clone before the user has filled in their keys).
load_dotenv()


# =============================================================================
# PATHS
# =============================================================================
# Everything is anchored to PROJECT_ROOT so the pipeline works whether you
# run it from VS Code, a cron job, or GitHub Actions.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent

DATA_DIR: Final[Path] = PROJECT_ROOT / "data"
RESUMES_TAILORED_DIR: Final[Path] = PROJECT_ROOT / "resumes" / "tailored"
RESUMES_PDF_DIR: Final[Path] = PROJECT_ROOT / "resumes" / "pdf"
QUIZZES_DIR: Final[Path] = PROJECT_ROOT / "quizzes"
LOGS_DIR: Final[Path] = PROJECT_ROOT / "logs"
REPORTS_DIR: Final[Path] = PROJECT_ROOT / "reports"
BACKUPS_DIR: Final[Path] = DATA_DIR / "backups"

# Canonical resume artefacts produced by Phase 1.
RESUME_PDF_SOURCE: Final[Path] = DATA_DIR / "Latest_Resume.pdf"
RESUME_MD: Final[Path] = DATA_DIR / "resume.md"
RESUME_JSON: Final[Path] = DATA_DIR / "resume.json"

# SQLite DB (Phase 6).
DB_PATH: Final[Path] = DATA_DIR / "job_hunt.db"

# Google OAuth artefacts (Phase 7). credentials.json is the OAuth client
# secret downloaded from Google Cloud Console; token.json is the cached
# refresh token after first-run consent.
GMAIL_CREDENTIALS_PATH: Final[Path] = PROJECT_ROOT / "credentials.json"
GMAIL_TOKEN_PATH: Final[Path] = PROJECT_ROOT / "token.json"


# =============================================================================
# CANDIDATE PROFILE (Sourabh Jha)
# =============================================================================
# Hard facts about the candidate. Used by the scorer to (a) frame the prompt
# correctly ("don't penalise this candidate for not having 10 years exp") and
# (b) to filter out roles that are categorically out of range.
CANDIDATE_NAME: Final[str] = "Sourabh Jha"
CANDIDATE_EMAIL: Final[str] = "codewithsourabhjha@gmail.com"
CANDIDATE_PHONE: Final[str] = "+91 76939 03439"
CANDIDATE_LINKEDIN: Final[str] = "https://www.linkedin.com/in/sk-jha"
CANDIDATE_GITHUB: Final[str] = "https://github.com/jha-sk"
CANDIDATE_LOCATION: Final[str] = "Gurugram, India"

# Years of professional experience. Used by the scorer prompt so Claude
# doesn't auto-reject roles asking for "5+ years".
CANDIDATE_YEARS_EXPERIENCE: Final[float] = 1.5

# Languages/skills, in priority order. The scorer uses this to weight matches.
PRIMARY_SKILLS: Final[list[str]] = ["Go", "Golang", "Python", "AI", "LLM", "RAG"]
SECONDARY_SKILLS: Final[list[str]] = ["Java", "C++", "TypeScript", "Node.js"]

# Salary floor (Phase 3 scoring + Phase 2 filtering for boards that expose it).
# India is the headline number. USD is the *post-tax* equivalent the system
# negotiates toward for remote-global roles — see notes in scorer.py.
MIN_SALARY_INR_LPA: Final[int] = 12
# 12 LPA INR ≈ $14,400/year gross. Post-Indian-tax it's ~₹10 LPA.
# A US remote contract with no Indian tax withholding needs ~$18-20K/year
# to clear ₹10 LPA in hand. We aim higher (~$30K) since US/EU companies
# usually pay considerably more than that for a backend/AI engineer role.
MIN_SALARY_USD_ANNUAL: Final[int] = 30_000

# Notice period in days — used in the cover letter / digest only.
NOTICE_PERIOD_DAYS: Final[int] = 90  # Negotiable to 30-45.


# =============================================================================
# JOB SOURCES (Phase 2)
# =============================================================================
# Toggle individual sources on/off here. Set to False to skip without
# deleting the integration code.
#
# Truth-table (verified during Phase 2 build):
#  - LinkedIn: removed — public RSS deprecated by LinkedIn years ago;
#              we cover LinkedIn-aggregated postings via JSearch instead.
#  - Wellfound: removed — public RSS killed in 2024; no free path.
#  - Naukri: removed — no public API; covered partially by Adzuna 'in'.
SOURCES: Final[dict[str, bool]] = {
    "remoteok":  True,   # https://remoteok.com/api — free, no auth.
    "himalayas": True,   # https://himalayas.app/jobs/api — free, no auth.
    "adzuna":    True,   # developer.adzuna.com — free 1000 calls/mo. Needs key.
    # JSearch disabled: Sourabh couldn't get a working RapidAPI subscription
    # at Phase 2 wrap-up (2026-05-30). Code is intact under src/sources/jsearch.py
    # — flip this back to True and add RAPIDAPI_KEY to .env to re-enable.
    "jsearch":   False,
}

# Per-source API budgets and pacing. Used to avoid blowing through free
# quotas. Increase these only if you've upgraded to a paid tier.
ADZUNA_MAX_CALLS_PER_RUN: Final[int] = 12   # ≈ 360/mo at 1 run/day
JSEARCH_MAX_CALLS_PER_RUN: Final[int] = 1   # ≈ 30/mo, well under 150 cap

# Keywords used by the PRE-LLM filter to decide "is this job possibly
# relevant". Match is case-insensitive, word-boundary. Be permissive here;
# the LLM scorer in Phase 3 does the real judging. False-positives are
# cheap (slightly more LLM tokens); false-negatives are expensive (you
# miss a great job).
#
# Sourabh-confirmed in Phase 1 wrap-up: include the AI/ML expansion
# (ML engineer, MLOps, GenAI, Agentic, Prompt engineer, Foundation models).
JOB_KEYWORDS: Final[list[str]] = [
    # Go ecosystem
    "golang", "go developer", "go engineer", "go programmer", "go backend",
    # Python ecosystem
    "python", "python developer", "python engineer", "python backend",
    # Generic backend / infra (broad — pre-filter only)
    "backend", "back-end", "back end", "platform engineer",
    # AI / LLM core
    "ai engineer", "ai generalist", "ai orchestrator", "llm", "rag",
    "vector database", "vector db",
    # AI/ML expansion
    "ml engineer", "machine learning engineer", "mlops", "genai", "gen ai",
    "agentic", "prompt engineer", "foundation model",
]

# Job types to exclude during the pre-LLM filter pass. Sourabh chose to
# exclude BOTH contracts and internships. Matched case-insensitively as
# substrings of the job title OR JD text.
EXCLUDED_JOB_TYPES: Final[list[str]] = [
    "intern",
    "internship",
    "contract",
    "contractor",
    "contract-to-hire",
    "freelance",
    "part-time",
    "part time",
]

# Locations the fetcher accepts. Anything matching one of these strings
# (case-insensitive substring) is kept.
ACCEPTED_LOCATIONS: Final[list[str]] = [
    "remote",
    "gurugram",
    "gurgaon",
    "india",
    "worldwide",
    "global",
    "anywhere",
]

# Job posting age cutoff. Anything older is dropped. Master prompt asked
# for 24h (1 day). Overridable via .env (useful for catch-up runs after
# a long weekend, or to widen to 2-3 days if 24h yields too few jobs).
MAX_JOB_AGE_DAYS: Final[int] = int(os.getenv("MAX_JOB_AGE_DAYS", "1"))

# Experience ceiling. If a JD demands more years than this, drop the job
# during the pre-LLM filter pass (saves tokens).
MAX_REQUIRED_YEARS_EXP: Final[int] = 4

# Hard exclude list. Confirmed with Sourabh: he wants to deprioritise
# service-based shops UNLESS they meet the 12 LPA floor. The list below is
# a starting set — they're filtered ONLY when the JD's stated salary is
# below MIN_SALARY_INR_LPA. If the salary meets the floor or is unstated,
# the job is kept and the scorer judges it on merits.
SERVICE_BASED_COMPANIES: Final[list[str]] = [
    "tcs", "tata consultancy",
    "infosys", "wipro", "cognizant",
    "capgemini", "hcl technologies", "hcl tech",
    "tech mahindra", "ltimindtree", "ltimindtree", "mphasis",
    "deloitte", "ey", "ernst & young", "pwc", "kpmg",
    "ibm consulting", "dxc", "ust global",
    # Accenture itself — never surface your current employer.
    "accenture",
]

# Companies that should get a score boost (Sourabh said: "startups mostly").
# Startups are detected by keyword/source heuristics in the scorer; this
# list is for SPECIFIC named companies you want prioritised. Add as you go.
PRIORITY_COMPANIES: Final[list[str]] = []

# Score boost (added to base score) for companies that match a priority
# keyword like "startup", "seed", "Series A", or a name in PRIORITY_COMPANIES.
PRIORITY_SCORE_BOOST: Final[int] = 5


# =============================================================================
# RESUME TAILORING RULES (Phase 4)
# =============================================================================
# Sections of resume.json the tailor is NEVER allowed to modify.
# Confirmed with Sourabh: education, certifications, company names,
# employment dates, and project NAMES are locked. The tailor may rewrite
# project descriptions, reorder bullets, and refresh skills framing.
LOCKED_RESUME_FIELDS: Final[list[str]] = [
    "education",
    "certifications",
    "experience.company",
    "experience.dates",
    "experience.title",
    "projects.name",     # Project descriptions/stack/bullets ARE editable.
]


# =============================================================================
# LLM PROVIDER + COST CONTROLS
# =============================================================================
# Provider switch comes from .env. Sourabh chose Gemini Free at Phase 1
# wrap-up so the default is gemini. To upgrade to paid Anthropic, set
# LLM_PROVIDER=anthropic in .env and supply ANTHROPIC_API_KEY.
LLM_PROVIDER: Final[str] = os.getenv("LLM_PROVIDER", "gemini").lower()

# Per-model name resolution. .env can override either entry independently.
MODEL_SCORER: Final[str] = (
    os.getenv("ANTHROPIC_MODEL_SCORER", "claude-haiku-4-5-20251001")
    if LLM_PROVIDER == "anthropic"
    else os.getenv("GEMINI_MODEL_SCORER", "gemini-2.5-flash")
)
MODEL_TAILOR: Final[str] = (
    os.getenv("ANTHROPIC_MODEL_TAILOR", "claude-sonnet-4-6")
    if LLM_PROVIDER == "anthropic"
    # gemini-2.5-flash-lite (NOT flash) because the scorer also uses
    # flash and both share its 20 RPD free-tier quota on Sourabh's
    # project. Separate model = separate daily quota bucket.
    else os.getenv("GEMINI_MODEL_TAILOR", "gemini-2.5-flash-lite")
)
MODEL_QUIZ: Final[str] = os.getenv("ANTHROPIC_MODEL_QUIZ", "claude-sonnet-4-6")
MODEL_GMAIL: Final[str] = os.getenv("ANTHROPIC_MODEL_GMAIL", "claude-haiku-4-5-20251001")

# Source API keys (.env). Empty string = source is "not configured" and
# its fetcher will skip with a log line rather than crashing.
ADZUNA_APP_ID: Final[str] = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY: Final[str] = os.getenv("ADZUNA_APP_KEY", "")
ADZUNA_COUNTRIES: Final[list[str]] = [
    c.strip().lower() for c in os.getenv("ADZUNA_COUNTRIES", "in,us").split(",") if c.strip()
]
RAPIDAPI_KEY: Final[str] = os.getenv("RAPIDAPI_KEY", "")
JSEARCH_DAILY_QUERY: Final[str] = os.getenv("JSEARCH_DAILY_QUERY", "AI engineer remote")

# Daily safety cap. If today's total input+output tokens exceed this, the
# pipeline halts and emails you. This is NOT a budget — it's a runaway brake.
DAILY_TOKEN_CAP: Final[int] = int(os.getenv("DAILY_TOKEN_CAP", "500000"))

# Where the per-run token tally is written. One JSONL line per LLM call.
TOKEN_USAGE_LOG: Final[Path] = LOGS_DIR / "token_usage.jsonl"

# Provider rate limits (requests per minute). Verified during Phase 3
# build (2026-05-30) for gemini-2.5-flash free tier on Sourabh's project:
# 5 RPM, ~250 RPD. Anthropic varies by tier — paid Hybrid starts ~60 RPM.
GEMINI_RPM: Final[int] = int(os.getenv("GEMINI_RPM", "5"))
ANTHROPIC_RPM: Final[int] = int(os.getenv("ANTHROPIC_RPM", "60"))

# Phase 3 scorer settings.
SCORER_TEMPERATURE: Final[float] = 0.0    # deterministic per master prompt
# 1024 is enough headroom for JobScore JSON even with verbose reasons.
# Was 512; got truncated mid-JSON on Gemini 2.5 because thinking tokens
# count toward this budget. We also disable thinking in llm_client.py.
SCORER_MAX_OUTPUT_TOKENS: Final[int] = 1024

# Per-token pricing (USD per million tokens). Used to estimate $ cost in
# token_usage logs. Free tiers store 0.00 — useful for the daily digest
# "X tokens used, $0 spent" line. Update if Anthropic publishes new rates.
LLM_PRICING_USD_PER_M_TOKENS: Final[dict[str, dict[str, float]]] = {
    # Gemini free-tier entries — cost 0.0 because we stay under the
    # free-tier RPD cap. Update to paid rates if you enable GCP billing.
    "gemini-2.5-flash":            {"input": 0.0, "output": 0.0},
    "gemini-2.5-flash-lite":       {"input": 0.0, "output": 0.0},
    "gemini-flash-latest":         {"input": 0.0, "output": 0.0},
    # Anthropic — actual list-price (no free tier; $5 starter credit only).
    "claude-haiku-4-5-20251001":   {"input": 0.80, "output": 4.0},
    "claude-sonnet-4-6":           {"input": 3.0,  "output": 15.0},
    "claude-opus-4-7":             {"input": 15.0, "output": 75.0},
}


# =============================================================================
# PIPELINE BEHAVIOUR (overridable via .env)
# =============================================================================
MIN_MATCH_SCORE: Final[int] = int(os.getenv("MIN_MATCH_SCORE", "60"))
TOP_JOBS_PER_DAY: Final[int] = int(os.getenv("TOP_JOBS_PER_DAY", "10"))
GHOSTED_THRESHOLD_DAYS: Final[int] = int(os.getenv("GHOSTED_THRESHOLD_DAYS", "14"))

# Phase 7: auto-draft follow-up emails for ghosted apps? Sourabh said yes.
# Drafts are saved to Gmail Drafts folder — never auto-sent.
AUTO_DRAFT_FOLLOWUPS: Final[bool] = True

# Phase 7: how far back to scan Gmail each run. 30 days covers all
# applications that could still be "live" (within the 14-day ghost
# threshold + some buffer for slow ATS responses).
GMAIL_LOOKBACK_DAYS: Final[int] = int(os.getenv("GMAIL_LOOKBACK_DAYS", "30"))

# Phase 7: OAuth scopes. modify covers read + send + drafts + labels —
# everything Phase 7 and Phase 10 need. Less granular than separate
# scopes but means one consent screen, one token file.
GMAIL_SCOPES: Final[list[str]] = [
    "https://www.googleapis.com/auth/gmail.modify",
]

# Phase 8: quiz delivery. Sourabh said both. Local file always written;
# this flag controls whether the digest email embeds the quiz.
QUIZ_EMAIL_INCLUDED: Final[bool] = True

# Phase 10: also print the digest to terminal when run locally. Sourabh
# said yes. Has no effect under GitHub Actions (stdout goes to the log).
DIGEST_TERMINAL_MIRROR: Final[bool] = True


# =============================================================================
# SCHEDULING (Phase 11)
# =============================================================================
# The daily run is scheduled in .github/workflows/daily_job_hunt.yml using
# a cron expression. Stored here for documentation; the workflow YAML is
# the actual source of truth (cron strings can't be templated from Python
# in GitHub Actions).
SCHEDULE_TIME_IST: Final[str] = "08:00"
SCHEDULE_CRON_UTC: Final[str] = "30 2 * * *"  # 02:30 UTC = 08:00 IST.


# =============================================================================
# SANITY CHECK — call this at script startup to fail fast if .env is broken.
# =============================================================================
def validate_config() -> None:
    """
    Raise a clear error if the .env is missing values we know we'll need.

    Called at the top of every entry-point script so we crash early with
    a useful message instead of failing deep inside an API call.
    """
    errors: list[str] = []

    if LLM_PROVIDER == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key or key == "sk-ant-REPLACE_ME":
            errors.append(
                "ANTHROPIC_API_KEY is missing or unset. Get one at "
                "https://console.anthropic.com/settings/keys and add it to .env"
            )
    elif LLM_PROVIDER == "gemini":
        if not os.getenv("GEMINI_API_KEY"):
            errors.append(
                "GEMINI_API_KEY is missing. Get one at "
                "https://aistudio.google.com/app/apikey and add it to .env"
            )
    else:
        errors.append(
            f"LLM_PROVIDER='{LLM_PROVIDER}' is invalid. "
            "Use 'anthropic' or 'gemini'."
        )

    if not RESUME_PDF_SOURCE.exists():
        errors.append(
            f"Resume PDF not found at {RESUME_PDF_SOURCE}. "
            "Copy your latest resume there and re-run."
        )

    if errors:
        raise SystemExit(
            "Configuration errors:\n  - " + "\n  - ".join(errors)
        )


# Convenience: ensure runtime directories exist whenever config is imported.
# Cheap (mkdir -p semantics), and means downstream code can assume paths
# without sprinkling `.mkdir(...)` calls everywhere.
for _d in (
    DATA_DIR, RESUMES_TAILORED_DIR, RESUMES_PDF_DIR,
    QUIZZES_DIR, LOGS_DIR, REPORTS_DIR, BACKUPS_DIR,
):
    _d.mkdir(parents=True, exist_ok=True)
