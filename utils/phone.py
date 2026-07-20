"""
Shared phone number helpers - used by both sheets_client.py (validating
Rep Phone values loaded from the Google Sheet) and main.py (validating a
target number immediately before a WhatsApp API call), so the two don't
duplicate normalization/validation logic.
"""

# This app operates in the UAE only - a rep's phone in the Google Sheet is
# often entered in local format (e.g. "0501234567" or "050-123-4567") with
# no country code, while WhatsApp's own "from" field always reports numbers
# in full international format ("971501234567"). Without adding the country
# code here, a rep's stored number and their actual WhatsApp sender id never
# match as the same string - which broke rep-reply detection entirely for
# any rep whose sheet number was in local format (a real incident: a rep
# replied to an escalation and got treated as an ordinary customer).
_UAE_COUNTRY_CODE = "971"


def to_whatsapp_number(phone: str) -> str:
    """Normalizes a phone number (which may have +, spaces, or dashes) to
    the digits-only international format the WhatsApp Cloud API expects and
    reports back in message["from"]. A leading '00' (international dialing
    prefix) is trimmed first; a local UAE mobile number (leading 0, 9 digits
    after it) then gets the UAE country code added; anything already
    carrying a country code (or ambiguous) is left as-is."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        return _UAE_COUNTRY_CODE + digits[1:]
    return digits


def is_plausible_phone(phone: str) -> bool:
    """Loose E.164-range check: 8-15 digits after stripping formatting.
    Not a full E.164 validator (doesn't check country codes) - just enough
    to catch obviously malformed sheet data (empty, too short, text typos)
    before it reaches the WhatsApp API as an opaque error."""
    digits = to_whatsapp_number(phone)
    return 8 <= len(digits) <= 15
