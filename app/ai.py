"""Генерация короткого названия задачи.

Порядок:
1) Ollama локально — полностью бесплатно, без ключей (если установлена)
2) Gemini — бесплатный облачный ключ Google
3) DeepSeek / OpenAI — если настроены
4) Эвристика по тексту условия
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.utils import title_from_condition

OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "").strip()  # пусто = взять первую подходящую

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"

_SYSTEM = (
    "Придумай короткое название идеи задачи научного турнира. "
    "Только суть, 3–8 слов на русском. "
    "Не копируй начало условия и не используй «Попробуйте», «Нужно», «Придумайте». "
    "Пример: «Распознать, случайны ли точки на картинке». "
    "Ответ — только название, без кавычек и точки."
)

_PREFERRED_OLLAMA = (
    "qwen2.5:3b",
    "qwen2.5:7b",
    "llama3.2",
    "llama3.2:3b",
    "gemma2:2b",
    "gemma2:9b",
    "mistral",
    "phi3",
)


@dataclass
class TitleSuggestion:
    title: str
    source: str  # ai | fallback
    warning: str | None = None


def _ollama_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def ollama_available() -> bool:
    return bool(_ollama_models())


def _pick_ollama_model(names: list[str]) -> str | None:
    if OLLAMA_MODEL and any(OLLAMA_MODEL == n or n.startswith(OLLAMA_MODEL + ":") for n in names):
        return next(n for n in names if n == OLLAMA_MODEL or n.startswith(OLLAMA_MODEL + ":"))
    if OLLAMA_MODEL:
        return OLLAMA_MODEL  # пусть Ollama сама ответит, если модели нет
    lower = {n.lower(): n for n in names}
    for pref in _PREFERRED_OLLAMA:
        if pref.lower() in lower:
            return lower[pref.lower()]
        for n in names:
            if n.lower().startswith(pref.lower().split(":")[0]):
                return n
    return names[0] if names else None


def ai_title_enabled() -> bool:
    if ollama_available():
        return True
    if os.getenv("GEMINI_API_KEY", "").strip():
        return True
    if os.getenv("DEEPSEEK_API_KEY", "").strip():
        return True
    if os.getenv("OPENAI_API_KEY", "").strip():
        return True
    return False


def ai_provider_label() -> str:
    if ollama_available():
        return "Ollama"
    if os.getenv("GEMINI_API_KEY", "").strip():
        return "Gemini"
    if os.getenv("DEEPSEEK_API_KEY", "").strip():
        return "DeepSeek"
    if os.getenv("OPENAI_API_KEY", "").strip():
        return "OpenAI"
    return "Ollama"


def suggest_title(condition: str) -> str | None:
    result = suggest_title_result(condition)
    return result.title if result else None


def suggest_title_result(condition: str) -> TitleSuggestion | None:
    text = (condition or "").strip()
    fallback = title_from_condition(text)
    if not text:
        return TitleSuggestion(fallback, "fallback") if fallback else None

    errors: list[str] = []

    # 1) Ollama — бесплатно локально
    models = _ollama_models()
    if models:
        model = _pick_ollama_model(models)
        if model:
            try:
                generated = _ollama_title(text, model=model)
                if generated:
                    return TitleSuggestion(generated, "ai")
            except Exception as e:
                errors.append(f"Ollama: {e}")

    # 2) Gemini free
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        try:
            generated = _gemini_title(text, api_key=gemini_key)
            if generated:
                return TitleSuggestion(generated, "ai")
        except urllib.error.HTTPError as e:
            errors.append(f"Gemini HTTP {e.code}")
        except Exception as e:
            errors.append(f"Gemini: {e}")

    # 3) DeepSeek
    deepseek = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if deepseek:
        try:
            generated = _openai_compat_title(
                text, api_key=deepseek, url=DEEPSEEK_URL, model=DEEPSEEK_MODEL
            )
            if generated:
                return TitleSuggestion(generated, "ai")
        except urllib.error.HTTPError as e:
            if e.code == 402:
                errors.append("DeepSeek: нет баланса")
            else:
                errors.append(f"DeepSeek HTTP {e.code}")
        except Exception as e:
            errors.append(f"DeepSeek: {e}")

    # 4) OpenAI
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key:
        try:
            model = os.getenv("OPENAI_MODEL", OPENAI_MODEL).strip() or OPENAI_MODEL
            generated = _openai_compat_title(
                text, api_key=openai_key, url=OPENAI_URL, model=model
            )
            if generated:
                return TitleSuggestion(generated, "ai")
        except Exception as e:
            errors.append(f"OpenAI: {e}")

    if fallback:
        warning = None
        if errors:
            warning = (
                "Нейросеть недоступна ("
                + "; ".join(errors[:2])
                + ") — сделано короткое название по смыслу"
            )
        elif not ai_title_enabled():
            warning = None  # тихий fallback без ключей/ollama
        return TitleSuggestion(fallback, "fallback", warning)
    return None


def _clean_model_title(raw: str) -> str | None:
    if not raw:
        return None
    title = raw.strip(" «»\"'").splitlines()[0].strip().rstrip(".")
    title = title.removeprefix("Название:").removeprefix("название:").strip()
    if len(title) > 80 or title.lower().startswith(("попробуйте", "нужно", "придумайте")):
        return None
    return title or None


def _user_prompt(condition: str) -> str:
    snippet = condition.strip()
    if len(snippet) > 2500:
        snippet = snippet[:2500] + "…"
    return f"{_SYSTEM}\n\nУсловие:\n{snippet}"


def _ollama_title(condition: str, *, model: str) -> str | None:
    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 40},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"Условие:\n{condition.strip()[:2500]}"},
        ],
    }
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = (data.get("message") or {}).get("content", "")
    return _clean_model_title(raw)


def _gemini_title(condition: str, *, api_key: str) -> str | None:
    model = os.getenv("GEMINI_MODEL", GEMINI_MODEL).strip() or GEMINI_MODEL
    url = GEMINI_URL.format(model=model, key=api_key)
    payload = {
        "contents": [{"parts": [{"text": _user_prompt(condition)}]}],
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


def _openai_compat_title(
    condition: str, *, api_key: str, url: str, model: str
) -> str | None:
    snippet = condition.strip()
    if len(snippet) > 2500:
        snippet = snippet[:2500] + "…"
    payload = {
        "model": model,
        "temperature": 0.4,
        "max_tokens": 40,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"Условие:\n{snippet}"},
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
    return _clean_model_title(raw)
