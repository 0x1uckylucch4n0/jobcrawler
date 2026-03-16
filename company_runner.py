#!/usr/bin/env python3
"""Company website application bot.
Picks up rows marked 'Company site' from the Google Sheet, navigates to the
company application form, and applies using Claude Opus vision + structured
page-level field extraction.
"""

import json, os, re, time, base64, requests, imaplib, email as email_lib, yaml
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import gspread

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from google.oauth2.service_account import Credentials
import anthropic
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# -- Config -------------------------------------------------------------------
SHEET_ID        = "18-_0J-ImLDrl2Z1Wm0iXskx02X0P9w6_pvG-nAjgkB8"
TAB_NAME        = "claudebot"
WORKSPACE       = os.path.expanduser("~/openclaw/apply/workspace")
PROFILE_DIR     = os.path.expanduser("~/openclaw/apply/company-profile")
SERVICE_ACCOUNT = f"{WORKSPACE}/service_account.json"
CV_PATH         = f"{WORKSPACE}/CV.pdf"
AWS_ACCESS_KEY  = os.environ.get("AWS_ACCESS_KEY", "")
AWS_SECRET_KEY  = os.environ.get("AWS_SECRET_KEY", "")
AWS_REGION      = "eu-west-2"
CLAUDE_MODEL    = "eu.anthropic.claude-opus-4-6-v1"
TG_TOKEN        = os.environ.get("TG_TOKEN", "")
TG_CHAT         = os.environ.get("TG_CHAT", "")
# Gmail password for reading verification codes (Oracle HCM, etc.)
LOG_FILE        = os.path.expanduser("~/openclaw/apply/runner.log")
WORKDAY_DEFAULT_PASSWORD = os.environ.get("WORKDAY_DEFAULT_PASSWORD", "")  # Standard password for all new Workday accounts
_workday_passwords = {}  # domain → password used (populated during account creation)
COL_STATUS      = 6  # Column F

MAX_PAGES       = 25   # max pages per application (Workday multi-page forms can be 15+ pages)
MAX_RETRIES     = 2    # error-fix retries per page
MAX_STUCK       = 2    # pages with zero progress before giving up

# -- Applicant Profile --------------------------------------------------------
PROFILE = {
    "first_name": "Alramina",
    "last_name": "Myrzabekova",
    "email": "alramina.myrzabekova@gmail.com",
    "phone": "7760289275",
    "phone_full": "+447760289275",
    "address_line_1": "27 Albert Embankment",
    "city": "London",
    "postcode": "SE1 7AQ",
    "country": "United Kingdom",
    "county": "Greater London",
    "current_role": "Technology Risk Consultant at EY Financial Services",
    "experience_years": "2",
    "education": "BSc (Hons) Political Economy (2:1), King's College London (2020-2023)",
    "visa_sponsorship": "Yes",
    "right_to_work_uk": "No - requires Skilled Worker visa sponsorship from the new employer",
    "notice_period": "2 weeks",
    "salary_expectation": "52000",
    "gender": "Female",
    "ethnicity": "Prefer not to say / I prefer not to answer",
    "disability": "No",
    "veteran": "No",
    "criminal_record": "No",
    "former_employee": "No",
    "over_18": "Yes",
    "authorized_to_work": "No",
    "languages": "English (Fluent), Russian (Fluent), Kazakh (Fluent), French (Intermediate), Spanish (Beginner)",
    "linkedin": "https://www.linkedin.com/in/alramina-myrzabekova",
    "hear_about_us": "Job Posting",
}

# -- Logging ------------------------------------------------------------------
def is_workday_url(url):
    """Return True if URL is a Workday ATS page (any tenant)."""
    return "myworkdayjobs.com" in url or "myworkdaysite.com" in url


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass

def tg_photo(photo_bytes, caption=""):
    """Send a photo to Telegram (for CAPTCHA screenshots)."""
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            data={"chat_id": TG_CHAT, "caption": caption[:1024]},
            files={"photo": ("captcha.png", photo_bytes, "image/png")},
            timeout=15,
        )
    except Exception:
        pass

def tg_poll_reply(prompt_msg, timeout=300):
    """Send a message to Telegram and wait for a reply (for CAPTCHA solving).
    Returns the reply text or None if timeout."""
    if not TG_TOKEN or not TG_CHAT:
        return None
    try:
        # Get current update_id to only look at new messages
        resp = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": -1, "limit": 1}, timeout=10,
        )
        data = resp.json()
        last_id = 0
        if data.get("result"):
            last_id = data["result"][-1]["update_id"]

        # Poll for reply
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(5)
            resp = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset": last_id + 1, "limit": 10}, timeout=10,
            )
            data = resp.json()
            for update in data.get("result", []):
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) == str(TG_CHAT):
                    text = msg.get("text", "").strip()
                    if text:
                        # Acknowledge
                        last_id = update["update_id"]
                        return text
        return None
    except Exception as e:
        log(f"  tg_poll error: {e}")
        return None


# -- Answer Cache -------------------------------------------------------------
ANSWER_CACHE_PATH = os.path.expanduser("~/openclaw/apply/answer_cache.yaml")

def _load_answer_cache():
    """Load cached answers from YAML file."""
    try:
        if os.path.exists(ANSWER_CACHE_PATH):
            with open(ANSWER_CACHE_PATH) as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}

def _save_answer_cache(cache):
    """Save answer cache to YAML file."""
    try:
        os.makedirs(os.path.dirname(ANSWER_CACHE_PATH), exist_ok=True)
        with open(ANSWER_CACHE_PATH, "w") as f:
            yaml.dump(cache, f, default_flow_style=False, allow_unicode=True)
    except Exception as e:
        log(f"  cache save error: {e}")

def _cache_key(label):
    """Normalize a field label into a cache key."""
    if not label:
        return ""
    # Lowercase, strip whitespace, remove special chars
    key = re.sub(r'[^a-z0-9 ]', '', label.lower().strip())
    key = re.sub(r'\s+', ' ', key).strip()
    return key

def cache_answers(fields, actions):
    """Cache successful fill/select/radio answers by question label."""
    cache = _load_answer_cache()
    # Build a map of selector -> action for quick lookup
    action_map = {}
    for a in actions:
        if not isinstance(a, dict):
            continue
        sel = a.get("selector", "")
        if sel:
            action_map[sel] = a

    for f in fields:
        label = f.get("label", "")
        key = _cache_key(label)
        if not key or len(key) < 5:
            continue
        # Find matching action
        fid = f.get("id", "")
        fname = f.get("name", "")
        matched_action = None
        for sel, a in action_map.items():
            if (fid and fid in sel) or (fname and fname in sel) or (label and label in sel):
                matched_action = a
                break
        if not matched_action:
            continue
        value = matched_action.get("value", matched_action.get("pick", ""))
        if value and len(value) > 0:
            cache[key] = {
                "value": value,
                "action": matched_action.get("action", "fill"),
                "label": label,
            }
    _save_answer_cache(cache)

def get_cached_answers(fields):
    """Look up cached answers for fields. Returns list of actions from cache."""
    cache = _load_answer_cache()
    if not cache:
        return []
    cached_actions = []
    for f in fields:
        label = f.get("label", "")
        key = _cache_key(label)
        if not key or key not in cache:
            continue
        # Skip if field already has a value
        if f.get("value") and len(f["value"].strip()) > 1:
            continue
        entry = cache[key]
        fid = f.get("id", "")
        selector = f"[id=\"{fid}\"]" if fid else f.get("selector", label)
        field_type = f.get("type", "")
        cached_action = entry.get("action", "fill")
        # Don't apply a custom_dropdown action to a plain text/tel/number input
        if cached_action == "custom_dropdown" and field_type in ("text", "tel", "number", "email", "textarea"):
            continue
        cached_actions.append({
            "action": cached_action,
            "selector": selector,
            "value": entry["value"],
            "_cached": True,
        })
        log(f"    cache hit: '{label[:40]}' = '{entry['value'][:30]}'")
    return cached_actions


# -- Application History (dedup) ----------------------------------------------
HISTORY_PATH = os.path.expanduser("~/openclaw/apply/application_history.yaml")

def _load_history():
    try:
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH) as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}

def _save_history(history):
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, "w") as f:
            yaml.dump(history, f, default_flow_style=False, allow_unicode=True)
    except Exception:
        pass

def _history_key(company, role):
    """Create a dedup key from company + role."""
    c = re.sub(r'[^a-z0-9]', '', company.lower())
    r = re.sub(r'[^a-z0-9]', '', role.lower())[:60]
    return f"{c}:{r}"

def was_already_applied(company, role):
    """Check if we already applied to this company/role combo.
    Exact key match first, then fuzzy: checks if role matches any entry for a
    company whose normalised name contains or is contained by this company name."""
    h = _load_history()
    key = _history_key(company, role)
    if key in h:
        return True
    # Fuzzy: normalised role must match exactly; company name just needs to overlap
    c_norm = re.sub(r'[^a-z0-9]', '', company.lower())
    r_norm = re.sub(r'[^a-z0-9]', '', role.lower())[:60]
    for existing_key, entry in h.items():
        ec = re.sub(r'[^a-z0-9]', '', entry.get("company", "").lower())
        er = re.sub(r'[^a-z0-9]', '', entry.get("role", "").lower())[:60]
        if er == r_norm and (c_norm in ec or ec in c_norm) and len(c_norm) >= 3:
            return True
    return False

def record_application(company, role, success, reason=""):
    """Record an application attempt."""
    h = _load_history()
    key = _history_key(company, role)
    h[key] = {
        "company": company,
        "role": role[:80],
        "success": success,
        "reason": reason[:100],
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _save_history(h)


# -- Sheet helpers ------------------------------------------------------------
def get_sheet():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(TAB_NAME)

def extract_url(cell):
    m = re.search(r'HYPERLINK\("([^"]+)"', cell, re.I)
    return m.group(1) if m else cell

def extract_title(cell):
    m = re.search(r'HYPERLINK\("[^"]+",\s*"([^"]+)"', cell, re.I)
    return m.group(1) if m else cell

def load_workspace_profile():
    out = {}
    for key, fname in [
        ("cv", "CV.md"),
        ("cover", "COVER_LETTER.md"),
        ("answers", "APPLICATION_ANSWERS.md"),
        ("screen", "screening_answers.json"),
    ]:
        try:
            with open(f"{WORKSPACE}/{fname}") as f:
                out[key] = f.read()
        except Exception:
            out[key] = ""
    return out


# =============================================================================
#  FIELD EXTRACTOR -- scans the DOM for every interactive element
# =============================================================================

EXTRACT_FIELDS_JS = """() => {
    const results = [];
    const seen = new Set();

    function getLabel(el) {
        // 1. aria-label
        let label = el.getAttribute('aria-label') || '';
        // 2. label[for]
        if (!label && el.id) {
            const lbl = document.querySelector('label[for="' + el.id + '"]');
            if (lbl) label = lbl.innerText.trim();
        }
        // 3. aria-labelledby
        if (!label && el.getAttribute('aria-labelledby')) {
            const ids = el.getAttribute('aria-labelledby').split(' ');
            for (const id of ids) {
                const le = document.getElementById(id);
                if (le && le.innerText.trim()) { label = le.innerText.trim(); break; }
            }
        }
        // 4. placeholder
        if (!label) label = el.getAttribute('placeholder') || '';
        // 5. enclosing label
        if (!label) {
            const enc = el.closest('label');
            if (enc) label = enc.innerText.trim().split('\\n')[0];
        }
        // 6. nearest preceding text (fieldset legend, div label, etc.)
        if (!label) {
            const parent = el.closest('fieldset,.form-group,[class*="form-field"],[class*="formField"],[class*="question"],div');
            if (parent) {
                const lg = parent.querySelector('legend,label,[class*="label"],[slot="label"]');
                if (lg && lg.innerText.trim()) label = lg.innerText.trim().split('\\n')[0];
            }
        }
        // 7. data-automation-id (Workday)
        if (!label) label = el.getAttribute('data-automation-id') || '';
        return label.slice(0, 150);
    }

    // -- Standard inputs, selects, textareas --
    document.querySelectorAll('input,select,textarea').forEach(el => {
        if (!el.offsetParent) return;
        const key = el.id || el.name || el.getAttribute('data-automation-id') || '';
        if (key && seen.has(key)) return;
        if (key) seen.add(key);

        const tag = el.tagName;
        const type = (el.type || 'text').toLowerCase();

        if (type === 'hidden' || type === 'submit') return;

        const field = {
            tag, type, label: getLabel(el),
            id: el.id || '', name: el.name || '',
            automationId: el.getAttribute('data-automation-id') || '',
            value: (el.value || '').slice(0, 200),
            required: el.required || el.getAttribute('aria-required') === 'true',
            disabled: el.disabled,
            readonly: el.readOnly,
            checked: el.checked || false,
        };

        if (tag === 'SELECT') {
            field.options = Array.from(el.options).map(o => o.text.trim()).filter(t => t);
        }
        if (type === 'radio') {
            // Group all radios by name
            const groupKey = 'radio:' + (el.name || el.id);
            if (seen.has(groupKey)) return;
            seen.add(groupKey);
            const siblings = el.name
                ? document.querySelectorAll('input[type="radio"][name="' + el.name + '"]')
                : [el];
            field.options = Array.from(siblings).filter(s => s.offsetParent).map(s => {
                const lbl = document.querySelector('label[for="'+s.id+'"]');
                return {
                    id: s.id,
                    selector: s.id ? '[id="' + s.id + '"]' : '',
                    value: s.value,
                    text: (lbl || s.parentElement || s).innerText.trim().split('\\n')[0],
                    checked: s.checked
                };
            });
            field.type = 'radio_group';
        }

        results.push(field);
    });

    // -- Custom dropdowns (Oracle JET, React Select, Workday) --
    document.querySelectorAll('[role="combobox"],[class*="oj-combobox"],[class*="oj-select"],[class*="css-"][class*="-control"],button[aria-haspopup="listbox"]').forEach(el => {
        if (!el.offsetParent) return;
        const key = 'custom:' + (el.id || el.getAttribute('data-automation-id') || Math.random());
        if (seen.has(key)) return;
        seen.add(key);
        const input = el.querySelector('input');
        results.push({
            tag: 'CUSTOM_DROPDOWN', type: 'custom_dropdown',
            label: getLabel(el) || (input ? getLabel(input) : ''),
            id: el.id || '', name: '',
            automationId: el.getAttribute('data-automation-id') || '',
            value: (input ? input.value : el.innerText || '').trim().slice(0, 200),
            required: el.getAttribute('aria-required') === 'true',
            disabled: false, readonly: false, checked: false,
            options: []
        });
    });

    // -- Standalone checkboxes not caught above --
    document.querySelectorAll('input[type="checkbox"]').forEach(el => {
        if (!el.offsetParent) return;
        const key = 'cb:' + (el.id || el.name || '');
        if (key !== 'cb:' && seen.has(key)) return;
        if (key !== 'cb:') seen.add(key);
        const lbl = el.id ? document.querySelector('label[for="'+el.id+'"]') : null;
        results.push({
            tag: 'INPUT', type: 'checkbox',
            label: getLabel(el) || (lbl ? lbl.innerText.trim() : ''),
            id: el.id || '', name: el.name || '',
            selector: el.id ? '[id="' + el.id + '"]' : '',
            automationId: el.getAttribute('data-automation-id') || '',
            value: '', required: el.required,
            disabled: el.disabled, readonly: false,
            checked: el.checked
        });
    });

    // -- Yes/No button toggles (Ashby, custom ATS) --
    const yesnoSeen = new Set();
    document.querySelectorAll('button').forEach(btn => {
        if (!btn.offsetParent) return;
        const text = btn.innerText.trim();
        if (text !== 'Yes' && text !== 'No') return;
        const parent = btn.parentElement;
        if (!parent) return;
        // Find the hidden checkbox name to use as unique key
        const hiddenCb = parent.querySelector('input[type="checkbox"],input[type="hidden"]');
        const name = hiddenCb ? hiddenCb.name : '';
        if (!name || yesnoSeen.has(name)) return;
        yesnoSeen.add(name);
        // Get question text
        let question = '';
        let el = parent;
        for (let i = 0; i < 5 && !question; i++) {
            const prev = el.previousElementSibling;
            if (prev) {
                const t = prev.innerText.trim().split('\\n')[0];
                if (t && t.length > 5 && t.length < 200) { question = t; break; }
            }
            el = el.parentElement;
            if (!el) break;
        }
        // Check if already answered (one button has "selected" class or aria-pressed)
        const buttons = parent.querySelectorAll('button');
        const selected = Array.from(buttons).find(b =>
            b.classList.toString().includes('selected') || b.getAttribute('aria-pressed') === 'true'
            || b.getAttribute('data-state') === 'on'
        );
        results.push({
            tag: 'YESNO_TOGGLE', type: 'yesno_toggle',
            label: question || 'Yes/No question',
            id: name, name: name,
            selector: '', // executor handles these specially
            automationId: '',
            value: selected ? selected.innerText.trim() : '',
            required: true,
            disabled: false, readonly: false, checked: false,
            options: Array.from(buttons).map(b => b.innerText.trim())
        });
    });

    // -- File upload inputs (may be hidden but still interactive) --
    document.querySelectorAll('input[type="file"]').forEach(el => {
        const key = 'file:' + (el.id || el.name || 'anon');
        if (seen.has(key)) return;
        seen.add(key);
        results.push({
            tag: 'INPUT', type: 'file',
            label: getLabel(el) || 'File upload',
            id: el.id || '', name: el.name || '',
            automationId: el.getAttribute('data-automation-id') || '',
            value: el.value ? 'has_file' : '',
            required: el.required, disabled: el.disabled,
            readonly: false, checked: false
        });
    });

    return results;
}"""


def extract_page_fields(page):
    """Run JS to extract all form fields from the current page."""
    try:
        return page.evaluate(EXTRACT_FIELDS_JS)
    except Exception as e:
        log(f"  Field extraction error: {e}")
        return []


# =============================================================================
#  CLAUDE PAGE ANALYZER -- sends screenshot + fields, gets back batch actions
# =============================================================================

SYSTEM_PROMPT = """\
You are an expert job application bot. You are filling out a job application form for:

{profile_summary}

APPLICANT DATA (use these exact values):
{profile_json}

WORKSPACE FILES AVAILABLE:
- CV/Resume: ready for upload when a file upload field is present

YOUR TASK:
Look at the screenshot and the extracted form fields below. Return a JSON array of
ALL actions needed to fill every empty/unfilled field on this visible page.

ACTION TYPES:
- {{"action": "fill", "selector": "<css_selector>", "value": "<text>"}}
  For text inputs, email fields, textareas, phone numbers, etc.

- {{"action": "select_option", "selector": "<css_selector>", "value": "<option_text>"}}
  For native <select> dropdowns. Use the visible option text.

- {{"action": "custom_dropdown", "selector": "<css_selector>", "search": "<text_to_type>", "pick": "<option_text>"}}
  For custom dropdowns (Oracle JET, React Select, Workday). Will click to open, type to search, then pick.

- {{"action": "radio", "selector": "<css_selector>"}}
  Click a specific radio button by its ID selector (e.g. "#radio_yes").

- {{"action": "check", "selector": "<css_selector>"}}
  Check a checkbox (for terms, agreements, acknowledgments).

- {{"action": "upload_cv"}}
  Upload the CV/resume to a file input on the page.

- {{"action": "yesno_toggle", "selector": "[name='hidden_input_name']", "value": "Yes", "description": "question text"}}
  For Yes/No toggle button groups (type=yesno_toggle in extracted fields). Use the field name as selector.
  Include the question text in "description" for fallback matching.

- {{"action": "click", "selector": "<css_selector>", "description": "<what_and_why>"}}
  Click a button or link (e.g. to expand a section, dismiss a dialog, acknowledge something).
  Use this for any interactive element that isn't a form field.

SELECTOR PRIORITY (use the first one that applies):
1. [id="elementId"] if the field has an id (ALWAYS use attribute syntax, NEVER #id)
2. Use the "selector" field from the extracted data if provided (for radios/checkboxes)
3. [data-automation-id="value"] for Workday fields
4. [name="value"] for fields with a name attribute
5. "label text" as a last resort (the executor will search by label)

CRITICAL SELECTOR RULES:
- ALWAYS use [id="xxx"] NOT #xxx for selectors. IDs starting with digits break #id syntax.
- ONLY use selectors from the extracted fields data below. NEVER invent or guess CSS class selectors.
- For custom dropdowns (type=custom_dropdown): use the field's id, name, or automationId from the data.
- If a field has automationId, use [data-automation-id="value"] as selector.
- NEVER use selectors like button.css-xxxxx — these are random class names that change on every load.
- For radio buttons without standard IDs (e.g. Ashby): use the VISIBLE OPTION TEXT as the value.
  The executor will search by label text if the CSS selector doesn't match.
- For yesno_toggle fields: use [name="the_field_name"] and include the question in "description".

RULES:
- Fill EVERY empty field on the page. Don't skip any.
- For fields that already have correct values, skip them.
- For phone number fields: use "7760289275" (no country code prefix -- it's usually a separate dropdown).
- For "How did you hear about us" / source fields: pick "Job Posting", "Online", "LinkedIn", or the simplest available option. Never pick "Employee Referral".
- For yes/no questions: answer based on the applicant profile (sponsorship=Yes, over 18=Yes, former employee=No, criminal record=No, etc.)
- For "right to work in UK" / "eligible to work" / "work authorization" / "do you currently have the right to work" questions: answer No -- the applicant does NOT currently have the right to work without sponsorship and requires a Skilled Worker visa sponsored by the new employer. Do NOT answer Yes to these questions.
- For open text / cover letter / "why this role" fields: write 2-3 professional sentences referencing the applicant's ACTUAL CV experience and tailored to the specific job description provided. Mention relevant projects, skills, and achievements from the CV. Never fabricate experience or skills not in the CV.
- For ethnicity/race questions: ALWAYS pick "I prefer not to answer" or "Prefer not to say". Never guess ethnicity.
- For disability/communities questions: ALWAYS pick "I prefer not to answer" or "None of the above".
- If you see a "Discard Application?" / "Leave?" dialog: return a click action on "Continue" or "Cancel" to stay.
- If you see a "Use My Last Application" prompt: ignore it, do NOT click it.
- If the page looks like a success/confirmation page ("Thank you", "Application submitted"): return {{"action": "DONE"}}
- If the page is completely broken or requires something impossible (e.g. login to unknown system): return {{"action": "SKIP", "reason": "explanation"}}
- Do NOT include a "click next/submit" action -- navigation is handled separately.

CRITICAL: Your response must be ONLY a valid JSON array. No text before or after. No explanation.
No markdown. No code fences. No "Looking at..." or "I need to..." preamble. JUST the JSON array.
Example: [{{"action": "fill", "selector": "[id=\"firstName\"]", "value": "Alramina"}}, {{"action": "check", "selector": "[id=\"agreeTerms\"]"}}]
If there are no fields to fill (page is already complete or is a non-form page): return []
"""


def build_system_prompt(role, company, ws_profile, job_description=""):
    profile_summary = (
        f"Name: {PROFILE['first_name']} {PROFILE['last_name']}\n"
        f"Applying for: {role} at {company}\n"
        f"Email: {PROFILE['email']} | Phone: {PROFILE['phone']}\n"
        f"Location: {PROFILE['address_line_1']}, {PROFILE['city']}, {PROFILE['postcode']}\n"
        f"Current role: {PROFILE['current_role']} ({PROFILE['experience_years']} years)\n"
        f"Education: {PROFILE['education']}\n"
        f"Visa sponsorship required: {PROFILE['visa_sponsorship']}"
    )
    cv_text = ws_profile.get("cv", "")
    answers_text = ws_profile.get("answers", "")
    extra = ""
    if cv_text:
        extra += f"\n\nFULL CV:\n{cv_text[:4000]}"
    if answers_text:
        extra += f"\n\nPRE-WRITTEN ANSWERS (adapt these to the question, don't copy verbatim):\n{answers_text[:3000]}"
    if job_description:
        extra += f"\n\nJOB DESCRIPTION (use this to tailor 'why this role' and cover letter answers):\n{job_description[:2000]}"

    return SYSTEM_PROMPT.format(
        profile_summary=profile_summary + extra,
        profile_json=json.dumps(PROFILE, indent=2),
    )


def screenshot_b64(page):
    return base64.b64encode(page.screenshot(full_page=False)).decode()


def analyze_page(client, page, fields, system, role, company, error_context=None):
    """Send screenshot + field data to Claude, get back action list."""
    img = screenshot_b64(page)

    # Slim down field data — only include actionable fields (skip disabled/readonly/already-filled)
    slim_fields = []
    for f in fields:
        if f.get("disabled") or f.get("readonly"):
            continue
        slim = {
            "type": f["type"],
            "label": f.get("label", ""),
            "id": f.get("id", ""),
        }
        if f.get("selector"):
            slim["selector"] = f["selector"]
        if f.get("automationId"):
            slim["automationId"] = f["automationId"]
        if f.get("name"):
            slim["name"] = f["name"]
        if f.get("value"):
            slim["value"] = f["value"]
        if f.get("required"):
            slim["required"] = True
        if f.get("options"):
            slim["options"] = f["options"]
        if f.get("checked"):
            slim["checked"] = True
        slim_fields.append(slim)

    user_text = f"Applying for: {role} at {company}\n\n"
    user_text += f"Current URL: {page.url}\n\n"
    user_text += f"EXTRACTED FORM FIELDS ({len(slim_fields)} found):\n"
    user_text += json.dumps(slim_fields, indent=2, default=str)[:10000]

    if error_context:
        user_text += f"\n\nPREVIOUS ATTEMPT HAD ERRORS:\n{error_context}"
        user_text += "\nPlease fix these issues. Try alternative selectors or values."

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
                {"type": "text", "text": user_text},
            ],
        }],
    )

    raw = resp.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    # Try to extract JSON array even if there's surrounding text
    try:
        actions = json.loads(raw)
        if isinstance(actions, dict):
            actions = [actions]
        return actions
    except json.JSONDecodeError:
        pass

    # Fallback 1: find JSON array in the response
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if match:
        try:
            actions = json.loads(match.group())
            if isinstance(actions, list):
                return actions
        except json.JSONDecodeError:
            pass

    # Fallback 2: truncated JSON array — try to recover complete objects
    match = re.search(r'\[(.+)', raw, re.DOTALL)
    if match:
        inner = match.group(1)
        # Find all complete JSON objects
        recovered = []
        for obj_match in re.finditer(r'\{[^{}]*\}', inner):
            try:
                obj = json.loads(obj_match.group())
                recovered.append(obj)
            except json.JSONDecodeError:
                continue
        if recovered:
            log(f"  Recovered {len(recovered)} actions from truncated JSON")
            return recovered

    log(f"  Claude returned non-JSON: {raw[:150]}")
    return []


# =============================================================================
#  ACTION EXECUTOR -- handles all interaction types with fallbacks
# =============================================================================

def _find_element(page, selector):
    """Try multiple strategies to find an element from a selector string."""
    if not selector:
        return None

    # Normalize: #id where id starts with a digit -> [id="..."] (CSS spec requirement)
    normalized = selector
    if normalized.startswith("#"):
        raw_id = normalized[1:]
        if raw_id and (raw_id[0].isdigit() or raw_id[0] == '-'):
            normalized = f'[id="{raw_id}"]'

    # Strategy 1: Direct CSS selector
    for sel in [normalized, selector] if normalized != selector else [selector]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            pass

    # Strategy 2: Attribute selector fallback for any #id
    if selector.startswith("#") and normalized == selector:
        try:
            loc = page.locator(f'[id="{selector[1:]}"]')
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            pass

    # Strategy 3: data-automation-id (Workday) -- extract from selector like [data-automation-id='...']
    da_match = re.search(r'data-automation-id[=\s]*["\']([^"\']+)', selector)
    if da_match:
        da_id = da_match.group(1)
        for tag in ["button", "div", "input", "select", "a", "*"]:
            try:
                loc = page.locator(f'{tag}[data-automation-id="{da_id}"]')
                if loc.count() > 0 and loc.first.is_visible():
                    return loc.first
            except Exception:
                pass

    # Strategy 4: If selector looks like a label (no # or [ prefix), search by label/text
    if not selector.startswith(("#", "[", ".", "/")):
        label = selector.strip('"').strip("'")
        for loc in [
            page.get_by_label(re.compile(re.escape(label), re.I)),
            page.get_by_placeholder(re.compile(re.escape(label), re.I)),
            page.get_by_text(re.compile(re.escape(label), re.I)),
        ]:
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    return loc.first
            except Exception:
                pass

    return None


def _fill_with_events(page, el, value):
    """Fill a field and dispatch events for frameworks like Oracle JET, Angular, React.
    Uses React-compatible native setter + event dispatch to ensure React state updates."""
    try:
        el.scroll_into_view_if_needed()
        time.sleep(0.1)
    except Exception:
        pass

    # Primary strategy: React-compatible fill via native setter + synthetic events.
    # This works for React, Angular, Vue, Oracle JET, and plain HTML.
    try:
        el.evaluate(f"""el => {{
            // Focus the element
            el.focus();
            // Use native setter to bypass React's synthetic event system
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            )?.set || Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value'
            )?.set;
            if (nativeSetter) {{
                nativeSetter.call(el, {json.dumps(value)});
            }} else {{
                el.value = {json.dumps(value)};
            }}
            // Dispatch events that React/Angular/Vue listen to
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            el.dispatchEvent(new FocusEvent('blur', {{bubbles: true}}));
        }}""")
        page.keyboard.press("Tab")
        time.sleep(0.3)
        return True
    except Exception:
        pass

    # Fallback 1: Playwright .fill() (works for simple HTML forms)
    try:
        el.fill(value)
        time.sleep(0.2)
        return True
    except Exception:
        pass

    # Fallback 2: Click + type character by character (works for custom inputs)
    try:
        el.click()
        time.sleep(0.1)
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        el.type(value, delay=20)
        time.sleep(0.1)
        el.evaluate("""el => {
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new FocusEvent('blur', {bubbles: true}));
        }""")
        page.keyboard.press("Tab")
        time.sleep(0.2)
        return True
    except Exception:
        pass

    return False


def _select_native(page, el, value):
    """Select an option from a native <select> dropdown."""
    try:
        el.scroll_into_view_if_needed()
        el.select_option(label=value)
        el.evaluate("el => el.dispatchEvent(new Event('change', {bubbles: true}))")
        time.sleep(0.3)
        return True
    except Exception:
        pass
    # Try by partial match
    try:
        options = el.evaluate("""el => Array.from(el.options).map(o => ({
            index: o.index, text: o.text.trim(), value: o.value
        }))""")
        val_lower = value.lower()
        for opt in options:
            if val_lower in opt["text"].lower():
                el.select_option(index=opt["index"])
                el.evaluate("el => el.dispatchEvent(new Event('change', {bubbles: true}))")
                log(f"    -> partial match: '{opt['text']}'")
                time.sleep(0.3)
                return True
    except Exception:
        pass
    return False


def _custom_dropdown(page, el, search_text, pick_text):
    """Handle custom dropdown: click to open, type to filter, pick option.
    Works with Workday, Oracle JET, React Select, and other custom dropdown widgets."""
    try:
        el.scroll_into_view_if_needed()
        time.sleep(0.2)
    except Exception:
        pass

    # Strategy 1: Click the element (or its child arrow/button) to open
    opened = False
    for click_target in [el]:
        try:
            click_target.click()
            time.sleep(1.0)
            # Check if options appeared
            if page.locator('[role="option"], [role="listbox"] li').count() > 0:
                opened = True
                break
        except Exception:
            pass

    if not opened:
        # Try clicking child elements (arrow, icon, etc.)
        for child_sel in ['[class*="arrow"]', '[class*="icon"]', 'button', 'span']:
            try:
                child = el.locator(child_sel).first
                if child.is_visible():
                    child.click()
                    time.sleep(1.0)
                    if page.locator('[role="option"], [role="listbox"] li').count() > 0:
                        opened = True
                        break
            except Exception:
                continue

    if not opened:
        # Last try: focus + arrow down to open
        try:
            el.focus()
            page.keyboard.press("ArrowDown")
            time.sleep(0.8)
        except Exception:
            pass

    # Collect all option selectors to try
    opt_selectors = [
        '[role="option"]',
        '[role="listbox"] li',
        'li[class*="option"]',
        'div[class*="option"]',
        '[data-automation-id*="option"]',
        'ul[role="listbox"] > li',
        '.css-yk16xz-menu div',  # React Select
    ]

    pick_lower = (pick_text or search_text or "").lower()

    # If we have search text, type it first to filter
    if search_text and search_text.strip():
        try:
            # Clear any existing text first
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            time.sleep(0.2)
            page.keyboard.type(search_text[:20], delay=60)
            time.sleep(1.2)
        except Exception:
            pass

    # Try to find and click a matching option
    for opt_sel in opt_selectors:
        try:
            opts = page.locator(opt_sel)
            count = opts.count()
            if count == 0:
                continue
            for i in range(min(count, 30)):  # Cap at 30 to avoid slow iteration
                opt = opts.nth(i)
                try:
                    if not opt.is_visible():
                        continue
                except Exception:
                    continue
                text = opt.inner_text().strip().lower()
                if not text:
                    continue
                if pick_lower and (pick_lower in text or text in pick_lower):
                    opt.click()
                    log(f"    -> picked '{opt.inner_text().strip()[:50]}'")
                    time.sleep(0.4)
                    return True
        except Exception:
            continue

    # If typed search didn't find match, clear and try without search
    if search_text:
        try:
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            time.sleep(0.5)
        except Exception:
            pass

    # Fallback: pick first visible non-empty option
    for opt_sel in ['[role="option"]', '[role="listbox"] li', 'li[class*="option"]']:
        try:
            opts = page.locator(opt_sel)
            for i in range(min(opts.count(), 15)):
                opt = opts.nth(i)
                try:
                    if opt.is_visible() and opt.inner_text().strip():
                        txt = opt.inner_text().strip()
                        opt.click()
                        log(f"    -> picked first available: '{txt[:50]}'")
                        time.sleep(0.4)
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    # Close dropdown if nothing worked
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def execute_actions(page, actions):
    """Execute a batch of actions returned by Claude."""
    results = []
    for act in actions:
        if not isinstance(act, dict):
            continue
        action = act.get("action", "")
        # Normalize action name aliases
        action_aliases = {
            "dropdown": "custom_dropdown",
            "select": "select_option",
            "select_dropdown": "custom_dropdown",
            "type": "fill",
            "input": "fill",
            "checkbox": "check",
            "upload": "upload_cv",
            "file_upload": "upload_cv",
        }
        action = action_aliases.get(action, action)
        selector = act.get("selector", "")
        value = act.get("value", "")
        ok = False

        try:
            if action == "DONE":
                return "DONE"
            if action == "SKIP":
                return f"SKIP: {act.get('reason', 'unknown')}"

            if action == "fill":
                el = _find_element(page, selector)
                if el:
                    ok = _fill_with_events(page, el, value)
                    if ok:
                        log(f"    fill '{selector[:40]}' = '{value[:40]}'")
                        # Track password used on Workday for later Sign In reuse
                        if is_workday_url(page.url) and "password" in selector.lower() and value:
                            domain_m = re.search(r'https?://([^/]+)', page.url)
                            if domain_m:
                                _workday_passwords[domain_m.group(1)] = value
                else:
                    log(f"    fill: element not found '{selector[:50]}'")

            elif action in ("select_option", "custom_dropdown"):
                el = _find_element(page, selector)
                if el:
                    # Try native <select> first
                    tag = el.evaluate("el => el.tagName").lower()
                    if tag == "select":
                        ok = _select_native(page, el, value)
                        if ok:
                            log(f"    select '{selector[:40]}' = '{value[:40]}'")
                    else:
                        # Custom dropdown (Workday, Oracle JET, React Select)
                        search = act.get("search", value)
                        pick = act.get("pick", value)
                        ok = _custom_dropdown(page, el, search, pick)
                        if ok:
                            log(f"    dropdown '{selector[:40]}' = '{pick[:40]}'")
                        else:
                            log(f"    dropdown: couldn't select '{value[:40]}' in '{selector[:40]}'")
                else:
                    log(f"    dropdown: element not found '{selector[:50]}'")

            elif action == "radio":
                el = _find_element(page, selector)
                if not el and value:
                    # Fallback 1: find radio by visible label text
                    try:
                        el = page.get_by_label(re.compile(re.escape(value[:30]), re.I)).first
                        if not el.is_visible():
                            el = None
                    except Exception:
                        el = None
                if not el and value:
                    # Fallback 2: find by text near radio input
                    try:
                        loc = page.locator(f'label:has-text("{value[:30]}") input[type="radio"]')
                        if loc.count() > 0:
                            el = loc.first
                    except Exception:
                        pass
                if not el and value:
                    # Fallback 3: Ashby-style — click label element directly (no input accessible)
                    try:
                        clicked_via_js = page.evaluate(f"""() => {{
                            const labels = document.querySelectorAll('label, [role="radio"], [class*="radio"]');
                            for (const lbl of labels) {{
                                if (!lbl.offsetParent) continue;
                                const text = lbl.innerText.trim().toLowerCase();
                                const val = {json.dumps(value.lower())};
                                if (text === val || text.startsWith(val) || val.startsWith(text)) {{
                                    lbl.click();
                                    return true;
                                }}
                            }}
                            return false;
                        }}""")
                        if clicked_via_js:
                            ok = True
                            log(f"    radio (JS label click) '{value[:50]}'")
                    except Exception:
                        pass
                if el:
                    try:
                        el.scroll_into_view_if_needed()
                        el.check()
                        ok = True
                    except Exception:
                        try:
                            el.click()
                            ok = True
                        except Exception:
                            try:
                                el.evaluate("el => el.click()")
                                ok = True
                            except Exception:
                                pass
                    if ok:
                        log(f"    radio '{(value or selector)[:50]}'")
                else:
                    log(f"    radio: element not found '{selector[:50]}'")

            elif action == "check":
                el = _find_element(page, selector)
                if el:
                    try:
                        el.scroll_into_view_if_needed()
                        if not el.is_checked():
                            el.check()
                        ok = True
                    except Exception:
                        try:
                            el.click()
                            ok = True
                        except Exception:
                            pass
                    if ok:
                        log(f"    check '{selector[:50]}'")
                else:
                    log(f"    check: element not found '{selector[:50]}'")

            elif action == "upload_cv":
                cv_path = next(
                    (p for p in [
                        CV_PATH,
                        os.path.expanduser("~/Downloads/New CV Alramina 2026.pdf"),
                        os.path.expanduser("~/Downloads/New CV Alramina 2026 (1).pdf"),
                    ] if os.path.exists(p)),
                    None,
                )
                if cv_path:
                    file_input = page.locator('input[type="file"]')
                    if file_input.count() > 0:
                        file_input.first.set_input_files(cv_path)
                        log(f"    uploaded CV: {cv_path}")
                        ok = True
                    else:
                        log("    upload_cv: no file input found")
                else:
                    log("    upload_cv: no CV file found on disk")

            elif action == "yesno_toggle":
                # Click Yes or No button inside a toggle group identified by hidden input name
                answer = value.strip()  # "Yes" or "No"
                name = selector.strip("[]\"'").replace('name=', '').replace('"', '')
                try:
                    # Strategy 1: Find via JS — locate the hidden input, get parent, click button
                    clicked_js = page.evaluate(f"""() => {{
                        const input = document.querySelector('input[name="{name}"]');
                        if (!input) return false;
                        const container = input.parentElement;
                        if (!container) return false;
                        const btns = container.querySelectorAll('button');
                        for (const btn of btns) {{
                            if (btn.innerText.trim() === '{answer}') {{
                                btn.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}""")
                    if clicked_js:
                        ok = True
                        log(f"    yesno '{name[:30]}' = '{answer}'")
                    else:
                        # Strategy 2: find all visible Yes/No buttons, click the right one by index
                        # Use the question label to find the right group
                        question = act.get("description", "")
                        if question:
                            # Find the question text, then the nearest Yes/No button after it
                            clicked = page.evaluate(f"""() => {{
                                const labels = document.querySelectorAll('label,span,p,div,h3,h4');
                                for (const lbl of labels) {{
                                    if (!lbl.offsetParent) continue;
                                    if (lbl.innerText.trim().includes('{question[:40]}')) {{
                                        // Look for a Yes/No container nearby
                                        let el = lbl.nextElementSibling || lbl.parentElement;
                                        for (let i = 0; i < 5; i++) {{
                                            if (!el) break;
                                            const btns = el.querySelectorAll('button');
                                            for (const btn of btns) {{
                                                if (btn.innerText.trim() === '{answer}') {{
                                                    btn.click();
                                                    return true;
                                                }}
                                            }}
                                            el = el.nextElementSibling || el.parentElement;
                                        }}
                                    }}
                                }}
                                return false;
                            }}""")
                            if clicked:
                                ok = True
                                log(f"    yesno (by question) = '{answer}'")
                        if not ok:
                            log(f"    yesno: couldn't click '{answer}' for '{name[:40]}'")
                except Exception as e:
                    log(f"    yesno error: {e}")

            elif action == "click":
                el = _find_element(page, selector)
                if el:
                    try:
                        el.scroll_into_view_if_needed()
                        el.click()
                        ok = True
                    except Exception:
                        try:
                            el.evaluate("el => el.click()")
                            ok = True
                        except Exception:
                            pass
                    desc = act.get("description", "")
                    if ok:
                        log(f"    click '{selector[:40]}' ({desc[:40]})")
                else:
                    log(f"    click: element not found '{selector[:50]}'")

        except Exception as e:
            log(f"    action error ({action} {selector[:30]}): {e}")

        results.append({"action": action, "selector": selector, "ok": ok})
        time.sleep(0.3)

    return results


# =============================================================================
#  PAGE VERIFICATION -- check for errors after filling
# =============================================================================

def get_page_errors(page):
    """Extract visible validation errors from the page."""
    try:
        return page.evaluate("""() => {
            const errors = [];
            const sels = '[class*="error"],[class*="Error"],[role="alert"],[class*="invalid"],[class*="validation"],[class*="field-error"],[class*="missing"],[class*="Missing"],[class*="banner"],[class*="toast"],[class*="notification"]';
            document.querySelectorAll(sels).forEach(el => {
                if (!el.offsetParent) return;
                const text = el.innerText.trim();
                if (text && text.length > 2 && text.length < 200) {
                    errors.push(text);
                }
            });
            // Also check for red-colored text near form elements (common pattern for inline errors)
            document.querySelectorAll('span,p,div').forEach(el => {
                if (!el.offsetParent || el.children.length > 3) return;
                const text = el.innerText.trim();
                if (!text || text.length < 5 || text.length > 200) return;
                const style = getComputedStyle(el);
                const color = style.color;
                // Match red-ish colors (rgb where r > 180, g < 100, b < 100)
                const m = color.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                if (m && parseInt(m[1]) > 180 && parseInt(m[2]) < 100 && parseInt(m[3]) < 100) {
                    if (text.toLowerCase().includes('required') || text.toLowerCase().includes('missing') || text.toLowerCase().includes('entry')) {
                        errors.push(text);
                    }
                }
            });
            return [...new Set(errors)].slice(0, 10);
        }""")
    except Exception:
        return []


def count_empty_required(page):
    """Count required fields that are still empty."""
    try:
        return page.evaluate("""() => {
            let empty = 0;
            document.querySelectorAll('input,select,textarea').forEach(el => {
                if (!el.offsetParent) return;
                if (el.type === 'hidden' || el.type === 'submit' || el.type === 'file') return;
                // Skip honeypot fields (off-screen, tabindex=-1, or tiny dimensions)
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return;
                if (el.tabIndex === -1 && el.name && ['website','url','hp','bot','trap'].some(h => el.name.toLowerCase().includes(h))) return;
                const req = el.required || el.getAttribute('aria-required') === 'true';
                if (!req) return;
                if (el.type === 'checkbox' || el.type === 'radio') {
                    if (el.type === 'radio') {
                        const name = el.name;
                        if (name) {
                            const group = document.querySelectorAll('input[type="radio"][name="'+name+'"]');
                            if (!Array.from(group).some(r => r.checked)) empty++;
                        }
                    } else if (!el.checked) empty++;
                } else if (!el.value.trim()) {
                    empty++;
                }
            });
            return empty;
        }""")
    except Exception:
        return 0


# =============================================================================
#  NAVIGATION -- find next/submit button, detect success
# =============================================================================

def is_success_page(page):
    """Check if the current page is a confirmation/thank-you page.
    Only checks VISIBLE text, not hidden JSON/config data in the HTML source."""
    try:
        visible_text = page.evaluate("""() => {
            // Get text from the main visible body, excluding script/style/hidden elements
            const clone = document.body.cloneNode(true);
            clone.querySelectorAll('script,style,noscript,[type="application/json"],[type="application/ld+json"],template').forEach(el => el.remove());
            return clone.innerText.toLowerCase();
        }""")
        markers = [
            "application submitted", "thank you for applying",
            "application received", "successfully submitted",
            "application complete", "we'll be in touch",
            "application has been submitted", "thanks for applying",
            "your application has been", "we have received your application",
            # Ashby ATS
            "you've applied", "you have applied", "application was submitted",
            "your application is submitted", "successfully applied",
            "we received your application", "your application is in",
            # Oracle HCM
            "my applications", "under review", "application is under review",
        ]
        if any(m in visible_text for m in markers):
            # Oracle HCM: "My Applications" page can also show OTHER jobs, not just success
            # Only count as success if we also see "under review" or Oracle HCM URL pattern
            if "my applications" in visible_text and "oraclecloud.com" in page.url:
                if "under review" in visible_text or "/myApplications" in page.url:
                    return True
                # Skip "my applications" marker if not confirmed as Oracle HCM success
                other_markers = [m for m in markers if m != "my applications"]
                if not any(m in visible_text for m in other_markers):
                    return False
            return True
        # Ashby / Workable: URL pattern for confirmation
        url = page.url.lower()
        if any(p in url for p in ["/confirmation", "/apply/success", "/application/success", "?success=true", "/apply/confirmation"]):
            return True
        # Oracle HCM: /myApplications URL = application list (only success if "under review" visible)
        if "oraclecloud.com" in page.url and "/myapplications" in page.url.lower():
            if "under review" in visible_text:
                return True
        return False
    except Exception:
        return False


def click_next_button(page):
    """Find and click the next/continue/submit button. Returns button text or None."""
    # Workday: data-automation-id buttons first (most reliable)
    for wd_id in [
        "bottom-navigation-next-button",
        "bottom-navigation-done-button",
        "bottom-navigation-footer-button",
        "jobApply",
    ]:
        btn = page.locator(f'[data-automation-id="{wd_id}"]')
        if btn.count() > 0 and btn.first.is_visible():
            try:
                btn.first.evaluate("el => el.click()")
                log(f"  -> Clicked Workday button: {wd_id}")
                return wd_id
            except Exception:
                pass

    # Generic: try common button labels
    # Note: Skip "Create Account" on Workday — auth is handled by _handle_workday_auth_page
    workday_url = is_workday_url(page.url)
    for label in [
        "Next", "Continue", "Save and Continue", "Save & Continue",
        "Submit Application", "Submit", "Review and Submit",
        "Review & Submit", "Apply", "Apply Now", "Send Application",
        "Confirm", "Done", "Finish", "Complete",
        *([] if workday_url else ["Create Account", "Sign In", "Register"]),
        "Save and Next", "Proceed",
    ]:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
                if loc.count() > 0:
                    vis = [loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()]
                    if vis:
                        vis[0].scroll_into_view_if_needed()
                        try:
                            vis[0].click()
                        except Exception:
                            vis[0].evaluate("el => el.click()")
                        log(f"  -> Clicked '{label}'")
                        return label
            except Exception:
                continue

    # Last resort: JS search for any forward-looking button
    try:
        clicked = page.evaluate("""() => {
            const targets = ['next','continue','submit','apply','save and continue','done','finish','review','sign in','log in','login'];
            const btns = [...document.querySelectorAll('button,a[role="button"],input[type="submit"]')];
            for (const t of targets) {
                const btn = btns.find(b => b.offsetParent && b.innerText.trim().toLowerCase().includes(t));
                if (btn) { btn.click(); return btn.innerText.trim().slice(0, 30); }
            }
            return null;
        }""")
        if clicked:
            log(f"  -> Clicked (JS): '{clicked}'")
            return clicked
    except Exception:
        pass

    return None


def handle_new_tabs(page, context):
    """If a new tab opened, switch to it and return the new page. Otherwise return original page."""
    try:
        all_pages = context.pages
        new_pages = [p for p in all_pages if p != page and not p.is_closed()]
        if new_pages:
            new_page = new_pages[-1]
            # Wait for load, then poll until URL is non-blank (handles slow Workday new tabs)
            try:
                new_page.wait_for_load_state("domcontentloaded", timeout=12000)
            except Exception:
                pass
            # If still about:blank, wait up to 8 more seconds for URL to resolve
            for _ in range(16):
                if new_page.url not in ("about:blank", ""):
                    break
                time.sleep(0.5)
            if "linkedin.com" not in new_page.url and new_page.url not in ("about:blank", ""):
                log(f"  Switched to new tab: {new_page.url}")
                Stealth().apply_stealth_sync(new_page)
                for old_p in all_pages:
                    if old_p != new_page and not old_p.is_closed():
                        try:
                            old_p.close()
                        except Exception:
                            pass
                return new_page
            else:
                for np in new_pages:
                    try:
                        np.close()
                    except Exception:
                        pass
    except Exception as e:
        log(f"  Tab check error: {e}")
    return page


# =============================================================================
#  LINKEDIN -> COMPANY URL
# =============================================================================

def get_company_apply_url(page, linkedin_url):
    """Navigate to LinkedIn job, click Apply, catch the company URL."""
    page.goto(linkedin_url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector(
            ".job-view-layout, .jobs-details, .jobs-unified-top-card", timeout=15000
        )
    except Exception:
        pass
    time.sleep(4)

    # Scroll down to make sure apply button is loaded (some pages lazy-load it)
    try:
        page.mouse.move(640, 400)
        page.mouse.wheel(0, 300)
        time.sleep(1)
        page.mouse.wheel(0, -300)
        time.sleep(1)
    except Exception:
        pass

    # Find the Apply button (not Easy Apply)
    apply_btn = None
    for pattern in [
        r"apply on company website", r"apply on employer", r"apply on",
        r"apply externally", r"apply at",
        r"^apply$", r"^apply now$",
    ]:
        for role in ("link", "button"):
            try:
                loc = page.get_by_role(role, name=re.compile(pattern, re.I))
                if loc.count() > 0:
                    for i in range(min(loc.count(), 3)):
                        el = loc.nth(i)
                        aria = el.get_attribute("aria-label") or ""
                        text = el.inner_text() or ""
                        if "easy" not in aria.lower() and "easy" not in text.lower():
                            apply_btn = el
                            break
            except Exception:
                continue
        if apply_btn:
            break

    # Fallback: aria-label
    if not apply_btn:
        for sel in [
            'button[aria-label*="Apply"]:not([aria-label*="Easy"])',
            'a[aria-label*="Apply"]:not([aria-label*="Easy"])',
            '.jobs-apply-button:not([aria-label*="Easy"])',
            'a.jobs-apply-button',
        ]:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    apply_btn = loc.first
                    break
            except Exception:
                continue

    # Fallback: JS search for any "Apply" button/link that isn't Easy Apply
    if not apply_btn:
        try:
            found = page.evaluate("""() => {
                const els = [...document.querySelectorAll('button,a')].filter(e => e.offsetParent);
                for (const el of els) {
                    const text = (el.innerText || '').trim().toLowerCase();
                    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    if ((text.includes('apply') || aria.includes('apply')) &&
                        !text.includes('easy') && !aria.includes('easy') &&
                        !text.includes('save') && text.length < 40) {
                        el.setAttribute('data-found-apply', 'true');
                        return true;
                    }
                }
                return false;
            }""")
            if found:
                apply_btn = page.locator('[data-found-apply="true"]').first
        except Exception:
            pass

    if not apply_btn:
        log("  No apply button found on LinkedIn page")
        return None

    # Click and catch new tab
    try:
        with page.context.expect_page(timeout=12000) as new_page_info:
            apply_btn.click()
        new_page = new_page_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=15000)
        url = new_page.url
        log(f"  Company URL (new tab): {url}")
        new_page.close()
        if "linkedin.com" not in url and "chrome-error" not in url and url.startswith("http"):
            return url
    except Exception:
        pass

    # Fallback: check existing tabs
    try:
        all_pages = page.context.pages
        new_pages = [p for p in all_pages if p != page and not p.is_closed()]
        if new_pages:
            np = new_pages[-1]
            try:
                np.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            url = np.url
            np.close()
            if "linkedin.com" not in url and "chrome-error" not in url and url not in ("about:blank", "") and url.startswith("http"):
                return url
    except Exception:
        pass

    # Fallback: inline navigation
    try:
        apply_btn.click()
        time.sleep(4)
        if "linkedin.com" not in page.url:
            return page.url
    except Exception:
        pass

    return None


# =============================================================================
#  MAIN APPLICATION FUNCTION -- the page loop
# =============================================================================

def _auto_answer_yesno_toggles(page):
    """Auto-click Yes/No toggle buttons based on question text.
    Runs BEFORE Claude analysis to handle custom button groups that aren't standard form inputs."""
    try:
        answered = page.evaluate("""() => {
            const answered = [];
            // Find all Yes/No button pairs
            const buttons = document.querySelectorAll('button');
            const groups = new Map(); // parent -> {yes, no, question}

            buttons.forEach(btn => {
                if (!btn.offsetParent) return;
                const text = btn.innerText.trim();
                if (text !== 'Yes' && text !== 'No') return;
                const parent = btn.parentElement;
                if (!parent) return;

                const key = parent; // group by parent element
                if (!groups.has(key)) {
                    // Find question text
                    let question = '';
                    // Strategy 1: find label[for] pointing to the hidden input in this group
                    const hiddenInput = parent.querySelector('input[type="checkbox"],input[type="hidden"]');
                    if (hiddenInput && hiddenInput.name) {
                        const lbl = document.querySelector('label[for="' + hiddenInput.name + '"]');
                        if (lbl) question = lbl.innerText.trim().split('\\n')[0];
                    }
                    // Strategy 2: walk up DOM looking for preceding text
                    if (!question) {
                        let el = parent;
                        for (let i = 0; i < 8; i++) {
                            if (!el) break;
                            const prev = el.previousElementSibling;
                            if (prev) {
                                const t = prev.innerText.trim().split('\\n')[0];
                                if (t && t.length > 5 && t.length < 200 && !t.match(/^(Yes|No)$/)) {
                                    question = t; break;
                                }
                            }
                            el = el.parentElement;
                        }
                    }
                    groups.set(key, {yes: null, no: null, question});
                }
                const g = groups.get(key);
                if (text === 'Yes') g.yes = btn;
                if (text === 'No') g.no = btn;
            });

            groups.forEach((g, parent) => {
                if (!g.yes || !g.no) return;
                // Always re-click — after failed submit, toggles can reset silently

                const q = g.question.toLowerCase();
                let clickYes = true; // default

                // Questions where answer should be No
                if (q.includes('graduating') || q.includes('degree in 202') ||
                    q.includes('former employee') || q.includes('worked here') ||
                    q.includes('previously employed') || q.includes('criminal') ||
                    q.includes('disability') || q.includes('veteran')) {
                    clickYes = false;
                }
                // Questions where answer should be Yes
                if (q.includes('based in the uk') || q.includes('currently based') ||
                    q.includes('work from') || q.includes('london office') ||
                    q.includes('authorized') || q.includes('authorised') ||
                    q.includes('right to work') || q.includes('eligible') ||
                    q.includes('18 year') || q.includes('legal age') ||
                    q.includes('agree') || q.includes('consent') ||
                    q.includes('acknowledge')) {
                    clickYes = true;
                }

                const btn = clickYes ? g.yes : g.no;
                btn.click();
                answered.push({question: g.question.slice(0, 60), answer: clickYes ? 'Yes' : 'No'});
            });

            return answered;
        }""")
        for a in answered:
            log(f"    auto-toggle: '{a['question']}' = {a['answer']}")
    except Exception as e:
        log(f"    auto-toggle error: {e}")


def _auto_check_consent(page):
    """Auto-check any unchecked consent/agree/acknowledge checkboxes."""
    try:
        checked = page.evaluate("""() => {
            const checked = [];
            document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                if (!cb.offsetParent && !cb.closest('[class*="consent"]')) return;
                if (cb.checked) return;
                // Get label text
                let label = '';
                if (cb.id) {
                    const lbl = document.querySelector('label[for="' + cb.id + '"]');
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) {
                    const enc = cb.closest('label');
                    if (enc) label = enc.innerText.trim();
                }
                if (!label) {
                    const parent = cb.parentElement;
                    if (parent) label = parent.innerText.trim();
                }
                const l = label.toLowerCase();
                if (l.includes('i agree') || l.includes('i acknowledge') ||
                    l.includes('i consent') || l.includes('i accept') ||
                    l.includes('i confirm') || l.includes('i understand') ||
                    l.includes('terms') || l.includes('privacy') ||
                    l.includes('data retention') || l.includes('data processing')) {
                    cb.click();
                    checked.push(label.slice(0, 60));
                }
            });
            return checked;
        }""")
        for label in checked:
            log(f"    auto-consent: checked '{label}'")
    except Exception as e:
        log(f"    auto-consent error: {e}")


def _auto_fill_workable_required_radios(page):
    """Auto-fill unanswered required radio-button questions in Workable ATS.
    Workable groups are <div data-ui="radio-group"> with <label> options.
    Picks 'Prefer not to say/answer' if available, otherwise the first option."""
    if "workable.com" not in page.url:
        return
    try:
        filled = page.evaluate("""() => {
            const filled = [];
            // Find all radio groups that have no checked option
            const groups = document.querySelectorAll('[data-ui="radio-group"], [data-testid="radio-group"], .radio-group');
            groups.forEach(group => {
                if (!group.offsetParent) return;
                const radios = group.querySelectorAll('input[type="radio"]');
                if (!radios.length) return;
                const anyChecked = Array.from(radios).some(r => r.checked);
                if (anyChecked) return;
                // Find a 'prefer not to say/answer' option, else first option
                let picked = null;
                const labels = group.querySelectorAll('label');
                for (const lbl of labels) {
                    const t = lbl.innerText.toLowerCase();
                    if (t.includes('prefer not') || t.includes('decline') || t.includes('do not wish')) {
                        picked = lbl;
                        break;
                    }
                }
                if (!picked && labels.length) picked = labels[0];
                if (picked) {
                    const inp = picked.querySelector('input[type="radio"]') ||
                                document.getElementById(picked.getAttribute('for'));
                    if (inp) { inp.click(); filled.push(picked.innerText.trim().slice(0, 50)); }
                    else { picked.click(); filled.push(picked.innerText.trim().slice(0, 50)); }
                }
            });
            // Also handle native radio inputs with required attr not in above groups
            const reqRadioNames = new Set();
            document.querySelectorAll('input[type="radio"][required], input[type="radio"][aria-required="true"]').forEach(r => {
                if (!r.offsetParent) return;
                reqRadioNames.add(r.name);
            });
            reqRadioNames.forEach(name => {
                const radios = document.querySelectorAll('input[type="radio"][name="' + name + '"]');
                if (Array.from(radios).some(r => r.checked)) return;
                // Try 'prefer not to say', else first
                let picked = null;
                for (const r of radios) {
                    const lbl = document.querySelector('label[for="' + r.id + '"]');
                    const t = (lbl ? lbl.innerText : r.value || '').toLowerCase();
                    if (t.includes('prefer not') || t.includes('decline')) { picked = r; break; }
                }
                if (!picked) picked = radios[0];
                if (picked) { picked.click(); filled.push('radio:' + name.slice(0, 40)); }
            });
            return filled;
        }""")
        for label in (filled or []):
            log(f"    workable-radio: auto-selected '{label}'")
    except Exception as e:
        log(f"    workable-radio error: {e}")


def _auto_fill_oracle_jet_dropdowns(page):
    """Auto-fill Oracle JET custom dropdowns (oj-select-one, oj-combobox-one).
    These are NOT standard <select> elements — they require click-to-open + option click.
    Targets EEO/diversity fields with 'Prefer not to' answers."""
    if "oraclecloud.com" not in page.url:
        return  # Only run on Oracle HCM pages

    try:
        # Step 1: Find all unfilled Oracle JET dropdowns and their labels
        unfilled = page.evaluate("""() => {
            const results = [];
            // Oracle JET uses specific container patterns
            const containers = document.querySelectorAll(
                '.oj-select, .oj-combobox, [class*="oj-select-one"], [class*="oj-combobox-one"]'
            );
            containers.forEach(c => {
                if (!c.offsetParent) return;
                // Check if it has a selected value
                const chosen = c.querySelector('.oj-select-chosen, .oj-combobox-input');
                if (!chosen) return;
                const currentVal = (chosen.innerText || chosen.value || '').trim();
                // Skip if already filled (has real text, not placeholder)
                if (currentVal && currentVal.length > 1 && currentVal !== 'Select' &&
                    !currentVal.includes('required')) return;

                // Find label
                let label = '';
                // Try aria-labelledby
                const lblId = c.getAttribute('aria-labelledby') ||
                              c.closest('[aria-labelledby]')?.getAttribute('aria-labelledby');
                if (lblId) {
                    const lbl = document.getElementById(lblId);
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) {
                    // Walk up DOM to find label
                    let el = c;
                    for (let i = 0; i < 6; i++) {
                        el = el.parentElement;
                        if (!el) break;
                        const lbl = el.querySelector('label, .oj-label, [class*="label"]');
                        if (lbl && lbl.innerText.trim()) {
                            label = lbl.innerText.trim().split('\\n')[0];
                            break;
                        }
                    }
                }
                if (!label) {
                    // Try preceding sibling
                    const prev = c.previousElementSibling;
                    if (prev) label = prev.innerText.trim().split('\\n')[0];
                }

                // Get the clickable trigger element
                const arrow = c.querySelector('.oj-select-arrow, .oj-combobox-arrow, [class*="arrow"]');
                const trigger = arrow || chosen || c;

                results.push({
                    label: label.slice(0, 80),
                    // Return a unique selector to find the trigger
                    idx: results.length
                });
            });
            return results;
        }""")

        if not unfilled:
            return

        for item in unfilled:
            label = item["label"]
            log(f"    oracle-jet: opening dropdown '{label}'")

            try:
                # Click the dropdown to open it using JavaScript index
                page.evaluate(f"""() => {{
                    const containers = document.querySelectorAll(
                        '.oj-select, .oj-combobox, [class*="oj-select-one"], [class*="oj-combobox-one"]'
                    );
                    let idx = 0;
                    for (const c of containers) {{
                        if (!c.offsetParent) continue;
                        const chosen = c.querySelector('.oj-select-chosen, .oj-combobox-input');
                        if (!chosen) continue;
                        const val = (chosen.innerText || chosen.value || '').trim();
                        if (val && val.length > 1 && val !== 'Select' && !val.includes('required')) continue;
                        if (idx === {item['idx']}) {{
                            const arrow = c.querySelector('.oj-select-arrow, .oj-combobox-arrow, [class*="arrow"]');
                            (arrow || chosen || c).click();
                            return;
                        }}
                        idx++;
                    }}
                }}""")
                time.sleep(0.8)

                # Now look for "Prefer not to" option in the dropdown list
                prefer_patterns = [
                    "prefer not", "decline to", "choose not", "don't wish",
                    "do not wish", "rather not", "not to disclose",
                ]

                option_clicked = False
                for pattern in prefer_patterns:
                    try:
                        opt = page.locator(f'li[role="option"]:has-text("{pattern}")').first
                        if opt.is_visible(timeout=500):
                            opt.click()
                            log(f"    oracle-jet: selected '{pattern}...' for '{label[:40]}'")
                            option_clicked = True
                            break
                    except Exception:
                        continue

                if not option_clicked:
                    # For specific EEO questions, try known option texts
                    q = label.lower()
                    fallback_options = []
                    if "title" in q or "salutation" in q:
                        fallback_options = ["Ms.", "Ms", "Miss"]
                    elif "race" in q or "ethnic" in q:
                        fallback_options = ["Two or More", "Other", "Prefer"]
                    elif "sexual orientation" in q or "sexual_orientation" in q:
                        fallback_options = ["Heterosexual", "Straight", "Heterosexual / Straight", "Prefer not to say"]
                    elif "gender identity" in q or "gender_identity" in q:
                        fallback_options = ["Female", "Woman", "Prefer not to say"]
                    elif "sex" in q:
                        fallback_options = ["Female", "Prefer"]
                    elif "gender" in q:
                        fallback_options = ["Woman", "Female", "Prefer"]
                    elif "veteran" in q or "military" in q:
                        fallback_options = ["not a", "prefer not", "no"]
                    elif "disability" in q:
                        fallback_options = ["don't wish", "prefer not", "no"]
                    elif "related to" in q or "employee" in q or "relative" in q:
                        fallback_options = ["No", "no"]
                    elif "requirement" in q or "adjustment" in q or "reasonable" in q:
                        fallback_options = ["No", "no"]

                    for opt_text in fallback_options:
                        try:
                            opt = page.locator(f'li[role="option"]:has-text("{opt_text}")').first
                            if opt.is_visible(timeout=500):
                                opt.click()
                                log(f"    oracle-jet: selected '{opt_text}...' for '{label[:40]}'")
                                option_clicked = True
                                break
                        except Exception:
                            continue

                if not option_clicked:
                    # Last resort: pick the first option
                    try:
                        first_opt = page.locator('li[role="option"]').first
                        if first_opt.is_visible(timeout=500):
                            txt = first_opt.inner_text()[:40]
                            first_opt.click()
                            log(f"    oracle-jet: selected first option '{txt}' for '{label[:40]}'")
                        else:
                            page.keyboard.press("Escape")
                    except Exception:
                        page.keyboard.press("Escape")

                time.sleep(0.3)
            except Exception as e:
                log(f"    oracle-jet: error on '{label[:40]}': {e}")
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass

    except Exception as e:
        log(f"    oracle-jet error: {e}")


def _auto_fill_workday_dropdowns(page):
    """Auto-fill Workday custom dropdown fields that Claude struggles with.
    Workday uses custom prompt containers with specific CSS patterns."""
    if not is_workday_url(page.url) and "wd3." not in page.url and "wd5." not in page.url:
        return

    try:
        # Find all unfilled Workday dropdown prompts using their DOM pattern
        unfilled = page.evaluate("""() => {
            const results = [];
            // Workday dropdowns: div[data-automation-id] containing a button[aria-haspopup]
            // or prompt containers with empty values
            const prompts = document.querySelectorAll(
                '[data-automation-id*="formField"], [data-automation-id*="promptOption"], ' +
                '[data-automation-id*="Dropdown"], [data-automation-id*="dropdown"], ' +
                '[data-automation-id*="sourcePrompt"], [data-automation-id*="source"]'
            );
            prompts.forEach(p => {
                if (!p.offsetParent) return;
                const btn = p.querySelector('button[aria-haspopup], [role="combobox"]');
                if (!btn) return;
                const val = btn.innerText.trim().toLowerCase();
                const EMPTY_VALS2 = ['', 'select', 'select one', 'none selected', 'please select',
                                     '-- none --', '- none -', 'choose one', 'choose...'];
                if (val && !EMPTY_VALS2.includes(val) && !val.includes('required')) return;
                // Get label
                const aid = p.getAttribute('data-automation-id') || '';
                let label = '';
                const lbl = p.querySelector('label');
                if (lbl) label = lbl.innerText.trim();
                results.push({aid, label: label.slice(0, 60), idx: results.length});
            });

            // Also find ANY visible button[aria-haspopup="listbox"] that's empty
            document.querySelectorAll('button[aria-haspopup="listbox"]').forEach(btn => {
                if (!btn.offsetParent) return;
                const val = btn.innerText.trim().toLowerCase();
                const EMPTY_VALS = ['', 'select', 'select one', 'none selected', 'please select',
                                    '-- none --', '- none -', 'choose one', 'choose...'];
                if (val && !EMPTY_VALS.includes(val)) return;
                // Check not already found
                const aid = btn.getAttribute('data-automation-id') || '';
                if (results.some(r => r.aid === aid && aid)) return;
                // Get label from aria-labelledby
                let label = '';
                const lblId = btn.getAttribute('aria-labelledby');
                if (lblId) {
                    const lbl = document.getElementById(lblId.split(' ')[0]);
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) {
                    // Walk up to find label
                    let el = btn;
                    for (let i = 0; i < 5; i++) {
                        el = el.parentElement;
                        if (!el) break;
                        const l = el.querySelector('label');
                        if (l) { label = l.innerText.trim(); break; }
                    }
                }
                results.push({aid, label: label.slice(0, 60), idx: results.length, isBtn: true});
            });
            return results;
        }""")

        if not unfilled:
            return

        # Determine preferred values based on label text
        for item in unfilled:
            label = item["label"].lower()
            aid = item["aid"].lower()

            preferred = []
            if "hear" in label or "source" in label or "source" in aid or "hear" in aid:
                preferred = ["LinkedIn", "Job Posting", "Website", "Job Board", "Internet", "Online", "Social Media"]
            elif "county" in label or "region" in label or "countryregion" in aid:
                preferred = ["Greater London", "London"]
            elif ("country" in label or "country" in aid) and "region" not in aid and "code" not in label:
                preferred = ["United Kingdom", "UK", "Great Britain"]
            elif "phone" in label and ("type" in label or "device" in label):
                preferred = ["Mobile", "Cell"]
            elif ("phone" in label and "code" in label) or "phonecode" in aid or "countryPhoneCode" in item.get("aid", ""):
                preferred = ["United Kingdom (+44)", "United Kingdom", "+44"]
            else:
                preferred = []

            log(f"    workday-dropdown: filling '{item['label']}' (aid={item['aid'][:30]})")

            try:
                # Find and click the dropdown button
                if item.get("isBtn"):
                    # Direct button reference
                    btns = page.locator('button[aria-haspopup="listbox"]')
                    btn = None
                    for i in range(btns.count()):
                        b = btns.nth(i)
                        if b.is_visible() and not b.inner_text().strip():
                            btn = b
                            break
                    if not btn:
                        continue
                else:
                    container = page.locator(f'[data-automation-id="{item["aid"]}"]').first
                    if not container.is_visible(timeout=500):
                        continue
                    btn = container.locator('button[aria-haspopup], [role="combobox"]').first
                    if not btn.is_visible(timeout=500):
                        # Try clicking the container itself
                        btn = container

                btn.click()
                time.sleep(0.8)

                clicked = False
                for val in preferred:
                    try:
                        page.keyboard.type(val[:10], delay=50)
                        time.sleep(0.8)
                        opt = page.locator(f'[role="option"]:has-text("{val}")').first
                        if opt.is_visible(timeout=1000):
                            opt.click()
                            log(f"    workday-dropdown: selected '{val}' for '{item['label'][:40]}'")
                            clicked = True
                            break
                        else:
                            page.keyboard.press("Control+a")
                            page.keyboard.press("Backspace")
                            time.sleep(0.3)
                    except Exception:
                        try:
                            page.keyboard.press("Control+a")
                            page.keyboard.press("Backspace")
                        except Exception:
                            pass

                if not clicked:
                    # Pick first available option
                    try:
                        first_opt = page.locator('[role="option"]').first
                        if first_opt.is_visible(timeout=500):
                            txt = first_opt.inner_text().strip()
                            first_opt.click()
                            log(f"    workday-dropdown: picked first '{txt[:40]}' for '{item['label'][:40]}'")
                        else:
                            page.keyboard.press("Escape")
                    except Exception:
                        page.keyboard.press("Escape")

                time.sleep(0.3)
            except Exception as e:
                log(f"    workday-dropdown: error '{item['label'][:40]}': {e}")
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass

    except Exception as e:
        log(f"    workday-dropdown error: {e}")


def _workday_fill_phone_code(page):
    """Fill the Workday phone country code dropdown (phoneNumber--countryPhoneCode).
    This field defaults to 'Select One' and often isn't filled by the generic handlers."""
    if not is_workday_url(page.url):
        return False
    try:
        # Check if the phone code button is visible and showing a placeholder
        state = page.evaluate("""() => {
            const PLACEHOLDERS = ['select one', 'select', '', 'none selected'];
            const selectors = [
                '[id*="countryPhoneCode"]',
                '[data-automation-id*="countryPhoneCode"]',
                '[id*="phoneCode"]',
            ];
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    if (!el.offsetParent) continue;
                    const btn = el.tagName === 'BUTTON' ? el : el.querySelector('button[aria-haspopup]');
                    if (!btn) continue;
                    const val = btn.innerText.trim().toLowerCase();
                    if (PLACEHOLDERS.includes(val)) return {needs_fill: true};
                }
            }
            return {needs_fill: false};
        }""") or {}
        if not state.get("needs_fill"):
            return False

        log("    phone-code-fix: filling country phone code dropdown")

        # Click to open the dropdown
        opened = page.evaluate("""() => {
            const selectors = [
                '[id*="countryPhoneCode"]',
                '[data-automation-id*="countryPhoneCode"]',
                '[id*="phoneCode"]',
            ];
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    if (!el.offsetParent) continue;
                    const btn = el.tagName === 'BUTTON' ? el : el.querySelector('button[aria-haspopup]');
                    if (!btn) continue;
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")
        if not opened:
            return False

        time.sleep(0.8)

        # Type "United Kingdom" to filter
        try:
            page.keyboard.type("United Kingdom", delay=30)
            time.sleep(0.8)
        except Exception:
            pass

        # Find and click the UK +44 option
        opts = page.locator('[role="option"]')
        count = opts.count()
        for i in range(min(count, 10)):
            try:
                txt = opts.nth(i).inner_text().strip()
                if "united kingdom" in txt.lower() and "+44" in txt:
                    opts.nth(i).evaluate("el => el.click()")
                    log(f"    phone-code-fix: selected '{txt}'")
                    time.sleep(0.5)
                    return True
            except Exception:
                continue

        # Fallback: first UK option
        for i in range(min(count, 10)):
            try:
                txt = opts.nth(i).inner_text().strip()
                if "united kingdom" in txt.lower():
                    opts.nth(i).evaluate("el => el.click()")
                    log(f"    phone-code-fix: selected '{txt}' (fallback)")
                    time.sleep(0.5)
                    return True
            except Exception:
                continue

        page.keyboard.press("Escape")
        return False

    except Exception as e:
        log(f"    phone-code-fix error: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _workday_fill_county_region(page):
    """Directly fill the address--countryRegion (County/Region) dropdown in Workday.
    Uses JS to enumerate listbox options and find 'Greater London' or 'London'."""
    if not is_workday_url(page.url):
        return False
    try:
        # Find the county/region button — it has id containing 'countryRegion' OR aria-label containing 'County'
        result = page.evaluate("""() => {
            // Find the county region button — Workday uses id="address--countryRegion"
            // or a container with data-automation-id containing countryRegion
            let btn = null;

            // Direct ID selector (most reliable)
            const directEl = document.getElementById('address--countryRegion');
            if (directEl) {
                // It might be a button or a container
                if (directEl.tagName === 'BUTTON') {
                    btn = directEl;
                } else {
                    btn = directEl.querySelector('button[aria-haspopup], [role="combobox"]') || directEl;
                }
            }

            if (!btn) {
                // Try data-automation-id variations
                for (const sel of [
                    '[data-automation-id="countryRegion"] button',
                    '[data-automation-id*="countryRegion"] button',
                    '[id*="countryRegion"] button'
                ]) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent) { btn = el; break; }
                }
            }

            if (!btn) {
                // Try looking for label "County" near a button
                const labels = document.querySelectorAll('label');
                for (const lbl of labels) {
                    if (!lbl.offsetParent) continue;
                    if (/^county/i.test(lbl.innerText.trim())) {
                        const id = lbl.getAttribute('for');
                        if (id) {
                            const el = document.getElementById(id);
                            if (el) { btn = el; break; }
                        }
                        const parent = lbl.parentElement;
                        if (parent) {
                            const b = parent.querySelector('button[aria-haspopup]');
                            if (b) { btn = b; break; }
                        }
                    }
                }
            }

            if (!btn) return {found: false};
            return {found: true, currentVal: btn.innerText.trim()};
        }""")
        if not result or not result.get("found"):
            return False
        current = result.get("currentVal", "")
        # If already filled with something meaningful (not empty/Select/Select One)
        current_lower = current.lower()
        already_filled = (
            current and
            current_lower not in ("select", "select one", "") and
            "select one" not in current_lower and
            "required" not in current_lower and
            len(current) > 3
        )
        if already_filled:
            log(f"    county-fix: already set to '{current[:40]}' — skipping")
            return True

        # Click the county region element using JS to bypass click_filter overlay
        clicked = page.evaluate("""() => {
            // Try direct id first
            let btn = document.getElementById('address--countryRegion');
            if (btn) { btn.click(); return true; }

            // Try data-automation-id selectors
            for (const sel of [
                '[data-automation-id="countryRegion"] button',
                '[data-automation-id*="countryRegion"] button',
                '[id*="countryRegion"] button'
            ]) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent) { el.click(); return true; }
            }

            // Try click_filter overlay for county/region
            const overlays = document.querySelectorAll('[data-automation-id="click_filter"]');
            for (const ov of overlays) {
                const label = (ov.getAttribute('aria-label') || '').toLowerCase();
                if (label.includes('county') || label.includes('region')) {
                    ov.click();
                    return true;
                }
            }

            // Try label-based approach
            const labels = document.querySelectorAll('label');
            for (const lbl of labels) {
                if (!lbl.offsetParent) continue;
                if (/^county/i.test(lbl.innerText.trim())) {
                    const id = lbl.getAttribute('for');
                    if (id) {
                        const el = document.getElementById(id);
                        if (el) { el.click(); return true; }
                    }
                    const parent = lbl.parentElement;
                    if (parent) {
                        const b = parent.querySelector('button[aria-haspopup], [data-automation-id="click_filter"]');
                        if (b) { b.click(); return true; }
                    }
                }
            }
            return false;
        }""")
        if not clicked:
            return False
        time.sleep(1.0)

        # Now type to filter
        for search in ["Greater London", "London"]:
            page.keyboard.type(search, delay=50)
            time.sleep(1.0)
            # Look for the option
            opts = page.locator('[role="option"]')
            count = opts.count()
            for i in range(min(count, 20)):
                opt = opts.nth(i)
                try:
                    if not opt.is_visible():
                        continue
                    txt = opt.inner_text().strip()
                    if "london" in txt.lower():
                        opt.click()
                        log(f"    county-fix: selected '{txt}' for County/Region")
                        time.sleep(0.5)
                        return True
                except Exception:
                    continue
            # Clear and try next search
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            time.sleep(0.3)

        page.keyboard.press("Escape")
        return False
    except Exception as e:
        log(f"    county-fix error: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _workday_fill_phone_code(page):
    """Fill the phoneNumber--countryPhoneCode dropdown in Workday with United Kingdom (+44)."""
    if not is_workday_url(page.url):
        return False
    try:
        # Check if field is visible and not already set
        result = page.evaluate("""() => {
            for (const sel of [
                '[data-automation-id="phoneNumber--countryPhoneCode"] button',
                '[data-automation-id="phoneNumber--countryPhoneCode"] [role="combobox"]',
                '#phoneNumber--countryPhoneCode',
                '[id*="countryPhoneCode"]'
            ]) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent) return {found: true, val: el.innerText.trim()};
            }
            return {found: false, val: ''};
        }""")
        if not result or not result.get("found"):
            return False
        current = result.get("val", "")
        if "+44" in current or "United Kingdom" in current:
            return True  # already set

        # Click to open the dropdown via JS
        clicked = page.evaluate("""() => {
            for (const sel of [
                '[data-automation-id="phoneNumber--countryPhoneCode"] [data-automation-id="click_filter"]',
                '[data-automation-id="phoneNumber--countryPhoneCode"] button[aria-haspopup]',
                '[data-automation-id="phoneNumber--countryPhoneCode"] [role="combobox"]',
                '#phoneNumber--countryPhoneCode',
                '[id*="countryPhoneCode"]'
            ]) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent) { el.click(); return true; }
            }
            return false;
        }""")
        if not clicked:
            return False
        time.sleep(1.0)

        # Type to filter and pick United Kingdom (+44)
        page.keyboard.type("United Kingdom", delay=50)
        time.sleep(1.0)
        opts = page.locator('[role="option"]')
        for i in range(min(opts.count(), 20)):
            opt = opts.nth(i)
            try:
                if not opt.is_visible():
                    continue
                txt = opt.inner_text().strip()
                if "united kingdom" in txt.lower():
                    opt.click()
                    log(f"    phone-code-fix: selected '{txt}'")
                    time.sleep(0.5)
                    return True
            except Exception:
                continue

        page.keyboard.press("Escape")
        return False
    except Exception as e:
        log(f"    phone-code-fix error: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _workday_fill_source_dropdown(page):
    """Directly fill the 'How Did You Hear About Us' / source dropdown in Workday."""
    if not is_workday_url(page.url):
        return False
    try:
        log("    source-fix: checking")
        # Close any open dropdowns first
        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)
            page.keyboard.press("Escape")
            time.sleep(0.3)

        # ── NEW: Workday multiselect handler ─────────────────────────────────────
        # Blue Owl (and some other tenants) render "How Did You Hear About Us?" as
        # a multiSelectContainer widget, not a standard dropdown button.
        # Interaction: click multiselectInputContainer → wait for options → click best option.
        except Exception:
            pass
        try:
            # Use Playwright native clicks (not JS) so React synthetic events fire properly
            multi_inputs = page.locator('[data-automation-id="multiselectInputContainer"]')
            for i in range(multi_inputs.count()):
                try:
                    container = multi_inputs.nth(i)
                    if not container.is_visible(timeout=300):
                        continue
                    # Check if this container belongs to the "How Did You Hear About Us?" field
                    lbl_text = container.evaluate("""el => {
                        let node = el.parentElement;
                        for (let lvl = 0; lvl < 8 && node; lvl++, node = node.parentElement) {
                            const lbl = node.querySelector('label');
                            if (lbl && /how did you hear|hear about us/i.test(lbl.innerText))
                                return lbl.innerText;
                        }
                        return '';
                    }""") or ""
                    if not re.search(r'how did you hear|hear about us', lbl_text, re.I):
                        continue
                    # Check if already filled via aria instruction
                    aria_text = container.evaluate("""el => {
                        let node = el.parentElement;
                        for (let lvl = 0; lvl < 8 && node; lvl++, node = node.parentElement) {
                            const ai = node.querySelector('[data-automation-id="promptAriaInstruction"]');
                            if (ai) return ai.innerText.trim().toLowerCase();
                        }
                        return '';
                    }""") or ""
                    if aria_text and not aria_text.startswith("0 item"):
                        log("    source-fix: multiselect already filled — done")
                        return True
                    # Open with Playwright native click (triggers React events)
                    container.click()
                    log("    source-fix: multiselect opened — selecting option")
                    time.sleep(1.5)
                    opts = page.locator('[role="option"]')
                    PRIORITY = ["job board", "linkedin", "agency", "career site", "website", "online", "other"]
                    selected = None
                    for kw in PRIORITY:
                        for j in range(min(opts.count(), 30)):
                            try:
                                txt = opts.nth(j).inner_text().strip()
                                if kw in txt.lower():
                                    opts.nth(j).scroll_into_view_if_needed(timeout=1000)
                                    opts.nth(j).click()  # Playwright native click with scroll
                                    log(f"    source-fix: multiselect selected '{txt}'")
                                    selected = txt
                                    break
                            except Exception:
                                continue
                        if selected:
                            break
                    if not selected and opts.count() > 0:
                        for j in range(min(opts.count(), 10)):
                            try:
                                txt = opts.nth(j).inner_text().strip()
                                if txt and "referral" not in txt.lower():
                                    opts.nth(j).scroll_into_view_if_needed(timeout=1000)
                                    opts.nth(j).click()
                                    log(f"    source-fix: multiselect fallback '{txt}'")
                                    selected = txt
                                    break
                            except Exception:
                                continue
                    if selected:
                        time.sleep(1.0)
                        # Close dropdown: press Escape if still open
                        try:
                            if page.locator('[role="listbox"]').is_visible(timeout=500):
                                page.keyboard.press("Escape")
                                time.sleep(0.3)
                        except Exception:
                            pass
                        return True
                    break  # Found the right container but couldn't select — fall through
                except Exception as ce:
                    continue
        except Exception as me:
            log(f"    source-fix: multiselect error: {me}")
        # ── END multiselect handler ──────────────────────────────────────────────

        try:
            pass  # placeholder for original try block continuation
        except Exception:
            pass
        # Find the source dropdown by known automation id or label text
        # Find and click the source dropdown
        # First check current value WITHOUT clicking, and whether the field is visible at all
        good_source_vals = {"linkedin", "job posting", "job board", "website", "online", "internet", "social media", "nb careers"}
        source_state = page.evaluate("""() => {
            // Direct ID check — use EXACT match only to avoid matching phone code elements
            for (const sel of ['[id="source--source"]', '[data-automation-id="source--source"]']) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent) return {visible: true, val: el.innerText.trim() || el.value || ''};
            }
            // Button/combobox with aria-labelledby / aria-label
            const allBtns = Array.from(document.querySelectorAll(
                'button[aria-haspopup="listbox"], [role="combobox"]'
            )).filter(b => b.offsetParent);
            for (const btn of allBtns) {
                const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                if (/how did you hear|hear about us/i.test(ariaLabel))
                    return {visible: true, val: btn.innerText.trim()};
                for (const id of (btn.getAttribute('aria-labelledby') || '').split(' ')) {
                    const lbl = document.getElementById(id);
                    if (lbl && /how did you hear|hear about us/i.test(lbl.innerText))
                        return {visible: true, val: btn.innerText.trim()};
                }
            }
            // click_filter overlays
            for (const ov of document.querySelectorAll('[data-automation-id="click_filter"]')) {
                const label = (ov.getAttribute('aria-label') || '').toLowerCase();
                if (label.includes('hear') || label.includes('referral') || label.includes('how did'))
                    return {visible: true, val: ov.innerText.trim()};
            }
            // Label proximity
            const labels = document.querySelectorAll('label');
            for (const lbl of labels) {
                if (!lbl.offsetParent) continue;
                if (!/how did you hear|hear about us/i.test(lbl.innerText)) continue;
                const lblRect = lbl.getBoundingClientRect();
                for (const btn of allBtns) {
                    const r = btn.getBoundingClientRect();
                    if (Math.abs(r.top - lblRect.top) < 200 && Math.abs(r.left - lblRect.left) < 500)
                        return {visible: true, val: btn.innerText.trim()};
                }
                return {visible: false, val: '', labelFound: true};
            }
            return {visible: false, val: '', labelFound: false};
        }""") or {}
        if not source_state.get("visible"):
            # If the label is present but no button found, try direct combobox input before giving up.
            if source_state.get("labelFound"):
                log("    source-fix: label found but no dropdown button — trying direct combobox input")
                filled = False
                for src_sel in ['[id="source--source"]', '[data-automation-id="source--source"]']:
                    try:
                        inp = page.locator(src_sel)
                        if inp.count() > 0:
                            inp.first.scroll_into_view_if_needed(timeout=2000)
                            inp.first.click(timeout=2000)
                            time.sleep(0.3)
                            inp.first.fill("")
                            time.sleep(0.2)
                            inp.first.type("LinkedIn", delay=40)
                            time.sleep(0.8)
                            opts = page.locator('[role="option"]')
                            if opts.count() > 0:
                                for i in range(min(opts.count(), 10)):
                                    try:
                                        txt = opts.nth(i).inner_text().strip()
                                        if "linkedin" in txt.lower():
                                            opts.nth(i).evaluate("el => el.click()")
                                            log(f"    source-fix: label-found combobox selected '{txt}'")
                                            time.sleep(0.5)
                                            filled = True
                                            break
                                    except Exception:
                                        continue
                            if not filled and opts.count() > 0:
                                for i in range(min(opts.count(), 10)):
                                    try:
                                        txt = opts.nth(i).inner_text().strip()
                                        if txt and "referral" not in txt.lower() and "employee" not in txt.lower():
                                            opts.nth(i).evaluate("el => el.click()")
                                            log(f"    source-fix: label-found fallback '{txt}'")
                                            time.sleep(0.5)
                                            filled = True
                                            break
                                    except Exception:
                                        continue
                            if filled:
                                return True
                            page.keyboard.press("Escape")
                            break
                    except Exception:
                        continue
            log("    source-fix: source dropdown not visible on this section — skipping")
            return True  # Not an error, field is on a different section
        current_source = source_state.get("val", "")
        if current_source:
            log(f"    source-fix: current source value = '{current_source[:40]}'")

        # If current value looks like a country name, the label traversal found the wrong element.
        # Country names are multi-word with capital letters, often containing "Islands", "States", etc.
        country_like_patterns = [
            r'\b(islands|states|republic|kingdom|federation|emirates|africa|america|europe|asia)\b',
        ]
        if current_source and any(re.search(p, current_source, re.I) for p in country_like_patterns):
            log(f"    source-fix: button shows country value — trying direct input approach")
            # The proximity check found the Country button. Try direct text-input approach:
            # [id="source--source"] is Workday's combobox text input for source-of-hire
            filled = False
            for src_sel in ['[id="source--source"]', '[data-automation-id="source--source"]']:
                try:
                    inp = page.locator(src_sel)
                    if inp.count() > 0 and inp.first.is_visible(timeout=1000):
                        inp.first.click()
                        time.sleep(0.3)
                        inp.first.fill("")
                        time.sleep(0.2)
                        inp.first.type("LinkedIn", delay=40)
                        time.sleep(0.8)
                        # Try to click a matching option
                        opts = page.locator('[role="option"]')
                        if opts.count() > 0:
                            for i in range(min(opts.count(), 10)):
                                try:
                                    txt = opts.nth(i).inner_text().strip()
                                    if "linkedin" in txt.lower():
                                        opts.nth(i).evaluate("el => el.click()")
                                        log(f"    source-fix: direct-input selected '{txt}'")
                                        time.sleep(0.5)
                                        filled = True
                                        break
                                except Exception:
                                    continue
                        if not filled and opts.count() > 0:
                            # Fallback: pick first non-referral option
                            for i in range(min(opts.count(), 10)):
                                try:
                                    txt = opts.nth(i).inner_text().strip()
                                    if txt and "referral" not in txt.lower() and "employee" not in txt.lower():
                                        opts.nth(i).evaluate("el => el.click()")
                                        log(f"    source-fix: direct-input fallback '{txt}'")
                                        time.sleep(0.5)
                                        filled = True
                                        break
                                except Exception:
                                    continue
                        if filled:
                            return True
                        page.keyboard.press("Escape")
                        break
                except Exception:
                    continue
            return True  # Not an error even if not found

        # Now click to open — try multiple approaches
        # First: debug dump to find the actual source field element
        src_debug = page.evaluate("""() => {
            const labels = document.querySelectorAll('label');
            for (const lbl of labels) {
                if (!lbl.offsetParent) continue;
                if (!/how did you hear|hear about us/i.test(lbl.innerText)) continue;
                const results = [];
                // Walk up 5 levels from label to find interactive elements
                let node = lbl.parentElement;
                for (let lvl = 0; lvl < 5 && node; lvl++, node = node.parentElement) {
                    const els = node.querySelectorAll('button, [role="button"], [role="combobox"], [role="listbox"], select, input[type="text"], [data-automation-id]');
                    for (const el of els) {
                        if (!el.offsetParent) continue;
                        results.push({
                            tag: el.tagName,
                            id: el.id || '',
                            autoId: el.getAttribute('data-automation-id') || '',
                            role: el.getAttribute('role') || '',
                            text: (el.innerText || el.value || '').trim().slice(0, 30),
                            lvl: lvl
                        });
                    }
                    if (results.length > 0) break;
                }
                return results.slice(0, 5);
            }
            return [];
        }""") or []
        if src_debug:
            log(f"    source-fix: nearby elements: {src_debug}")

        # Log what [id="source--source"] actually is on this page
        src_elem_info = page.evaluate("""() => {
            const el = document.querySelector('[id="source--source"]');
            if (!el) return null;
            let path = [];
            let node = el;
            for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
                path.push(node.tagName + (node.id ? '#'+node.id : '') + (node.getAttribute('data-automation-id') ? '['+node.getAttribute('data-automation-id')+']' : ''));
            }
            return {tag: el.tagName, id: el.id, type: el.type || '', autoId: el.getAttribute('data-automation-id') || '', visible: !!el.offsetParent, val: (el.innerText || el.value || '').trim().slice(0, 40), path: path.join(' > ')};
        }""")
        if src_elem_info:
            log(f"    source-fix: [id=source--source] info: {src_elem_info}")

        # If the dropdown/multiselect is already open (e.g. fill action typed into searchbox),
        # skip the JS click and go straight to option selection
        _pre_opts = page.locator('[role="option"]')
        _pre_count = _pre_opts.count()
        if _pre_count > 0:
            log(f"    source-fix: dropdown already open ({_pre_count} options) — skipping click")
            clicked = 'already-open'
        else:
            clicked = page.evaluate("""() => {
            // Approach 0a: find formField container with "how did you hear" label, click its button
            // This is the most targeted approach and avoids clicking the wrong element
            for (const container of document.querySelectorAll('[data-automation-id^="formField"]')) {
                const lbl = container.querySelector('label');
                if (!lbl || !/how did you hear|hear about us/i.test(lbl.innerText)) continue;
                // Found the right container — click its dropdown button
                const btn = container.querySelector('button[aria-haspopup], button[data-automation-id], [role="combobox"]');
                if (btn && btn.offsetParent) { btn.click(); return 'form-field-container'; }
                // Try clicking any visible interactive element in the container
                const anyBtn = container.querySelector('button');
                if (anyBtn && anyBtn.offsetParent) { anyBtn.click(); return 'form-field-button'; }
            }
            // Approach 0b: find by label proximity, broader element types
            const labels = document.querySelectorAll('label');
            for (const lbl of labels) {
                if (!lbl.offsetParent) continue;
                if (!/how did you hear|hear about us/i.test(lbl.innerText)) continue;
                const lblRect = lbl.getBoundingClientRect();
                // Search more broadly: buttons, selects, [role="button"], [data-automation-id*="select"]
                const candidates = Array.from(document.querySelectorAll(
                    'button, [role="button"], [role="combobox"], select, [data-automation-id*="select"], [data-automation-id*="dropdown"]'
                )).filter(el => el.offsetParent);
                let best = null, minDist = Infinity;
                for (const el of candidates) {
                    const r = el.getBoundingClientRect();
                    const dy = r.top - lblRect.top;
                    const dx = Math.abs(r.left - lblRect.left);
                    if (dy >= -20 && dy < 300 && dx < 600) {
                        const dist = Math.sqrt(dx*dx + dy*dy);
                        if (dist < minDist) { minDist = dist; best = el; }
                    }
                }
                if (best) { best.click(); return 'label-proximity-broad'; }
            }
            // Approach 0c: direct EXACT ID (Workday standard source--source) — ONLY if not phone code
            for (const sel of ['[id="source--source"]', '[data-automation-id="source--source"]']) {
                const el = document.querySelector(sel);
                // Skip if this element is inside a phone number section
                if (el && el.offsetParent) {
                    const phoneParent = el.closest('[data-automation-id*="phone"], [id*="phone"]');
                    if (!phoneParent) { el.click(); return 'direct-id'; }
                }
            }

            const allBtns = Array.from(document.querySelectorAll('button[aria-haspopup="listbox"]'))
                .filter(b => b.offsetParent);

            // Approach 1: button with aria-labelledby / aria-label matching "hear about us"
            for (const btn of allBtns) {
                const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                if (/how did you hear|hear about us/i.test(ariaLabel)) {
                    btn.click(); return 'aria-label';
                }
                for (const id of (btn.getAttribute('aria-labelledby') || '').split(' ')) {
                    const lbl = document.getElementById(id);
                    if (lbl && /how did you hear|hear about us/i.test(lbl.innerText)) {
                        btn.click(); return 'aria-labelledby';
                    }
                }
            }
            // Approach 2: click_filter overlay with aria-label containing hear/referral
            for (const ov of document.querySelectorAll('[data-automation-id="click_filter"]')) {
                const label = (ov.getAttribute('aria-label') || '').toLowerCase();
                if (label.includes('hear') || label.includes('referral') || label.includes('how did')) {
                    ov.click(); return 'click_filter';
                }
            }
            // Approach 3: proximity — find the button visually closest to the "How Did You Hear" label
            const labels3 = document.querySelectorAll('label');
            for (const lbl of labels3) {
                if (!lbl.offsetParent) continue;
                if (!/how did you hear|hear about us/i.test(lbl.innerText)) continue;
                const lblRect = lbl.getBoundingClientRect();
                let closest = null, minDist = Infinity;
                for (const btn of allBtns) {
                    const r = btn.getBoundingClientRect();
                    const dy = r.top - lblRect.top;
                    const dx = Math.abs(r.left - lblRect.left);
                    if (dy >= -20 && dy < 200 && dx < 500) {
                        const dist = Math.sqrt(dx*dx + dy*dy);
                        if (dist < minDist) { minDist = dist; closest = btn; }
                    }
                }
                if (closest) { closest.click(); return 'proximity'; }
            }
            return false;
        }""")
        log(f"    source-fix: click result = {clicked!r}")
        if not clicked:
            return False
        time.sleep(1.0)

        # List all available options, then pick best match
        time.sleep(1.0)
        opts = page.locator('[role="option"]')
        all_opts = []
        count = opts.count()
        log(f"    source-fix: found {count} options in dropdown")
        # Bail if wrong dropdown: options look like phone/country codes with NO source keywords present
        SOURCE_KEYWORDS = {"agency", "job board", "job posting", "linkedin", "website", "online",
                           "internet", "social media", "referral", "career site", "other", "network",
                           "organization", "contractor", "former", "external", "internal"}
        if count > 20 or count > 50:
            sample_texts = []
            for i in range(min(count, 15)):
                try:
                    t = opts.nth(i).inner_text().strip()
                    if t:
                        sample_texts.append(t)
                except Exception:
                    pass
            log(f"    source-fix: sample options: {sample_texts[:8]}")
            has_source_keywords = any(
                any(kw in t.lower() for kw in SOURCE_KEYWORDS) for t in sample_texts
            )
            if has_source_keywords:
                pass  # This is actually the right dropdown — proceed
            else:
                is_phone_codes = sum(1 for t in sample_texts if re.search(r'\(\+\d+\)', t)) >= 2
                is_country_list = sum(1 for t in sample_texts if re.search(
                    r'\b(island|states|republic|kingdom|africa|america|europe)\b', t, re.I)) >= 2
                if is_phone_codes or is_country_list or count > 200:
                    reason = "phone codes" if is_phone_codes else ("country list" if is_country_list else f"{count} options")
                    log(f"    source-fix: wrong dropdown ({reason}) — pressing Escape")
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.3)
                    except Exception:
                        pass
                    return False
        for i in range(min(count, 50)):
            opt = opts.nth(i)
            try:
                txt = opt.inner_text().strip()
                visible = opt.is_visible()
                if visible and txt:
                    all_opts.append((i, txt, opt))
            except Exception:
                continue

        log(f"    source-fix: visible options: {[t for _, t, _ in all_opts[:10]]}")

        if all_opts:
            # Priority: LinkedIn > Job Posting > job board > website > online > first non-referral
            priority_keywords = ["linkedin", "job posting", "job board", "website", "online", "internet", "social"]
            selected_txt = None
            for kw in priority_keywords:
                for idx, txt, opt in all_opts:
                    if kw in txt.lower():
                        # Use evaluate to click to bypass click_filter
                        try:
                            opt.evaluate("el => el.click()")
                        except Exception:
                            opt.click()
                        log(f"    source-fix: selected '{txt}' for How Did You Hear About Us")
                        time.sleep(1.0)
                        selected_txt = txt
                        break
                if selected_txt:
                    break
            if not selected_txt:
                # If none matched, pick first (not employee referral or referral)
                for idx, txt, opt in all_opts:
                    if "employee" not in txt.lower() and "referral" not in txt.lower():
                        try:
                            opt.evaluate("el => el.click()")
                        except Exception:
                            opt.click()
                        log(f"    source-fix: fallback selected '{txt}' for How Did You Hear About Us")
                        time.sleep(1.0)
                        selected_txt = txt
                        break

            if selected_txt:
                # Close dropdown if still open
                try:
                    if page.locator('[role="listbox"]').is_visible(timeout=500):
                        page.keyboard.press("Escape")
                        time.sleep(0.5)
                except Exception:
                    pass
                time.sleep(0.5)
                # Verify source field — if it shows a good value already, skip sub-options
                good_vals = ["linkedin", "job posting", "job board", "website", "online", "internet", "social", "agency", "career", "board"]
                try:
                    source_now = page.evaluate("""() => {
                        for (const sel of ['[id="source--source"]', '[data-automation-id="source--source"]']) {
                            const el = document.querySelector(sel);
                            if (el && el.offsetParent) return el.innerText.trim() || el.value || '';
                        }
                        return '';
                    }""") or ""
                    if any(kw in source_now.lower() for kw in good_vals):
                        log(f"    source-fix: value confirmed '{source_now[:40]}' — done")
                        return True
                except Exception:
                    pass
                # Check if a sub-dropdown appeared (Workday sometimes has source sub-selection)
                time.sleep(0.5)
                sub_opts = page.locator('[role="option"]')
                if sub_opts.count() > 0:
                    # Sub-dropdown appeared — pick LinkedIn or first non-referral option
                    sub_all = []
                    for i in range(min(sub_opts.count(), 30)):
                        opt = sub_opts.nth(i)
                        try:
                            if opt.is_visible():
                                txt = opt.inner_text().strip()
                                if txt:
                                    sub_all.append((txt, opt))
                        except Exception:
                            continue
                    if sub_all:
                        log(f"    source-fix: sub-options: {[t for t, _ in sub_all[:5]]}")
                        # Guard: if all sub-options look like phone codes, close and return
                        phone_code_count = sum(1 for t, _ in sub_all if re.search(r'\(\+\d+\)', t))
                        if phone_code_count >= len(sub_all) or (phone_code_count > 0 and len(sub_all) <= 2):
                            log("    source-fix: sub-options are phone codes — closing dropdown")
                            try:
                                page.keyboard.press("Escape")
                                time.sleep(0.3)
                            except Exception:
                                pass
                            return True
                        for kw in ["linkedin", "nb careers", "careers", "website", "job"]:
                            for stxt, sopt in sub_all:
                                if kw in stxt.lower():
                                    try:
                                        sopt.evaluate("el => el.click()")
                                    except Exception:
                                        sopt.click()
                                    log(f"    source-fix: selected sub-option '{stxt}'")
                                    time.sleep(0.5)
                                    return True
                        # Pick first non-referral
                        for stxt, sopt in sub_all:
                            if "referral" not in stxt.lower() and "employee" not in stxt.lower():
                                try:
                                    sopt.evaluate("el => el.click()")
                                except Exception:
                                    sopt.click()
                                log(f"    source-fix: fallback sub-option '{stxt}'")
                                time.sleep(0.5)
                                return True
                return True

        # If no options visible yet, try typing to filter
        for search in ["Job Posting", "Job Board", "Website", "Online"]:
            page.keyboard.type(search[:10], delay=50)
            time.sleep(0.8)
            opts = page.locator('[role="option"]')
            count = opts.count()
            for i in range(min(count, 30)):
                opt = opts.nth(i)
                try:
                    if not opt.is_visible():
                        continue
                    txt = opt.inner_text().strip()
                    if search.lower() in txt.lower() or txt.lower() in search.lower():
                        opt.click()
                        log(f"    source-fix: typed-search selected '{txt}' for How Did You Hear About Us")
                        time.sleep(0.5)
                        return True
                except Exception:
                    continue
            # Clear search
            try:
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                time.sleep(0.3)
            except Exception:
                pass

        page.keyboard.press("Escape")
        return False
    except Exception as e:
        log(f"    source-fix error: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _handle_captcha_via_telegram(page, company, role):
    """Screenshot the CAPTCHA, send to Telegram, wait for human to solve it on the page.
    The human should solve it directly in the browser (which is visible on her screen).
    We just wait and check if the CAPTCHA disappeared."""
    try:
        screenshot = page.screenshot(full_page=False)
        tg_photo(
            screenshot,
            caption=f"CAPTCHA detected!\n{role} @ {company}\n\nPlease solve it in the browser window. I'll wait up to 5 minutes.",
        )
        tg(f"Waiting for you to solve the CAPTCHA in the browser...")

        # Wait up to 5 minutes, checking every 10 seconds if CAPTCHA is gone
        for i in range(30):
            time.sleep(10)
            try:
                vis = page.evaluate("() => document.body.innerText.toLowerCase().slice(0, 2000)")
                if not any(x in vis for x in ["captcha", "verify you are human", "i'm not a robot", "recaptcha"]):
                    tg(f"CAPTCHA solved! Continuing with {company}...")
                    return True
            except Exception:
                pass
        tg(f"CAPTCHA timeout for {company} — skipping")
        return False
    except Exception as e:
        log(f"  captcha-tg error: {e}")
        return False


def _scrape_job_description(page):
    """Extract the job description text from the current page (for tailored answers)."""
    try:
        return page.evaluate("""() => {
            // Try common job description containers
            const selectors = [
                '[class*="job-description"]', '[class*="jobDescription"]',
                '[class*="description"]', '[data-automation-id="jobPostingDescription"]',
                'article', '[class*="posting-page"]', '[class*="content-wrapper"]',
                '.job-details', '#job-details', '.description',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.innerText.trim().length > 200) {
                    return el.innerText.trim().slice(0, 3000);
                }
            }
            // Fallback: get the longest text block on the page
            const divs = [...document.querySelectorAll('div,section')];
            let best = '';
            for (const d of divs) {
                const t = d.innerText.trim();
                if (t.length > best.length && t.length < 5000) best = t;
            }
            return best.slice(0, 3000);
        }""")
    except Exception:
        return ""


def apply_company(page, url, role, company, ws_profile, client):
    """Apply to a single job. Returns (success: bool, reason: str)."""
    # Resolve company URL from LinkedIn if needed
    if "linkedin.com" in url:
        log("  Finding company apply URL via LinkedIn...")
        company_url = get_company_apply_url(page, url)
        if not company_url:
            return False, "couldn't find company apply URL from LinkedIn"
    else:
        company_url = url

    log(f"  Navigating to: {company_url}")
    for attempt in range(3):
        try:
            page.goto(company_url, wait_until="domcontentloaded", timeout=30000)
            break
        except Exception as e:
            err = str(e)
            if any(s in err for s in ["ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED"]):
                return False, f"Page failed to load: {err[:80]}"
            if attempt < 2:
                log(f"  Navigation attempt {attempt+1} failed, retrying...")
                time.sleep(5)
    time.sleep(4)

    # Handle 429 rate limit
    try:
        page_text = page.content().lower()[:500]
        if "429" in page.title() or "too many requests" in page_text:
            log("  429 rate limit -- waiting 45s")
            time.sleep(45)
            page.reload(wait_until="domcontentloaded", timeout=20000)
            time.sleep(3)
    except Exception:
        pass

    # Early detection: CAPTCHA, blocked, or requires account creation with CAPTCHA
    try:
        page_text_lower = page.evaluate("() => document.body.innerText.toLowerCase().slice(0, 3000)")
        page_url_lower = page.url.lower()
        if any(x in page_text_lower for x in ["captcha", "verify you are human", "i'm not a robot", "recaptcha"]):
            if TG_TOKEN and TG_CHAT:
                solved = _handle_captcha_via_telegram(page, company, role)
                if solved:
                    log("  CAPTCHA solved via Telegram, continuing...")
                else:
                    return False, "CAPTCHA detected — Telegram solve timed out"
            else:
                return False, "CAPTCHA detected on landing page"
        if any(x in page_text_lower for x in ["access denied", "403 forbidden", "you have been blocked"]):
            return False, "blocked/access denied"
        # SuccessFactors: requires account creation with CAPTCHA (can't automate)
        if "successfactors" in page_url_lower or "sap.com/career" in page_url_lower:
            if any(x in page_text_lower for x in ["create account", "register", "sign up"]):
                if "captcha" in page_text_lower or "recaptcha" in page.content().lower()[:5000]:
                    return False, "SuccessFactors account creation requires CAPTCHA"
        # Taleo: often requires account creation
        if "taleo" in page_url_lower and "create" in page_text_lower and "account" in page_text_lower:
            return False, "Taleo requires account creation"
        # iCIMS: sometimes requires sign-in
        if "icims.com" in page_url_lower and "sign in" in page_text_lower and "create" in page_text_lower:
            # iCIMS can sometimes work without account — only skip if login is required
            pass
    except Exception:
        pass

    # Scrape job description from the landing page (before form navigation)
    job_desc = _scrape_job_description(page)
    if job_desc:
        log(f"  Scraped job description ({len(job_desc)} chars)")

    system = build_system_prompt(role, company, ws_profile, job_description=job_desc)
    context = page.context
    stuck_count = 0
    prev_url = ""
    captcha_counts = {}  # url → count of CAPTCHAs solved on that URL

    for page_num in range(MAX_PAGES):
        current_url = page.url
        log(f"\n  [Page {page_num + 1}] {current_url}")

        # Check for success
        if is_success_page(page):
            return True, "submitted"

        # Workday job listing page — click Apply and catch new tab
        # Workday job listing URLs look like /job/City/Role_Title/REQ123
        # (as opposed to /apply/... or /login or auth pages)
        if is_workday_url(current_url) and re.search(r'/job/[^/]+/[^/]+', current_url):
            page = _workday_click_apply_and_catch_tab(page, context)
            current_url = page.url  # URL may have changed
            prev_url = ""
            time.sleep(2)
            # Fall through to auth handler below

        # Workday auth page handler (Create Account / Sign In loop fix)
        # Must run BEFORE Claude call so we never waste an API call on auth pages
        if is_workday_url(current_url):
            if _handle_workday_auth_page(page):
                time.sleep(2)
                prev_url = ""  # Reset stuck detection so URL change is noticed
                continue

        # Quick CAPTCHA/verification check each page
        try:
            # Use full page text (not sliced) to avoid missing messages buried below nav
            vis_text = page.evaluate("() => document.body.innerText.toLowerCase()")
            vis_short = vis_text[:3000]  # shorter slice for captcha checks (faster)

            # Only trigger if it's an actual CAPTCHA challenge, not just a footer mention
            real_captcha = (
                "verify you are human" in vis_short
                or "i'm not a robot" in vis_short
                or "im not a robot" in vis_short
                or ("captcha" in vis_short and any(x in vis_short for x in ["solve", "puzzle", "drag", "select all", "click all", "type the"]))
            )
            if real_captcha:
                captcha_counts[current_url] = captcha_counts.get(current_url, 0) + 1
                if captcha_counts[current_url] > 4:
                    return False, f"CAPTCHA appeared {captcha_counts[current_url]} times on same page — giving up"
                if TG_TOKEN and TG_CHAT:
                    solved = _handle_captcha_via_telegram(page, company, role)
                    if solved:
                        log("  CAPTCHA solved via Telegram, clicking Next...")
                        time.sleep(2)
                        click_next_button(page)
                        time.sleep(4)
                        continue
                return False, "CAPTCHA on form page"

            email_verify_triggered = any(phrase in vis_text for phrase in [
                "verification code", "verify your email", "enter the code",
                "we sent a code", "check your email", "sent you an email",
                "confirm your email", "activate your account", "click the link",
                "verify your account", "please verify", "email has been sent",
                "check the email", "open the email",
            ])
            # Oracle HCM: /apply/email is the EMAIL ENTRY page (not OTP) — skip it.
            # The actual OTP page has 6 individual single-digit inputs.
            if not email_verify_triggered and "oraclecloud.com" in page.url:
                try:
                    otp_inputs = page.locator('input[maxlength="1"]')
                    if otp_inputs.count() >= 6:
                        log("  Oracle HCM OTP page detected (6 individual digit inputs)")
                        email_verify_triggered = True
                except Exception:
                    pass
            # Workday-specific: proactively check Gmail after account creation
            # Workday shows "check your email" but it's often in a modal/alert not in body
            if not email_verify_triggered and is_workday_url(page.url):
                if any(phrase in vis_text for phrase in [
                    "account has been created", "we've sent", "email has been sent",
                    "please check your inbox", "you will receive", "email to verify",
                    "verification email", "account created",
                ]):
                    email_verify_triggered = True
            if email_verify_triggered:
                if handle_email_verification(page):
                    log("  Email verification completed, continuing...")
                    time.sleep(3)
                    continue
                # Don't hard-fail here — let Claude try to handle the page
        except Exception:
            pass

        # Handle new tabs from previous navigation
        page = handle_new_tabs(page, context)

        # Dismiss discard/leave dialogs
        _dismiss_leave_dialogs(page)

        time.sleep(1)

        # Dismiss cookie consent banners before any other actions
        _dismiss_cookie_banner(page)

        # Pre-fill: auto-click Yes/No toggles and consent checkboxes
        _auto_answer_yesno_toggles(page)
        _auto_check_consent(page)
        _auto_fill_workable_required_radios(page)
        _auto_fill_oracle_jet_dropdowns(page)
        _auto_fill_workday_dropdowns(page)
        _workday_fill_county_region(page)
        _workday_fill_phone_code(page)
        _workday_fill_source_dropdown(page)

        # Extract fields
        fields = extract_page_fields(page)
        log(f"  Found {len(fields)} fields")

        # Step 1: Apply cached answers first (saves Claude API calls)
        cached = get_cached_answers(fields)
        if cached:
            log(f"  Applying {len(cached)} cached answers")
            execute_actions(page, cached)
            time.sleep(0.5)
            # Re-run Workday source-fix AFTER cache — cache may have overwritten the source
            # dropdown with a value that doesn't exist in this tenant's options
            if is_workday_url(page.url):
                _workday_fill_phone_code(page)
                _workday_fill_source_dropdown(page)
            # Re-extract fields to see what's left
            fields = extract_page_fields(page)

        # Step 2: Ask Claude to analyze page and return actions for remaining fields
        # Filter out multiselect search inputs that are handled by source-fix (prevent interference)
        MULTISELECT_IDS = {"source--source"}
        fields_for_claude = [f for f in fields if f.get("id") not in MULTISELECT_IDS]
        actions = analyze_page(client, page, fields_for_claude, system, role, company)
        log(f"  Claude returned {len(actions)} actions")

        # Check for terminal actions
        if any(a.get("action") == "DONE" for a in actions if isinstance(a, dict)):
            return True, "submitted"
        skip = next((a for a in actions if isinstance(a, dict) and a.get("action") == "SKIP"), None)
        if skip:
            reason = skip.get("reason", "skipped by Claude")
            # If Claude detected a CAPTCHA, try Telegram before giving up
            if any(w in reason.lower() for w in ["captcha", "puzzle", "verify you are human", "robot"]):
                captcha_counts[current_url] = captcha_counts.get(current_url, 0) + 1
                if captcha_counts[current_url] > 4:
                    return False, f"CAPTCHA appeared {captcha_counts[current_url]} times on same page — giving up"
                if TG_TOKEN and TG_CHAT:
                    solved = _handle_captcha_via_telegram(page, company, role)
                    if solved:
                        log("  CAPTCHA solved via Telegram, clicking Next...")
                        time.sleep(2)
                        click_next_button(page)
                        time.sleep(4)
                        continue
                return False, f"CAPTCHA detected — Telegram solve timed out: {reason}"
            return False, reason

        # Execute all actions
        if actions:
            result = execute_actions(page, actions)
            if result == "DONE":
                return True, "submitted"
            if isinstance(result, str) and result.startswith("SKIP:"):
                return False, result[5:].strip()

            # Step 3: Cache successful answers for future applications
            try:
                cache_answers(fields, actions)
            except Exception:
                pass
        time.sleep(1)

        # Quick re-fill pass: React sometimes clears fields during re-render.
        # Re-execute fill actions for any fields that are now empty.
        if actions:
            fields_check = extract_page_fields(page)
            empty_ids = {
                f["id"] for f in fields_check
                if f.get("id") and not f.get("value")
                and f["type"] in ("text", "email", "tel", "textarea")
                and not f.get("disabled") and not f.get("readonly")
            }
            if empty_ids:
                refills = [
                    a for a in actions
                    if isinstance(a, dict) and a.get("action") == "fill"
                    and any(eid in a.get("selector", "") for eid in empty_ids)
                ]
                if refills:
                    log(f"  Re-filling {len(refills)} fields cleared by React")
                    execute_actions(page, refills)
                    time.sleep(0.5)

        # Verify and retry if errors
        for retry in range(MAX_RETRIES):
            errors = get_page_errors(page)
            empty = count_empty_required(page)
            if not errors and empty == 0:
                break
            # Check for fatal errors that can't be fixed by retrying
            error_text = " ".join(errors).lower()
            if any(fatal in error_text for fatal in [
                "spam", "blocked", "too many", "rate limit",
                "account has been", "suspended", "banned",
            ]):
                log(f"  Fatal error detected: {errors[0][:100]}")
                return False, f"blocked: {errors[0][:100]}"
            log(f"  Errors: {errors[:5]} | Empty required: {empty} (retry {retry+1})")
            # Re-run targeted fixers before asking Claude
            _workday_fill_county_region(page)
            _workday_fill_phone_code(page)
            _workday_fill_source_dropdown(page)
            error_ctx = f"Validation errors: {errors}\nEmpty required fields: {empty}"
            fields_retry = [f for f in extract_page_fields(page) if f.get("id") not in MULTISELECT_IDS]
            fix_actions = analyze_page(
                client, page, fields_retry, system, role, company,
                error_context=error_ctx,
            )
            if fix_actions:
                execute_actions(page, fix_actions)
                time.sleep(1)

        # Click next/submit
        time.sleep(0.5)
        clicked = click_next_button(page)
        if clicked:
            time.sleep(4)
            # Re-apply toggles/dropdowns in case validation reset them
            _auto_answer_yesno_toggles(page)
            _auto_check_consent(page)
            _auto_fill_oracle_jet_dropdowns(page)
            _auto_fill_workday_dropdowns(page)
            _workday_fill_county_region(page)
            _workday_fill_phone_code(page)
            _workday_fill_source_dropdown(page)
            # Check for success after navigation
            page = handle_new_tabs(page, context)
            if is_success_page(page):
                return True, "submitted"
        else:
            log("  No next button found -- scrolling to look for more")
            page.mouse.move(640, 400)
            page.mouse.wheel(0, 600)
            time.sleep(1)

        # Stuck detection
        if current_url == prev_url:
            stuck_count += 1

            # Workday-specific: if stuck on account creation page
            if stuck_count >= 2 and is_workday_url(current_url):
                page_lower = ""
                try:
                    page_lower = page.evaluate("() => document.body.innerText.toLowerCase()")
                except Exception:
                    pass

                # On first stuck round: proactively check Gmail for verification email
                if stuck_count == 2:
                    log("  Workday stuck — proactively checking Gmail for verification email...")
                    if handle_email_verification(page):
                        log("  Workday email verification completed, continuing...")
                        stuck_count = 0
                        time.sleep(3)
                        continue

                # Every stuck round: try switching to Sign In if page has create account form
                if "create account" in page_lower or "already have an account" in page_lower:
                    log("  Workday: switching to Sign In flow...")
                    signed_in = False
                    for sign_in_sel in [
                        'a:has-text("Sign In")',
                        'button:has-text("Sign In")',
                        '[data-automation-id="signIn"]',
                        'a[href*="signIn"]',
                        'a[href*="signin"]',
                        'text=Sign In',
                    ]:
                        try:
                            el = page.locator(sign_in_sel).last  # use .last to get inline link not header
                            if el.count() > 0 and el.is_visible(timeout=1000):
                                el.click()
                                time.sleep(3)
                                log("  Workday: clicked Sign In")
                                # Fill email + last known password
                                try:
                                    page.fill('[data-automation-id="email"]', PROFILE["email"])
                                    time.sleep(0.5)
                                    # Try cached password (normalized key), fall back to default
                                    cache = _load_answer_cache()
                                    pwd_val = ""
                                    for raw_key in ["password", "new password"]:
                                        if raw_key in cache:
                                            pwd_val = cache[raw_key].get("value", "")
                                            break
                                    if not pwd_val:
                                        domain_m = re.search(r'https?://([^/]+)', page.url)
                                        pwd_val = _workday_passwords.get(domain_m.group(1) if domain_m else "", WORKDAY_DEFAULT_PASSWORD)
                                    page.fill('[data-automation-id="password"]', pwd_val)
                                except Exception:
                                    pass
                                stuck_count = 0
                                signed_in = True
                                break
                        except Exception:
                            continue
                    if signed_in:
                        continue

            # TAL ATS register loop: email already registered → try Sign In
            if stuck_count == 2 and "tal.net" in current_url and "/candidate/register" in current_url:
                log("  TAL ATS: stuck on register — trying Sign In instead...")
                for sign_in_sel in [
                    'a:has-text("Sign In")', 'a:has-text("Log In")', 'a:has-text("Login")',
                    'button:has-text("Sign In")', 'a[href*="login"]', 'a[href*="signin"]',
                ]:
                    try:
                        el = page.locator(sign_in_sel).first
                        if el.count() > 0 and el.is_visible(timeout=1000):
                            el.click()
                            time.sleep(3)
                            log("  TAL ATS: clicked Sign In")
                            try:
                                page.fill('[type="email"], [id*="email"], [name*="email"]', PROFILE["email"])
                                page.fill('[type="password"], [id*="password"], [name*="password"]', WORKDAY_DEFAULT_PASSWORD)
                            except Exception:
                                pass
                            stuck_count = 0
                            break
                    except Exception:
                        continue

            if stuck_count >= MAX_STUCK * 3:
                # Absolute cap — never loop more than 3x MAX_STUCK on same URL
                return False, f"stuck on same page for {stuck_count} rounds (gave up)"
            if stuck_count >= MAX_STUCK:
                # Try scrolling up and looking for button we missed
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(1)
                clicked = click_next_button(page)
                if not clicked:
                    return False, f"stuck on same page for {MAX_STUCK} rounds"
                time.sleep(4)
                # Don't reset stuck_count — keep accumulating
        else:
            stuck_count = 0
        prev_url = current_url

    return False, "max pages reached"


def _dismiss_cookie_banner(page):
    """Dismiss cookie consent banners (Accept/Agree/OK buttons)."""
    try:
        for label in ["Accept all", "Accept All", "Accept", "Agree", "OK", "Allow all", "Allow All", "Got it", "I agree"]:
            btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                time.sleep(0.5)
                return
        # Fallback: look for cookie banner by common selectors
        for selector in ["#onetrust-accept-btn-handler", ".cookie-accept", "[id*='cookie'] button", "[class*='cookie'] button"]:
            try:
                el = page.locator(selector)
                if el.count() > 0 and el.first.is_visible():
                    el.first.click()
                    time.sleep(0.5)
                    return
            except Exception:
                continue
    except Exception:
        pass


def _dismiss_leave_dialogs(page):
    """Dismiss 'Discard Application?' / 'Leave page?' dialogs.
    Also blocks 'Use My Last Application' (Workday) which would overwrite our profile."""
    try:
        # Workday: block "Use My Last Application" / "Autofill with Resume" dialog
        if is_workday_url(page.url):
            _workday_dismiss_autofill_now(page)

        dialog = page.locator('[role="dialog"], .modal, [class*="modal"]')
        if dialog.count() == 0:
            return
        text = ""
        try:
            text = dialog.first.inner_text().lower()
        except Exception:
            return

        discard_words = ["discard", "leave", "lose your", "are you sure",
                         "unsaved", "cancel application", "exit", "abandon"]
        signin_words = ["sign in", "sign up", "create account", "log in",
                        "password", "email address", "register"]

        if any(w in text for w in discard_words) and not any(w in text for w in signin_words):
            for btn_label in ["Continue", "No", "Stay", "Cancel", "Keep editing"]:
                btn = page.get_by_role("button", name=re.compile(rf"^{btn_label}$", re.I))
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    log(f"  Dismissed dialog: clicked '{btn_label}'")
                    time.sleep(1)
                    return
    except Exception:
        pass


_workday_auth_state = {}  # domain → {"signin_attempts": int, "created": bool}
_workday_apply_clicked = set()  # URLs where Apply was already clicked (avoid double-click)


def _workday_dismiss_autofill_now(page):
    """Immediately dismiss Workday's 'autofill with resume' / 'use my last application' prompt.
    Called right when the in-page form is detected, while the prompt is still visible."""
    try:
        # First, dump all visible buttons to understand what's on the page
        btns_debug = page.evaluate("""() => {
            const all = [...document.querySelectorAll('button, [role="button"]')];
            return all
                .filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                .map(b => (b.innerText || b.textContent || '').trim().substring(0, 50));
        }""")
        log(f"  Workday autofill: visible buttons = {btns_debug[:10]}")

        clicked = page.evaluate("""() => {
            const bodyText = document.body.innerText.toLowerCase();
            const hasPrompt = bodyText.includes('use my last application') ||
                              bodyText.includes('autofill with resume') ||
                              bodyText.includes('autofill with');
            if (!hasPrompt) return null;
            // Find the "skip autofill" button — must NOT be the autofill button itself
            const blockWords = ['autofill', 'resume', 'last application', 'main content',
                                'navigation', 'skip to', 'upload'];
            const prefer = [
                'apply manually', 'enter manually', 'fill out manually', 'fill manually',
                'fill in manually', 'no, thanks', 'no thanks', 'decline',
                'start fresh', 'start from scratch',
            ];
            const all = [
                ...document.querySelectorAll('button'),
                ...document.querySelectorAll('[role="button"]'),
            ];
            for (const label of prefer) {
                const el = all.find(b => {
                    const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                    if (blockWords.some(sw => t.includes(sw))) return false;
                    return t === label || t.startsWith(label);
                });
                if (el) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        el.click();
                        return (el.innerText || el.textContent || '').trim();
                    }
                }
            }
            return 'prompt-found-no-button';
        }""")
        if clicked and clicked != 'prompt-found-no-button':
            log(f"  Workday autofill prompt: dismissed via '{clicked}'")
            time.sleep(2)  # Wait for SPA transition after dismissal
        elif clicked == 'prompt-found-no-button':
            log("  Workday autofill prompt: visible but no dismiss button found")
        # else: no prompt present
    except Exception as e:
        log(f"  Workday autofill prompt dismiss error: {e}")


def _workday_click_apply_and_catch_tab(page, context):
    """On a Workday job listing page, click Apply and catch the resulting new tab.
    Returns the new page (auth/apply form) or the original page if nothing opened."""
    current_url = page.url
    if current_url in _workday_apply_clicked:
        return page  # Already successfully handled Apply here

    # Check if there's an Apply button visible
    apply_btn = None
    for sel in [
        '[data-automation-id="jobPostingApplyButton"]',
        '[data-automation-id="jobApplyButton"]',
        '[data-automation-id="applyButton"]',
        'a[data-automation-id="jobAction"]',
        'button[data-automation-id="Apply"]',
        'a[aria-label="Apply"]',
        'button[aria-label="Apply"]',
    ]:
        btn = page.locator(sel)
        if btn.count() > 0:
            try:
                if btn.first.is_visible(timeout=1000):
                    apply_btn = btn.first
                    break
            except Exception:
                pass

    if not apply_btn:
        # Try by role/text (allow partial match for "Apply Now", "Apply for this job", etc.)
        for label in ["Apply", "Apply Now"]:
            btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
            if btn.count() > 0:
                try:
                    if btn.first.is_visible(timeout=500):
                        apply_btn = btn.first
                        break
                except Exception:
                    pass
        if not apply_btn:
            # Try link with Apply text as last resort
            for lnk_sel in ['a:has-text("Apply")', 'a:has-text("Apply Now")']:
                try:
                    btn = page.locator(lnk_sel).first
                    if btn.is_visible(timeout=500):
                        apply_btn = btn
                        break
                except Exception:
                    pass

    if not apply_btn:
        log("  Workday: no Apply button found on job listing page (skipping)")
        return page  # No Apply button found on this page

    log("  Workday: clicking Apply button on job listing page...")

    # Playwright .click() selectors to try (ordered by specificity)
    # IMPORTANT: must use Playwright's .click() not page.evaluate(el.click) —
    # only real user gestures open target=_blank new tabs (popup-blocked otherwise)
    click_selectors = [
        '[data-automation-id="jobPostingApplyButton"]',
        '[data-automation-id="jobApplyButton"]',
        '[data-automation-id="applyButton"]',
        '[data-automation-id="applyNowButton"]',
        'a[aria-label="Apply"]',
        'button[aria-label="Apply"]',
    ]

    def _try_playwright_click():
        """Try clicking Apply with each selector; return True on first success."""
        for sel in click_selectors:
            try:
                loc = page.locator(sel).first
                if page.locator(sel).count() > 0 and loc.is_visible(timeout=500):
                    loc.click(timeout=5000)
                    log(f"  Workday Apply: clicked via {sel}")
                    return True
            except Exception:
                continue
        # Last resort: find by text
        for text_sel in ['a:has-text("Apply")', 'button:has-text("Apply")']:
            try:
                loc = page.locator(text_sel).first
                if page.locator(text_sel).count() > 0 and loc.is_visible(timeout=500):
                    loc.click(timeout=5000)
                    log(f"  Workday Apply: clicked via text match")
                    return True
            except Exception:
                continue
        return False

    # Method 1: Playwright click with new-tab detection
    try:
        with context.expect_page(timeout=10000) as new_page_info:
            if not _try_playwright_click():
                log("  Workday: no Apply selector matched (no new-tab attempt)")
                raise Exception("no selector matched")
        new_page = new_page_info.value
        # Wait for URL to resolve (not about:blank)
        for _ in range(20):
            if new_page.url not in ("about:blank", ""):
                break
            time.sleep(0.5)
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
            pass
        log(f"  Workday: Apply opened new tab → {new_page.url[:80]}")
        Stealth().apply_stealth_sync(new_page)
        _workday_apply_clicked.add(current_url)
        try:
            page.close()
        except Exception:
            pass
        return new_page
    except Exception:
        pass

    # Check if Apply opened an in-page form BEFORE method-2 re-click
    # Uses DOM-based detection (not text) to avoid false positives from job description
    def _in_page_form_open():
        try:
            reason = page.evaluate("""() => {
                const bodyText = document.body.innerText.toLowerCase();
                if (document.querySelector('[role="dialog"][aria-modal="true"]'))
                    return 'dialog-modal';
                if (document.querySelector('[data-automation-id="dialog"]'))
                    return 'wd-dialog';
                if (document.querySelector('[data-automation-id="formContainer"]'))
                    return 'wd-formContainer';
                if (document.querySelector('[data-automation-id="signInSubmitButton"]'))
                    return 'wd-signIn-btn';
                if (document.querySelector('[data-automation-id="createAccountSubmitButton"]'))
                    return 'wd-createAccount-btn';
                // Workday autofill prompt text
                if (bodyText.includes('use my last application') || bodyText.includes('autofill with resume'))
                    return 'autofill-text';
                // Visible password/email inputs (auth form in overlay)
                const inputs = document.querySelectorAll('input[type="password"], input[type="email"]');
                const hasAuthInputs = [...inputs].some(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                if (hasAuthInputs) return 'auth-inputs';
                return null;
            }""")
            if reason:
                log(f"  Workday: in-page form detected: {reason}")
                return True
            return False
        except Exception:
            return False

    if _in_page_form_open():
        log("  Workday: Apply opened in-page form (detected after method-1)")
        _workday_dismiss_autofill_now(page)
        _workday_apply_clicked.add(current_url)
        return page

    # Method 2: Same-tab navigation (some Workday tenants navigate in-page)
    try:
        _try_playwright_click()
        time.sleep(3)
        if page.url != current_url:
            log(f"  Workday: Apply navigated same tab → {page.url[:80]}")
            _workday_apply_clicked.add(current_url)
            return page
        # Check if a new tab appeared anyway (sometimes context.expect_page misses)
        new_tabs = [p for p in context.pages if p != page and not p.is_closed()]
        if new_tabs:
            nt = new_tabs[-1]
            for _ in range(16):
                if nt.url not in ("about:blank", ""):
                    break
                time.sleep(0.5)
            if nt.url not in ("about:blank", "") and "linkedin.com" not in nt.url:
                log(f"  Workday: Apply opened tab (method 2) → {nt.url[:80]}")
                Stealth().apply_stealth_sync(nt)
                _workday_apply_clicked.add(current_url)
                try:
                    page.close()
                except Exception:
                    pass
                return nt
    except Exception as e:
        log(f"  Workday: Apply click error: {e}")

    # Method 3: Check if Apply opened a dialog/auth form after method-2 click
    if _in_page_form_open():
        log("  Workday: Apply opened in-page form/dialog (method-3 check)")
        _workday_dismiss_autofill_now(page)
        _workday_apply_clicked.add(current_url)
        return page

    log("  Workday: Apply click did not navigate — will retry on next iteration")
    return page


def _workday_click_signin_button(page):
    """Click the Sign In submit button. Returns True if clicked."""
    # First try clicking the click_filter overlay div (Workday's custom button)
    for aria_label in ["Sign In", "sign in"]:
        overlay = page.locator(f'[data-automation-id="click_filter"][aria-label="{aria_label}"]')
        if overlay.count() > 0:
            try:
                overlay.first.evaluate("el => el.click()")
                time.sleep(4)
                log(f"  Workday auth: clicked Sign In via click_filter overlay")
                return True
            except Exception:
                pass
    for btn_sel in [
        '[data-automation-id="signInSubmitButton"]',
        '[data-automation-id="submitButton"]',
    ]:
        btn = page.locator(btn_sel)
        if btn.count() > 0:
            try:
                btn.first.evaluate("el => el.click()")
                time.sleep(4)
                log(f"  Workday auth: JS-clicked Sign In via {btn_sel}")
                return True
            except Exception:
                pass
    btn = page.get_by_role("button", name=re.compile(r"^sign.?in$", re.I))
    if btn.count() > 0:
        try:
            btn.first.evaluate("el => el.click()")
            time.sleep(4)
            log("  Workday auth: JS-clicked Sign In button (role match)")
            return True
        except Exception:
            pass
    return False


def _handle_workday_auth_page(page):
    """Handle Workday Create Account / Sign In pages without going through Claude.
    Called at the top of each loop iteration when URL contains myworkdayjobs.com.

    Flow:
      - Create Account page (first time) → click Sign In link (try existing account)
      - Sign In page → fill credentials and submit
      - Sign In fails (wrong password / no account) → go to Create Account, fill all fields
      - Create Account filled → submit → triggers email verification
      - Email verification → open Gmail → click link → continue

    Returns True if the page was handled (caller should `continue`), False otherwise.
    """
    try:
        page_text = page.evaluate("() => document.body.innerText")
    except Exception:
        return False
    page_lower = page_text.lower()

    # Only act when we're clearly on an auth page
    is_auth = any(x in page_lower for x in [
        "create account", "already have an account",
        "don't have an account", "create your workday account",
    ])

    # Also check for a standalone visible password input (Sign In page marker)
    try:
        has_visible_password = page.evaluate("""() => {
            const pwds = document.querySelectorAll('input[type="password"]');
            return [...pwds].some(el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
        }""")
    except Exception:
        has_visible_password = False

    if not is_auth and not has_visible_password:
        return False

    # Get domain key for state tracking
    domain_match = re.search(r'https?://([^/]+)', page.url)
    domain_key = domain_match.group(1) if domain_match else "workday"
    state = _workday_auth_state.setdefault(domain_key, {"signin_attempts": 0, "created": False})

    # Determine page type: Create Account has "verify new password" / "confirm password"
    is_create_account_page = any(x in page_lower for x in [
        "verify new password", "confirm password", "confirm new password",
        "retype password", "create your workday account",
    ])

    # ----------------------------------------------------------------
    # CREATE ACCOUNT PAGE
    # ----------------------------------------------------------------
    if is_create_account_page:
        # If Sign In has already failed (no account / wrong pw), fill Create Account form
        if state["signin_attempts"] >= 2 and not state["created"]:
            log("  Workday auth: Sign In failed — filling Create Account form")
            return _workday_fill_create_account(page, domain_key, state)

        # First visit to Create Account: try Sign In link (account may already exist)
        log("  Workday auth: Create Account page — trying Sign In first")
        for sel in [
            'a:has-text("Sign In")',
            '[data-automation-id="signIn"]',
            'a[href*="signIn"]',
            'a[href*="signin"]',
        ]:
            try:
                els = page.locator(sel)
                for i in range(min(els.count(), 5)):
                    el = els.nth(i)
                    try:
                        if el.is_visible(timeout=500):
                            el.click()
                            time.sleep(2)
                            log("  Workday auth: clicked Sign In link")
                            return True
                    except Exception:
                        continue
            except Exception:
                continue

        # Couldn't find Sign In link — fill Create Account now
        log("  Workday auth: no Sign In link found — filling Create Account")
        return _workday_fill_create_account(page, domain_key, state)

    # ----------------------------------------------------------------
    # SIGN IN PAGE (has visible password input + sign-in text)
    # ----------------------------------------------------------------
    if not has_visible_password:
        return False

    has_signin_label = "sign in" in page_lower or "log in" in page_lower
    if not has_signin_label:
        return False

    # If we've created an account but sign-in still fails, check Gmail for verification email
    if state["signin_attempts"] >= 2 and state["created"]:
        log(f"  Workday auth: account created but sign-in failing — checking Gmail for verification link")
        # Directly check Gmail for a Workday verification email (don't rely on page text)
        try:
            body_text, body_html, gmail_tab = _open_gmail_and_find_email(page, wait_seconds=20)
            # IMAP fallback if browser Gmail is not logged in
            if not body_html and GMAIL_APP_PASSWORD:
                log("  Workday auth: browser Gmail unavailable — trying IMAP...")
                body_text, body_html = _fetch_gmail_body_imap(wait_seconds=5)
            if body_html:
                link_patterns = [
                    r'href=["\']?(https?://[^"\'>\s]+(?:verif|confirm|activat|reset|token)[^"\'>\s]*)',
                    r'href=["\']?(https?://[^"\'>\s]+workday[^"\'>\s]*)',
                ]
                verify_link = None
                for pat in link_patterns:
                    m = re.search(pat, body_html, re.I)
                    if m:
                        verify_link = m.group(1).rstrip('.')
                        break
                if verify_link:
                    log(f"  Workday auth: clicking verification link from Gmail")
                    verify_tab = page.context.new_page()
                    try:
                        verify_tab.goto(verify_link, wait_until="domcontentloaded", timeout=20000)
                        time.sleep(3)
                        log(f"  Workday auth: verification link opened → {verify_tab.url[:80]}")
                        verify_tab.close()
                        if gmail_tab:
                            try: gmail_tab.close()
                            except: pass
                        state["signin_attempts"] = 0  # Reset so next iteration tries sign-in fresh
                        # Reload so we get the Sign In page instead of "check your email"
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=15000)
                            time.sleep(2)
                        except Exception:
                            pass
                        return True
                    except Exception as e:
                        log(f"  Workday auth: verification link error: {e}")
                        try: verify_tab.close()
                        except: pass
                else:
                    log("  Workday auth: no verification link found in Gmail")
                if gmail_tab:
                    try: gmail_tab.close()
                    except: pass
            else:
                log("  Workday auth: Gmail returned no email body")
        except Exception as e:
            log(f"  Workday auth: Gmail check error: {e}")
        # If verification failed, mark for manual intervention but don't loop infinitely
        if state["signin_attempts"] >= 8:
            log("  Workday auth: too many sign-in attempts with no verification — giving up")
            return False
        return True  # Let next iteration try sign-in again after verification

    # If we've already tried signing in 2+ times without account creation, navigate to Create Account
    if state["signin_attempts"] >= 2 and not state["created"]:
        log(f"  Workday auth: Sign In failed {state['signin_attempts']} times — switching to Create Account")
        # Try to find and click "Create Account" / "Don't have an account" link
        navigated = False
        for sel in [
            'a:has-text("Create Account")',
            'a:has-text("Don\'t have an account")',
            '[data-automation-id="createAccount"]',
            'a[href*="createAccount"]',
            'a[href*="register"]',
            'button:has-text("Create Account")',
        ]:
            try:
                els = page.locator(sel)
                for i in range(min(els.count(), 3)):
                    el = els.nth(i)
                    if el.is_visible(timeout=500):
                        el.evaluate("el => el.click()")
                        time.sleep(2)
                        log(f"  Workday auth: clicked Create Account link via {sel}")
                        navigated = True
                        break
                if navigated:
                    break
            except Exception:
                continue
        # If can't find link, try JS to trigger Create Account page via URL manipulation
        if not navigated:
            log("  Workday auth: no Create Account link — filling form directly")
            return _workday_fill_create_account(page, domain_key, state)
        return True  # Let next iteration handle the Create Account page

    # Get password to try — ALWAYS use OpenClaw default for nb.wd1 (not Moelis cache)
    password = _workday_passwords.get(domain_key, "")
    # If we haven't set a password for this domain yet, use the default (not the cache)
    if not password:
        password = WORKDAY_DEFAULT_PASSWORD
    # Only fall back to cache if default is somehow empty
    if not password:
        cache = _load_answer_cache()
        password = (
            cache.get("password", {}).get("value", "")
            or cache.get("new password", {}).get("value", "")
        )

    log(f"  Workday auth: Sign In page — attempt {state['signin_attempts']+1}, pw='{password[:8]}...'")

    try:
        # Fill email
        for email_sel in [
            '[data-automation-id="email"]',
            'input[type="email"]',
            'input[name="username"]',
            'input[name="email"]',
        ]:
            el = page.locator(email_sel)
            if el.count() > 0 and el.first.is_visible(timeout=1000):
                el.first.fill(PROFILE["email"])
                break
        time.sleep(0.3)

        # Fill password
        for pwd_sel in [
            '[data-automation-id="password"]',
            'input[type="password"]',
            'input[name="password"]',
        ]:
            el = page.locator(pwd_sel)
            if el.count() > 0 and el.first.is_visible(timeout=1000):
                el.first.fill(password)
                break
        time.sleep(0.3)

        clicked = _workday_click_signin_button(page)
        if not clicked:
            log("  Workday auth: couldn't find Sign In button")
            return False

        state["signin_attempts"] += 1

        # Wait a bit then check outcome
        time.sleep(3)
        new_text = ""
        try:
            new_text = page.evaluate("() => document.body.innerText.toLowerCase()")
        except Exception:
            pass

        signin_error = any(x in new_text for x in [
            "incorrect", "invalid credentials", "not found", "wrong password",
            "does not match", "no account", "we couldn't find",
            "account does not exist", "your password is incorrect",
            "password you entered", "invalid username", "couldn't find",
        ])

        # Also check if still on sign-in page after submit (URL unchanged = failure)
        still_signin = has_visible_password and ("sign in" in new_text or "log in" in new_text)
        if still_signin and state["signin_attempts"] >= 1:
            # Password might be wrong — mark as error
            log(f"  Workday auth: still on Sign In page after submit — password likely wrong")
            signin_error = True

        if signin_error:
            log(f"  Workday auth: Sign In failed (attempt {state['signin_attempts']})")
            # On second failure immediately switch to Create Account on next iteration
            if state["signin_attempts"] >= 2:
                log("  Workday auth: will create new account on next iteration")

        # Check if email verification needed after successful sign-in
        if any(x in new_text for x in ["verify your email", "check your email", "verification"]):
            log("  Workday auth: email verification needed after sign-in")
            if handle_email_verification(page):
                log("  Workday auth: email verified successfully")

        return True

    except Exception as e:
        log(f"  Workday auth: error during sign-in: {e}")
        return False


def _workday_fill_create_account(page, domain_key, state):
    """Fill and submit the Workday Create Account form."""
    password = WORKDAY_DEFAULT_PASSWORD
    _workday_passwords[domain_key] = password  # Remember for Sign In after verification

    try:
        # Email
        for sel in ['[data-automation-id="email"]', 'input[type="email"]', 'input[name="email"]']:
            el = page.locator(sel)
            if el.count() > 0 and el.first.is_visible(timeout=1000):
                el.first.fill(PROFILE["email"])
                break
        time.sleep(0.3)

        # Password
        for sel in ['[data-automation-id="password"]', 'input[type="password"][name*="password"]',
                    'input[type="password"]']:
            els = page.locator(sel)
            # Fill first visible password field (new password), then second (verify)
            visible = [els.nth(i) for i in range(els.count()) if els.nth(i).is_visible(timeout=300)]
            if visible:
                visible[0].fill(password)
                if len(visible) > 1:
                    visible[1].fill(password)  # Verify New Password
                break
        time.sleep(0.3)

        # Alternatively fill by label selectors
        for verify_sel in [
            '[data-automation-id="verifyPassword"]',
            'input[name*="verify"]',
            'input[name*="confirm"]',
        ]:
            el = page.locator(verify_sel)
            if el.count() > 0 and el.first.is_visible(timeout=500):
                el.first.fill(password)
                break
        time.sleep(0.3)

        # Tick "I acknowledge" / privacy notice checkbox (required on some Workday tenants)
        for ack_sel in [
            '[data-automation-id="createAccountAgreement"]',
            'input[type="checkbox"][id*="acknowledge"]',
            'input[type="checkbox"][id*="agree"]',
            'input[type="checkbox"][id*="privacy"]',
            'input[type="checkbox"][id*="terms"]',
        ]:
            el = page.locator(ack_sel)
            if el.count() > 0 and el.first.is_visible(timeout=500):
                if not el.first.is_checked():
                    el.first.check()
                    log("  Workday auth: ticked 'I acknowledge' checkbox")
                break
        # Fallback: find any unchecked checkbox near text "acknowledge" or "agree"
        try:
            ack_boxes = page.locator('input[type="checkbox"]')
            for i in range(ack_boxes.count()):
                box = ack_boxes.nth(i)
                if box.is_visible(timeout=300) and not box.is_checked():
                    label_text = ""
                    try:
                        label_text = box.evaluate("""el => {
                            const lbl = el.closest('label') || document.querySelector('label[for="'+el.id+'"]');
                            return lbl ? lbl.innerText.toLowerCase() : '';
                        }""")
                    except Exception:
                        pass
                    if any(w in label_text for w in ["acknowledge", "agree", "privacy", "terms"]):
                        box.check()
                        log("  Workday auth: ticked acknowledgement checkbox via fallback")
                        break
        except Exception:
            pass
        time.sleep(0.3)

        # Click Create Account button — use JS click to bypass click_filter overlay
        clicked_create = False
        # First try the click_filter overlay div (Workday's custom button layer)
        for aria_label in ["Create Account", "create account"]:
            overlay = page.locator(f'[data-automation-id="click_filter"][aria-label="{aria_label}"]')
            if overlay.count() > 0:
                try:
                    overlay.first.evaluate("el => el.click()")
                    state["created"] = True
                    clicked_create = True
                    log("  Workday auth: JS-clicked Create Account via click_filter overlay")
                    time.sleep(4)
                    break
                except Exception:
                    pass
        if not clicked_create:
            for btn_sel in [
                '[data-automation-id="createAccountSubmitButton"]',
                '[data-automation-id="createAccount"]',
                'button:has-text("Create Account")',
            ]:
                btn = page.locator(btn_sel)
                if btn.count() > 0:
                    try:
                        btn.first.evaluate("el => el.click()")
                        state["created"] = True
                        clicked_create = True
                        log(f"  Workday auth: JS-clicked Create Account via {btn_sel}")
                        time.sleep(4)
                        break
                    except Exception:
                        pass
        if not clicked_create:
            btn = page.get_by_role("button", name=re.compile(r"create.?account", re.I))
            if btn.count() > 0:
                try:
                    btn.first.evaluate("el => el.click()")
                    state["created"] = True
                    log("  Workday auth: JS-clicked Create Account (role match)")
                    time.sleep(4)
                except Exception:
                    pass

        # Check for email verification message
        try:
            page_after = page.evaluate("() => document.body.innerText.toLowerCase()")
            if any(x in page_after for x in ["check your email", "verify your email", "sent you an email"]):
                log("  Workday auth: account created — email verification needed")
                if handle_email_verification(page):
                    log("  Workday auth: email verified — reloading to Sign In page")
                    state["signin_attempts"] = 0  # Reset so Sign In will work
                    # After verification the Workday page needs a reload / navigation
                    # to show the Sign In form (original page still shows "check your email")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=15000)
                        time.sleep(2)
                    except Exception:
                        pass
        except Exception:
            pass

        return True

    except Exception as e:
        log(f"  Workday auth: error during Create Account: {e}")
        return False


# =============================================================================
#  EMAIL VERIFICATION CODE READER (Oracle HCM, SuccessFactors, etc.)
# =============================================================================

GMAIL_USER = PROFILE["email"]
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


def _open_gmail_and_find_email(page, wait_seconds=20):
    """Open Gmail in a new tab using the existing logged-in browser session.
    Searches for recent verification/confirm emails.
    Returns (body_text, body_html, gmail_tab) or (None, None, None)."""
    context = page.context
    gmail_tab = None
    try:
        time.sleep(min(wait_seconds, 15))
        gmail_tab = context.new_page()
        gmail_tab.goto("https://mail.google.com/mail/u/0/#inbox",
                       wait_until="domcontentloaded", timeout=20000)
        time.sleep(3)

        if "accounts.google" in gmail_tab.url or "signin" in gmail_tab.url:
            log("  email-verify: Gmail not logged in — attempting auto sign-in...")
            try:
                # Fill email
                email_el = gmail_tab.locator('input[type="email"]')
                if email_el.count() > 0 and email_el.first.is_visible(timeout=3000):
                    email_el.first.fill(PROFILE["email"])
                    gmail_tab.keyboard.press("Enter")
                    time.sleep(2)
                # Fill password
                pwd_el = gmail_tab.locator('input[type="password"]')
                if pwd_el.count() > 0 and pwd_el.first.is_visible(timeout=5000):
                    pwd_el.first.fill("11Musya11!")
                    gmail_tab.keyboard.press("Enter")
                    time.sleep(4)
                # Check if now logged in
                if "accounts.google" in gmail_tab.url or "signin" in gmail_tab.url:
                    log("  email-verify: Gmail auto sign-in failed — skipping browser method")
                    gmail_tab.close()
                    return None, None, None
                log("  email-verify: Gmail auto sign-in succeeded")
            except Exception as e:
                log(f"  email-verify: Gmail sign-in error: {e}")
                gmail_tab.close()
                return None, None, None

        search_terms = ["verify", "verification", "confirm your", "activate", "code"]
        for term in search_terms:
            try:
                sb = gmail_tab.locator('input[aria-label="Search mail"]')
                if not sb.is_visible(timeout=3000):
                    break
                sb.fill(term)
                gmail_tab.keyboard.press("Enter")
                time.sleep(2)

                # Try unread first, then any
                for row_sel in ['tr.zA.zE', 'tr.zA']:
                    first = gmail_tab.locator(row_sel).first
                    if first.count() > 0 and first.is_visible(timeout=1500):
                        first.click()
                        time.sleep(2)
                        body_el = gmail_tab.locator('div.a3s')
                        if body_el.count() > 0:
                            body_text = body_el.first.inner_text()
                            body_html = body_el.first.inner_html()
                            log(f"  email-verify: found email matching '{term}'")
                            return body_text, body_html, gmail_tab
                        break
            except Exception:
                continue

        gmail_tab.close()
        return None, None, None
    except Exception as e:
        log(f"  email-verify browser error: {e}")
        if gmail_tab:
            try:
                gmail_tab.close()
            except Exception:
                pass
        return None, None, None


def _fetch_gmail_body_imap(wait_seconds=20):
    """Fetch the most recent verification/confirm email body via IMAP.
    Returns (body_text, body_html) or (None, None) if unavailable."""
    if not GMAIL_APP_PASSWORD:
        return None, None
    try:
        time.sleep(min(wait_seconds, 15))
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")
        msg_id = None
        for search_q in [
            '(UNSEEN FROM "eno.fa.sender")',
            '(UNSEEN FROM "oracle")',
            '(UNSEEN SUBJECT "verif")',
            '(UNSEEN SUBJECT "confirm")',
            '(UNSEEN SUBJECT "activat")',
            '(UNSEEN SUBJECT "workday")',
        ]:
            _, data = mail.search(None, search_q)
            if data[0]:
                ids = data[0].split()
                if ids:
                    msg_id = ids[-1]
                    break
        if not msg_id:
            # also try read emails from last 5 minutes
            import datetime
            since = (datetime.datetime.now() - datetime.timedelta(minutes=5)).strftime("%d-%b-%Y")
            for search_q in [
                f'(SINCE "{since}" FROM "eno.fa.sender")',
                f'(SINCE "{since}" FROM "oracle")',
                f'(SINCE "{since}" SUBJECT "verif")',
                f'(SINCE "{since}" SUBJECT "confirm")',
                f'(SINCE "{since}" SUBJECT "activat")',
                f'(SINCE "{since}" SUBJECT "workday")',
            ]:
                _, data = mail.search(None, search_q)
                if data[0]:
                    ids = data[0].split()
                    if ids:
                        msg_id = ids[-1]
                        break
        if not msg_id:
            mail.logout()
            return None, None
        _, msg_data = mail.fetch(msg_id, "(RFC822)")
        msg = email_lib.message_from_bytes(msg_data[0][1])
        mail.logout()
        body_text, body_html = "", ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                payload = part.get_payload(decode=True)
                if payload:
                    decoded = payload.decode(errors="ignore")
                    if ct == "text/plain" and not body_text:
                        body_text = decoded
                    elif ct == "text/html" and not body_html:
                        body_html = decoded
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body_text = payload.decode(errors="ignore")
        return body_text or None, body_html or None
    except Exception as e:
        log(f"  email-verify IMAP body error: {e}")
        return None, None


def fetch_verification_code(wait_seconds=45, max_age_seconds=120):
    """Check Gmail IMAP for a recent verification code email.
    Returns the code string or None if not found."""
    if not GMAIL_APP_PASSWORD:
        return None

    time.sleep(min(wait_seconds, 15))

    for attempt in range(3):
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            mail.select("inbox")

            data = (None, [b""])
            # Oracle HCM sends from eno.fa.sender@workflow.mail.us2.cloud.oracle.com
            for search_query in [
                '(UNSEEN FROM "eno.fa.sender")',
                '(UNSEEN FROM "oracle")',
                '(UNSEEN SUBJECT "verification")',
                '(UNSEEN SUBJECT "code")',
                '(UNSEEN SUBJECT "verify")',
                '(UNSEEN SUBJECT "OTP")',
                '(UNSEEN SUBJECT "confirm")',
            ]:
                _, data = mail.search(None, search_query)
                if data[0]:
                    break

            ids = data[0].split() if data[0] else []
            if not ids:
                mail.logout()
                if attempt < 2:
                    time.sleep(10)
                    continue
                return None

            _, msg_data = mail.fetch(ids[-1], "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html"):
                        body = part.get_payload(decode=True).decode(errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")

            mail.logout()

            m = re.search(r'(?:verification|security|confirm)\s*(?:code|number|pin)\s*(?:is|:)?\s*(\d{4,8})', body, re.I)
            if m:
                return m.group(1)
            m = re.search(r'\b(\d{6})\b', body)
            if m:
                return m.group(1)
            m = re.search(r'\b(\d{4})\b', body)
            if m:
                return m.group(1)
            return None

        except Exception as e:
            log(f"  email-verify IMAP error: {e}")
            if attempt < 2:
                time.sleep(5)

    return None


def handle_email_verification(page):
    """Detect and handle email verification pages.
    Tries browser-based Gmail first (no App Password needed), falls back to IMAP.
    Handles both 'enter code' and 'click link' verification types.
    Returns True if verification was completed, False otherwise."""
    try:
        vis_text = page.evaluate("() => document.body.innerText.toLowerCase().slice(0, 3000)")
    except Exception:
        return False

    is_verify = any(phrase in vis_text for phrase in [
        "verification code", "verify your email", "enter the code",
        "we sent a code", "one-time", "check your email", "sent you an email",
        "confirm your email", "activate your account", "click the link",
    ])

    # Oracle HCM OTP page: 6 individual single-digit inputs indicate OTP even without text phrases
    if not is_verify and "oraclecloud.com" in page.url:
        try:
            if page.locator('input[maxlength="1"]').count() >= 6:
                is_verify = True
        except Exception:
            pass

    if not is_verify:
        return False

    log("  Detected email verification — checking Gmail via browser...")

    # --- Method 1: Browser-based Gmail ---
    body_text, body_html, gmail_tab = _open_gmail_and_find_email(page, wait_seconds=20)
    if body_text and body_html:
        try:
            # Check for verification link first (Workday, Oracle account activation)
            link_patterns = [
                r'href=["\']?(https?://[^"\'>\s]+(?:verif|confirm|activat|reset|token)[^"\'>\s]*)',
                r'href=["\']?(https?://[^"\'>\s]+workday[^"\'>\s]*)',
                r'href=["\']?(https?://[^"\'>\s]+oracle[^"\'>\s]*)',
            ]
            verify_link = None
            for pat in link_patterns:
                m = re.search(pat, body_html, re.I)
                if m:
                    verify_link = m.group(1).rstrip('.')
                    break

            if verify_link:
                log(f"  email-verify: clicking verification link")
                verify_tab = page.context.new_page()
                try:
                    verify_tab.goto(verify_link, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(3)
                    log(f"  email-verify: link opened → {verify_tab.url[:80]}")
                    # If it redirected to the ATS, bring that page into focus
                    if gmail_tab:
                        gmail_tab.close()
                    verify_tab.close()
                    return True
                except Exception as e:
                    log(f"  email-verify: link error: {e}")
                    try:
                        verify_tab.close()
                    except Exception:
                        pass

            # Extract numeric code from email body
            code = None
            m = re.search(r'(?:verification|security|confirm|your)\s*(?:code|number|pin)\s*(?:is|:)?\s*(\d{4,8})', body_text, re.I)
            if m:
                code = m.group(1)
            if not code:
                m = re.search(r'\b(\d{6})\b', body_text)
                if m:
                    code = m.group(1)
            if not code:
                m = re.search(r'\b(\d{4})\b', body_text)
                if m:
                    code = m.group(1)

            if gmail_tab:
                try:
                    gmail_tab.close()
                except Exception:
                    pass

            if code:
                log(f"  email-verify: found code {code} via browser Gmail")
            else:
                log("  email-verify: email found but no code/link extracted")

        except Exception as e:
            log(f"  email-verify extraction error: {e}")
            code = None
            if gmail_tab:
                try:
                    gmail_tab.close()
                except Exception:
                    pass
    else:
        # --- Method 2: IMAP fallback ---
        log("  email-verify: browser method failed, trying IMAP...")
        code = fetch_verification_code()

    if not code:
        log("  email-verify: could not retrieve code or link")
        return False

    # Oracle HCM: 6 individual single-digit circular inputs
    try:
        otp_inputs_loc = page.locator('input[maxlength="1"]')
        otp_count = otp_inputs_loc.count()
        if otp_count >= 6 and len(code) >= 6:
            log(f"  email-verify: Oracle HCM OTP — filling {otp_count} individual digit inputs with code {code}")
            for i in range(min(otp_count, len(code))):
                inp = otp_inputs_loc.nth(i)
                try:
                    inp.scroll_into_view_if_needed()
                    inp.click()
                    time.sleep(0.05)
                    inp.fill(code[i])
                    time.sleep(0.08)
                except Exception as e:
                    log(f"  email-verify: digit {i} fill error: {e}")
            time.sleep(0.5)
            for label in ["Verify", "Submit", "Confirm", "Continue", "Next"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
                    if btn.count() > 0 and btn.first.is_visible():
                        btn.first.click()
                        log(f"  email-verify: Oracle OTP entered and clicked '{label}'")
                        time.sleep(3)
                        return True
                except Exception:
                    continue
            page.keyboard.press("Enter")
            time.sleep(3)
            log("  email-verify: Oracle OTP entered and pressed Enter")
            return True
    except Exception as e:
        log(f"  email-verify: Oracle OTP handling error: {e}")

    # Generic single-field path
    code_input = None
    for sel in [
        'input[autocomplete="one-time-code"]',
        'input[type="text"][name*="code"]',
        'input[type="text"][name*="verify"]',
        'input[type="number"]',
        'input[type="tel"]',
        'input[name*="otp"]',
        'input[name*="pin"]',
        'input[id*="code"]',
        'input[id*="verify"]',
        'input[id*="otp"]',
    ]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                code_input = loc.first
                break
        except Exception:
            continue

    if not code_input:
        try:
            inputs = page.locator('input[type="text"],input:not([type])')
            for i in range(inputs.count()):
                inp = inputs.nth(i)
                if inp.is_visible() and not inp.input_value():
                    code_input = inp
                    break
        except Exception:
            pass

    if not code_input:
        log("  email-verify: couldn't find code input field")
        return False

    _fill_with_events(page, code_input, code)
    time.sleep(1)

    # Click verify/submit button
    for label in ["Verify", "Submit", "Confirm", "Continue", "Next"]:
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                log(f"  email-verify: entered code and clicked '{label}'")
                time.sleep(3)
                return True
        except Exception:
            continue

    # Try pressing Enter as last resort
    page.keyboard.press("Enter")
    time.sleep(3)
    log("  email-verify: entered code and pressed Enter")
    return True


# =============================================================================
#  MAIN -- reads sheet, launches browser, applies to each job
# =============================================================================

def main():
    log(f"\n{'='*50}")
    log(f"Company Runner -- {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log(f"{'='*50}")

    try:
        sheet = get_sheet()
        rows = sheet.get("A:G", value_render_option="FORMULA")
    except Exception as e:
        log(f"Sheet error: {e}")
        tg(f"Company runner sheet error: {e}")
        return

    headers = rows[0] if rows else []
    applied_idx = next(
        (i for i, h in enumerate(headers) if h.lower() in ("applied", "claude")), 5
    )

    pending = []
    for i, row in enumerate(rows[1:], start=2):
        while len(row) < 7:
            row.append("")
        status = row[applied_idx].strip().lower()

        if status == "no" or "applied" in status or status.startswith("manual"):
            continue

        url = extract_url(row[0])
        title = extract_title(row[0])
        company = row[1] if len(row) > 1 else ""
        notes = row[6] if len(row) > 6 else ""

        if "company site" in status:
            apply_url = notes.strip() if notes.strip().startswith("http") else url
            pending.append((i, apply_url, title, company))
        elif not status and "linkedin.com" not in url.lower():
            pending.append((i, url, title, company))

    log(f"Found {len(pending)} company site jobs")
    if not pending:
        return

    ws_profile = load_workspace_profile()
    client = anthropic.AnthropicBedrock(
        aws_access_key=AWS_ACCESS_KEY,
        aws_secret_key=AWS_SECRET_KEY,
        aws_region=AWS_REGION,
    )
    applied = 0

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
                "--no-first-run",
            ],
            viewport={"width": 1280, "height": 900},
        )
        # Close stale tabs
        for old_page in ctx.pages[1:]:
            try:
                old_page.close()
            except Exception:
                pass
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        Stealth().apply_stealth_sync(page)

        # Check LinkedIn login
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if "login" in page.url or "authwall" in page.url:
            log("Not logged in to LinkedIn -- session may have expired")

        for row_idx, url, title, company in pending:
            log(f"\n{'~'*50}")
            log(f"JOB: {title} @ {company}")
            log(f"URL: {url}")
            log(f"{'~'*50}")

            # Skip if already applied (dedup)
            if was_already_applied(company, title):
                log(f"  SKIPPED: already applied to this role")
                continue

            # Close stray tabs
            try:
                for extra in ctx.pages:
                    if extra != page and not extra.is_closed():
                        extra.close()
            except Exception:
                pass

            try:
                ok, reason = apply_company(page, url, title, company, ws_profile, client)
            except Exception as e:
                ok, reason = False, str(e)[:200]
                log(f"  Exception: {e}")

            # Record in history
            record_application(company, title, ok, reason)

            if ok:
                sheet.update_cell(row_idx, COL_STATUS, "Applied")
                tg(f"*Applied!*\n*{title}* @ {company}\n_(company site)_")
                log("  APPLIED")
                applied += 1
            else:
                sheet.update_cell(row_idx, COL_STATUS, "Manual needed")
                tg(f"*Manual needed*\n*{title}* @ {company}\n{reason}\n{url}")
                log(f"  MANUAL NEEDED: {reason}")

            # Reset for next job
            try:
                for extra in ctx.pages[1:]:
                    try:
                        extra.close()
                    except Exception:
                        pass
                page = ctx.pages[0]
                page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
            except Exception:
                pass
            time.sleep(5)

        ctx.close()

    log(f"\nDone. Applied {applied}/{len(pending)}")
    if pending:
        tg(f"Company run complete: {applied}/{len(pending)} applied")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        # Quick test mode: python company_runner.py <url> [title] [company]
        test_url = sys.argv[1]
        test_title = sys.argv[2] if len(sys.argv) > 2 else "Test Job"
        test_company = sys.argv[3] if len(sys.argv) > 3 else "Test Company"
        log(f"\n{'='*50}")
        log(f"SINGLE URL TEST -- {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        log(f"URL: {test_url}")
        log(f"{'='*50}")
        ws_profile = load_workspace_profile()
        client = anthropic.AnthropicBedrock(
            aws_access_key=AWS_ACCESS_KEY,
            aws_secret_key=AWS_SECRET_KEY,
            aws_region=AWS_REGION,
        )
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                    "--no-first-run",
                ],
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            Stealth().apply_stealth_sync(page)
            ok, reason = apply_company(page, test_url, test_title, test_company, ws_profile, client)
            log(f"\nResult: {'APPLIED' if ok else 'FAILED'} -- {reason}")
            ctx.close()
    else:
        main()
