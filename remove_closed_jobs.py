"""
Checks every LinkedIn URL in the sheet and removes rows where the job
is no longer accepting applications.
"""
import os
import time
import requests
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


def is_closed(url: str) -> bool:
    """Fetch the LinkedIn job page and check if it's closed."""
    if not url or "linkedin.com" not in url:
        return False
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        if "authwall" in resp.url or "login" in resp.url or resp.status_code in (401, 403):
            return False  # Can't determine without auth — keep the row
        return "no longer accepting applications" in resp.text.lower()
    except Exception as e:
        print(f"    Error checking {url}: {e}")
        return False


def extract_linkedin_url(cell_value: str) -> str:
    """Extract the LinkedIn URL from a HYPERLINK formula or plain value."""
    if cell_value.startswith('=HYPERLINK('):
        # =HYPERLINK("url", "title")
        parts = cell_value.split('"')
        if len(parts) >= 2:
            return parts[1]
    if "linkedin.com" in cell_value:
        return cell_value
    return ""


def main():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet(TAB_NAME)

    # Fetch formula values so we can extract URLs from HYPERLINK formulas
    formula_rows = sheet.get_all_values(value_render_option="FORMULA")
    display_rows = sheet.get_all_values()

    if not formula_rows:
        print("Sheet is empty.")
        return

    headers = display_rows[0]
    role_col = headers.index("Role")  # Contains the hyperlink formula
    total = len(formula_rows) - 1
    print(f"Checking {total} rows for closed LinkedIn jobs...\n")

    rows_to_delete = []

    for i, row in enumerate(formula_rows[1:], start=2):  # 1-indexed, row 1 = header
        cell = row[role_col] if len(row) > role_col else ""
        url = extract_linkedin_url(cell)

        if not url:
            continue  # Not a LinkedIn job, skip

        display_row = display_rows[i - 1] if i - 1 < len(display_rows) else []
        company = display_row[1] if len(display_row) > 1 else ""
        print(f"  [{i}] Checking {company}...", end=" ", flush=True)

        if is_closed(url):
            print("CLOSED — will remove")
            rows_to_delete.append(i)
        else:
            print("open")

        time.sleep(0.5)  # Be polite to LinkedIn

    if not rows_to_delete:
        print("\nNo closed jobs found.")
        return

    print(f"\nRemoving {len(rows_to_delete)} closed job(s)...")

    # Delete from bottom to top so row indices stay valid
    for row_idx in sorted(rows_to_delete, reverse=True):
        sheet.delete_rows(row_idx)
        print(f"  Deleted row {row_idx}")
        time.sleep(0.3)

    print(f"\nDone. Removed {len(rows_to_delete)} closed jobs.")


if __name__ == "__main__":
    main()
