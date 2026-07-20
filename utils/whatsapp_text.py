"""
Shared text-sanitizing helper for WhatsApp template parameters. Meta's
Cloud API rejects (error 132018) any template parameter text containing
newline/tab characters or more than 4 consecutive spaces - a restriction
that only applies to templates, not free-form messages, so this is needed
anywhere user-generated or free-text content (a customer's enquiry, a
company name from the sheet, etc.) is passed into a template's parameters.
"""
import re

_WHITESPACE_RUN = re.compile(r"\s+")


def sanitize_template_param(text: str) -> str:
    """Collapses all whitespace (including newlines/tabs) to single spaces,
    since Meta rejects newlines/tabs and runs of more than 4 spaces in
    template parameter text."""
    return _WHITESPACE_RUN.sub(" ", text or "").strip()
