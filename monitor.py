"""
3-hour active monitor:
- Keeps Telegram bot alive
- Validates sheet (no duplicates, correct columns, sponsor fill)
- Purges dead links every hour
- Sends Telegram status updates every 30 min
"""
import os, re, sys, time, subprocess, requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = "18-_0J-ImLDrl2Z1Wm0iXskx02X0P9w6_pvG-nAjgkB8"
TAB_NAME = "claudebot"
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "service_account.json")
BOT_SCRIPT = os.path.join(os.path.dirname(__file__), "bot.py")
PYTHON = "/Users/aly4x/.pyenv/versions/3.11.9/bin/python"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = "711278301"

def tg(msg):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def get_sheet():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(TAB_NAME)


def ensure_bot_running():
    result = subprocess.run(["pgrep", "-f", "claudebot/bot.py"], capture_output=True)
    if result.returncode != 0:
        print("Bot not running — restarting via launchd...")
        subprocess.run(["launchctl", "unload", os.path.expanduser("~/Library/LaunchAgents/com.aly4x.claudebot.plist")])
        time.sleep(2)
        subprocess.run(["launchctl", "load", os.path.expanduser("~/Library/LaunchAgents/com.aly4x.claudebot.plist")])
        tg("⚠️ B1mo Bot had stopped — restarted automatically.")
        return False
    return True


def fix_sheet_columns(sheet):
    """Fix any rows with data in wrong columns (offset bug)."""
    formula_rows = sheet.get_all_values(value_render_option="FORMULA")
    display_rows = sheet.get_all_values()

    updates = []
    clears = []
    for i, (frow, drow) in enumerate(zip(formula_rows[1:], display_rows[1:]), start=2):
        if drow[0]:
            continue
        if not any(drow):
            continue
        first_col = next((j for j, v in enumerate(drow) if v), None)
        if first_col is None or first_col < 7:
            continue
        chunk = frow[first_col:]
        new_row = (chunk + [""] * 8)[:8]
        for col_idx, val in enumerate(new_row):
            updates.append(gspread.Cell(i, col_idx + 1, val))
        for col_idx in range(first_col, len(frow)):
            if frow[col_idx]:
                clears.append(gspread.Cell(i, col_idx + 1, ""))

    if updates:
        sheet.update_cells(updates, value_input_option="USER_ENTERED")
    if clears:
        sheet.update_cells(clears)
    return len([u for u in updates if u.col == 1])


def remove_duplicates(sheet):
    """Remove rows with duplicate URLs, keeping the one with most data."""
    formula_rows = sheet.get_all_values(value_render_option="FORMULA")
    display_rows = sheet.get_all_values()

    seen_urls = {}
    to_delete = []

    for i, (frow, drow) in enumerate(zip(formula_rows[1:], display_rows[1:]), start=2):
        cell = frow[0] if frow else ""
        m = re.search(r'HYPERLINK\("([^"]+)"', cell)
        url = m.group(1).strip() if m else (cell.strip() if cell.startswith("http") else "")
        if not url:
            continue
        if url in seen_urls:
            # Keep the row with more data (applied status wins)
            prev_i, prev_row = seen_urls[url]
            prev_status = prev_row[5] if len(prev_row) > 5 else ""
            curr_status = drow[5] if len(drow) > 5 else ""
            if "applied" in prev_status.lower():
                to_delete.append(i)  # delete current
            else:
                to_delete.append(prev_i)  # delete previous
                seen_urls[url] = (i, drow)
        else:
            seen_urls[url] = (i, drow)

    if to_delete:
        spreadsheet = sheet.spreadsheet
        reqs = []
        for row_idx in sorted(set(to_delete), reverse=True):
            reqs.append({"deleteDimension": {
                "range": {"sheetId": sheet.id, "dimension": "ROWS",
                          "startIndex": row_idx - 1, "endIndex": row_idx}
            }})
        for chunk_start in range(0, len(reqs), 10):
            spreadsheet.batch_update({"requests": reqs[chunk_start:chunk_start+10]})
            time.sleep(1.5)
    return len(to_delete)


def fill_visa_sponsor(sheet):
    """Fill blank Visa Sponsor cells for confirmed sponsors."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from sponsor_check import load_register, is_sponsor
    sponsors = load_register()

    display_rows = sheet.get_all_values()
    headers = display_rows[0]
    try:
        company_col = headers.index("Company")
        visa_col = headers.index("Visa Sponsor")
    except ValueError:
        return 0

    updates = []
    for i, row in enumerate(display_rows[1:], start=2):
        company = row[company_col].strip() if len(row) > company_col else ""
        current = row[visa_col].strip() if len(row) > visa_col else ""
        if not company or current:
            continue
        if is_sponsor(company, sponsors):
            updates.append(gspread.Cell(i, visa_col + 1, "✅ Confirmed"))

    if updates:
        sheet.update_cells(updates)
    return len(updates)


def sheet_status(sheet):
    rows = sheet.get_all_values()
    data = rows[1:]
    total = len(data)
    applied = sum(1 for r in data if len(r) > 5 and "applied" in r[5].lower())
    pending = sum(1 for r in data if len(r) > 5 and not r[5].strip())
    manual = sum(1 for r in data if len(r) > 5 and "manual" in r[5].lower())
    company = sum(1 for r in data if len(r) > 5 and "company" in r[5].lower())
    return total, applied, pending, manual, company


def run_cycle(sheet, cycle_num, last_purge):
    print(f"\n{'='*50}")
    print(f"[{datetime.now().strftime('%H:%M')}] Monitor cycle {cycle_num}")

    bot_ok = ensure_bot_running()

    # Fix column offsets
    fixed = fix_sheet_columns(sheet)
    if fixed:
        print(f"  Fixed {fixed} offset rows")

    # Remove duplicates
    removed = remove_duplicates(sheet)
    if removed:
        print(f"  Removed {removed} duplicate rows")

    # Fill sponsor column for new rows
    filled = fill_visa_sponsor(sheet)
    if filled:
        print(f"  Filled {filled} visa sponsor cells")

    # Purge dead links every ~60 min (every 2 cycles)
    purged = 0
    if cycle_num % 2 == 0:
        print("  Running dead link check...")
        try:
            result = subprocess.run(
                [PYTHON, os.path.join(os.path.dirname(__file__), "purge_dead_links.py")],
                capture_output=True, text=True, timeout=300
            )
            match = re.search(r"Removed (\d+) dead", result.stdout)
            if match:
                purged = int(match.group(1))
        except Exception as e:
            print(f"  Dead link purge error: {e}")

    # Status report to Telegram every cycle
    total, applied, pending, manual, company = sheet_status(sheet)
    status_lines = [
        f"📊 *B1mo Monitor Update* — {datetime.now().strftime('%H:%M')}",
        f"Sheet: {total} roles | ✅ {applied} applied | ⏳ {pending} pending",
        f"🔗 {company} company site | ⚠️ {manual} manual",
    ]
    if fixed: status_lines.append(f"🔧 Fixed {fixed} misaligned rows")
    if removed: status_lines.append(f"🗑 Removed {removed} duplicates")
    if filled: status_lines.append(f"🏢 Filled {filled} sponsor cells")
    if purged: status_lines.append(f"🧹 Purged {purged} dead links")
    if not bot_ok: status_lines.append("⚠️ Bot had crashed — restarted")

    tg("\n".join(status_lines))
    print("\n".join(status_lines))


def main():
    print(f"Monitor started at {datetime.now().strftime('%H:%M')}")
    tg(f"👀 *B1mo Monitor active* — watching your sheet for 3 hours. I'll update you every 30 min.")

    sheet = get_sheet()
    cycle = 1
    last_purge = 0

    for _ in range(6):  # 6 cycles × 30 min = 3 hours
        run_cycle(sheet, cycle, last_purge)
        cycle += 1
        if cycle <= 6:
            print(f"Sleeping 30 min until next cycle...")
            time.sleep(30 * 60)

    tg("✅ *3-hour monitor complete.* All systems running. Welcome back!")
    print("Monitor complete.")


if __name__ == "__main__":
    main()
