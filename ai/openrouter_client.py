"""
Thin client for OpenRouter's chat completions endpoint (OpenAI-compatible).
Docs: https://openrouter.ai/docs
"""
import time

import requests

from config import config


class OpenRouterError(Exception):
    pass


# Free-tier OpenRouter models get rate-limited upstream by the underlying
# provider under normal traffic - a short retry with backoff smooths over
# these transient blips instead of failing the customer's message outright.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SEC = 1.5


def chat_completion(messages, temperature: float = 0.3, max_tokens: int = 600, model: str = None) -> str:
    if not config.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set in .env")

    last_error = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return _call_once(messages, temperature, max_tokens, model)
        except OpenRouterError as e:
            last_error = e
            if getattr(e, "status_code", None) not in _RETRYABLE_STATUS_CODES or attempt == _MAX_ATTEMPTS:
                raise
            time.sleep(_BACKOFF_BASE_SEC * attempt)

    raise last_error


def _call_once(messages, temperature: float, max_tokens: int, model: str = None) -> str:
    resp = requests.post(
        f"{config.OPENROUTER_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # Optional but recommended by OpenRouter for analytics/rate limits:
            "HTTP-Referer": "https://www.wurth.ae/",
            "X-Title": "Wurth UAE WhatsApp Agent",
        },
        json={
            "model": model or config.OPENROUTER_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=30,
    )

    if resp.status_code != 200:
        err = OpenRouterError(f"OpenRouter error {resp.status_code}: {resp.text}")
        err.status_code = resp.status_code
        raise err

    if not resp.text.strip():
        raise OpenRouterError("OpenRouter returned an empty response body (model may be overloaded/unavailable)")

    try:
        data = resp.json()
    except ValueError as e:
        raise OpenRouterError(f"OpenRouter returned non-JSON response: {resp.text[:500]}") from e

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise OpenRouterError(f"Unexpected OpenRouter response: {data}") from e
