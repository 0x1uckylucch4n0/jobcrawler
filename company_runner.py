#!/usr/bin/env python3
"""OpenClaw — Company website application bot.
Picks up rows marked '🔗 Company site' from the sheet, navigates to the company
application form via the LinkedIn job URL, and applies using Claude vision.
"""

import os, re, time, base64, requests
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import anthropic
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ── Config ───────────────────────────────────────────────────────────────────────
SHEET_ID        = "18-_0J-ImLDrl2Z1Wm0iXskx02X0P9w6_pvG-nAjgkB8"
TAB_NAME        = "claudebot"
WORKSPACE       = os.path.expanduser("~/openclaw/apply/workspace")
PROFILE_DIR     = os.path.expanduser("~/openclaw/apply/company-profile")
SERVICE_ACCOUNT = f"{WORKSPACE}/service_account.json"
CV_PATH         = f"{WORKSPACE}/CV.pdf"   # for file upload fields
AWS_ACCESS_KEY  = os.environ.get("AWS_ACCESS_KEY", "")
AWS_SECRET_KEY  = os.environ.get("AWS_SECRET_KEY", "")
AWS_REGION      = "eu-west-2"
CLAUDE_MODEL    = "eu.anthropic.claude-opus-4-6-v1"
TG_TOKEN        = os.environ.get("TG_TOKEN", "")
TG_CHAT         = os.environ.get("TG_CHAT", "")
LOG_FILE        = os.path.expanduser("~/openclaw/apply/runner.log")
NB_WORKDAY_EMAIL    = "alramina.myrzabekova@gmail.com"
NB_WORKDAY_PASSWORD = "Alramina@2024!"
COL_STATUS      = 6  # Column F

# ── Logging ──────────────────────────────────────────────────────────────────────
def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception: pass

# ── Sheet / profile helpers ───────────────────────────────────────────────────────
def get_sheet():
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(TAB_NAME)

def extract_url(cell):
    m = re.search(r'HYPERLINK\("([^"]+)"', cell, re.I)
    return m.group(1) if m else cell

def extract_title(cell):
    m = re.search(r'HYPERLINK\("[^"]+",\s*"([^"]+)"', cell, re.I)
    return m.group(1) if m else cell

def load_profile():
    out = {}
    for key, fname in [("cv","CV.md"),("cover","COVER_LETTER.md"),("answers","APPLICATION_ANSWERS.md"),("screen","screening_answers.json")]:
        try:
            with open(f"{WORKSPACE}/{fname}") as f: out[key] = f.read()
        except: out[key] = ""
    return out

# ── Claude vision ─────────────────────────────────────────────────────────────────
SYSTEM = """\
You are filling in a job application on a company website for Alramina Myrzabekova.

Profile:
- First name: Alramina | Last name: Myrzabekova
- Email: alramina.myrzabekova@gmail.com
- Phone: 7760289275 (WITHOUT +44 prefix — country code is selected separately in its own field)
- Address: 27 Albert Embankment, London, SE1 7AQ, United Kingdom
- City: London | Postcode: SE1 7AQ | Country: United Kingdom
- Current role: Technology Risk Consultant, EY Financial Services (~2 years)
- Education: BSc Political Economy, King's College London (2020-2023)
- Visa: Skilled Worker Visa — requires sponsorship: YES
- Notice period: 2 weeks | Expected salary: £52,000
- Languages: English (Fluent), Russian (Fluent), Kazakh (Fluent), French (Intermediate), Spanish (Beginner)

Field rules — follow exactly:
- "How did you hear about us" / "Source" / "Referral": click the field or the list icon (≡) next to it to open the dropdown. Then click "Job Posting" if visible. If "Job Posting" is not visible, pick any simple single-level option that doesn't open a sub-menu. Do NOT pick "Employee Referral" (it has confusing sub-menus). Do NOT type "LinkedIn".
- "County" / "Region" / "State": select "Greater London" or the first London-related option available; if not found pick the first option
- If you see a "Discard Application?" or "Are you sure?" dialog: always click "Continue" or "Cancel" to stay on the form
- "Previously worked here" / "Former employee": always "No"
- "Require visa sponsorship": always "Yes"
- "Years of experience": answer "2"
- "Veteran" / "Military": always "No"
- "Criminal record" / "Conviction": always "No"
- "Disability": always "No" or "I don't wish to answer"
- "Gender": Female
- "Ethnicity": prefer not to say
- "Salary expectation": 52000
- For any open text / cover letter field: write a concise professional 2-3 sentence answer

IMPORTANT - when you see the "My Information" page on a Workday form, do the following IN ORDER without waiting to visually confirm each step:
1. fill "How Did You Hear About Us?" — the execute command will open the dropdown and pick an option automatically
2. click "No" for any "Have you ever been an employee" question
3. Then proactively fill ALL these fields (the system scrolls to them automatically even if not visible):
   fill "Given Name(s)" "Alramina"
   fill "Family Name(s)" "Myrzabekova"
   fill "Email" "alramina.myrzabekova@gmail.com"
   fill "Phone Number" "7760289275"
   select "County" "Greater London"
4. click "Save and Continue"

DO NOT keep scrolling if you can't see a field. Instead, just issue the fill command and the system will scroll to it.
DO NOT click "Use My Last Application" — it loads a wrong profile. If you see "Continue Application", click it.

Commands — respond with ONE only:
  click "visible text"              — click a button or link
  fill "field label" "value"        — type into a text field
  select "field label" "option"     — pick a dropdown option
  upload_cv                         — upload the CV/resume file
  scroll                            — scroll down
  DONE                              — application successfully submitted
  SKIP: reason                      — cannot proceed (e.g. broken page)

- If you see a thank you / confirmation page: DONE
- Only output the single command, nothing else
- NEVER issue SKIP before step 60. Always keep trying different approaches.
- If fill or select doesn't seem to work, try the same command again or try click instead.
- For "Family Name" fields, try: fill "Family Name" "Myrzabekova" (without the (s))
- If "APPLY NOW" / "Apply" button doesn't seem to work: try scroll first, then click again. The system will automatically catch any new tab that opens — just keep clicking.
- If on a sign-in page and credentials fail: click "Forgot your password?" then fill the email field.
- If stuck on same page for 5+ steps: scroll up, then try the main action button again.
"""

def screenshot_b64(page):
    return base64.b64encode(page.screenshot(full_page=False)).decode()

def claude_step(client, page, role, company, profile, messages):
    img = screenshot_b64(page)
    messages.append({
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
            {"type": "text", "text": f"Applying for {role} at {company}. What should I do next?"},
        ]
    })
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=150,
        system=SYSTEM,
        messages=messages,
    )
    cmd = resp.content[0].text.strip()
    messages.append({"role": "assistant", "content": cmd})
    return cmd

# ── Command execution ─────────────────────────────────────────────────────────────
def execute(page, cmd, profile):
    try:
        if cmd.startswith('click '):
            text = re.search(r'"([^"]+)"', cmd).group(1)
            # Never click "Use My Last Application" — it loads a different profile
            if "last application" in text.lower():
                log(f"  Blocked: '{text}' — ignoring")
                return
            # If a listbox is open, click within it — then close it
            listbox = page.locator('[role="listbox"]')
            if listbox.count() > 0:
                for opt_loc in [
                    listbox.locator('[role="option"]').filter(has_text=re.compile(re.escape(text), re.I)),
                    listbox.locator('li').filter(has_text=re.compile(re.escape(text), re.I)),
                ]:
                    if opt_loc.count() > 0:
                        try: opt_loc.first.click()
                        except: opt_loc.first.evaluate("el => el.click()")
                        time.sleep(0.5)
                        # Close the listbox after selection
                        page.keyboard.press("Escape")
                        time.sleep(0.3)
                        return
                # Option not in listbox — close it first then try normal click
                page.keyboard.press("Escape")
                time.sleep(0.3)
            # Workday nav buttons by data-automation-id (more reliable than text match)
            if any(w in text.lower() for w in ["save and continue", "next", "continue", "submit", "done"]):
                for wd_id in ["bottom-navigation-next-button", "bottom-navigation-done-button",
                               "bottom-navigation-footer-button"]:
                    btn = page.locator(f'[data-automation-id="{wd_id}"]')
                    if btn.count() > 0 and btn.first.is_visible():
                        btn.first.evaluate("el => el.click()"); time.sleep(0.5); return
            for loc in [
                page.get_by_role("button", name=re.compile(re.escape(text), re.I)),
                page.get_by_role("link", name=re.compile(re.escape(text), re.I)),
                page.get_by_role("radio", name=re.compile(re.escape(text), re.I)),
                page.get_by_role("option", name=re.compile(re.escape(text), re.I)),
                page.get_by_label(re.compile(re.escape(text), re.I)),
            ]:
                if loc.count() > 0:
                    el = loc.first
                    el.scroll_into_view_if_needed()
                    try:
                        el.click()
                    except Exception:
                        el.evaluate("el => el.click()")
                    return
            # Last resort: JS querySelector text search
            try:
                page.evaluate(f"""() => {{
                    const els = [...document.querySelectorAll('button,a,[role=button]')];
                    const el = els.find(e => e.innerText && e.innerText.toLowerCase().includes('{text.lower()[:30]}'));
                    if (el) el.click();
                }}""")
            except: pass
            log(f"  click: couldn't find '{text}'")

        elif cmd.startswith('fill '):
            m = re.findall(r'"([^"]+)"', cmd)
            if len(m) >= 2:
                label, value = m[0], m[1]
                lbl_lower = label.lower()
                for loc in [
                    page.get_by_label(re.compile(re.escape(label), re.I)),
                    page.get_by_placeholder(re.compile(re.escape(label), re.I)),
                ]:
                    if loc.count() > 0:
                        el = loc.first
                        el.scroll_into_view_if_needed()
                        # "Hear about us" fields: open dropdown, pick a simple top-level option
                        if any(w in lbl_lower for w in ["hear about", "source", "referral"]):
                            el.click()
                            time.sleep(1)
                            picked = False
                            # Try non-hierarchical options first (NB Careers Website, Other)
                            for target in ["NB Careers Website", "Neuberger Berman Recruitment Team", "Other", "Job Posting", "LinkedIn"]:
                                for opt_sel in [
                                    f'[role="option"]:has-text("{target}")',
                                    f'[role="listbox"] li:has-text("{target}")',
                                ]:
                                    opt = page.locator(opt_sel)
                                    if opt.count() > 0:
                                        opt.first.click()
                                        log(f"    → picked '{target}'")
                                        picked = True
                                        time.sleep(0.5)
                                        # If a sub-menu opened, pick first option in it
                                        sub = page.locator('[role="listbox"]')
                                        if sub.count() > 0:
                                            sub_opts = sub.locator('[role="option"], li')
                                            if sub_opts.count() > 0:
                                                sub_opts.first.click()
                                                log(f"    → picked sub-option")
                                                time.sleep(0.3)
                                        # Close dropdown
                                        page.keyboard.press("Escape")
                                        time.sleep(0.3)
                                        break
                                if picked:
                                    break
                            return
                        # Phone number: strip +44 prefix since country code already selected
                        if "phone" in lbl_lower and "extension" not in lbl_lower and "country" not in lbl_lower:
                            value = value.lstrip("+").removeprefix("44").lstrip("0")
                            value = "0" + value if not value.startswith("0") else value
                        el.fill(value)
                        time.sleep(0.8)
                        # Auto-pick first suggestion for other typeahead fields
                        for opt_sel in [
                            '[role="listbox"] [role="option"]',
                            '[role="listbox"] li',
                            '[role="option"]',
                        ]:
                            opt = page.locator(opt_sel)
                            if opt.count() > 0:
                                opt.first.click()
                                log(f"    → picked first suggestion")
                                break
                        return
                # Fallback: try data-automation-id for Workday fields
                wd_map = {
                    "given name": ["legalNameSection_firstName", "firstName"],
                    "first name": ["legalNameSection_firstName", "firstName"],
                    "family name": ["legalNameSection_lastName", "lastName"],
                    "last name": ["legalNameSection_lastName", "lastName"],
                    "email": ["email", "emailAddress"],
                    "phone": ["phone", "phoneNumber", "phone-number"],
                }
                for key, ids in wd_map.items():
                    if key in lbl_lower:
                        for aid in ids:
                            loc = page.locator(f'[data-automation-id="{aid}"]')
                            if loc.count() > 0:
                                try:
                                    el = loc.first; el.scroll_into_view_if_needed()
                                    el.triple_click(); el.fill(value); time.sleep(0.3)
                                    log(f"    → filled via data-automation-id={aid}")
                                    return
                                except: pass
                log(f"  fill: couldn't find '{label}'")

        elif cmd.startswith('select '):
            m = re.findall(r'"([^"]+)"', cmd)
            if len(m) >= 2:
                label, option = m[0], m[1]
                loc = page.get_by_label(re.compile(re.escape(label), re.I))
                if loc.count() > 0:
                    el = loc.first
                    el.scroll_into_view_if_needed()
                    # Try standard <select> first
                    try:
                        el.select_option(label=option)
                        return
                    except Exception:
                        pass
                    # Fall back: click to open custom listbox, then click option
                    try:
                        el.click()
                        time.sleep(0.5)
                        # For county/region fields, try London variants first
                        lbl_lower = label.lower()
                        candidates = [option]
                        if "county" in lbl_lower or "region" in lbl_lower or "state" in lbl_lower:
                            candidates = ["Greater London", "London", option]
                        picked = False
                        for candidate in candidates:
                            for opt_loc in [
                                page.get_by_role("option", name=re.compile(re.escape(candidate), re.I)),
                                page.locator('[role="listbox"] li').filter(has_text=candidate),
                                page.locator('[role="listbox"] [role="option"]').filter(has_text=candidate),
                            ]:
                                if opt_loc.count() > 0:
                                    opt_loc.first.click()
                                    picked = True
                                    break
                            if picked:
                                return
                        # Last resort: pick first available option
                        for first_sel in [
                            '[role="listbox"] [role="option"]',
                            '[role="listbox"] li',
                        ]:
                            first_opt = page.locator(first_sel)
                            if first_opt.count() > 0:
                                first_opt.first.click()
                                log(f"    → picked first available option")
                                return
                    except Exception:
                        pass
                    log(f"  select: couldn't pick '{option}' for '{label}'")

        elif cmd == 'upload_cv':
            cv_path = next((p for p in [
                CV_PATH,
                os.path.expanduser("~/Downloads/New CV Alramina 2026.pdf"),
                os.path.expanduser("~/Downloads/New CV Alramina 2026 (1).pdf"),
            ] if os.path.exists(p)), None)
            if not cv_path:
                log("  upload_cv: no CV file found"); return
            # Set directly on hidden file input (bypasses system dialog)
            file_input = page.locator('input[type="file"]')
            if file_input.count() > 0:
                file_input.first.set_input_files(cv_path)
                log(f"  Uploaded CV: {cv_path}")
            else:
                log("  upload_cv: no file input found")

        elif cmd == 'scroll':
            # Mouse wheel scroll works for any container (Workday inner div, window, etc.)
            page.mouse.move(640, 400)
            page.mouse.wheel(0, 600)
            time.sleep(0.3)
            # Also scroll window in case content is in the page not a container
            page.evaluate("window.scrollBy(0, 600)")

    except Exception as e:
        log(f"  execute error ({cmd}): {e}")


# ── Workday-specific helpers ──────────────────────────────────────────────────────
def workday_signin(page):
    """Handle Workday sign-in using data-automation-id selectors."""
    email_field = page.locator('[data-automation-id="email"]')
    if email_field.count() == 0:
        return True  # No sign-in needed

    log("  Workday sign-in detected — attempting programmatic login")
    time.sleep(1)

    for attempt in range(3):
        try:
            ef = page.locator('[data-automation-id="email"]')
            if ef.count() > 0:
                ef.first.triple_click(); ef.first.fill(NB_WORKDAY_EMAIL); time.sleep(0.5)

            pf = page.locator('[data-automation-id="password"]')
            if pf.count() > 0:
                pf.first.triple_click(); pf.first.fill(NB_WORKDAY_PASSWORD); time.sleep(0.5)

            sb = page.locator('[data-automation-id="signInSubmitButton"]')
            if sb.count() > 0:
                sb.first.click()
            else:
                page.get_by_role("button", name=re.compile("sign in", re.I)).first.click()
            time.sleep(4)

            # Success: sign-in form gone
            if page.locator('[data-automation-id="email"]').count() == 0:
                log("  Workday sign-in successful")
                return True

            # Check error text
            err = page.locator('[data-automation-id="errorMessage"], .wd-error-text')
            err_txt = err.first.inner_text()[:100] if err.count() > 0 and err.first.is_visible() else ""
            log(f"  Sign-in attempt {attempt+1} failed: {err_txt}")
            time.sleep(2)
        except Exception as e:
            log(f"  workday_signin error: {e}")

    # All attempts failed — trigger forgot password and notify
    log("  Triggering Forgot Password flow")
    try:
        forgot = page.get_by_text(re.compile("forgot.*password", re.I))
        if forgot.count() > 0:
            forgot.first.click(); time.sleep(2)
            reset_email = page.locator('[data-automation-id="email"]')
            if reset_email.count() > 0:
                reset_email.first.fill(NB_WORKDAY_EMAIL)
                page.get_by_role("button", name=re.compile("submit|reset|send", re.I)).first.click()
                time.sleep(2)
    except Exception as e:
        log(f"  forgot password error: {e}")
    tg(f"⚠️ *NB Workday sign-in failed*\nReset email sent to {NB_WORKDAY_EMAIL}\nCheck Gmail, reset password to: `{NB_WORKDAY_PASSWORD}`")
    return False


def _wd_fill(page, auto_id, value):
    """Fill a single Workday field by data-automation-id, scroll to it first."""
    loc = page.locator(f'[data-automation-id="{auto_id}"]')
    if loc.count() == 0:
        return False
    try:
        el = loc.first
        el.scroll_into_view_if_needed()
        time.sleep(0.2)
        if not el.is_editable():
            return False
        el.triple_click()
        el.fill(value)
        time.sleep(0.3)
        log(f"    → {auto_id} = {value!r}")
        return True
    except Exception as e:
        log(f"    → {auto_id} error: {e}")
        return False


def _wd_select_dropdown(page, auto_id, candidates):
    """Open a Workday custom dropdown and pick the first matching candidate."""
    # Try button variant (multiSelectButton inside a formField)
    for sel in [
        f'[data-automation-id="{auto_id}"] [data-automation-id="multiSelectButton"]',
        f'[data-automation-id="{auto_id}"]',
        f'[data-automation-id="formField-{auto_id}"] [data-automation-id="multiSelectButton"]',
    ]:
        btn = page.locator(sel)
        if btn.count() > 0:
            try:
                btn.first.scroll_into_view_if_needed()
                btn.first.click()
                time.sleep(1)
                for candidate in candidates:
                    opt = page.locator(f'[role="option"]:has-text("{candidate}"), [role="listbox"] li:has-text("{candidate}")')
                    if opt.count() > 0:
                        opt.first.click()
                        log(f"    → {auto_id} = {candidate!r}")
                        time.sleep(0.4)
                        page.keyboard.press("Escape")
                        time.sleep(0.3)
                        return True
                # No match — pick first non-empty option
                first = page.locator('[role="option"]:not([aria-disabled="true"]), [role="listbox"] li')
                if first.count() > 0:
                    txt = first.first.inner_text().strip()
                    first.first.click()
                    log(f"    → {auto_id} = first option: {txt!r}")
                    time.sleep(0.3)
                    page.keyboard.press("Escape")
                    return True
            except Exception as e:
                log(f"    → dropdown {auto_id} error: {e}")
    return False


def workday_autofill_myinfo(page):
    """Directly fill ALL My Information page fields using data-automation-id.
    Covers: source, former-employee, name, email, phone, address, country, county."""
    log("  Workday autofill: filling My Information (full)")
    time.sleep(2)

    # ── How Did You Hear About Us ─────────────────────────────────────────────
    sources = ["NB Careers Website", "Neuberger Berman Recruitment Team", "Job Posting", "Other", "Corporate Website", "LinkedIn"]
    _wd_select_dropdown(page, "sourceType", sources)

    # ── Former employee = No ──────────────────────────────────────────────────
    for sel in [
        '[data-automation-id="previousWorker"] input[value="0"]',
        '[data-automation-id="previousWorker"] input[value*="false"]',
        '[data-automation-id="formField-previousWorker"] input[type="radio"][value*="0"]',
    ]:
        r = page.locator(sel)
        if r.count() > 0:
            try: r.first.evaluate("el => el.click()"); time.sleep(0.3); break
            except: pass
    try:
        for rb in page.get_by_role("radio", name=re.compile("^no$", re.I)).all():
            if rb.is_visible(): rb.click(); time.sleep(0.2); break
    except: pass

    # ── Legal Name ────────────────────────────────────────────────────────────
    for aid, val in [
        ("legalNameSection_firstName", "Alramina"),
        ("legalNameSection_lastName",  "Myrzabekova"),
        ("firstName", "Alramina"),
        ("lastName",  "Myrzabekova"),
    ]:
        _wd_fill(page, aid, val)

    # ── Email ─────────────────────────────────────────────────────────────────
    for aid in ["email", "emailAddress"]:
        if _wd_fill(page, aid, "alramina.myrzabekova@gmail.com"):
            break

    # ── Phone ─────────────────────────────────────────────────────────────────
    # Country code dropdown first (pick United Kingdom / +44)
    _wd_select_dropdown(page, "countryPhoneCode", ["United Kingdom (+44)", "United Kingdom", "+44"])
    for aid in ["phone", "phoneNumber", "phone-number", "phoneDevice"]:
        if _wd_fill(page, aid, "7760289275"):
            break
    # Fallback by label
    try:
        ph = page.get_by_label(re.compile(r"^phone", re.I))
        if ph.count() > 0 and ph.first.is_editable():
            ph.first.triple_click(); ph.first.fill("7760289275"); time.sleep(0.2)
    except: pass

    # ── Address ───────────────────────────────────────────────────────────────
    _wd_fill(page, "addressSection_addressLine1", "27 Albert Embankment")
    _wd_fill(page, "addressSection_city", "London")
    _wd_fill(page, "addressSection_postalCode", "SE1 7AQ")

    # Country dropdown
    _wd_select_dropdown(page, "addressSection_countryRegion", ["United Kingdom", "UK"])

    # County / Region / State
    _wd_select_dropdown(page, "addressSection_region", ["Greater London", "London", "England"])

    # Fallback label-based address fills
    for lbl, val in [("Address Line 1", "27 Albert Embankment"), ("City", "London"), ("Postal Code", "SE1 7AQ")]:
        try:
            el = page.get_by_label(re.compile(re.escape(lbl), re.I))
            if el.count() > 0 and el.first.is_editable():
                el.first.triple_click(); el.first.fill(val); time.sleep(0.2)
        except: pass

    time.sleep(1)

    # ── Save and Continue ─────────────────────────────────────────────────────
    for btn_id in ["bottom-navigation-next-button", "bottom-navigation-done-button",
                   "bottom-navigation-footer-button"]:
        btn = page.locator(f'[data-automation-id="{btn_id}"]')
        if btn.count() > 0 and btn.first.is_visible():
            btn.first.evaluate("el => el.click()")
            log("  Workday autofill: clicked Next")
            time.sleep(4)
            return
    try:
        btn = page.get_by_role("button", name=re.compile("save and continue|next|continue", re.I))
        if btn.count() > 0:
            btn.first.click(); log("  Workday autofill: clicked Next (fallback)"); time.sleep(4)
    except: pass

# ── Get company URL from LinkedIn job ────────────────────────────────────────────
def get_company_apply_url(page, linkedin_url):
    """Navigate to LinkedIn job, click Apply, catch new tab or navigation."""
    page.goto(linkedin_url, wait_until="domcontentloaded", timeout=30000)
    # Wait for job details to actually render
    try:
        page.wait_for_selector('.job-view-layout, .jobs-details, .jobs-unified-top-card', timeout=10000)
    except Exception:
        pass
    time.sleep(3)

    # Log all buttons to help debug
    try:
        btns = page.evaluate("""() => Array.from(document.querySelectorAll('button,a')).map(b => ({
            tag: b.tagName, text: (b.innerText||'').trim().slice(0,60),
            aria: b.getAttribute('aria-label'), href: b.getAttribute('href')
        })).filter(b => b.text || b.aria)""")
        apply_btns = [b for b in btns if 'apply' in (b['text'] or '').lower() or 'apply' in (b['aria'] or '').lower()]
        for b in apply_btns[:10]:
            log(f"  Found button: tag={b['tag']} text={repr(b['text'])} aria={repr(b['aria'])}")
    except: pass

    # Find the Apply button — broad search, exclude Easy Apply
    apply_btn = None
    for pattern in [
        r"apply on company website",
        r"apply on employer",
        r"apply on",
        r"^apply$",
        r"^apply now$",
    ]:
        for role in ("link", "button"):
            loc = page.get_by_role(role, name=re.compile(pattern, re.I))
            if loc.count() > 0:
                # Make sure it's not Easy Apply
                aria = loc.first.get_attribute("aria-label") or ""
                text = loc.first.inner_text() or ""
                if "easy" not in aria.lower() and "easy" not in text.lower():
                    apply_btn = loc.first
                    log(f"  Using apply btn: '{text.strip()}' / '{aria}'")
                    break
        if apply_btn:
            break

    # Fallback: aria-label contains Apply but not Easy
    if not apply_btn:
        for sel in [
            'button[aria-label*="Apply"]:not([aria-label*="Easy"])',
            'a[aria-label*="Apply"]:not([aria-label*="Easy"])',
        ]:
            loc = page.locator(sel)
            if loc.count() > 0:
                apply_btn = loc.first
                log(f"  Using fallback selector: {sel}")
                break

    if not apply_btn:
        log("  No apply button found on page")
        return None

    # Click and catch new tab
    try:
        with page.context.expect_page(timeout=8000) as new_page_info:
            apply_btn.click()
        new_page = new_page_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=10000)
        url = new_page.url
        log(f"  New tab URL: {url}")
        new_page.close()
        if "linkedin.com" not in url:
            return url
    except Exception as e:
        log(f"  New tab catch failed: {e}")

    # Fallback: check if a new page already opened
    try:
        all_pages = page.context.pages
        new_pages = [p for p in all_pages if p != page and not p.is_closed()]
        if new_pages:
            new_page = new_pages[-1]
            try: new_page.wait_for_load_state("domcontentloaded", timeout=8000)
            except: pass
            url = new_page.url
            log(f"  Found open tab: {url}")
            new_page.close()
            if "linkedin.com" not in url and url not in ("about:blank", ""):
                return url
    except Exception as e:
        log(f"  context pages check: {e}")

    # Fallback: inline navigation
    try:
        apply_btn.click()
        time.sleep(4)
        if "linkedin.com" not in page.url:
            return page.url
    except Exception:
        pass

    return None

# ── Main application function ─────────────────────────────────────────────────────
def apply_company(page, url, role, company, profile, client):
    if "linkedin.com" in url:
        log(f"  Finding company apply URL via LinkedIn...")
        company_url = get_company_apply_url(page, url)
        if not company_url:
            return False, "couldn't find company apply URL"
    else:
        company_url = url

    log(f"  Applying at: {company_url}")
    for nav_attempt in range(3):
        try:
            page.goto(company_url, wait_until="domcontentloaded", timeout=30000)
            break
        except Exception as e:
            err = str(e)
            if "ERR_HTTP_RESPONSE_CODE_FAILURE" in err or "ERR_NAME_NOT_RESOLVED" in err or "ERR_CONNECTION_REFUSED" in err:
                return False, f"Page failed to load: {err[:80]}"
            if nav_attempt < 2:
                log(f"  goto failed (attempt {nav_attempt+1}), retrying..."); time.sleep(5)
            # else continue — some ATS pages redirect/abort but have content
    time.sleep(4)

    # Handle 429 Too Many Requests — wait and retry
    try:
        if "429" in page.title() or "429" in page.url or "too many requests" in page.content().lower()[:500]:
            log("  429 rate limit — waiting 45s then retrying")
            time.sleep(45)
            page.reload(wait_until="domcontentloaded", timeout=20000)
            time.sleep(3)
    except: pass

    # Workday: handle Continue Application button + sign-in up front
    if "myworkdayjobs.com" in company_url:
        # Click Continue Application / Apply (use JS click — more reliable)
        for btn_text in ["Continue Application", "Apply", "Apply Now"]:
            btn = page.get_by_role("button", name=re.compile(re.escape(btn_text), re.I))
            if btn.count() > 0 and btn.first.is_visible():
                try: btn.first.evaluate("el => el.click()")
                except: btn.first.click()
                log(f"  Workday: clicked '{btn_text}'"); time.sleep(4); break
        # Sign in
        if not workday_signin(page):
            return False, "Workday sign-in failed — reset email sent to Gmail"
        time.sleep(2)

    messages = []
    for step in range(120):
        # Check success
        try:
            html = page.content().lower()
            if any(s in html for s in ["application submitted", "thank you for applying",
                                        "application received", "successfully submitted",
                                        "application complete", "we'll be in touch"]):
                return True, "submitted"
            # Handle 429 rate limit mid-loop
            if "429" in page.title() or ("too many requests" in html[:500] and "http error 429" in html[:500]):
                log("  429 rate limit in loop — waiting 45s")
                time.sleep(45)
                page.go_back(wait_until="domcontentloaded", timeout=10000)
                time.sleep(3)
        except: pass

        # Dismiss ONLY "Discard / Leave / Lose progress" dialogs — NOT sign-in or apply dialogs
        try:
            dialog = page.locator('[role="dialog"], .modal, [class*="modal"]')
            if dialog.count() > 0:
                dialog_text = ""
                try: dialog_text = dialog.first.inner_text().lower()
                except: pass
                is_discard = any(w in dialog_text for w in [
                    "discard", "leave", "lose your", "are you sure", "unsaved", "cancel application",
                    "exit", "abandon", "close application"
                ])
                is_signin = any(w in dialog_text for w in [
                    "sign in", "sign up", "create account", "log in", "password", "email address",
                    "already have an account", "register", "forgot"
                ])
                if is_discard and not is_signin:
                    for stay_btn in ["Continue", "No", "Stay", "Cancel"]:
                        btn = page.get_by_role("button", name=re.compile(rf"^{stay_btn}$", re.I))
                        if btn.count() > 0 and btn.first.is_visible():
                            btn.first.click()
                            log(f"  Dismissed discard dialog: clicked '{stay_btn}'")
                            time.sleep(1)
                            break
        except: pass

        # Workday: if on My Information page, autofill directly without Claude
        if "myworkdayjobs.com" in page.url:
            html_lower = ""
            try: html_lower = page.content().lower()
            except: pass
            if "my information" in html_lower:
                # Call autofill at step 0, and again every 8 steps if still stuck
                if step == 0 or step % 8 == 0:
                    workday_autofill_myinfo(page)
                    time.sleep(2)
                    continue

        cmd = claude_step(client, page, role, company, profile, messages)
        log(f"  Step {step+1}: {cmd}")

        if cmd == "DONE":
            return True, "submitted"
        if cmd.startswith("SKIP:"):
            return False, cmd[5:].strip()

        execute(page, cmd, profile)
        time.sleep(2)

        # Switch to any new tab that opened (e.g. APPLY NOW, external apply links)
        try:
            all_pages = page.context.pages
            new_pages = [p for p in all_pages if p != page and not p.is_closed()]
            if new_pages:
                new_page = new_pages[-1]
                try: new_page.wait_for_load_state("domcontentloaded", timeout=8000)
                except: pass
                if "linkedin.com" not in new_page.url and new_page.url not in ("about:blank", ""):
                    log(f"  Switched to new tab: {new_page.url}")
                    # Close old page tabs (keep context open)
                    for old_p in all_pages:
                        if old_p != new_page:
                            try: old_p.close()
                            except: pass
                    page = new_page
                else:
                    for np in new_pages:
                        try: np.close()
                        except: pass
        except Exception as e:
            log(f"  new tab check error: {e}")

    return False, "max steps reached"

# ── Main ──────────────────────────────────────────────────────────────────────────
def main():
    log(f"\n{'='*40}\nCompany Runner — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}")

    try:
        sheet = get_sheet()
        rows = sheet.get("A:G", value_render_option="FORMULA")
    except Exception as e:
        log(f"Sheet error: {e}"); tg(f"⚠️ Company runner sheet error: {e}"); return

    headers = rows[0] if rows else []
    applied_idx = next((i for i, h in enumerate(headers) if h.lower() in ("applied", "claude")), 5)

    pending = []
    for i, row in enumerate(rows[1:], start=2):
        while len(row) < 7: row.append("")
        status = row[applied_idx].strip().lower()

        # Skip if marked "no" or already done
        if status == "no" or status.startswith("✅") or status.startswith("⚠️"):
            continue

        url = extract_url(row[0])
        title = extract_title(row[0])
        company = row[1] if len(row) > 1 else ""
        notes = row[6] if len(row) > 6 else ""  # col G = Notes (may have saved company URL)

        if "company site" in status:
            # LinkedIn runner already tried — use saved company URL from Notes if available
            apply_url = notes.strip() if notes.strip() and notes.strip().startswith("http") else url
            pending.append((i, apply_url, title, company))
        elif not status and "linkedin.com" not in url.lower():
            # Blank status + non-LinkedIn URL → apply directly
            pending.append((i, url, title, company))

    log(f"Found {len(pending)} company site jobs")
    if not pending: return

    profile = load_profile()
    client = anthropic.AnthropicBedrock(
        aws_access_key=AWS_ACCESS_KEY,
        aws_secret_key=AWS_SECRET_KEY,
        aws_region=AWS_REGION,
    )
    applied = 0

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
                "--no-first-run",
            ],
            viewport={"width": 1280, "height": 900},
        )
        # Close all pre-existing tabs from previous session except one
        for old_page in ctx.pages[1:]:
            try: old_page.close()
            except: pass
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        Stealth().apply_stealth_sync(page)

        # Check LinkedIn login (needed to access job pages)
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if "login" in page.url or "authwall" in page.url:
            log("Not logged in to LinkedIn — session expired, continuing anyway")

        for row_idx, url, title, company in pending:
            log(f"\n→ {title} @ {company}\n  {url}")
            # Close any stray tabs from previous job before starting
            try:
                for extra in ctx.pages:
                    if extra != page and not extra.is_closed():
                        try: extra.close()
                        except: pass
            except: pass
            try:
                ok, reason = apply_company(page, url, title, company, profile, client)
            except Exception as e:
                ok, reason = False, str(e)
                log(f"  Exception: {e}")

            if ok:
                sheet.update_cell(row_idx, COL_STATUS, "✅ Applied")
                tg(f"✅ *Applied!*\n*{title}* @ {company}\n_(company site)_")
                log("  ✅ Applied"); applied += 1
            else:
                sheet.update_cell(row_idx, COL_STATUS, "⚠️ Manual needed")
                tg(f"⚠️ *Manual needed*\n*{title}* @ {company}\n{reason}\n{url}")
                log(f"  ⚠️ {reason}")

            # Reset page to a clean state for next job
            try:
                # Close all tabs except one, navigate that one back to LinkedIn
                for extra in ctx.pages[1:]:
                    try: extra.close()
                    except: pass
                page = ctx.pages[0]
                page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
            except: pass
            time.sleep(5)

        ctx.close()

    log(f"\nDone. Applied {applied}/{len(pending)}")
    if pending:
        tg(f"📊 Company run complete: {applied}/{len(pending)} applied")

if __name__ == "__main__":
    main()
