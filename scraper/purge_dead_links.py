"""
Checks every URL in the claudebot sheet and removes rows where the job
is no longer live (closed, 404, expired, or redirected to login/authwall).
"""
import os, re, time, requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = "18-_0J-ImLDrl2Z1Wm0iXskx02X0P9w6_pvG-nAjgkB8"
TAB_NAME = "claudebot"
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "service_account.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

DEAD_PHRASES = [
    "no longer accepting applications",
    "job is no longer available",
    "this job has expired",
    "position has been filled",
    "vacancy has been filled",
    "posting has been removed",
    "this position is no longer",
    "job posting is no longer",
    "this listing has expired",
    "application deadline has passed",
]


def extract_url(cell: str) -> str:
    m = re.search(r'HYPERLINK\("([^"]+)"', cell)
    if m:
        return m.group(1)
    if cell.startswith("http"):
        return cell
    return ""


def is_dead(url: str) -> tuple[bool, str]:
    """Returns (dead, reason)."""
    if not url:
        return True, "no url"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)

        # Auth wall = can't verify but assume live (LinkedIn requires login for some)
        if "authwall" in resp.url or "/login" in resp.url:
            return False, "auth wall (kept)"

        if resp.status_code == 404:
            return True, "404"

        if resp.status_code >= 400:
            return True, f"HTTP {resp.status_code}"

        text = resp.text.lower()
        for phrase in DEAD_PHRASES:
            if phrase in text:
                return True, phrase[:40]

        return False, "live"
    except requests.exceptions.ConnectionError:
        return True, "connection error"
    except Exception as e:
        return False, f"error (kept): {e}"


def main():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet(TAB_NAME)

    formula_rows = sheet.get_all_values(value_render_option="FORMULA")
    display_rows = sheet.get_all_values()

    headers = display_rows[0]
    total = len(formula_rows) - 1
    print(f"Checking {total} rows...\n")

    to_delete = []
    url_cache = {}

    for i, (frow, drow) in enumerate(zip(formula_rows[1:], display_rows[1:]), start=2):
        cell = frow[0] if frow else ""
        url = extract_url(cell)
        company = drow[1] if len(drow) > 1 else ""
        title = drow[0] if drow else f"row {i}"

        if not url:
            print(f"  [{i}] No URL — removing ({company})")
            to_delete.append(i)
            continue

        if url in url_cache:
            dead, reason = url_cache[url]
        else:
            dead, reason = is_dead(url)
            url_cache[url] = (dead, reason)

        status = "DEAD" if dead else "live"
        print(f"  [{i}] {status} ({reason}) — {company}: {title[:50]}")

        if dead:
            to_delete.append(i)

        time.sleep(0.4)

    print(f"\n{'='*50}")
    print(f"Dead rows to remove: {len(to_delete)}")
    print(f"Remaining live rows: {total - len(to_delete)}")

    if not to_delete:
        print("Nothing to delete.")
        return

    # Delete from bottom to top
    for row_idx in sorted(to_delete, reverse=True):
        sheet.delete_rows(row_idx)
        time.sleep(0.2)

    print(f"\nDone. Removed {len(to_delete)} dead rows.")


if __name__ == "__main__":
    main()
