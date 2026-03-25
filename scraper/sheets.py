"""
Writes new jobs to the 'claudebot' tab in Alramina's Google Sheet.
Matches existing tracker layout with conditional formatting on Stage and Outcome columns.
"""
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import os

SHEET_ID = "18-_0J-ImLDrl2Z1Wm0iXskx02X0P9w6_pvG-nAjgkB8"
TAB_NAME = "claudebot"
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "service_account.json")

# Matches existing tracker layout (minus Team column)
HEADERS = ["Role", "Company", "Location", "Stage", "Visa Sponsor", "Claude", "Notes", "Easy Apply"]

_sheet = None
_spreadsheet = None


def _rgb(hex_color: str) -> dict:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return {"red": r / 255, "green": g / 255, "blue": b / 255}


def _text_rule(text: str, bg_hex: str, sheet_id: int, col_index: int) -> dict:
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": col_index,
                    "endColumnIndex": col_index + 1,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_CONTAINS",
                        "values": [{"userEnteredValue": text}]
                    },
                    "format": {
                        "backgroundColor": _rgb(bg_hex)
                    }
                }
            },
            "index": 0
        }
    }


def _apply_borders(spreadsheet, sheet_id: int, start_row: int, end_row: int):
    border = {"style": "SOLID", "width": 1, "color": {"red": 0, "green": 0, "blue": 0}}
    spreadsheet.batch_update({"requests": [{
        "updateBorders": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": 0,
                "endColumnIndex": len(HEADERS),
            },
            "top":             border,
            "bottom":          border,
            "left":            border,
            "right":           border,
            "innerHorizontal": border,
            "innerVertical":   border,
        }
    }]})


def _apply_conditional_formatting(spreadsheet, sheet_id: int):
    stage_col = 3   # Column D (0-indexed)

    rules = []

    # Stage column rules (D)
    stage_rules = [
        ("rejected",    "#f4cccc"),  # red
        ("declined",    "#f4cccc"),
        ("unsuccessful","#f4cccc"),
        ("offer",       "#d9ead3"),  # green
        ("accepted",    "#d9ead3"),
        ("final round", "#c9daf8"),  # blue
        ("interview",   "#fce5cd"),  # orange
        ("phone",       "#fce5cd"),
        ("assessment",  "#fff2cc"),  # yellow
        ("online test", "#fff2cc"),
        ("test",        "#fff2cc"),
        ("applied",     "#d0e0f5"),  # light blue
        ("withdrawn",   "#d9d9d9"),  # grey
    ]

    # Outcome column rules (G)
    outcome_rules = [
        ("rejected",    "#f4cccc"),
        ("declined",    "#f4cccc"),
        ("unsuccessful","#f4cccc"),
        ("offer",       "#d9ead3"),
        ("accepted",    "#d9ead3"),
        ("withdrawn",   "#d9d9d9"),
    ]

    claude_col = 5  # Column F (0-indexed)

    for text, color in stage_rules:
        rules.append(_text_rule(text, color, sheet_id, stage_col))

    # Claude column status colours
    claude_rules = [
        ("Applied",       "#d9ead3"),  # green  — OpenClaw applied
        ("Company site",  "#c9daf8"),  # blue   — needs company runner
        ("Manual needed", "#fff2cc"),  # yellow — needs manual apply
        ("no",            "#f4cccc"),  # red    — skip
    ]
    for text, color in claude_rules:
        rules.append(_text_rule(text, color, sheet_id, claude_col))

    spreadsheet.batch_update({"requests": rules})


def _get_sheet():
    global _sheet, _spreadsheet
    if _sheet is None:
        sa_json = os.environ.get("GOOGLE_SA_JSON", "")
        if sa_json:
            import json as _json
            info = _json.loads(sa_json)
            creds = Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        else:
            creds = Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        client = gspread.authorize(creds)
        _spreadsheet = client.open_by_key(SHEET_ID)

        try:
            _sheet = _spreadsheet.worksheet(TAB_NAME)
        except gspread.WorksheetNotFound:
            _sheet = _spreadsheet.add_worksheet(title=TAB_NAME, rows=1000, cols=len(HEADERS))
            _sheet.append_row(HEADERS)
            _apply_conditional_formatting(_spreadsheet, _sheet.id)

    return _sheet


def log_jobs_to_sheet(jobs: list[dict]):
    if not jobs:
        return

    try:
        sheet = _get_sheet()

        existing = sheet.get_all_values(value_render_option="FORMULA")
        if not existing:
            sheet.append_row(HEADERS)
            _apply_conditional_formatting(_spreadsheet, sheet.id)
            existing = [HEADERS]

        # Build set of URLs already in the sheet to prevent duplicates
        import re as _re
        existing_urls = set()
        for row in existing[1:]:
            cell = row[0] if row else ""
            m = _re.search(r'HYPERLINK\("([^"]+)"', cell)
            if m:
                existing_urls.add(m.group(1).strip())
            elif cell.startswith("http"):
                existing_urls.add(cell.strip())

        rows = []
        skipped = 0
        for job in jobs:
            url = job.get("url", "")
            if url and url.strip() in existing_urls:
                skipped += 1
                continue  # already in sheet — don't add duplicate

            sponsor = "✅ Confirmed" if job.get("is_sponsor") is True else ""
            title = job.get("title", "").replace('"', "'")
            role_formula = f'=HYPERLINK("{url}", "{title}")' if url else title
            easy_apply = "✓" if job.get("is_easy_apply") else ""

            rows.append([
                role_formula,
                job.get("company", ""),
                job.get("location", ""),
                "",          # Stage
                sponsor,     # Visa Sponsor
                "",          # Claude
                "",          # Notes
                easy_apply,  # Easy Apply
            ])

        # Use explicit range to avoid column-offset bug when table width > 8
        next_row = len(sheet.get_all_values()) + 1
        end_row = next_row + len(rows) - 1
        sheet.update(
            f"A{next_row}:H{end_row}",
            rows,
            value_input_option="USER_ENTERED",
        )

        # Apply borders to all rows (including newly added ones)
        total_rows = end_row
        _apply_borders(_spreadsheet, sheet.id, 0, total_rows)

        # Extend the basic filter to cover new rows so column filtering keeps working
        _spreadsheet.batch_update({"requests": [{
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sheet.id,
                        "startRowIndex": 0,
                        "endRowIndex": total_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(HEADERS),
                    }
                }
            }
        }]})

        print(f"Logged {len(rows)} jobs to Google Sheets. (skipped {skipped} duplicates)")

    except Exception as e:
        print(f"Google Sheets error: {e}")
