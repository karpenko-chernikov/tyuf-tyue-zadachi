"""Генерация короткого названия задачи.

Если задан OPENAI_API_KEY — через нейросеть (OpenAI).
Иначе (или при ошибке) — эвристика из текста условия.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from app.utils import title_from_condition

DEFAULT_MODEL = "gpt-4o-mini"


def suggest_title(condition: str) -> str | None:
    fallback = title_from_condition(condition)
    text = (condition or "").strip()
    if not text:
        return fallback

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return fallback

    try:
        generated = _openai_title(text, api_key=api_key)
        if generated:
            return generated
    except Exception:
        pass
    return fallback


def ai_title_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def _openai_title(condition: str, *, api_key: str) -> str | None:
    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    # Не гоняем огромные условия целиком
    snippet = condition.strip()
    if len(snippet) > 2500:
        snippet = snippet[:2500] + "…"

    payload = {
        "model": model,
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
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    raw = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if not raw:
        return None

    # На всякий случай убираем кавычки и обрезаем
    title = raw.strip(" «»\"'").splitlines()[0].strip().rstrip(".")
    if len(title) > 80:
        title = title[:77].rstrip(" ,;:") + "…"
    return title or None
