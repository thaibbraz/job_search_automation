"""
Fetches all paid users from the Jobbyo API and syncs their
first name, last name, and email into a Google Sheet.

Rows are keyed by email — existing rows are updated, new ones appended.
Row 1 is the header; data starts at row 2.

Env vars required:
  JOBO_API_KEY                  — Jobbyo backend key
  GOOGLE_SHEETS_CREDENTIALS     — path to service account JSON (default: credentials.json)
  GOOGLE_SHEETS_SPREADSHEET_ID  — target spreadsheet ID
"""

import os
import requests
import gspread
from google.oauth2.service_account import Credentials

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

JOBO_API_KEY = os.getenv("JOBO_API_KEY", "")
CREDS_PATH = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "credentials.json")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
BASE_URL = "https://fastapi-service-03-160893319817.europe-southwest1.run.app"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

HEADERS = ["First Name", "Last Name", "Email"]


def get_paid_users():
    resp = requests.get(
        f"{BASE_URL}/users/paid",
        headers={"x-api-key": JOBO_API_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def split_name(display_name: str):
    parts = (display_name or "").strip().split(" ", 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return first, last


def main():
    if not JOBO_API_KEY:
        raise RuntimeError("JOBO_API_KEY not set")
    if not SPREADSHEET_ID:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID not set")

    print("Fetching paid users...")
    users = get_paid_users()
    print(f"  {len(users)} users found")

    creds = Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(SPREADSHEET_ID).sheet1

    # Ensure header row
    existing = sheet.get_all_values()
    if not existing or existing[0] != HEADERS:
        sheet.update("A1", [HEADERS])
        existing = sheet.get_all_values()

    # Build email → row-index map (1-based, skipping header)
    email_to_row = {}
    for i, row in enumerate(existing[1:], start=2):
        email = row[2].strip().lower() if len(row) > 2 else ""
        if email:
            email_to_row[email] = i

    updates = []
    appends = []

    for user in users:
        email = (user.get("email") or "").strip()
        if not email:
            continue
        first, last = split_name(user.get("displayName") or "")
        row_data = [first, last, email]

        if email.lower() in email_to_row:
            row_idx = email_to_row[email.lower()]
            updates.append((row_idx, row_data))
        else:
            appends.append(row_data)

    if updates:
        for row_idx, row_data in updates:
            sheet.update(f"A{row_idx}", [row_data])
        print(f"  Updated {len(updates)} existing rows")

    if appends:
        sheet.append_rows(appends, value_input_option="RAW")
        print(f"  Appended {len(appends)} new rows")

    print("Done.")


if __name__ == "__main__":
    main()
