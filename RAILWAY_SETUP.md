# Railway Deployment Guide

## One-time setup

### 1. Create a Railway project
```
railway login
railway init
```
Select "Deploy from GitHub repo" and connect this repo.

### 2. Add a Volume
In the Railway dashboard → your service → **Volumes** → Add Volume → mount at `/data`.

This volume persists across restarts and stores:
- `/data/linkedin-profile/` — LinkedIn browser session (cookies)
- `/data/company-profile/` — Company site browser session
- `/data/workspace/` — CV, cover letter, answers files
- `/data/runner.log` — logs

### 3. Set environment variables
In Railway → your service → **Variables**, add:

| Variable | Value |
|----------|-------|
| `DATA_DIR` | `/data` |
| `HEADLESS` | `1` |
| `AWS_ACCESS_KEY` | your AWS key |
| `AWS_SECRET_KEY` | your AWS secret |
| `AWS_REGION` | `eu-west-2` |
| `TG_TOKEN` | your Telegram bot token |
| `TG_CHAT` | your Telegram chat ID |
| `GOOGLE_SA_JSON` | full JSON content of service_account.json (single line) |
| `WORKDAY_DEFAULT_PASSWORD` | password for Workday accounts |
| `RUN_INTERVAL_HOURS` | `2` (run every 2 hours) |
| `BATCH_LIMIT` | `20` (jobs per run) |
| `RESUME_MD` | content of CV.md |
| `COVER_LETTER_MD` | content of COVER_LETTER.md |
| `ANSWERS_MD` | content of APPLICATION_ANSWERS.md |
| `SCREENING_JSON` | content of screening_answers.json |

### 4. Upload CV.pdf to the volume
The bot needs `CV.pdf` for file-upload fields. SSH into the running container and copy it:
```
railway run bash
# then inside:
cp /app/workspace/CV.pdf /data/workspace/CV.pdf
```
Or add `CV_PDF_BASE64` as an env var and add decode logic.

### 5. First LinkedIn login (one-time)
LinkedIn needs a real browser login to save the session cookie.

**Option A (easiest):** Run locally first with `HEADLESS=0` — log in once, then the
`linkedin-profile/` directory has the session. Copy that directory to `/data/linkedin-profile/`
on Railway using `railway run bash`.

**Option B:** Add a `/login` Telegram command (future enhancement).

Once the session is saved to the volume it persists indefinitely (until LinkedIn expires it,
usually 30–90 days).

---

## Telegram commands (once running)
- `/run` — trigger an immediate run right now
- `/stop` — pause the scheduled runs
- `/start` — resume scheduled runs
- `/status` — show last run time and whether paused

---

## Running locally (unchanged)
Still works exactly as before — just don't set `DATA_DIR` or `HEADLESS` in your local env:
```
python linkedin_runner.py --limit 10
```
