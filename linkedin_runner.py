#!/usr/bin/env python3
"""OpenClaw — LinkedIn Easy Apply bot"""

import os, re, time, requests
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
PROFILE_DIR     = os.path.expanduser("~/openclaw/apply/linkedin-profile")
SERVICE_ACCOUNT = f"{WORKSPACE}/service_account.json"
AWS_ACCESS_KEY  = os.environ.get("AWS_ACCESS_KEY", "")
AWS_SECRET_KEY  = os.environ.get("AWS_SECRET_KEY", "")
AWS_REGION      = "eu-west-2"
CLAUDE_MODEL    = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
TG_TOKEN        = os.environ.get("TG_TOKEN", "")
TG_CHAT         = os.environ.get("TG_CHAT", "")
LOG_FILE        = os.path.expanduser("~/openclaw/apply/runner.log")
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

# ── Sheet helpers ─────────────────────────────────────────────────────────────────
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

# ── Form helpers ──────────────────────────────────────────────────────────────────
def field_label(page, el):
    """Get the visible label for a form element."""
    for attr in ("aria-label", "placeholder"):
        v = el.get_attribute(attr)
        if v and v.strip(): return v.strip()
    fid = el.get_attribute("id")
    if fid:
        try:
            t = page.locator(f'label[for="{fid}"]').inner_text()
            if t.strip(): return t.strip()
        except: pass
    labelledby = el.get_attribute("aria-labelledby")
    if labelledby:
        for lid in labelledby.split():
            try:
                t = page.locator(f"#{lid}").inner_text()
                if t.strip(): return t.strip()
            except: pass
    return ""

def is_years_field(label):
    lw = label.lower()
    return "year" in lw and any(w in lw for w in
        ["experience","work","model","financ","real estate","private equity","investment","industry"])

def claude_select(client, question, options, role, company, profile):
    """Ask Claude to pick the best option from a dropdown list."""
    try:
        opts = "\n".join(f"- {o}" for o in options if o.strip() and o.lower() != "select an option")
        r = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=100,
            messages=[{"role":"user","content":
                f"Pick the best answer for this LinkedIn application dropdown question for Alramina Myrzabekova "
                f"(Technology Risk Consultant at EY, 2 years exp, KCL Political Economy grad, fluent English, "
                f"based in London UK, requires visa sponsorship, female) applying for {role} at {company}.\n\n"
                f"Question: {question}\n\nOptions:\n{opts}\n\n"
                f"Reply with ONLY the exact option text, nothing else."}])
        return r.content[0].text.strip()
    except Exception as e:
        log(f"  Claude select error: {e}")
        return ""

def claude_answer(client, question, role, company, profile):
    try:
        r = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=250,
            messages=[{"role":"user","content":
                f"Answer this LinkedIn application question for Alramina Myrzabekova "
                f"(Technology Risk Consultant at EY, 2 years exp, KCL Political Economy grad) "
                f"applying for {role} at {company}.\n\n"
                f"Question: {question}\n\n"
                f"CV: {profile['cv'][:800]}\n\n"
                f"Reply with ONLY the answer, under 100 words."}])
        return r.content[0].text.strip()
    except Exception as e:
        log(f"  Claude error: {e}")
        return ""

def fill_fields(page, profile, client, role, company):
    """Fill any empty visible fields in the Easy Apply modal (main frame + iframes)."""

    def _fill_context(ctx, ctx_page):
        """Fill inputs/textareas inside a Playwright frame or locator context."""
        # Select 2026 resume if visible
        try:
            radios = ctx.locator('input[type="radio"]')
            for i in range(radios.count()):
                r = radios.nth(i)
                if not r.is_visible(): continue
                txt = r.evaluate("el => (el.closest('label') || el.parentElement || el).innerText || ''")
                if "2026" in txt and not r.is_checked():
                    r.click()
                    log("  Selected 2026 resume")
                    return True  # resume page done
        except: pass

        # Fill empty text/number inputs
        try:
            for inp in ctx.locator('input[type="text"],input[type="number"],input[type="tel"],input:not([type])').all():
                try:
                    if not inp.is_visible(): continue
                    if inp.input_value().strip(): continue
                    label = field_label(ctx_page, inp)
                    log(f"  Empty input: '{label}'")
                    if inp.get_attribute("type") == "number" or is_years_field(label):
                        inp.fill("2")
                    elif "phone" in label.lower() or "mobile" in label.lower():
                        inp.fill("7760289275")
                    elif "salary" in label.lower():
                        inp.fill("52000")
                    elif label:
                        ans = claude_answer(client, label, role, company, profile)
                        if ans: inp.fill(ans[:200])
                except: pass
        except: pass

        # Fill empty textareas
        try:
            for ta in ctx.locator('textarea').all():
                try:
                    if not ta.is_visible(): continue
                    if ta.input_value().strip(): continue
                    label = field_label(ctx_page, ta)
                    log(f"  Empty textarea: '{label}'")
                    ans = claude_answer(client, label or "Why are you interested in this role?", role, company, profile)
                    if ans: ta.fill(ans)
                except: pass
        except: pass

        # Fill unselected dropdowns
        try:
            for sel in ctx.locator('select').all():
                try:
                    if not sel.is_visible(): continue
                    if sel.input_value().strip() and sel.input_value() != "Select an option": continue
                    label = field_label(ctx_page, sel)
                    options = sel.locator('option').all_inner_texts()
                    log(f"  Empty select: '{label}' options={options[:6]}")
                    # Rule-based fast path
                    lbl = label.lower()
                    picked = None
                    if "english" in lbl and "proficien" in lbl:
                        picked = next((o for o in options if "native" in o.lower() or "bilingual" in o.lower()), None)
                    elif any(w in lbl for w in ["uk based", "looking for a uk", "right to work"]):
                        picked = next((o for o in options if o.strip().lower() in ("yes", "yes, i do")), None)
                    elif any(w in lbl for w in ["sponsorship", "visa"]):
                        picked = next((o for o in options if "yes" in o.lower()), None)
                    elif any(w in lbl for w in ["keen eye", "read the advert", "thoroughly read", "commit"]):
                        picked = next((o for o in options if "yes" in o.lower()), None)
                    # Experience / skill / leadership / qualification questions → always Yes
                    elif any(w in lbl for w in [
                        "experience", "built", "model", "leadership", "led", "managed",
                        "execution", "responsibility", "transaction", "deal", "project",
                        "pursuing", "career", "proficient", "familiar", "knowledge",
                        "certified", "qualification", "degree", "worked", "background",
                        "skill", "ability", "capable", "completed", "independently",
                        "have you", "do you have", "did you", "are you",
                    ]):
                        picked = next((o for o in options if o.strip().lower() in ("yes", "yes, i do", "yes, i have", "yes, i am")), None)
                        if not picked:
                            picked = next((o for o in options if "yes" in o.lower()), None)
                    # Fallback to Claude
                    if not picked and label:
                        picked = claude_select(client, label, options, role, company, profile)
                    if picked:
                        sel.select_option(label=picked)
                        log(f"    → selected '{picked}'")
                except: pass
        except: pass

        return False

    # Try main frame dialog first
    dialog = page.locator('[role="dialog"]').first
    if _fill_context(dialog, page):
        return

    # Also search all iframes (LinkedIn embeds form content in iframe)
    for frame in page.frames[1:]:
        try:
            if _fill_context(frame, frame.page):
                return
        except: pass

# ── Apply ─────────────────────────────────────────────────────────────────────────
def apply(page, url, role, company, profile, client):
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)

    # Click Easy Apply (it's a link on LinkedIn, not a button)
    ea = page.get_by_role("link", name=re.compile(r"^Easy Apply", re.I))
    if ea.count() == 0:
        ea = page.get_by_role("button", name=re.compile(r"^Easy Apply", re.I))
    if ea.count() == 0:
        return False, "no Easy Apply"
    ea.first.click()
    log("  Easy Apply clicked")

    try:
        page.wait_for_selector('[role="dialog"]', timeout=8000)
        log("  Modal open")
    except:
        return False, "modal didn't open"

    for step in range(12):
        time.sleep(2)

        # Check if submitted
        try:
            html = page.content().lower()
            if any(s in html for s in ["application submitted","applied successfully","thank you for applying"]):
                return True, "submitted"
        except: pass

        # Fill empty fields on this page
        fill_fields(page, profile, client, role, company)
        time.sleep(1)

        # Click Next / Review / Submit application.
        # Try main page first, then any iframes (LinkedIn embeds form in iframe).
        clicked = False
        for label in ["Submit application", "Review", "Next"]:
            # Main page
            btn = page.locator('button').filter(has_text=label)
            if btn.count() > 0:
                btn.first.click()
                log(f"  Step {step+1}: clicked '{label}'")
                clicked = True
            else:
                # Search inside any iframes
                for frame in page.frames[1:]:
                    try:
                        fbtn = frame.locator('button').filter(has_text=label)
                        if fbtn.count() > 0:
                            fbtn.first.click()
                            log(f"  Step {step+1}: clicked '{label}' (iframe)")
                            clicked = True
                            break
                    except Exception:
                        pass
            if clicked:
                if label == "Submit application":
                    time.sleep(3)
                    return True, "submitted"
                break

        if not clicked:
            log(f"  Step {step+1}: no nav button found")
            return False, "no nav button"

    return False, "max steps reached"

# ── Main ──────────────────────────────────────────────────────────────────────────
def main():
    log(f"\n{'='*40}\nLinkedIn Runner — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}")

    try:
        sheet = get_sheet()
        rows = sheet.get("A:G", value_render_option="FORMULA")
    except Exception as e:
        log(f"Sheet error: {e}"); tg(f"⚠️ Sheet error: {e}"); return

    headers = rows[0] if rows else []
    applied_idx = next((i for i,h in enumerate(headers) if h.lower() in ("applied","claude")), 5)

    pending = []
    for i, row in enumerate(rows[1:], start=2):
        while len(row) <= applied_idx: row.append("")
        if not row[applied_idx].strip():
            url = extract_url(row[0])
            if "linkedin.com" in url.lower():
                pending.append((i, url, extract_title(row[0]), row[1] if len(row)>1 else ""))

    log(f"Found {len(pending)} pending LinkedIn jobs")
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
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        Stealth().apply_stealth_sync(page)

        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if "login" in page.url or "authwall" in page.url:
            log("Not logged in — please log in and press Enter"); input()

        for row_idx, url, title, company in pending:
            log(f"\n→ {title} @ {company}\n  {url}")
            try:
                ok, reason = apply(page, url, title, company, profile, client)
            except Exception as e:
                ok, reason = False, str(e)
                log(f"  Exception: {e}")

            if ok:
                sheet.update_cell(row_idx, COL_STATUS, "✅ Applied")
                tg(f"✅ *Applied!*\n*{title}* @ {company}")
                log("  ✅ Applied"); applied += 1
            elif "no easy apply" in reason.lower() or "modal didn't open" in reason.lower():
                # Try to grab the external apply URL from the job page and save to Notes (col G)
                try:
                    company_url = ""
                    for sel in ['a[href*="apply"]:not([href*="linkedin"])', '[data-tracking-control-name*="apply-link"]']:
                        loc = page.locator(sel)
                        if loc.count() > 0:
                            href = loc.first.get_attribute("href")
                            if href and "linkedin.com" not in href:
                                company_url = href
                                break
                    if company_url:
                        sheet.update_cell(row_idx, 7, company_url)  # col G = Notes
                        log(f"  Saved company URL: {company_url}")
                except Exception: pass
                sheet.update_cell(row_idx, COL_STATUS, "🔗 Company site")
                log("  🔗 Company site")
            else:
                sheet.update_cell(row_idx, COL_STATUS, "⚠️ Manual needed")
                tg(f"⚠️ *Manual needed*\n*{title}* @ {company}\n{reason}\n{url}")
                log(f"  ⚠️ {reason}")

            time.sleep(30)

        ctx.close()
    log(f"\nDone. Applied {applied}/{len(pending)}")
    tg(f"📊 Run complete: {applied}/{len(pending)} applied")

if __name__ == "__main__":
    main()
