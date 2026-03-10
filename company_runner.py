#!/usr/bin/env python3
"""Company website application bot.
Picks up rows marked 'Company site' from the Google Sheet, navigates to the
company application form, and applies using Claude Opus vision + structured
page-level field extraction.
"""

import json, os, re, time, base64, requests, imaplib, email as email_lib
from datetime import datetime
import gspread
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
COL_STATUS      = 6  # Column F

MAX_PAGES       = 12   # max pages per application (most forms are 3-6 pages)
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
    "education": "BSc Political Economy, King's College London (2020-2023)",
    "visa_sponsorship": "Yes",
    "notice_period": "2 weeks",
    "salary_expectation": "52000",
    "gender": "Female",
    "ethnicity": "Prefer not to say / I prefer not to answer",
    "disability": "No",
    "veteran": "No",
    "criminal_record": "No",
    "former_employee": "No",
    "over_18": "Yes",
    "authorized_to_work": "Yes",
    "languages": "English (Fluent), Russian (Fluent), Kazakh (Fluent), French (Intermediate), Spanish (Beginner)",
    "linkedin": "https://www.linkedin.com/in/alramina-myrzabekova",
    "hear_about_us": "Job Posting",
}

# -- Logging ------------------------------------------------------------------
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
- For open text / cover letter / "why this role" fields: write 2-3 professional sentences referencing the applicant's background.
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


def build_system_prompt(role, company, ws_profile):
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
        extra += f"\n\nCV SUMMARY:\n{cv_text[:3000]}"
    if answers_text:
        extra += f"\n\nPRE-WRITTEN ANSWERS:\n{answers_text[:2000]}"

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
        ]
        return any(m in visible_text for m in markers)
    except Exception:
        return False


def click_next_button(page):
    """Find and click the next/continue/submit button. Returns button text or None."""
    # Workday: data-automation-id buttons first (most reliable)
    for wd_id in [
        "bottom-navigation-next-button",
        "bottom-navigation-done-button",
        "bottom-navigation-footer-button",
        "autofillWithResume",
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
    for label in [
        "Next", "Continue", "Save and Continue", "Save & Continue",
        "Submit Application", "Submit", "Review and Submit",
        "Review & Submit", "Apply", "Apply Now", "Send Application",
        "Confirm", "Done", "Finish", "Complete",
        "Create Account", "Sign In", "Register",
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
            const targets = ['next','continue','submit','apply','save and continue','done','finish','review'];
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
            try:
                new_page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
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
        if "linkedin.com" not in url:
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
            if "linkedin.com" not in url and url not in ("about:blank", ""):
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
                    if "race" in q or "ethnic" in q:
                        fallback_options = ["Two or More", "Other", "Prefer"]
                    elif "sex" in q:
                        fallback_options = ["Female", "Prefer"]
                    elif "gender" in q:
                        fallback_options = ["Woman", "Female", "Prefer"]
                    elif "veteran" in q or "military" in q:
                        fallback_options = ["not a", "prefer not", "no"]
                    elif "disability" in q:
                        fallback_options = ["don't wish", "prefer not", "no"]

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
    if "myworkdayjobs.com" not in page.url and "wd3." not in page.url and "wd5." not in page.url:
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
                const val = btn.innerText.trim();
                if (val && val.length > 2 && val !== 'Select' && !val.includes('required')) return;
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
                const val = btn.innerText.trim();
                if (val && val.length > 2 && val !== 'Select') return;
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
                preferred = ["Job Posting", "LinkedIn", "Website", "Job Board", "Internet", "Online"]
            elif "country" in label or "country" in aid:
                preferred = ["United Kingdom", "UK", "Great Britain"]
            elif "phone" in label and ("type" in label or "device" in label):
                preferred = ["Mobile", "Cell"]
            elif "phone" in label and "code" in label:
                preferred = ["United Kingdom (+44)", "United Kingdom", "+44"]
            else:
                # Unknown dropdown - pick first option
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

    system = build_system_prompt(role, company, ws_profile)
    context = page.context
    stuck_count = 0
    prev_url = ""

    for page_num in range(MAX_PAGES):
        current_url = page.url
        log(f"\n  [Page {page_num + 1}] {current_url}")

        # Check for success
        if is_success_page(page):
            return True, "submitted"

        # Quick CAPTCHA/verification check each page
        try:
            vis_text = page.evaluate("() => document.body.innerText.toLowerCase().slice(0, 2000)")
            if "captcha" in vis_text or "recaptcha" in vis_text or "verify you are human" in vis_text:
                return False, "CAPTCHA on form page"
            if "verification code" in vis_text and "email" in vis_text and "enter" in vis_text:
                if GMAIL_APP_PASSWORD and handle_email_verification(page):
                    log("  Email verification completed, continuing...")
                    continue  # Re-enter page loop
                return False, "email verification required"
        except Exception:
            pass

        # Handle new tabs from previous navigation
        page = handle_new_tabs(page, context)

        # Dismiss discard/leave dialogs
        _dismiss_leave_dialogs(page)

        time.sleep(1)

        # Pre-fill: auto-click Yes/No toggles and consent checkboxes
        _auto_answer_yesno_toggles(page)
        _auto_check_consent(page)
        _auto_fill_oracle_jet_dropdowns(page)
        _auto_fill_workday_dropdowns(page)

        # Extract fields
        fields = extract_page_fields(page)
        log(f"  Found {len(fields)} fields")

        # Ask Claude to analyze page and return all actions
        actions = analyze_page(client, page, fields, system, role, company)
        log(f"  Claude returned {len(actions)} actions")

        # Check for terminal actions
        if any(a.get("action") == "DONE" for a in actions if isinstance(a, dict)):
            return True, "submitted"
        skip = next((a for a in actions if isinstance(a, dict) and a.get("action") == "SKIP"), None)
        if skip:
            return False, skip.get("reason", "skipped by Claude")

        # Execute all actions
        if actions:
            result = execute_actions(page, actions)
            if result == "DONE":
                return True, "submitted"
            if isinstance(result, str) and result.startswith("SKIP:"):
                return False, result[5:].strip()
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
            error_ctx = f"Validation errors: {errors}\nEmpty required fields: {empty}"
            fields_retry = extract_page_fields(page)
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
            if stuck_count >= MAX_STUCK:
                # Try scrolling up and looking for button we missed
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(1)
                clicked = click_next_button(page)
                if not clicked:
                    return False, f"stuck on same page for {MAX_STUCK} rounds"
                time.sleep(4)
                stuck_count = 0
        else:
            stuck_count = 0
        prev_url = current_url

    return False, "max pages reached"


def _dismiss_leave_dialogs(page):
    """Dismiss 'Discard Application?' / 'Leave page?' dialogs."""
    try:
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


# =============================================================================
#  EMAIL VERIFICATION CODE READER (Oracle HCM, SuccessFactors, etc.)
# =============================================================================

GMAIL_USER = PROFILE["email"]
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

def fetch_verification_code(wait_seconds=45, max_age_seconds=120):
    """Check Gmail IMAP for a recent verification code email.
    Returns the code string or None if not found.
    Supports Oracle HCM, SuccessFactors, Workday, and generic OTP patterns."""
    if not GMAIL_APP_PASSWORD:
        log("  email-verify: GMAIL_APP_PASSWORD not set, skipping")
        return None

    # Wait a bit for the email to arrive
    time.sleep(min(wait_seconds, 15))

    for attempt in range(3):
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            mail.select("inbox")

            # Search for recent emails with verification-related subjects
            _, data = mail.search(None, '(UNSEEN SUBJECT "verification")')
            if not data[0]:
                _, data = mail.search(None, '(UNSEEN SUBJECT "code")')
            if not data[0]:
                _, data = mail.search(None, '(UNSEEN SUBJECT "verify")')
            if not data[0]:
                _, data = mail.search(None, '(UNSEEN SUBJECT "OTP")')

            ids = data[0].split()
            if not ids:
                mail.logout()
                if attempt < 2:
                    log(f"  email-verify: no verification email yet, waiting... (attempt {attempt+1})")
                    time.sleep(10)
                    continue
                return None

            # Get the most recent email
            _, msg_data = mail.fetch(ids[-1], "(RFC822)")
            raw_email = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw_email)

            # Check age
            date_str = msg.get("Date", "")
            try:
                from email.utils import parsedate_to_datetime
                msg_date = parsedate_to_datetime(date_str)
                age = (datetime.now(msg_date.tzinfo) - msg_date).total_seconds()
                if age > max_age_seconds:
                    log(f"  email-verify: email too old ({int(age)}s)")
                    mail.logout()
                    return None
            except Exception:
                pass  # Can't parse date, try anyway

            # Extract body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="ignore")
                        break
                    elif part.get_content_type() == "text/html":
                        body = part.get_payload(decode=True).decode(errors="ignore")
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")

            # Extract verification code — try common patterns
            code = None
            # Pattern 1: "verification code is 123456" or "code: 123456"
            m = re.search(r'(?:verification|security|confirm)\s*(?:code|number|pin)\s*(?:is|:)?\s*(\d{4,8})', body, re.I)
            if m:
                code = m.group(1)
            # Pattern 2: Standalone 6-digit code (most common)
            if not code:
                m = re.search(r'\b(\d{6})\b', body)
                if m:
                    code = m.group(1)
            # Pattern 3: 4-digit OTP
            if not code:
                m = re.search(r'\b(\d{4})\b', body)
                if m:
                    code = m.group(1)

            mail.logout()

            if code:
                log(f"  email-verify: found code {code}")
                return code
            else:
                log(f"  email-verify: email found but no code extracted")
                return None

        except Exception as e:
            log(f"  email-verify error: {e}")
            if attempt < 2:
                time.sleep(5)

    return None


def handle_email_verification(page):
    """Detect and handle email verification pages.
    Returns True if verification was completed, False if couldn't handle."""
    try:
        vis_text = page.evaluate("() => document.body.innerText.toLowerCase().slice(0, 3000)")
    except Exception:
        return False

    # Check if this is a verification page
    is_verify = (
        ("verification code" in vis_text or "verify your email" in vis_text or
         "enter the code" in vis_text or "we sent a code" in vis_text or
         "one-time" in vis_text)
        and ("email" in vis_text or "code" in vis_text)
    )

    if not is_verify:
        return False

    log("  Detected email verification page — attempting to read code from Gmail...")
    code = fetch_verification_code()
    if not code:
        return False

    # Find the code input field and fill it
    code_input = None
    for sel in [
        'input[type="text"][name*="code"]',
        'input[type="text"][name*="verify"]',
        'input[type="tel"]',
        'input[type="number"]',
        'input[autocomplete="one-time-code"]',
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

    # Fallback: find any empty visible text input
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

    # Fill and submit
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

        if status == "no" or status.startswith("applied") or status.startswith("manual"):
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
