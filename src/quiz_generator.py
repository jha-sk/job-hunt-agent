r"""
src/quiz_generator.py — Job Hunt Agent · Phase 8 quiz + self-evaluation.

WHAT IT DOES
------------
Every morning, generates a tailored mock-interview quiz based on today's
top-3 scored jobs:
  - 5 technical questions (JD-aligned: Go, Python, AI/ML, backend, infra)
  - 2 behavioural (STAR-format prompts)
  - 1 system-design (scaled to Sourabh's ~1.5y experience, not principal level)
Each question has a difficulty label (Fresher / Mid / Senior, default Mid),
model answer, and source job reference.

Saves to `quizzes/quiz_YYYY-MM-DD.md`. Phase 10's digest will bundle it
into the morning email.

DEDUP
-----
Memory table category=`quiz_question_seen` stores hashes of every question
we've ever generated. The LLM prompt receives a "do not repeat" list of
topics asked in the last 30 days, so the model doesn't keep giving the
same Go-channels question every week.

SELF-EVALUATION
---------------
After Sourabh does the quiz, he marks each question:
    python -m src.quiz_generator list                # show today's questions w/ numbers
    python -m src.quiz_generator mark 3 struggled    # mark q3 as struggled

Results land in memory (category=`quiz_question_result`), feeding the
Phase 9 learning engine ("focus future quizzes on areas Sourabh struggles with").

HOW TO RUN
----------
    .\.venv\Scripts\python.exe -m src.quiz_generator              # generate today's quiz
    .\.venv\Scripts\python.exe -m src.quiz_generator --dry-run    # plan, no LLM
    .\.venv\Scripts\python.exe -m src.quiz_generator list         # show today's questions
    .\.venv\Scripts\python.exe -m src.quiz_generator mark <n> <easy|struggled|nailed>
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    CANDIDATE_NAME,
    LLM_PROVIDER,
    MIN_MATCH_SCORE,
    MODEL_TAILOR,   # quiz reuses tailor's flash-lite model + quota bucket
    QUIZZES_DIR,
)
from src import db  # noqa: E402
from src.llm_client import DailyQuotaExhausted, LLMClient  # noqa: E402

log = logging.getLogger("quiz")

# Categories used in the memory table for dedup + evaluation tracking.
MEMORY_CAT_SEEN   = "quiz_question_seen"
MEMORY_CAT_RESULT = "quiz_question_result"

# Don't re-ask the same question within this many days.
DEDUP_WINDOW_DAYS = 30


# =============================================================================
# LLM schema
# =============================================================================
QuestionType = Literal["technical", "behavioral", "system_design"]
Difficulty   = Literal["Fresher", "Mid", "Senior"]
Result       = Literal["easy", "struggled", "nailed"]


class QuizQuestion(BaseModel):
    """One question + model answer for the daily quiz."""
    type: QuestionType
    topic: str = Field(
        max_length=120,
        description="Short topic label like 'Go concurrency: WaitGroup vs errgroup' or 'STAR: handling conflicting priorities'.",
    )
    difficulty: Difficulty = Field(
        description=(
            "Mid is the default for Sourabh (1.5y exp). Use Fresher for "
            "fundamentals he should nail; Senior to stretch."
        ),
    )
    question: str = Field(
        min_length=20, max_length=1200,
        description="The question text, as you'd ask in an interview.",
    )
    model_answer: str = Field(
        min_length=80, max_length=3000,
        description=(
            "A strong 2-4 paragraph answer. Show what 'good' looks like at "
            "Mid level. For behavioural questions use the STAR structure."
        ),
    )
    follow_up_hints: list[str] = Field(
        default_factory=list, max_length=3,
        description="Up to 3 probing follow-ups an interviewer would ask.",
    )
    source_job_company: Optional[str] = Field(
        default=None,
        description="Company name from top-3 jobs this question was inspired by (or None for general).",
    )


class DailyQuiz(BaseModel):
    """The full 8-question quiz for one day."""
    questions: list[QuizQuestion] = Field(
        min_length=8, max_length=8,
        description=(
            "EXACTLY 8 questions in this order: 5 technical, 2 behavioural, "
            "1 system_design. The 'type' field on each enforces the mix."
        ),
    )
    focus_summary: str = Field(
        min_length=40, max_length=400,
        description="2-3 sentences explaining what today's quiz drills.",
    )


# =============================================================================
# Prompt
# =============================================================================
SYSTEM_PROMPT = f"""\
You design daily mock-interview quizzes for {CANDIDATE_NAME}, a backend
engineer aiming at Go / Python / AI-engineer roles.

CANDIDATE PROFILE (locked):
- Associate Software Engineer @ Accenture, ~1.5 years experience.
- Primary skills: Go (strongest), Python, backend systems, Docker/K8s/
  Terraform, AWS/Azure/GCP, CI/CD, observability (Prometheus/Grafana/ELK).
- Stretch: AI/LLM/RAG/MLOps.
- DEFAULT QUESTION DIFFICULTY: 'Mid'. Use 'Fresher' for fundamentals
  he should ace; 'Senior' to stretch him. Do NOT make every question
  Senior — overload is demotivating.

QUIZ STRUCTURE (always exactly 8 questions in this order):
  1-5: type='technical'. Mix of Go internals, Python, system thinking,
       AI/LLM concepts. Spread across the top-3 jobs' tech stacks where
       possible; fill remaining slots with general role-relevant topics.
  6-7: type='behavioral'. Use STAR prompts. Topics relevant to a junior-
       to-mid engineer's day: cross-team conflict, scope negotiation,
       production incident, learning a new stack fast, disagreeing with
       a senior, dealing with ambiguity.
  8:   type='system_design'. SCALED TO MID LEVEL — NOT principal. Good
       examples: "rate limiter for an internal API", "URL shortener",
       "Kafka-vs-Redis-streams trade-off for an event pipeline", "design
       /metrics endpoint for a fleet of microservices". NOT good for
       Sourabh: "design YouTube", "design global CDN" — those are
       principal-level and unfair at 1.5y.

MODEL ANSWER QUALITY:
- 2-4 short paragraphs each.
- Behavioural answers MUST follow STAR (Situation, Task, Action, Result)
  and use a plausible Accenture/personal-project scenario from Sourabh's
  background.
- Technical answers should show "what good looks like at Mid level" —
  enough depth to demonstrate understanding without rambling.
- System-design answer: high-level diagram-in-words + 2-3 trade-offs.

HARD RULES:
- DO NOT repeat any question from the "Already asked recently" list.
- DO NOT exceed 8 questions.
- DO NOT give all 'Senior' difficulty — target ~5x Mid, ~2x Senior,
  ~1x Fresher overall.

Output strictly the JSON schema given.
"""


USER_PROMPT_TEMPLATE = """\
=== TODAY'S TOP-{n_jobs} JOBS (use to source JD-relevant technical + system design questions) ===
{jobs_block}

=== ALREADY-ASKED TOPICS in the last {dedup_days} days — DO NOT REPEAT ===
{recent_topics_block}

=== ADDITIONAL FOCUS HINTS ===
{focus_hint}

Generate the 8-question quiz per the schema.
"""


def _render_jobs_block(top_jobs: list[dict]) -> str:
    if not top_jobs:
        return "(no top-scored jobs today — generate general Go/Python/AI questions)"
    lines: list[str] = []
    for i, j in enumerate(top_jobs, 1):
        # Truncate JD to keep token budget reasonable.
        jd = (j.get("jd_text") or "")[:1500]
        lines.append(
            f"--- Job #{i}: {j['title']} @ {j['company']} (score {j.get('match_score', '?')}/100) ---\n"
            f"Source: {j['source']}    Location: {j.get('location') or '?'}\n"
            f"JD excerpt:\n{jd}\n"
        )
    return "\n".join(lines)


def _render_recent_topics(recent: list[dict]) -> str:
    if not recent:
        return "(none — this is your first quiz)"
    return "\n".join(f"  - {r['value'].get('topic', '?')} ({r['value'].get('type', '?')})"
                     for r in recent if r.get("value"))


def _build_user_prompt(
    top_jobs: list[dict],
    recent_topics: list[dict],
    focus_hint: str,
) -> str:
    return USER_PROMPT_TEMPLATE.format(
        n_jobs=len(top_jobs),
        jobs_block=_render_jobs_block(top_jobs),
        dedup_days=DEDUP_WINDOW_DAYS,
        recent_topics_block=_render_recent_topics(recent_topics),
        focus_hint=focus_hint,
    )


# =============================================================================
# Dedup: pull already-asked questions within the window
# =============================================================================
def _question_hash(question_text: str) -> str:
    """Stable 16-char hash for memory keys."""
    return hashlib.sha256(question_text.encode("utf-8")).hexdigest()[:16]


def _recent_asked_questions(window_days: int = DEDUP_WINDOW_DAYS) -> list[dict]:
    """Return memory entries from category 'quiz_question_seen' whose
    updated_at falls within the dedup window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    all_entries = db.memory_list(MEMORY_CAT_SEEN)
    return [e for e in all_entries if e.get("updated_at", "") >= cutoff]


def _record_asked_questions(quiz: DailyQuiz, asked_on: str) -> None:
    """Write one memory row per question for future dedup."""
    for q in quiz.questions:
        db.memory_set(
            MEMORY_CAT_SEEN,
            _question_hash(q.question),
            {
                "topic":           q.topic,
                "type":            q.type,
                "difficulty":      q.difficulty,
                "asked_on":        asked_on,
                "source_company":  q.source_job_company,
            },
            source="quiz_generator",
        )


# =============================================================================
# Markdown rendering
# =============================================================================
def render_quiz_md(quiz: DailyQuiz, top_jobs: list[dict], today: str) -> str:
    """Quiz file Sourabh actually reads in the morning."""
    lines: list[str] = []
    lines.append(f"# Mock Interview Quiz — {today}")
    lines.append("")
    lines.append(f"**Today's focus:** {quiz.focus_summary}")
    lines.append("")
    if top_jobs:
        lines.append("**Source jobs:**")
        for j in top_jobs:
            lines.append(f"- {j['title']} @ {j['company']} "
                         f"({j.get('match_score', '?')}/100)")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> Work through each question UNDER A TIMER (15 min for tech, "
                 "5 min for behavioural, 25 min for system design). Then "
                 "compare your answer to the model. Mark with: "
                 "`python -m src.quiz_generator mark <n> <easy|struggled|nailed>`")
    lines.append("")

    for i, q in enumerate(quiz.questions, 1):
        type_label = {"technical": "🛠 Technical",
                      "behavioral": "👥 Behavioural (STAR)",
                      "system_design": "🏗 System Design"}.get(q.type, q.type)
        lines.append(f"## Q{i}. [{q.difficulty}] {type_label} — {q.topic}")
        if q.source_job_company:
            lines.append(f"*Inspired by: {q.source_job_company}*")
        lines.append("")
        lines.append(f"**Question:** {q.question}")
        lines.append("")
        if q.follow_up_hints:
            lines.append("**Likely follow-ups:**")
            for hint in q.follow_up_hints:
                lines.append(f"- {hint}")
            lines.append("")
        lines.append("<details><summary>Model answer</summary>")
        lines.append("")
        lines.append(q.model_answer)
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def quiz_path_for(today: str) -> Path:
    return QUIZZES_DIR / f"quiz_{today}.md"


# =============================================================================
# Pipeline: generate
# =============================================================================
def _gather_top_jobs() -> list[dict]:
    """Top-3 scored jobs at or above MIN_MATCH_SCORE."""
    return db.list_jobs(min_score=MIN_MATCH_SCORE, limit=3)


def _focus_hint_from_memory() -> str:
    """
    Read past `quiz_question_result` entries to detect topics Sourabh has
    struggled on recently. Returns a one-line hint for the prompt. Empty
    string if no history (early days).
    """
    results = db.memory_list(MEMORY_CAT_RESULT)
    if not results:
        return "(none yet — early days of quiz history)"
    struggled_topics: list[str] = []
    for r in results[-20:]:   # last 20 evaluations
        val = r.get("value") or {}
        if val.get("result") == "struggled" and val.get("topic"):
            struggled_topics.append(val["topic"])
    if not struggled_topics:
        return "(no topics flagged as 'struggled' yet)"
    # Most-common-first
    from collections import Counter
    top_struggles = [t for t, _ in Counter(struggled_topics).most_common(5)]
    return ("Sourabh has struggled with these recently — drill them harder:\n"
            + "\n".join(f"  - {t}" for t in top_struggles))


def run(dry_run: bool = False) -> Optional[Path]:
    log.info("====== Quiz generator starting ======")

    top_jobs = _gather_top_jobs()
    log.info("Top scored jobs available: %d (min_score=%d)", len(top_jobs), MIN_MATCH_SCORE)

    if not top_jobs:
        log.warning(
            "No jobs at or above MIN_MATCH_SCORE=%d today. "
            "Generating a general-purpose quiz from your profile only.",
            MIN_MATCH_SCORE,
        )

    recent = _recent_asked_questions()
    log.info("Recently-asked questions in dedup window: %d", len(recent))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = quiz_path_for(today)

    if dry_run:
        log.info("--dry-run: would generate quiz with %d top jobs, %d already-asked. "
                 "Would write %s.", len(top_jobs), len(recent), out_path)
        return None

    log.info("Using provider=%s model=%s", LLM_PROVIDER, MODEL_TAILOR)
    client = LLMClient(phase="quiz", model=MODEL_TAILOR)

    started = time.monotonic()
    try:
        quiz, _usage = client.complete_json(
            system=SYSTEM_PROMPT,
            user=_build_user_prompt(top_jobs, recent, _focus_hint_from_memory()),
            schema=DailyQuiz,
            max_output_tokens=4096,
        )
    except DailyQuotaExhausted:
        # Quiz is just one call/day — when quota is gone, log a single
        # warning (no traceback) and move on. Tomorrow's cron tries again.
        log.warning(
            "quiz: skipped — Gemini daily quota exhausted. "
            "Today's quiz won't be generated; tomorrow's run will retry."
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log.exception("quiz: LLM call failed — %s", exc)
        return None
    elapsed = time.monotonic() - started

    md = render_quiz_md(quiz, top_jobs, today)
    out_path.write_text(md, encoding="utf-8")
    log.info("quiz: wrote %s (%.1f KB) in %.1fs", out_path, out_path.stat().st_size / 1024, elapsed)

    # Memorise the questions for future dedup.
    _record_asked_questions(quiz, today)

    # Update today's daily_runs row.
    try:
        db.upsert_daily_run(today, quiz_generated=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("quiz: db daily_runs update failed: %s", exc)

    _print_quiz_summary(quiz, out_path)
    return out_path


def _print_quiz_summary(quiz: DailyQuiz, path: Path) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        print(f"Wrote {path}")
        return
    console = Console()
    console.print(f"\n[green]Quiz written:[/green] {path}")
    table = Table(title=f"{len(quiz.questions)} questions", show_header=True, header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Type")
    table.add_column("Difficulty")
    table.add_column("Topic")
    for i, q in enumerate(quiz.questions, 1):
        diff_color = {"Fresher": "green", "Mid": "yellow", "Senior": "red"}.get(q.difficulty, "white")
        table.add_row(
            str(i), q.type, f"[{diff_color}]{q.difficulty}[/{diff_color}]",
            q.topic if len(q.topic) <= 70 else q.topic[:69] + "…",
        )
    console.print(table)


# =============================================================================
# CLI subcommands: list + mark
# =============================================================================
def _today_quiz_path() -> Path:
    return quiz_path_for(datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def _read_today_questions() -> list[tuple[int, str]]:
    """Parse today's quiz .md and return [(question_number, question_text), ...].
    Used by `mark` to look up the right memory row when Sourabh evaluates."""
    path = _today_quiz_path()
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    out: list[tuple[int, str]] = []
    current_n: Optional[int] = None
    for line in text.splitlines():
        if line.startswith("## Q") and ". " in line:
            try:
                num_str = line.split("Q", 1)[1].split(".", 1)[0]
                current_n = int(num_str)
            except (ValueError, IndexError):
                current_n = None
        elif line.startswith("**Question:**") and current_n is not None:
            qtext = line.split("**Question:**", 1)[1].strip()
            out.append((current_n, qtext))
            current_n = None
    return out


def cmd_list(_args: argparse.Namespace) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for n, qtext in _read_today_questions():
            print(f"  {n}. {qtext[:80]}")
        return
    console = Console()
    questions = _read_today_questions()
    if not questions:
        console.print("[yellow]No quiz for today. Run: python -m src.quiz_generator[/yellow]")
        return
    table = Table(title=f"Today's quiz — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                  show_header=True, header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Question")
    for n, qtext in questions:
        table.add_row(str(n), qtext if len(qtext) <= 100 else qtext[:99] + "…")
    console.print(table)


def cmd_mark(args: argparse.Namespace) -> None:
    questions = _read_today_questions()
    by_num = dict(questions)
    if args.number not in by_num:
        print(f"No question #{args.number} in today's quiz. "
              f"Use `python -m src.quiz_generator list` to see numbers.")
        return
    qtext = by_num[args.number]
    qhash = _question_hash(qtext)

    # Pull the SEEN entry for full metadata, write a RESULT entry.
    seen = db.memory_get(MEMORY_CAT_SEEN, qhash) or {}
    db.memory_set(
        MEMORY_CAT_RESULT,
        qhash,
        {
            "result":     args.result,
            "topic":      seen.get("topic"),
            "type":       seen.get("type"),
            "difficulty": seen.get("difficulty"),
            "marked_on":  datetime.now(timezone.utc).isoformat(),
            "question_preview": qtext[:120],
        },
        source="quiz_generator.mark",
    )
    print(f"Marked Q{args.number} as '{args.result}'. "
          f"Phase 9 memory engine will use this to focus future drills.")


# =============================================================================
# CLI
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate and self-evaluate today's mock-interview quiz.",
    )
    sub = parser.add_subparsers(dest="cmd")

    # Default subcommand (when none given) is 'generate'.
    p_gen = sub.add_parser("generate", help="Generate today's quiz (default).")
    p_gen.add_argument("--dry-run", action="store_true")

    sub.add_parser("list", help="Show today's questions with numbers.")

    p_mark = sub.add_parser("mark", help="Mark a question after working through it.")
    p_mark.add_argument("number", type=int, help="Question number from `list`.")
    p_mark.add_argument("result", choices=["easy", "struggled", "nailed"])

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "httpx", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Default to 'generate' when no subcommand is given (matches how
    # `python -m src.scorer` works elsewhere in the pipeline).
    cmd = args.cmd or "generate"
    if cmd == "generate":
        run(dry_run=getattr(args, "dry_run", False))
    elif cmd == "list":
        cmd_list(args)
    elif cmd == "mark":
        cmd_mark(args)


if __name__ == "__main__":
    main()
