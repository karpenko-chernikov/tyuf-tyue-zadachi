"""Генерация короткого названия задачи через Google Gemini.

Единственный нейросетевой вариант. Если ключа нет или ошибка — эвристика по тексту.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.utils import title_from_condition

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

_SYSTEM = (
    "Придумай короткое название идеи задачи научного турнира. "
    "Только суть, 3–8 слов на русском. "
    "Не копируй начало условия и не используй «Попробуйте», «Нужно», «Придумайте». "
    "Пример: «Распознать, случайны ли точки на картинке». "
    "Ответ — только название, без кавычек и точки."
)


@dataclass
class TitleSuggestion:
    title: str
    source: str  # ai | fallback
    warning: str | None = None


def ai_title_enabled() -> bool:
    return bool(os.getenv("GEMINI_API_KEY", "").strip())


def ai_provider_label() -> str:
    return "Gemini"


def suggest_title(condition: str) -> str | None:
    result = suggest_title_result(condition)
    return result.title if result else None


def suggest_title_result(condition: str) -> TitleSuggestion | None:
    text = (condition or "").strip()
    fallback = title_from_condition(text)
    if not text:
        return TitleSuggestion(fallback, "fallback") if fallback else None

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return TitleSuggestion(fallback, "fallback") if fallback else None

    try:
        generated = _gemini_title(text, api_key=api_key)
        if generated:
            return TitleSuggestion(generated, "ai")
    except urllib.error.HTTPError as e:
        detail = f"Gemini HTTP {e.code}"
        try:
            body = e.read().decode("utf-8", errors="replace")
            err = json.loads(body).get("error", {})
            if isinstance(err, dict) and err.get("message"):
                detail = err["message"][:120]
        except Exception:
            pass
        if fallback:
            return TitleSuggestion(
                fallback,
                "fallback",
                f"Gemini недоступен ({detail}) — сделано короткое название по смыслу",
            )
        return None
    except Exception:
        if fallback:
            return TitleSuggestion(
                fallback,
                "fallback",
                "Gemini недоступен — сделано короткое название по смыслу",
            )
        return None

    if fallback:
        return TitleSuggestion(fallback, "fallback")
    return None


def _clean_model_title(raw: str) -> str | None:
    if not raw:
        return None
    title = raw.strip(" «»\"'").splitlines()[0].strip().rstrip(".")
    title = title.removeprefix("Название:").removeprefix("название:").strip()
    if len(title) > 80 or title.lower().startswith(("попробуйте", "нужно", "придумайте")):
        return None
    return title or None


def _gemini_title(condition: str, *, api_key: str) -> str | None:
    snippet = condition.strip()
    if len(snippet) > 2500:
        snippet = snippet[:2500] + "…"

    url = f"{GEMINI_URL}?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"{_SYSTEM}\n\nУсловие:\n{snippet}"},
                ]
            }
        ],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 40},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    parts = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [])
    )
    raw = "".join(p.get("text", "") for p in parts)
    return _clean_model_title(raw)
