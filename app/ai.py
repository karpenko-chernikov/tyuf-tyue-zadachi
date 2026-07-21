"""Генерация короткого названия задачи через DeepSeek.

DeepSeek — основной провайдер. OpenAI только как запасной вариант.
Если ключей нет или ошибка API — смысловая эвристика из текста условия.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.utils import title_from_condition

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"


@dataclass
class TitleSuggestion:
    title: str
    source: str  # ai | fallback
    warning: str | None = None


def _provider() -> tuple[str, str, str] | None:
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
        return "DeepSeek"
    return "DeepSeek" if p[0] == "deepseek" else "OpenAI"


def suggest_title(condition: str) -> str | None:
    result = suggest_title_result(condition)
    return result.title if result else None


def suggest_title_result(condition: str) -> TitleSuggestion | None:
    text = (condition or "").strip()
    fallback = title_from_condition(text)
    if not text:
        return TitleSuggestion(fallback, "fallback") if fallback else None

    provider = _provider()
    if not provider:
        return TitleSuggestion(fallback, "fallback") if fallback else None

    name, api_key, url = provider
    try:
        generated = _chat_title(text, api_key=api_key, url=url, provider=name)
        if generated:
            return TitleSuggestion(generated, "ai")
    except urllib.error.HTTPError as e:
        warning = None
        if e.code == 402:
            warning = "DeepSeek: нет баланса на аккаунте — сделано короткое название без нейросети"
        elif e.code in (401, 403):
            warning = "DeepSeek: проблема с ключом — сделано короткое название без нейросети"
        else:
            warning = f"DeepSeek недоступен ({e.code}) — сделано короткое название без нейросети"
        if fallback:
            return TitleSuggestion(fallback, "fallback", warning)
        return None
    except Exception:
        if fallback:
            return TitleSuggestion(
                fallback,
                "fallback",
                "Нейросеть недоступна — сделано короткое название по тексту",
            )
        return None

    if fallback:
        return TitleSuggestion(fallback, "fallback")
    return None


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
        "temperature": 0.4,
        "max_tokens": 40,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Придумай короткое название идеи задачи научного турнира. "
                    "Только суть, 3–8 слов на русском. "
                    "Не копируй начало условия и не используй «Попробуйте», «Нужно», «Придумайте». "
                    "Пример: «Распознать, случайны ли точки на картинке». "
                    "Ответ — только название, без кавычек и точки."
                ),
            },
            {
                "role": "user",
                "content": f"Условие:\n{snippet}",
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
    # если модель всё же вернула простыню — не принимаем
    if len(title) > 80 or title.lower().startswith(("попробуйте", "нужно", "придумайте")):
        return None
    return title or None
