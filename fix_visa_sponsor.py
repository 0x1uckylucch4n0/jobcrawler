"""
One-time script: fill the Visa Sponsor column (E) for existing sheet rows
using the updated fuzzy matching against the UK sponsor register.
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import gspread
from google.oauth2.service_account import Credentials
from sponsor_check import load_register, is_sponsor

SHEET_ID = "18-_0J-ImLDrl2Z1Wm0iXskx02X0P9w6_pvG-nAjgkB8"
TAB_NAME = "claudebot"
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "service_account.json")


def main():
    sponsors = load_register()

    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet(TAB_NAME)

    rows = sheet.get_all_values()
    if not rows:
        print("Sheet is empty.")
        return

    headers = rows[0]
    print(f"Headers: {headers}")
    print(f"Total data rows: {len(rows) - 1}")

    try:
        company_col = headers.index("Company")
        visa_col = headers.index("Visa Sponsor")
    except ValueError as e:
        print(f"Column not found: {e}")
        sys.exit(1)

    updates = []
    confirmed = []
    not_found = []

    for i, row in enumerate(rows[1:], start=2):  # row 2 onwards (1-indexed, row 1 = header)
        company = row[company_col].strip() if len(row) > company_col else ""
        if not company:
            continue

        current_val = row[visa_col].strip() if len(row) > visa_col else ""
        cell_col = visa_col + 1  # gspread is 1-indexed

        if is_sponsor(company, sponsors):
            if current_val != "✅ Confirmed":
                updates.append(gspread.Cell(i, cell_col, "✅ Confirmed"))
                confirmed.append(f"  Row {i}: {company}")
        else:
            not_found.append(f"  Row {i}: {company}")

    if confirmed:
        print(f"\nWill mark {len(confirmed)} companies as confirmed sponsors:")
        for c in confirmed:
            print(c)

    if not_found:
        print(f"\n{len(not_found)} companies NOT on register (left blank):")
        for c in not_found[:20]:  # limit output
            print(c)
        if len(not_found) > 20:
            print(f"  ... and {len(not_found) - 20} more")

    if updates:
        sheet.update_cells(updates)
        print(f"\nDone. Updated {len(updates)} cells.")
    else:
        print("\nNo updates needed — all confirmed sponsors already marked.")


if __name__ == "__main__":
    main()
