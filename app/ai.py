"""Генерация короткого названия задачи.

Предпочтительно DeepSeek (дешёвый/почти бесплатный).
Иначе OpenAI. Если ключей нет или ошибка — эвристика из текста условия.
"""

from __future__ import annotations

import json
import os
import urllib.request

from app.utils import title_from_condition

# DeepSeek: https://platform.deepseek.com — ключ бесплатно, на старте дают кредиты
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"


def _provider() -> tuple[str, str, str] | None:
    """Вернёт (name, api_key, url) или None."""
    deepseek = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if deepseek:
        return ("deepseek", deepseek, DEEPSEEK_URL)
    openai = os.getenv("OPENAI_API_KEY", "").strip()
    if openai:
        return ("openai", openai, OPENAI_URL)
    return None


def ai_title_enabled() -> bool:
    return _provider() is not None


def ai_provider_label() -> str:
    p = _provider()
    if not p:
        return ""
    return "DeepSeek" if p[0] == "deepseek" else "OpenAI"


def suggest_title(condition: str) -> str | None:
    fallback = title_from_condition(condition)
    text = (condition or "").strip()
    if not text:
        return fallback

    provider = _provider()
    if not provider:
        return fallback

    name, api_key, url = provider
    try:
        generated = _chat_title(text, api_key=api_key, url=url, provider=name)
        if generated:
            return generated
    except Exception:
        pass
    return fallback


def _model_for(provider: str) -> str:
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_MODEL", DEEPSEEK_MODEL).strip() or DEEPSEEK_MODEL
    return os.getenv("OPENAI_MODEL", OPENAI_MODEL).strip() or OPENAI_MODEL


def _chat_title(condition: str, *, api_key: str, url: str, provider: str) -> str | None:
    snippet = condition.strip()
    if len(snippet) > 2500:
        snippet = snippet[:2500] + "…"

    payload = {
        "model": _model_for(provider),
        "temperature": 0.3,
        "max_tokens": 60,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты придумываешь короткие понятные названия для идей задач "
                    "научных турниров (ТЮФ/ТЮЕ). "
                    "Ответь только названием на русском, без кавычек и точки в конце. "
                    "Длина — примерно 4–10 слов, максимум 70 символов. "
                    "Не пиши «Идея», номера и пояснения."
                ),
            },
            {
                "role": "user",
                "content": f"Условие задачи:\n\n{snippet}",
            },
        ],
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    raw = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if not raw:
        return None

    title = raw.strip(" «»\"'").splitlines()[0].strip().rstrip(".")
    if len(title) > 80:
        title = title[:77].rstrip(" ,;:") + "…"
    return title or None
