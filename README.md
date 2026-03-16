# JobCrawler — Automated Job Application Bot

Automatically applies to jobs from a Google Sheets tracker.

## Architecture

```
Google Sheet (claudebot tab)
    │
    ├─► linkedin_runner.py  — Easy Apply jobs on LinkedIn
    └─► company_runner.py   — Company website jobs (ATS: Workday, Oracle HCM, Greenhouse, Lever...)
```

## Status

| Component | Status |
|---|---|
| LinkedIn Easy Apply runner | ✅ Working |
| Company site runner | 🔧 In progress (needs ATS-specific handlers) |

---

## LinkedIn Runner (`linkedin_runner.py`)

**Fully working.** Runs every 30 min via launchd.

- Reads Google Sheet for jobs with LinkedIn Easy Apply URLs
- Uses Playwright + `playwright-stealth` + persistent Chrome profile (already logged in)
- Fills all form fields, handles dropdowns, always answers Yes to experience/skill questions
- Updates sheet column F: `✅ Applied`, `🔗 Company site`, `⚠️ Manual needed`
- When job requires company site: saves external URL to column G (Notes)
- Sends Telegram notifications on apply/failure

**Schedule:** `com.openclaw.linkedin.plist.example` → every :00 and :30

---

## Company Runner (`company_runner.py`)

**Needs fixing.** Should handle jobs marked `🔗 Company site` in the sheet.

### What it should do
1. Read rows where col F = `🔗 Company site` (or blank status with non-LinkedIn URL)
2. Detect ATS type from URL
3. Apply using ATS-specific handler
4. Update sheet col F to `✅ Applied` or `⚠️ Manual needed`

### ATS types to handle

| ATS | URL pattern | Status |
|---|---|---|
| Workday | `myworkdayjobs.com` | Partial (was working, broke) |
| Oracle HCM | `oraclecloud.com/hcmUI` | Partial (`oracle_hcm_apply.py`) |
| Greenhouse | `greenhouse.io` | Not started |
| Lever | `lever.co` | Not started |
| SmartRecruiters | `smartrecruiters.com` | Not started |
| Unknown | Any other | Fallback needed |

### Key issues to fix
- Oracle HCM: form fields (First Name, Last Name, radio buttons) not registering due to Oracle JET components
  - Fields need `Tab` keypress + `input/change/blur` event dispatch to pass Angular validation
  - "Are you at least 18?" and other radio groups need to be detected by question text
  - "How did you hear about us?" is a custom Oracle JET combobox (not a native `<select>`)
- Workday: was getting stuck on "Save and Continue" — address fields missing
- New tabs: clicking "Apply" sometimes opens a new tab — catch with `page.context.expect_page()`
- `playwright-stealth` needed on every new page/tab

### Work in progress
`oracle_hcm_apply.py` — standalone test script for Oracle HCM (Ford job).
Use this as the basis for the Oracle HCM handler in `company_runner.py`.

---

## Google Sheet

- **Sheet ID:** `18-_0J-ImLDrl2Z1Wm0iXskx02X0P9w6_pvG-nAjgkB8`
- **Tab:** `claudebot`
- **Columns:** Role (A), Company (B), Location (C), Stage (D), Visa Sponsor (E), Claude/Status (F), Notes/URL (G)
- **Status values:** blank=pending, `🔗 Company site`=needs company runner, `✅ Applied`=done, `⚠️ Manual needed`=failed, `no`=skip

---

## Setup

### Requirements
```bash
pip install playwright playwright-stealth gspread google-auth anthropic
playwright install chromium
```

### Workspace files (create manually — not in repo)
```
workspace/
  service_account.json   # Google Sheets service account credentials
  CV.pdf                 # CV for upload
  CV.md                  # CV as text for Claude prompts
  COVER_LETTER.md        # Cover letter template
  APPLICATION_ANSWERS.md # Pre-written answers to common questions
  screening_answers.json # Screening question answers
```

### Browser profiles (create by running once manually)
```
linkedin-profile/   # Persistent Chromium profile, logged into LinkedIn
company-profile/    # Separate profile for company sites
```

### Environment (macOS launchd)
See `com.openclaw.linkedin.plist.example` and `com.openclaw.company.plist.example`.
Copy to `~/Library/LaunchAgents/` and run `launchctl load`.

---

## Applicant Profile (hardcoded in both runners — update as needed)

```python
NAME_F   = "Alramina"
NAME_L   = "Myrzabekova"
EMAIL    = "alramina.myrzabekova@gmail.com"
PHONE    = "7760289275"
ADDR1    = "27 Albert Embankment"
CITY     = "London"
POSTCODE = "SE1 7AQ"
SALARY   = "52000"
```

---

## Telegram Notifications

Set `TG_TOKEN` and `TG_CHAT` in the runner files to receive Telegram alerts.

---

## AWS / Claude (Anthropic Bedrock)

Used for intelligent form field answering. Set:
```python
AWS_ACCESS_KEY = "..."
AWS_SECRET_KEY = "..."
AWS_REGION     = "eu-west-2"
CLAUDE_MODEL   = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
```

---

## Future: Recruiter Outreach Bot

Planned separate bot to message recruiters/employees on LinkedIn after applying.
