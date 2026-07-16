"""
Client for sending messages via the Meta WhatsApp Cloud API.
Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""
import requests

from config import config


class WhatsAppError(Exception):
    pass


def _base_url():
    return f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{config.WHATSAPP_PHONE_NUMBER_ID}/messages"


def _headers():
    return {
        "Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def send_text_message(to: str, body: str) -> dict:
    """to: recipient phone number in international format, no leading +, e.g. '9715XXXXXXXX'"""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body, "preview_url": False},
    }
    resp = requests.post(_base_url(), headers=_headers(), json=payload, timeout=15)
    if resp.status_code >= 300:
        raise WhatsAppError(f"WhatsApp send failed {resp.status_code}: {resp.text}")
    return resp.json()


def mark_as_read(message_id: str) -> dict:
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    resp = requests.post(_base_url(), headers=_headers(), json=payload, timeout=15)
    if resp.status_code >= 300:
        raise WhatsAppError(f"WhatsApp mark-as-read failed {resp.status_code}: {resp.text}")
    return resp.json()


def get_media_url(media_id: str) -> str:
    """Step 1 of downloading inbound media (images, voice notes, etc.):
    resolve a media_id to a short-lived download URL."""
    resp = requests.get(
        f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{media_id}",
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code >= 300:
        raise WhatsAppError(f"WhatsApp get_media_url failed {resp.status_code}: {resp.text}")
    return resp.json()["url"]


def download_media(media_id: str) -> bytes:
    """Step 2: download the actual media bytes. Requires the same auth header
    as the API itself, not just a plain GET."""
    url = get_media_url(media_id)
    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code >= 300:
        raise WhatsAppError(f"WhatsApp media download failed {resp.status_code}: {resp.text}")
    return resp.content


def send_template_message(to: str, template_name: str, language_code: str = "en", components: list = None) -> dict:
    """Used for outbound marketing broadcasts (must use a Meta-approved template)."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        },
    }
    if components:
        payload["template"]["components"] = components

    resp = requests.post(_base_url(), headers=_headers(), json=payload, timeout=15)
    if resp.status_code >= 300:
        raise WhatsAppError(f"WhatsApp template send failed {resp.status_code}: {resp.text}")
    return resp.json()
