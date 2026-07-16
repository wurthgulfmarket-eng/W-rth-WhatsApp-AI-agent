"""
Thin client for OpenRouter's chat completions endpoint (OpenAI-compatible).
Docs: https://openrouter.ai/docs
"""
import requests

from config import config


class OpenRouterError(Exception):
    pass


def chat_completion(messages, temperature: float = 0.3, max_tokens: int = 600, model: str = None) -> str:
    if not config.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set in .env")

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
        raise OpenRouterError(f"OpenRouter error {resp.status_code}: {resp.text}")

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
