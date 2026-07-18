"""
Shared phone number helpers - used by both sheets_client.py (validating
Rep Phone values loaded from the Google Sheet) and main.py (validating a
target number immediately before a WhatsApp API call), so the two don't
duplicate normalization/validation logic.
"""


def to_whatsapp_number(phone: str) -> str:
    """Normalizes a phone number (which may have +, spaces, or dashes) to
    the digits-only format the WhatsApp Cloud API expects."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def is_plausible_phone(phone: str) -> bool:
    """Loose E.164-range check: 8-15 digits after stripping formatting.
    Not a full E.164 validator (doesn't check country codes) - just enough
    to catch obviously malformed sheet data (empty, too short, text typos)
    before it reaches the WhatsApp API as an opaque error."""
    digits = to_whatsapp_number(phone)
    return 8 <= len(digits) <= 15
