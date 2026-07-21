"""
Thin client for Groq's Whisper transcription endpoint (OpenAI-compatible
audio API), used to turn a customer's WhatsApp voice note into text so it
can go through the same reply/lead-detection pipeline as a typed message.
Docs: https://console.groq.com/docs/speech-to-text
"""
import requests

from config import config

GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


class TranscriptionError(Exception):
    pass


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    if not config.GROQ_API_KEY:
        raise TranscriptionError("GROQ_API_KEY is not set - voice note transcription is unavailable")

    resp = requests.post(
        GROQ_TRANSCRIPTION_URL,
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        files={"file": ("voice_note.ogg", audio_bytes, mime_type)},
        data={"model": config.GROQ_TRANSCRIPTION_MODEL, "response_format": "json"},
        timeout=30,
    )

    if resp.status_code != 200:
        raise TranscriptionError(f"Groq transcription failed {resp.status_code}: {resp.text}")

    try:
        data = resp.json()
    except ValueError as e:
        raise TranscriptionError(f"Groq returned non-JSON response: {resp.text[:500]}") from e

    text = (data.get("text") or "").strip()
    if not text:
        raise TranscriptionError("Groq returned an empty transcription (voice note may be silent or unclear)")
    return text
