# Setup — GitHub Actions cron for the Job Hunt Agent

This guide walks you from "the project works on my laptop" to "it runs itself every morning at 8 AM IST".

You only do this once. After it's set up, the cron runs on its own and the only thing you touch is your inbox each morning (and re-uploading `token.json` ~weekly when Gmail's refresh token expires).

---

## Step 1 — Create a PRIVATE GitHub repo

> **PRIVATE matters.** The repo will contain your tailored resumes (which have your full contact info), your SQLite DB (which records every job you've applied to), and the `.env` template. None of that should be public.

1. Open https://github.com/new
2. Repository name: `job-hunt-agent` (or anything you like)
3. **Visibility: Private** ← non-negotiable
4. Do NOT initialize with a README — we already have files
5. Click **Create repository**
6. On the next page, GitHub shows the commands to push an existing repo. Don't follow those yet — we need to init + clean up first.

## Step 2 — Initialize git locally + first commit

From the project root:

```powershell
# One-time init
git init
git branch -M main

# Sanity check — make sure secrets are gitignored
git check-ignore .env credentials.json token.json
# Should print all three (= confirms they will NOT be committed). If any
# is missing, fix .gitignore before continuing.

# Stage + commit everything else
git add .
git status                         # eyeball — any .env / credentials.json showing? ABORT if so.
git commit -m "Initial commit — phases 1-11"
```

## Step 3 — Push to GitHub

Replace `<your-username>` with your GitHub username:

```powershell
git remote add origin https://github.com/<your-username>/job-hunt-agent.git
git push -u origin main
```

GitHub will prompt for credentials. Use a [Personal Access Token](https://github.com/settings/tokens) (classic, with `repo` scope) as the password — your account password won't work over HTTPS.

## Step 4 — Add Actions Secrets

GitHub → your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Add each of these. Names must match **exactly**:

### Required

| Secret name | Where to get the value |
|---|---|
| `GEMINI_API_KEY` | The Gemini key from your `.env` file. |
| `DIGEST_RECIPIENT_EMAIL` | `codewithsourabhjha@gmail.com` (or wherever you want the digest sent). |
| `GMAIL_CREDENTIALS_JSON` | The **entire contents** of `credentials.json` (your local OAuth client file). Open the file in Notepad, Ctrl+A, Ctrl+C, paste into the secret value. |
| `GMAIL_TOKEN_JSON` | The **entire contents** of `token.json` (your cached Gmail refresh token, created on first local run). Same paste-the-file trick. |

### Optional (only if you've enabled them)

| Secret name | Required when |
|---|---|
| `ADZUNA_APP_ID` | You set `SOURCES.adzuna = True` (it's True by default) and want Adzuna to actually fetch. |
| `ADZUNA_APP_KEY` | Same as above. |
| `ANTHROPIC_API_KEY` | You've switched `LLM_PROVIDER=anthropic`. |
| `RAPIDAPI_KEY` | You re-enabled JSearch (disabled by default). |

> **Privacy note on `GMAIL_CREDENTIALS_JSON`:** Google's docs say the desktop-app `client_secret` is technically not secret (it's distributed with installed apps), but stashing it as a GitHub Secret is the right hygiene regardless.

## Step 5 — Enable Actions

GitHub repos created today already have Actions enabled by default. To confirm:

1. Your repo → **Actions** tab
2. You should see "Daily Job Hunt" in the left sidebar
3. If GitHub shows a banner "Workflows aren't being run on this forked repository" or similar, click the button to enable.

## Step 6 — Test with a manual trigger BEFORE the first cron

1. Actions → **Daily Job Hunt** → **Run workflow** (top right)
2. Pick branch `main`
3. **Check `dry_run: true`** so this first test doesn't send a real email
4. **Run workflow**
5. Wait ~3 minutes, watch the live log
6. Expected result: all 9 phases complete, green checkmark

If it fails, the log will tell you which phase + line. Most common first-run failures:
- Missing secret → "GEMINI_API_KEY is missing" in the scorer phase
- Bad credentials.json paste → JSON parse error in gmail_client
- Bad token.json paste → Gmail OAuth flow tries to open browser (fails on Ubuntu)

## Step 7 — First real run

Once the dry-run succeeds, trigger again with `dry_run: false`. You should receive a "Daily Digest" email at the address in `DIGEST_RECIPIENT_EMAIL` within ~5 minutes.

## Step 8 — Wait for the cron

The schedule is `30 2 * * *` (02:30 UTC, 08:00 IST). GitHub's cron can lag by up to 15 minutes during high load — don't worry if the email arrives at 08:14 instead of 08:00.

You can watch it run live the next morning at Actions → Daily Job Hunt → the new workflow run that appears.

---

## What you do day-to-day after this

- **Read the digest email** that arrives at ~8 AM IST.
- For top jobs you want to pursue: click the apply link, submit the application, then locally run `python -m src.db_viewer apply <job_id>` so Gmail watcher tracks replies.
- Open `quizzes/quiz_<today>.md` (in your local clone — pull first with `git pull`) and work through it; mark each with `python -m src.quiz_generator mark <n> <result>`.
- When a recruiter email lands, the next morning's run will classify it; high-signal categories (shortlisted / interview_scheduled) trigger an extra alert email immediately.
- Once a week, scan `reports/weekly_*.md` (auto-generated) for trends.

## The one recurring chore

Gmail's refresh token for non-verified Test-User apps can expire roughly every 7 days. When the cron starts failing with "invalid_grant" or "Token has been expired", do this:

1. Locally: run `python -m src.gmail_watcher` → browser opens → re-auth → new `token.json` is written
2. Open the new `token.json` in Notepad → copy all
3. GitHub → repo → Settings → Secrets → `GMAIL_TOKEN_JSON` → **Update**, paste new value
4. Next cron run uses the fresh token

If this gets annoying, you can [submit your OAuth app for verification](https://support.google.com/cloud/answer/13463073) (free, ~1-2 week review) which removes the 7-day expiry. Most personal users just live with the weekly re-paste.

## How to disable the cron temporarily

GitHub → Actions → Daily Job Hunt → ··· → **Disable workflow**. Re-enable from the same place. The schedule pauses without losing your DB state.
