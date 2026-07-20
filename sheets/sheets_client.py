"""
Reads the public/shared Google Sheet that maps Company Name -> Sales Rep,
with fuzzy matching so "Al Futtaim Eng" matches "Al-Futtaim Engineering LLC".
Also supports exact lookup by the customer's WhatsApp number via a
"Company Phone" column, so a returning customer can be recognized instantly
without having to type their company name again.

Expected worksheet columns (header row, any order, case-insensitive):
    Company Name | Company Phone | Sales Rep Name | Rep Phone | Rep Email | Region

Company Phone should be the customer's WhatsApp-registered number, in
international format (with or without a leading +, spaces/dashes are
ignored during matching) - e.g. +971 50 123 4567 or 971501234567.

Setup:
1. Create a Google Cloud service account, enable Google Sheets API.
2. Download its JSON key to ./credentials/service_account.json
   (path configurable via GOOGLE_SERVICE_ACCOUNT_FILE in .env).
3. Share the target Google Sheet with the service account's client_email
   (found inside the JSON key) as Viewer.
4. Put the sheet's ID (from its URL) into GOOGLE_SHEET_ID in .env.
"""
import logging
import threading
import time

import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

from config import config
from utils.phone import is_plausible_phone, to_whatsapp_number

logger = logging.getLogger("wurth-agent.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

_cache = {"rows": None, "loaded_at": 0}
_lock = threading.Lock()
CACHE_TTL_SEC = 300  # re-fetch sheet at most every 5 minutes


def _get_client():
    creds = Credentials.from_service_account_file(config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def _normalize_key(header: str) -> str:
    return header.strip().lower().replace(" ", "_")


def _normalize_phone(phone: str) -> str:
    """Delegates to utils.phone.to_whatsapp_number so Company Phone matching
    uses the same UAE-country-code-aware normalization as rep_phone (see
    _load_rows) - a local-format number like '0501234567' now correctly
    becomes '971501234567' instead of just '501234567', matching what
    WhatsApp itself reports as the sender."""
    return to_whatsapp_number(phone)


def _load_rows(force: bool = False):
    with _lock:
        if not force and _cache["rows"] is not None and (time.time() - _cache["loaded_at"] < CACHE_TTL_SEC):
            return _cache["rows"]

        client = _get_client()
        sheet = client.open_by_key(config.GOOGLE_SHEET_ID)
        worksheet = sheet.worksheet(config.GOOGLE_SHEET_WORKSHEET_NAME)
        records = worksheet.get_all_records()  # list of dicts keyed by header row

        rows = []
        for r in records:
            normalized = {_normalize_key(k): v for k, v in r.items()}
            raw_rep_phone = str(normalized.get("rep_phone", "")).strip()
            row = {
                "company_name": str(normalized.get("company_name", "")).strip(),
                "company_phone": str(normalized.get("company_phone", "")).strip(),
                "rep_name": str(normalized.get("sales_rep_name", "")).strip(),
                # Normalized to full international format (UAE country code
                # added for local-format numbers) here at the source, so
                # every downstream consumer (customers.rep_phone,
                # escalation_attempts.target_phone, rep-reply detection)
                # consistently uses the same digit string WhatsApp itself
                # reports as the sender - a mismatch here previously caused
                # a rep replying to an escalation to be treated as an
                # ordinary customer instead of being recognized as the rep.
                "rep_phone": to_whatsapp_number(raw_rep_phone) if raw_rep_phone else "",
                "rep_email": str(normalized.get("rep_email", "")).strip(),
                "region": str(normalized.get("region", "")).strip(),
            }
            if row["rep_phone"] and not is_plausible_phone(row["rep_phone"]):
                logger.warning(
                    "Sheet row for company '%s' has an implausible Rep Phone value: %r - "
                    "escalation notifications to this rep may fail", row["company_name"], row["rep_phone"],
                )
            rows.append(row)

        _cache["rows"] = rows
        _cache["loaded_at"] = time.time()
        return rows


def find_rep_for_phone(customer_phone: str):
    """
    Exact match (after normalizing formatting) against the sheet's Company
    Phone column - used to instantly recognize a returning customer by their
    WhatsApp number, before ever asking for a company name.
    Returns the same dict shape as find_rep_for_company(), or None.
    """
    rows = _load_rows()
    if not rows or not customer_phone:
        return None

    target = _normalize_phone(customer_phone)
    if not target:
        return None

    for row in rows:
        if row["company_phone"] and _normalize_phone(row["company_phone"]) == target:
            result = dict(row)
            result["match_score"] = 100
            return result
    return None


def find_rep_for_company(company_name: str, threshold: int = None):
    """
    Fuzzy-matches company_name against the sheet's Company Name column.
    Returns dict {company_name, rep_name, rep_phone, rep_email, region, match_score}
    or None if nothing clears the threshold.
    """
    threshold = threshold if threshold is not None else config.FUZZY_MATCH_THRESHOLD
    rows = _load_rows()
    if not rows or not company_name:
        return None

    choices = {row["company_name"]: row for row in rows if row["company_name"]}
    match = process.extractOne(
        company_name, choices.keys(), scorer=fuzz.WRatio
    )
    if not match:
        return None

    matched_name, score, _ = match
    if score < threshold:
        return None

    result = dict(choices[matched_name])
    result["match_score"] = score
    return result


def refresh_cache():
    """Force a re-fetch on next lookup - call this if the sheet was just updated."""
    _load_rows(force=True)
