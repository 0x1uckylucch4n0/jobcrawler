# B1mo Job Bot — Project Context

## What this is
A Telegram bot that searches LinkedIn and Indeed for jobs matching Alramina's profile, sends real-time alerts, filters by visa sponsorship, and uses Claude Haiku to summarise job descriptions.

## Project location
`/Users/aly4x/claudebot/`

## Python
`/Users/aly4x/.pyenv/versions/3.11.9/bin/python`

## How to run
```bash
cd ~/claudebot
/Users/aly4x/.pyenv/versions/3.11.9/bin/python bot.py
```

## Key files
- `bot.py` — Telegram bot, command handlers, 30-min scheduler
- `job_search.py` — scrapes LinkedIn + Indeed via jobspy, filters and scores results
- `cv_profile.py` — Alramina's skills, search terms, target companies, locations, exclusions
- `summarizer.py` — Claude Haiku generates 2-sentence job summaries
- `job_store.py` — SQLite deduplication so repeat jobs aren't re-sent
- `sponsor_check.py` — checks UK visa sponsor register
- `sponsor_register.csv` — UK sponsor register data
- `.env` — `BOT_TOKEN` and `ANTHROPIC_API_KEY`

## Dependencies (all installed)
anthropic, python-telegram-bot 22.6, apscheduler 3.11, python-jobspy, python-dotenv

## Current status
- All code is written and dependencies are installed
- Bot was being restarted after Anthropic API key was added to .env
- **TODO: Rotate credentials** — BOT_TOKEN and ANTHROPIC_API_KEY were exposed in a saved terminal file (`~/Documents/Terminal Saved Output.txt`). Old keys should be revoked and .env updated before running.

## Bot commands
- `/start` — register and begin receiving alerts
- `/search` — manual search now
- `/stop` — stop alerts
- `/help` — show commands

## Search profile
- **Name**: Alramina Myrzabekova
- **Role**: Technology Risk Consultant at EY Financial Services, ~2 years experience
- **Locations**: London (primary), Paris, Amsterdam, Dubai, Zurich, Singapore
- **Target level**: Analyst / Associate (no grad schemes, no senior titles)
- **Target sectors**: fintech, financial services, consulting, PE, VC, IB, AI

## Notion integration (incomplete)
A second session was working on connecting Notion. User pasted a page ID instead of a real integration token. To get the right token:
1. Go to notion.so/profile/integrations
2. Create or open an integration
3. Copy the `secret_xxx` token
4. Share your Notion page with the integration
