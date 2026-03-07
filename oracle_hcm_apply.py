#!/usr/bin/env python3
"""Oracle HCM application handler — Oracle JET-aware field filling."""
import time, re, sys
from playwright.sync_api import sync_playwright

EMAIL    = "alramina.myrzabekova@gmail.com"
PHONE    = "7760289275"
NAME_F   = "Alramina"
NAME_L   = "Myrzabekova"
ADDR1    = "27 Albert Embankment"
CITY     = "London"
POSTCODE = "SE1 7AQ"
COUNTRY  = "United Kingdom"
CV_PATH  = "/Users/aly4x/openclaw/apply/workspace/CV.pdf"
APPLY_URL = "https://efds.fa.em5.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/58481/apply/email"

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def oracle_fill(page, el, value):
    """Fill an Oracle JET input and trigger its validation events."""
    try:
        el.scroll_into_view_if_needed()
        el.click()
        time.sleep(0.1)
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        el.type(value, delay=25)
        time.sleep(0.1)
        # Fire Oracle JET + standard events
        el.evaluate("""el => {
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            el.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        }""")
        page.keyboard.press("Tab")
        time.sleep(0.2)
        return True
    except Exception as e:
        log(f"    oracle_fill error: {e}")
        return False

def accept_privacy(page):
    for label_text in ["I understand and accept", "agree to the terms", "privacy notice"]:
        try:
            lbl = page.locator(f'label:has-text("{label_text}")')
            if lbl.count() > 0 and lbl.first.is_visible():
                lbl.first.click(); time.sleep(0.3)
                log(f"  ✓ Privacy: {label_text}")
                return True
        except: pass
    try:
        for cb in page.locator('input[type="checkbox"]').all():
            if not cb.is_visible() or cb.is_checked(): continue
            txt = cb.evaluate("el => (el.closest('label')||el.parentElement||el).innerText||''")
            if any(w in txt.lower() for w in ['privacy','terms','understand','accept','agree']):
                cb.check(); time.sleep(0.3)
                log(f"  ✓ Privacy checkbox: {txt[:50].strip()}")
                return True
    except: pass
    return False

def handle_yes_no_questions(page):
    """Handle 'Are you at least 18', 'worked for Ford', 'authorized to work' etc."""
    # Click Yes/No radio buttons based on question text
    filled = 0
    try:
        # Find all radio groups
        groups = page.evaluate("""() => {
            const seen = new Set();
            const res = [];
            document.querySelectorAll('input[type=radio]').forEach(r => {
                if (!r.offsetParent) return;
                const name = r.name || r.getAttribute('data-bind') || '';
                if (!name || seen.has(name)) return;
                seen.add(name);
                // Check if any in this group is already selected
                const siblings = document.querySelectorAll('input[type=radio][name="' + name + '"]');
                const anyChecked = Array.from(siblings).some(s => s.checked);
                if (anyChecked) return;
                // Get question label
                let label = '';
                const parent = r.closest('fieldset,div[class*="form-group"],div[class*="question"],oj-radioset,div');
                if (parent) {
                    const lg = parent.querySelector('legend,label[class*="label"],span[class*="label"],[slot="label"]');
                    if (lg) label = lg.innerText.trim();
                }
                if (!label) {
                    const prevEl = r.parentElement;
                    if (prevEl) label = prevEl.innerText.trim().split('\\n')[0];
                }
                // Get options
                const opts = Array.from(siblings).map(s => ({
                    id: s.id, value: s.value,
                    text: (document.querySelector('label[for="'+s.id+'"]')||s.parentElement||s).innerText.trim()
                }));
                res.push({name, label: label.slice(0,100), opts});
            });
            return res;
        }""")
        for grp in groups:
            lbl = grp['label'].lower()
            opts = grp['opts']
            pick = None
            if any(w in lbl for w in ['18 years','18 year','legal age','adult']):
                pick = next((o for o in opts if 'yes' in o['text'].lower()), None)
            elif any(w in lbl for w in ['worked for ford','ford motor','previous employee','previously employed','ever worked']):
                pick = next((o for o in opts if 'no' in o['text'].lower()), None)
            elif any(w in lbl for w in ['authorized','authorised','right to work','work in the uk','work in uk','eligible to work','legally entitled']):
                pick = next((o for o in opts if 'yes' in o['text'].lower()), None)
            elif any(w in lbl for w in ['require sponsor','visa sponsor','need sponsor','require work permit']):
                pick = next((o for o in opts if 'yes' in o['text'].lower()), None)
            elif any(w in lbl for w in ['current employee','work here','employed here','referral']):
                pick = next((o for o in opts if 'no' in o['text'].lower()), None)
            elif any(w in lbl for w in ['agree','consent','acknowledge','confirm','declare']):
                pick = next((o for o in opts if 'yes' in o['text'].lower()), None)
            else:
                # Default yes for unknown yes/no questions
                pick = next((o for o in opts if o['text'].strip().lower() == 'yes'), None)

            if pick and pick['id']:
                try:
                    radio = page.locator(f'#{pick["id"]}')
                    if radio.count() > 0:
                        radio.first.check()
                        log(f"  ✓ Radio '{pick['text']}' for: {grp['label'][:60]}")
                        filled += 1
                        time.sleep(0.2)
                except: pass
    except Exception as e:
        log(f"  Radio error: {e}")

    # Also click any visible 'Yes' buttons that look like answers (Oracle HCM sometimes uses buttons for radio)
    try:
        for btn in page.locator('button').all():
            if not btn.is_visible(): continue
            txt = (btn.inner_text() or '').strip().lower()
            if txt == 'yes':
                # Check context — is this inside a question about 18/age/authorized?
                ctx = btn.evaluate("el => (el.closest('div[class*=question],div[class*=field],fieldset,section')||el.parentElement||el).innerText||''")
                if any(w in ctx.lower() for w in ['18','age','authorized','authorised','legal','eligible','agree']):
                    btn.click(); time.sleep(0.3)
                    log(f"  ✓ Clicked Yes button (ctx: {ctx[:40].strip()})")
                    filled += 1
    except: pass

    return filled

def handle_oracle_select(page, label_text, value_hint):
    """Handle Oracle JET custom select/listbox by label text."""
    try:
        # Find the oj-select or custom combobox by nearby label
        comboboxes = page.locator('[role="combobox"],[role="listbox"] [role="option"]')
        # Find label element containing label_text
        lbl_el = page.locator(f'label:has-text("{label_text}"), span:has-text("{label_text}"), legend:has-text("{label_text}")')
        if lbl_el.count() == 0:
            return False
        # Get the associated form control
        lbl_for = lbl_el.first.get_attribute("for")
        if lbl_for:
            ctrl = page.locator(f'#{lbl_for}')
        else:
            # Use next sibling combobox
            ctrl = page.locator(f'text="{label_text}" >> xpath=../.. >> [role="combobox"]')
        if ctrl.count() == 0:
            return False
        ctrl.first.click()
        time.sleep(0.5)
        # Type to filter options
        page.keyboard.type(value_hint[:10])
        time.sleep(0.8)
        # Click first visible option
        opt = page.locator('[role="option"]:visible, li[role="option"]:visible')
        if opt.count() > 0:
            opt.first.click(); time.sleep(0.3)
            log(f"  ▼ Oracle select '{value_hint}' for '{label_text}'")
            return True
    except Exception as e:
        log(f"  Oracle select error ({label_text}): {e}")
    return False

def fill_all_fields(page):
    """Scan every visible input/select/textarea and fill it correctly."""
    filled = 0
    fields = page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('input,select,textarea').forEach(el => {
            if (!el.offsetParent) return;
            if (el.type === 'radio' || el.type === 'checkbox') return; // handled separately
            let label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
            if (el.id) {
                const lbl = document.querySelector('label[for="' + el.id + '"]');
                if (lbl) label = lbl.innerText.trim();
            }
            if (!label) {
                const pl = el.closest('label');
                if (pl) label = pl.innerText.trim().split('\\n')[0];
            }
            // Oracle JET: also check aria-labelledby
            if (!label && el.getAttribute('aria-labelledby')) {
                const ids = el.getAttribute('aria-labelledby').split(' ');
                for (const id of ids) {
                    const le = document.getElementById(id);
                    if (le && le.innerText.trim()) { label = le.innerText.trim(); break; }
                }
            }
            results.push({
                tag: el.tagName, type: (el.type||'text').toLowerCase(),
                label: label.trim().slice(0,100),
                id: el.id, name: el.name,
                value: el.value.slice(0,80),
                options: el.tagName==='SELECT'
                    ? Array.from(el.options).map(o => ({val: o.value, text: o.text.trim()}))
                    : [],
                required: el.required,
                readonly: el.readOnly,
                disabled: el.disabled
            });
        });
        return results;
    }""")

    log(f"  Found {len(fields)} fields: {[f['label'][:30] for f in fields[:10]]}")

    for f in fields:
        if f['disabled'] or f['readonly']: continue
        lbl  = f['label'].lower()
        tag  = f['tag']
        ftype = f['type']

        # Build locator
        if f['id']:
            el = page.locator(f'#{f["id"]}').first
        elif f['name']:
            el = page.locator(f'[name="{f["name"]}"]').first
        else:
            continue
        try:
            if not el.is_visible(): continue
        except: continue

        # ── SELECT dropdown ───────────────────────────────────────────────
        if tag == 'SELECT':
            if f['value'] and f['value'] not in ('', '0', '-1', 'Select'): continue
            opts = [o['text'] for o in f['options']]
            picked = None
            if any(w in lbl for w in ['country','nation']):
                picked = next((o for o in opts if 'united kingdom' in o.lower() or 'uk' == o.lower().strip()), None)
            elif any(w in lbl for w in ['county','region','state','province']):
                picked = next((o for o in opts if 'greater london' in o.lower()), None) or \
                         next((o for o in opts if 'london' in o.lower()), None) or \
                         next((o for o in opts if o.strip() and o.strip().lower() not in ('select','please select','')), None)
            elif any(w in lbl for w in ['phone type','contact type']):
                picked = next((o for o in opts if 'mobile' in o.lower() or 'cell' in o.lower()), opts[1] if len(opts)>1 else None)
            elif any(w in lbl for w in ['hear about','source','referred']):
                picked = next((o for o in opts if 'job board' in o.lower() or 'linkedin' in o.lower() or 'internet' in o.lower()), None) or \
                         next((o for o in opts if o.strip() and o.strip().lower() not in ('select','please select','')), None)
            elif len(opts) > 1:
                picked = next((o for o in opts if o.strip() and o.strip().lower() not in ('select','please select','')), None)
            if picked:
                try:
                    el.scroll_into_view_if_needed()
                    el.select_option(label=picked)
                    el.evaluate("el => { el.dispatchEvent(new Event('change',{bubbles:true})); }")
                    log(f"  ▼ Selected '{picked[:40]}' for: {f['label'][:40]}")
                    filled += 1; time.sleep(0.3)
                except Exception as e:
                    log(f"  ✗ Select failed ({f['label'][:30]}): {e}")
            continue

        # ── Text / email / tel / textarea ─────────────────────────────────
        if ftype in ('text','email','tel','number','search','url') or tag == 'TEXTAREA':
            # Skip if already has a correct value
            if f['value'] and ftype != 'email':
                # Check if value looks right
                if len(f['value']) > 2: continue

            value = None
            if 'email' in lbl:                                      value = EMAIL
            elif any(w in lbl for w in ['first name','given name','forename']):  value = NAME_F
            elif any(w in lbl for w in ['last name','surname','family name']):   value = NAME_L
            elif lbl in ('name','full name','your name','candidate name'):        value = f"{NAME_F} {NAME_L}"
            elif any(w in lbl for w in ['phone','mobile','telephone']) and 'country' not in lbl:
                value = PHONE
            elif any(w in lbl for w in ['address line 1','street address','address 1','addr1','street']): value = ADDR1
            elif any(w in lbl for w in ['city','town']):            value = CITY
            elif any(w in lbl for w in ['postcode','postal code','zip']): value = POSTCODE
            elif any(w in lbl for w in ['salary','expected','expectation']): value = "52000"
            elif any(w in lbl for w in ['linkedin','profile url']): value = "https://www.linkedin.com/in/alramina-myrzabekova"
            elif any(w in lbl for w in ['notice','notice period']): value = "2 weeks"
            elif ftype == 'number' or any(w in lbl for w in ['years of exp','year of exp','how many year']):
                value = "2"
            elif tag == 'TEXTAREA':
                value = ("I am applying for this role as it aligns with my background as a "
                         "Technology Risk Consultant at EY Financial Services. I have 2 years "
                         "of relevant experience and am eager to contribute to your team.")

            if value:
                if oracle_fill(page, el, value):
                    log(f"  ✎ Filled '{value[:30]}' → {f['label'][:40]}")
                    filled += 1

    return filled

def fill_oracle_custom_selects(page):
    """Handle Oracle JET custom comboboxes/listboxes that aren't native SELECT."""
    filled = 0
    # Look for oj-select-one, oj-combobox or div[role=combobox] elements
    combos = page.evaluate("""() => {
        const res = [];
        document.querySelectorAll('[role="combobox"],[class*="oj-combobox"],[class*="oj-select"]').forEach(el => {
            if (!el.offsetParent) return;
            // Check if it has a value already
            const input = el.querySelector('input');
            if (input && input.value && input.value.trim()) return;
            // Find label
            let label = el.getAttribute('aria-label') || '';
            const id = el.getAttribute('id') || '';
            if (!label && id) {
                const lbl = document.querySelector('label[for="' + id + '"]');
                if (lbl) label = lbl.innerText.trim();
            }
            if (!label) {
                const parent = el.closest('[class*="form-group"],[class*="question"],fieldset,div');
                if (parent) {
                    const lg = parent.querySelector('label,legend,span[class*="label"]');
                    if (lg) label = lg.innerText.trim();
                }
            }
            res.push({id, label: label.slice(0,80)});
        });
        return res;
    }""")
    for c in combos:
        lbl = c['label'].lower()
        if not lbl: continue
        if any(w in lbl for w in ['hear about','referred','source']):
            # Try to select "Job Board" or "LinkedIn"
            if oracle_handle_custom_dropdown(page, c['id'], c['label'], ['Job Board','LinkedIn','Internet','Online']):
                filled += 1
        elif any(w in lbl for w in ['country','nation']):
            if oracle_handle_custom_dropdown(page, c['id'], c['label'], ['United Kingdom']):
                filled += 1
        elif any(w in lbl for w in ['county','region','state']):
            if oracle_handle_custom_dropdown(page, c['id'], c['label'], ['Greater London','London']):
                filled += 1
    return filled

def oracle_handle_custom_dropdown(page, el_id, label, values_to_try):
    """Click Oracle JET combobox and select first matching value."""
    try:
        if el_id:
            ctrl = page.locator(f'#{el_id}')
        else:
            return False
        if ctrl.count() == 0: return False

        ctrl.first.click(); time.sleep(0.6)

        for val in values_to_try:
            # Type to filter
            page.keyboard.press("Control+a")
            page.keyboard.type(val[:8])
            time.sleep(0.8)
            opt = page.locator('[role="option"]:visible, li[role="option"]:visible').first
            if opt.count() > 0 and opt.is_visible():
                opt.click(); time.sleep(0.4)
                log(f"  ▼ Oracle combobox '{val}' for '{label[:40]}'")
                return True

        # Press Escape to close if nothing found
        page.keyboard.press("Escape")
    except Exception as e:
        log(f"  Oracle combo error ({label}): {e}")
    return False

def click_next(page):
    for label in ["NEXT", "Next", "CONTINUE", "Continue", "SAVE & CONTINUE",
                  "SAVE AND CONTINUE", "SUBMIT APPLICATION", "Submit Application",
                  "SUBMIT", "Submit", "APPLY", "Apply", "REVIEW AND SUBMIT"]:
        for role in ("button", "link"):
            btn = page.get_by_role(role, name=re.compile(rf"^{re.escape(label)}[\s▶►]*$", re.I))
            if btn.count() > 0:
                vis = [b for b in btn.all() if b.is_visible()]
                if vis:
                    vis[0].scroll_into_view_if_needed()
                    vis[0].click()
                    log(f"  → Clicked '{label}'")
                    return label
    return None

def upload_cv(page):
    fi = page.locator('input[type="file"]')
    if fi.count() > 0:
        try:
            fi.first.set_input_files(CV_PATH)
            log(f"  📎 Uploaded CV")
            time.sleep(2)
            return True
        except Exception as e:
            log(f"  ✗ CV upload failed: {e}")
    return False

def get_errors(page):
    try:
        return page.evaluate("""() =>
            Array.from(document.querySelectorAll('[class*="error"],[class*="alert"],[role="alert"],[class*="invalid"],[class*="required"]'))
            .filter(e => e.offsetParent && e.innerText.trim())
            .map(e => e.innerText.trim().slice(0, 100))
        """)
    except: return []

# ─── Main ────────────────────────────────────────────────────────────────────
log("Starting Oracle HCM application")
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        "/Users/aly4x/openclaw/apply/company-profile",
        headless=False,
        args=["--disable-blink-features=AutomationControlled",
              "--no-first-run", "--hide-crash-restore-bubble",
              "--disable-notifications"],
        permissions=[],
        viewport={"width": 1280, "height": 900}
    )
    for old in ctx.pages[1:]:
        try: old.close()
        except: pass
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(APPLY_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)

    stuck_count = 0
    last_fill_count = -1

    for step in range(60):
        current_url = page.url
        log(f"\n{'='*50}\n[Step {step+1}] {current_url}")

        html = page.content().lower()
        if any(s in html for s in ["application submitted","thank you for applying",
                                    "application received","successfully submitted",
                                    "application complete","we'll be in touch",
                                    "application has been submitted"]):
            log("\n✅ APPLICATION SUBMITTED SUCCESSFULLY!")
            time.sleep(5)
            break

        accept_privacy(page)
        time.sleep(0.3)

        upload_cv(page)

        n1 = fill_all_fields(page)
        time.sleep(0.5)
        n2 = handle_yes_no_questions(page)
        time.sleep(0.5)
        n3 = fill_oracle_custom_selects(page)
        total_filled = n1 + n2 + n3
        log(f"  Filled: {n1} text + {n2} radio + {n3} custom = {total_filled}")

        errs = get_errors(page)
        if errs:
            log(f"  Errors: {errs[:5]}")

        try:
            btns = page.evaluate("""() => Array.from(document.querySelectorAll('button,[role=button]'))
                .filter(b=>b.offsetParent).map(b=>(b.innerText||'').trim()).filter(t=>t)""")
            log(f"  Buttons: {btns[:10]}")
        except: pass

        clicked = click_next(page)
        if not clicked:
            log("  No forward button found")
            break

        time.sleep(4)

        if page.url == current_url:
            stuck_count += 1
            errs2 = get_errors(page)
            log(f"  Stuck {stuck_count}/5. Errors: {errs2[:3]}")
            if stuck_count >= 5 and total_filled == 0:
                log("  Stuck 5 times with 0 new fills — giving up")
                break
            elif stuck_count >= 8:
                log("  Stuck 8 times — giving up")
                break
        else:
            stuck_count = 0

    log("\nDone. Keeping browser open for 30s...")
    time.sleep(30)
    ctx.close()
