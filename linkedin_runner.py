#!/usr/bin/env python3
"""OpenClaw — LinkedIn Easy Apply bot"""

import os, re, time, requests
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import anthropic
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

# ── Config ───────────────────────────────────────────────────────────────────────
SHEET_ID        = "18-_0J-ImLDrl2Z1Wm0iXskx02X0P9w6_pvG-nAjgkB8"
TAB_NAME        = "claudebot"

# On Railway: WORKSPACE and PROFILE_DIR live on the mounted volume at /data
# Locally: fall back to ~/openclaw/apply/...
_DATA_ROOT      = os.environ.get("DATA_DIR", "")
if _DATA_ROOT:
    WORKSPACE   = f"{_DATA_ROOT}/workspace"
    PROFILE_DIR = f"{_DATA_ROOT}/linkedin-profile"
    LOG_FILE    = f"{_DATA_ROOT}/runner.log"
else:
    WORKSPACE   = os.path.expanduser("~/openclaw/apply/workspace")
    PROFILE_DIR = os.path.expanduser("~/openclaw/apply/linkedin-profile")
    LOG_FILE    = os.path.expanduser("~/openclaw/apply/runner.log")

SERVICE_ACCOUNT = f"{WORKSPACE}/service_account.json"
AWS_ACCESS_KEY  = os.environ.get("AWS_ACCESS_KEY", "")
AWS_SECRET_KEY  = os.environ.get("AWS_SECRET_KEY", "")
AWS_REGION      = os.environ.get("AWS_REGION", "eu-west-2")
CLAUDE_MODEL    = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
TG_TOKEN        = os.environ.get("TG_TOKEN", "")
TG_CHAT         = os.environ.get("TG_CHAT", "")
HEADLESS        = os.environ.get("HEADLESS", "0") == "1"
COL_STATUS      = 6  # Column F

# ── Logging ──────────────────────────────────────────────────────────────────────
def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)

def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception: pass

# ── Sheet helpers ─────────────────────────────────────────────────────────────────
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    sa_json = os.environ.get("GOOGLE_SA_JSON", "")
    if sa_json:
        import json as _json
        from google.oauth2.service_account import Credentials as _Creds
        info = _json.loads(sa_json)
        creds = _Creds.from_service_account_info(info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(TAB_NAME)

def extract_url(cell):
    m = re.search(r'HYPERLINK\("([^"]+)"', cell, re.I)
    return m.group(1) if m else cell

def extract_title(cell):
    m = re.search(r'HYPERLINK\("[^"]+",\s*"([^"]+)"', cell, re.I)
    return m.group(1) if m else cell

def load_profile():
    env_map = {
        "cv":     "RESUME_MD",
        "cover":  "COVER_LETTER_MD",
        "answers":"ANSWERS_MD",
        "screen": "SCREENING_JSON",
    }
    file_map = {
        "cv":     "CV.md",
        "cover":  "COVER_LETTER.md",
        "answers":"APPLICATION_ANSWERS.md",
        "screen": "screening_answers.json",
    }
    out = {}
    for key, env_key in env_map.items():
        val = os.environ.get(env_key, "")
        if val:
            out[key] = val
            continue
        try:
            with open(f"{WORKSPACE}/{file_map[key]}") as f:
                out[key] = f.read()
        except Exception:
            out[key] = ""
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
                    cur = sel.input_value().strip()
                    if cur and cur.lower() not in ("select an option", "select", ""): continue
                    label = field_label(ctx_page, sel)
                    options_raw = sel.locator('option').all_inner_texts()
                    options = [o.strip() for o in options_raw if o.strip()]
                    log(f"  Empty select: '{label}' options={options[:6]}")
                    # Rule-based fast path
                    lbl = label.lower()
                    picked = None
                    if "english" in lbl and "proficien" in lbl:
                        picked = next((o for o in options if "native" in o.lower() or "bilingual" in o.lower()), None)
                    elif any(w in lbl for w in ["uk based", "looking for a uk", "right to work"]):
                        picked = next((o for o in options if o.lower() in ("yes", "yes, i do")), None)
                    elif any(w in lbl for w in ["sponsorship", "visa"]):
                        picked = next((o for o in options if "yes" in o.lower()), None)
                    elif any(w in lbl for w in ["sc clearance", "clearance", "security clearance"]):
                        picked = next((o for o in options if o.lower() in ("no", "no, i do not")), None)
                        if not picked:
                            picked = next((o for o in options if "no" in o.lower()), None)
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
                        picked = next((o for o in options if o.lower() in ("yes", "yes, i do", "yes, i have", "yes, i am")), None)
                        if not picked:
                            picked = next((o for o in options if "yes" in o.lower()), None)
                    # Fallback to Claude
                    if not picked and label:
                        picked = claude_select(client, label, options, role, company, profile)
                    if picked:
                        # Use value= matching to avoid whitespace issues with label=
                        matched = next((o for o in options_raw if o.strip() == picked), None)
                        try:
                            sel.select_option(label=matched or picked)
                        except Exception:
                            sel.select_option(value=picked)
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
def get_company_apply_url(page, ctx):
    """After clicking Apply (non-Easy-Apply), capture the company site URL.
    LinkedIn opens it in a new tab — wait for it and return the URL."""
    try:
        with ctx.expect_page(timeout=8000) as new_page_info:
            pass
        new_page = new_page_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=10000)
        url = new_page.url
        new_page.close()
        if url and "linkedin.com" not in url:
            return url
    except Exception:
        pass
    # Fallback: check current page URL changed
    time.sleep(2)
    if "linkedin.com" not in page.url:
        return page.url
    return None


def find_apply_url_via_search(page, role, company):
    """Google search for the job apply link directly in the browser."""
    try:
        query = f'{role} {company} apply site:greenhouse.io OR site:ashbyhq.com OR site:workday.com OR site:lever.co OR site:jobs.smartrecruiters.com'
        page.goto(f"https://www.google.com/search?q={requests.utils.quote(query)}", wait_until="domcontentloaded", timeout=15000)
        time.sleep(2)
        # Look for first result link that isn't LinkedIn or a recruiter
        for a in page.locator('a[href^="http"]').all()[:20]:
            try:
                href = a.get_attribute("href") or ""
                if not href.startswith("http"):
                    continue
                skip = ["google.", "linkedin.", "indeed.", "glassdoor.", "reed.co", "totaljobs", "cv-library", "jobsite"]
                if any(s in href for s in skip):
                    continue
                ats = ["greenhouse.io", "ashbyhq.com", "myworkdayjobs.com", "lever.co", "smartrecruiters.com", "icims.com", "taleo.net", "successfactors", "jobvite.com"]
                if any(s in href for s in ats):
                    log(f"  Found ATS link via search: {href[:80]}")
                    return href
            except Exception:
                continue
        # Fallback: try a broader search on the company careers page
        query2 = f'{role} {company} apply now'
        page.goto(f"https://www.google.com/search?q={requests.utils.quote(query2)}", wait_until="domcontentloaded", timeout=15000)
        time.sleep(2)
        for a in page.locator('a[href^="http"]').all()[:15]:
            try:
                href = a.get_attribute("href") or ""
                skip = ["google.", "linkedin.", "indeed.", "glassdoor.", "reed.co", "totaljobs"]
                if any(s in href for s in skip):
                    continue
                if "career" in href.lower() or "job" in href.lower() or "apply" in href.lower():
                    log(f"  Found careers link via search: {href[:80]}")
                    return href
            except Exception:
                continue
    except Exception as e:
        log(f"  Search error: {e}")
    return None


def _check_captcha(page):
    """Return True if LinkedIn is showing an actual CAPTCHA or auth challenge — NOT cookie banners."""
    try:
        url = page.url.lower()
        # URL-based signals (most reliable)
        if "checkpoint" in url or "authwall" in url or "/uas/login" in url:
            return True
        # DOM-based: look for actual challenge elements
        challenge_selectors = [
            '[data-id="challenge"]',
            'form#captcha-form',
            'input#captchaUserResponseInput',
            '[id*="captcha"]',
            'iframe[src*="recaptcha"]',
            'iframe[src*="hcaptcha"]',
        ]
        for sel in challenge_selectors:
            if page.locator(sel).count() > 0:
                return True
    except Exception:
        pass
    return False


def _wait_for_captcha_solve(page, role, company):
    """Alert via Telegram and wait up to 90s for the user to solve CAPTCHA."""
    tg(f"🔒 *CAPTCHA on LinkedIn*\nSolve it in the browser to continue.\n_{role} @ {company}_")
    log("  CAPTCHA detected — waiting up to 90s for you to solve it...")
    for _ in range(90):
        time.sleep(1)
        if not _check_captcha(page):
            log("  CAPTCHA solved — continuing")
            time.sleep(2)
            return True
    log("  CAPTCHA not solved in time — skipping")
    return False


def apply(page, ctx, url, role, company, profile, client):
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)

    log(f"  Page: {page.url[:80]}")

    # Accept cookie consent banner if present
    try:
        consent = page.locator('button:has-text("Accept"), button:has-text("Accept all")')
        if consent.count() > 0 and consent.first.is_visible():
            consent.first.click()
            time.sleep(1)
            log("  Accepted cookie consent")
    except Exception:
        pass

    # Check for CAPTCHA before doing anything
    if _check_captcha(page):
        solved = _wait_for_captcha_solve(page, role, company)
        if not solved:
            return False, "captcha unsolved"
        # Re-navigate to job after solving
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

    # Click Easy Apply (it's a link on LinkedIn, not a button)
    ea = page.get_by_role("link", name=re.compile(r"^Easy Apply", re.I))
    if ea.count() == 0:
        ea = page.get_by_role("button", name=re.compile(r"^Easy Apply", re.I))
    if ea.count() > 0:
        ea.first.click()
        log("  Easy Apply clicked")
    else:
        # Try regular Apply button → redirects to company site
        apply_btn = None
        # Try get_by_role first (most reliable — matches visible accessible elements)
        for name_pat in [
            re.compile(r"^Apply on company website", re.I),
            re.compile(r"^Apply now", re.I),
            re.compile(r"^Apply$", re.I),
            re.compile(r"^Apply", re.I),
        ]:
            for role in ("link", "button"):
                loc = page.get_by_role(role, name=name_pat)
                if loc.count() > 0:
                    try:
                        if loc.first.is_visible(timeout=1500):
                            apply_btn = loc.first
                            break
                    except Exception:
                        pass
            if apply_btn:
                break
        # Fallback: data-tracking attribute (company apply links)
        if not apply_btn:
            loc = page.locator('[data-tracking-control-name*="apply-link"]')
            if loc.count() > 0:
                try:
                    if loc.first.is_visible(timeout=1500):
                        apply_btn = loc.first
                except Exception:
                    pass

        if apply_btn:
            log("  Regular Apply button found — clicking to get company URL")
            try:
                with ctx.expect_page(timeout=8000) as new_page_info:
                    apply_btn.click()
                new_page = new_page_info.value
                new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                company_url = new_page.url
                new_page.close()
            except Exception:
                apply_btn.click()
                time.sleep(3)
                company_url = page.url if "linkedin.com" not in page.url else None

            if company_url and "linkedin.com" not in company_url:
                return False, f"company_site:{company_url}"
        else:
            log("  No apply button found — searching online")
            company_url = find_apply_url_via_search(page, role, company)
            if company_url:
                return False, f"company_site:{company_url}"

        return False, "no Easy Apply"

    try:
        page.wait_for_selector('[role="dialog"]', timeout=8000)
        log("  Modal open")
    except:
        # Maybe CAPTCHA appeared after clicking Easy Apply
        if _check_captcha(page):
            _wait_for_captcha_solve(page, role, company)
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

    # Build set of URLs already applied to anywhere in the sheet
    applied_urls = set()
    for row in rows[1:]:
        while len(row) <= applied_idx: row.append("")
        status = row[applied_idx].strip()
        if "applied" in status.lower():
            u = extract_url(row[0])
            if u:
                applied_urls.add(u.strip())

    pending = []
    for i, row in enumerate(rows[1:], start=2):
        while len(row) <= applied_idx: row.append("")
        if not row[applied_idx].strip():
            url = extract_url(row[0])
            if "linkedin.com" in url.lower():
                if url.strip() in applied_urls:
                    log(f"  Skipping (already applied): {extract_title(row[0])}")
                    continue
                pending.append((i, url, extract_title(row[0]), row[1] if len(row)>1 else ""))

    pending = list(reversed(pending))  # bottom-up
    if "--limit" in __import__("sys").argv:
        idx = __import__("sys").argv.index("--limit")
        try:
            pending = pending[:int(__import__("sys").argv[idx + 1])]
        except (IndexError, ValueError):
            pass

    log(f"Found {len(pending)} pending LinkedIn jobs (bottom-up)")
    if not pending: return

    profile = load_profile()
    client = anthropic.AnthropicBedrock(
        aws_access_key=AWS_ACCESS_KEY,
        aws_secret_key=AWS_SECRET_KEY,
        aws_region=AWS_REGION,
    )
    applied = 0

    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-setuid-sandbox"],
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        stealth_sync(page)

        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if "login" in page.url or "authwall" in page.url or "uas/login" in page.url:
            log("Not logged in — waiting for you to log in in the browser (up to 3 min)...")
            tg("🔒 *LinkedIn not logged in* — please log in in the browser window and I'll continue automatically.")
            for _ in range(180):
                time.sleep(1)
                try:
                    if not any(x in page.url for x in ["login", "authwall", "uas/login", "checkpoint"]):
                        log("Logged in — continuing.")
                        break
                except Exception:
                    pass
            else:
                log("Timed out waiting for login.")
                tg("⚠️ LinkedIn login timed out — runner stopped.")
                ctx.close()
                return
        for row_idx, url, title, company in pending:
            log(f"\n→ {title} @ {company}\n  {url}")
            try:
                # Recover page if browser crashed in previous iteration
                try:
                    page.title()
                except Exception:
                    log("  Page closed — reopening...")
                    try:
                        page = ctx.new_page()
                        stealth_sync(page)
                    except Exception as reopen_err:
                        log(f"  Could not reopen page: {reopen_err} — stopping.")
                        break
                ok, reason = apply(page, ctx, url, title, company, profile, client)
            except Exception as e:
                ok, reason = False, str(e)
                log(f"  Exception: {e}")

            if ok:
                sheet.update_cell(row_idx, COL_STATUS, "✅ Applied")
                tg(f"✅ *Applied!*\n*{title}* @ {company}")
                log("  ✅ Applied"); applied += 1
            elif reason.startswith("company_site:"):
                # Got a direct company application URL — hand off to company_runner
                import subprocess, sys
                company_url = reason[len("company_site:"):]
                log(f"  Handing off to company_runner: {company_url[:80]}")
                sheet.update_cell(row_idx, 7, company_url)  # save URL to Notes col G
                try:
                    proc = subprocess.Popen(
                        [sys.executable, os.path.join(os.path.dirname(__file__), "company_runner.py"),
                         company_url, title, company],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                    )
                    try:
                        stdout, _ = proc.communicate(timeout=480)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        stdout, _ = proc.communicate()
                        log("  company_runner timed out (8 min)")
                    result_returncode = proc.returncode
                    output = stdout or ""
                except Exception as e:
                    output = str(e)
                    result_returncode = 1
                # Check actual output — company_runner exits 0 even on failure
                success = result_returncode == 0 and any(
                    s in output for s in ["✅ Applied", "submitted", "application_submitted", "dry_run_complete"]
                ) and not any(
                    s in output for s in ["FAILED", "failed", "CAPTCHA", "stuck on same page"]
                )
                if success:
                    sheet.update_cell(row_idx, COL_STATUS, "✅ Applied")
                    tg(f"✅ *Applied (company site)*\n*{title}* @ {company}")
                    log("  ✅ Applied via company_runner"); applied += 1
                else:
                    sheet.update_cell(row_idx, COL_STATUS, "⚠️ Manual needed")
                    tg(f"⚠️ *company_runner failed*\n*{title}* @ {company}\n{company_url}")
                    log("  ⚠️ company_runner failed")
                    if output: log(f"  Output tail: {output[-300:]}")
            elif "no easy apply" in reason.lower() or "modal didn't open" in reason.lower():
                sheet.update_cell(row_idx, COL_STATUS, "⚠️ Manual needed")
                tg(f"⚠️ *Manual needed*\n*{title}* @ {company}\n{reason}\n{url}")
                log(f"  ⚠️ {reason}")
            else:
                sheet.update_cell(row_idx, COL_STATUS, "⚠️ Manual needed")
                tg(f"⚠️ *Manual needed*\n*{title}* @ {company}\n{reason}\n{url}")
                log(f"  ⚠️ {reason}")

            time.sleep(15)

        ctx.close()
    log(f"\nDone. Applied {applied}/{len(pending)}")
    tg(f"📊 Run complete: {applied}/{len(pending)} applied")

if __name__ == "__main__":
    main()
