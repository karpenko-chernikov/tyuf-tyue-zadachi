"""Разбор экспорта Telegram Desktop (result.json) для импорта идей и комментариев."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.enums import DEFAULT_COMMENT_AUTHOR, DEFAULT_NAZNACHENIE, DEFAULT_TASK_AUTHOR, normalize_author
from app.utils import IDEA_RE, extract_urls, parse_paste

# Упоминание идеи в комментарии без заголовка новой идеи
IDEA_MENTION_RE = re.compile(
    r"(?:^|[\s,.:;—–\-])(?:к\s+)?иде[еия]\s*(?:№|#|номер)?\s*(\d+)",
    re.IGNORECASE,
)

# Короткий хвост вокруг ссылок («сюда же», «статья», …) — не отдельная идея
_SOURCE_TAIL_RE = re.compile(
    r"^(?:сюда\s+же|см\.?|смотри|ссылка|источник|вот|ещё|еще|также|тоже)?[\s.,:;!—–\-]*$",
    re.IGNORECASE,
)

DEFAULT_IMPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "imports"
PREFERRED_CHAT_SUBSTRING = "идеи для заданий"


@dataclass
class ImportRow:
    """Одна карточка на экране разбора."""

    index: int
    msg_id: int | None
    kind: str  # idea | comment | media | skip
    confidence: str  # high | medium | low
    save: bool
    text: str
    author: str
    telegram_datetime: str  # YYYY-MM-DDTHH:MM
    forwarded_from: str | None = None
    reply_to_msg_id: int | None = None
    # идея
    idea_number: int | None = None
    title: str | None = None
    condition: str | None = None
    naznachenie: str = DEFAULT_NAZNACHENIE
    sources: str | None = None
    video_url: str | None = None
    # комментарий / media → draft_N или task:ID
    link_to: str | None = None
    # относительные пути к файлам внутри папки экспорта
    media_paths: list[str] = field(default_factory=list)
    # дубликат в БД
    duplicate_task_id: int | None = None
    duplicate_label: str | None = None
    draft_key: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def extract_message_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(raw)


def extract_urls_from_message(msg: dict, text: str | None = None) -> list[str]:
    """Ссылки из текста и text_entities (в т.ч. text_link.href)."""
    found: list[str] = []
    seen: set[str] = set()

    def add(url: str | None) -> None:
        u = (url or "").strip().rstrip(").,;]")
        if not u:
            return
        if not u.startswith("http"):
            if u.startswith("www."):
                u = "https://" + u
            else:
                return
        if u not in seen:
            seen.add(u)
            found.append(u)

    body = text if text is not None else extract_message_text(msg.get("text"))
    for u in extract_urls(body):
        add(u)

    for ent in msg.get("text_entities") or []:
        if not isinstance(ent, dict):
            continue
        et = ent.get("type")
        if et in ("link", "url"):
            add(ent.get("text"))
        elif et == "text_link":
            add(ent.get("href") or ent.get("text"))
        elif et == "email":
            continue

    # text как массив entities (альтернативный формат Desktop)
    raw = msg.get("text")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                if item.get("type") in ("link", "url"):
                    add(item.get("text"))
                href = item.get("href")
                if href:
                    add(href)

    return found


def _merge_sources(*parts: str | None) -> str | None:
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        for line in str(part).splitlines():
            u = line.strip()
            if u and u not in seen:
                seen.add(u)
                out.append(u)
    return "\n".join(out) if out else None


def _sources_blob(urls: list[str]) -> str | None:
    return "\n".join(urls) if urls else None


def _video_from_urls(urls: list[str]) -> str | None:
    return next(
        (
            u
            for u in urls
            if any(x in u.lower() for x in ("youtube", "youtu.be", "instagram", "rutube", "vk.com/video", "vkvideo"))
        ),
        None,
    )


def _text_without_urls(text: str, urls: list[str]) -> str:
    rest = text or ""
    for u in urls:
        rest = rest.replace(u, "")
    rest = re.sub(r"\s+", " ", rest).strip(" \t\n\r.,;:!?—–-")
    return rest


def _enrich_text_with_urls(text: str, urls: list[str]) -> str:
    """Добавляет в текст href из text_link, если самой ссылки в тексте нет."""
    missing = [u for u in urls if u and u not in (text or "")]
    if not missing:
        return text or ""
    base = (text or "").rstrip()
    return f"{base}\n\n" + "\n".join(missing) if base else "\n".join(missing)


def _is_source_followup(text: str, urls: list[str], media_paths: list[str]) -> bool:
    """Сообщение почти только со ссылками → источник к предыдущей идее."""
    if not urls or media_paths:
        return False
    if _looks_like_new_idea(text):
        return False
    rest = _text_without_urls(text, urls)
    if not rest:
        return True
    if len(rest) <= 40 and _SOURCE_TAIL_RE.match(rest):
        return True
    if len(rest) <= 60 and re.search(
        r"стать|вики|wiki|источник|ссылк|habr|perplexity|nature|plos|youtube|rutube",
        rest,
        re.IGNORECASE,
    ):
        return True
    # несколько ссылок и почти нет текста
    if len(urls) >= 2 and len(rest) <= 30:
        return True
    return False

def extract_media_paths(msg: dict) -> list[str]:
    paths: list[str] = []
    for key in ("photo", "file"):
        val = msg.get(key)
        if not isinstance(val, str):
            continue
        val = val.strip()
        if not val:
            continue
        low = val.lower()
        if "not included" in low or "file not included" in low:
            continue
        if val not in paths:
            paths.append(val)
    return paths


def parse_export_datetime(msg: dict) -> datetime | None:
    date_s = msg.get("date")
    if isinstance(date_s, str) and date_s.strip():
        cleaned = date_s.strip().replace("Z", "")
        if "+" in cleaned[10:]:
            cleaned = cleaned.split("+", 1)[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(cleaned[:19], fmt)
            except ValueError:
                continue
    unixtime = msg.get("date_unixtime")
    if unixtime is not None:
        try:
            return datetime.fromtimestamp(int(unixtime))
        except (TypeError, ValueError, OSError):
            pass
    return None


def _sender_name(msg: dict) -> str:
    raw = msg.get("from") or msg.get("author") or ""
    if isinstance(raw, dict):
        return (raw.get("name") or raw.get("title") or "").strip()
    return str(raw).strip()


def _looks_like_new_idea(text: str) -> bool:
    """Первая содержательная строка — заголовок «Идея N»."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        return bool(IDEA_RE.match(line))
    return False


def _mentioned_idea_numbers(text: str) -> list[int]:
    return [int(m.group(1)) for m in IDEA_MENTION_RE.finditer(text or "")]


def _extract_complete_objects(raw: str, start_hint: int) -> list[str]:
    """Вытаскивает полные {...} объекты из возможно обрезанного JSON-массива."""
    objects: list[str] = []
    i = start_hint
    n = len(raw)
    while i < n:
        while i < n and raw[i] in " \t\r\n,":
            i += 1
        if i >= n or raw[i] != "{":
            break
        depth = 0
        in_str = False
        esc = False
        start = i
        for j in range(i, n):
            ch = raw[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    objects.append(raw[start : j + 1])
                    i = j + 1
                    break
        else:
            break
    return objects


def load_json_lenient(payload: str | bytes | dict) -> dict | list:
    if isinstance(payload, dict) or isinstance(payload, list):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8-sig")
    text = (payload or "").strip()
    if not text:
        raise ValueError("Пустой JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Обрезанный экспорт: собираем только полностью закрытые чаты
        marker = text.find('"list"')
        if marker < 0:
            raise
        bracket = text.find("[", marker)
        if bracket < 0:
            raise
        chunks = _extract_complete_objects(text, bracket + 1)
        chats = []
        for chunk in chunks:
            try:
                chats.append(json.loads(chunk))
            except json.JSONDecodeError:
                continue
        if not chats:
            raise ValueError("JSON обрезан и не удалось вытащить ни одного полного чата")
        return {"chats": {"list": chats}, "about": "lenient parse of truncated export"}


def list_chats(data: dict | list) -> list[dict]:
    if isinstance(data, list):
        return [{"name": "(список сообщений)", "messages": data, "index": 0}]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("messages"), list):
        return [{"name": data.get("name") or "(чат)", "messages": data["messages"], "index": 0}]
    chats = data.get("chats")
    if isinstance(chats, dict):
        out = []
        for i, chat in enumerate(chats.get("list") or []):
            if isinstance(chat, dict):
                out.append(
                    {
                        "name": chat.get("name") or f"Чат {i + 1}",
                        "type": chat.get("type"),
                        "messages": chat.get("messages") or [],
                        "index": i,
                    }
                )
        return out
    return []


def pick_chat(chats: list[dict], chat_name: str | None = None) -> dict:
    if not chats:
        raise ValueError("В экспорте нет чатов")
    if chat_name:
        needle = chat_name.strip().lower()
        for c in chats:
            if (c.get("name") or "").strip().lower() == needle:
                return c
        for c in chats:
            if needle in (c.get("name") or "").lower():
                return c
    for c in chats:
        if PREFERRED_CHAT_SUBSTRING in (c.get("name") or "").lower():
            return c
    # чат с наибольшим числом «Идея N»
    best = None
    best_score = -1
    for c in chats:
        score = 0
        for m in c.get("messages") or []:
            if _looks_like_new_idea(extract_message_text(m.get("text"))):
                score += 1
        if score > best_score:
            best_score = score
            best = c
    return best or chats[0]


def load_export_messages(
    payload: str | bytes | dict,
    *,
    chat_name: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Возвращает (messages выбранного чата, список чатов для UI)."""
    data = load_json_lenient(payload)
    chats = list_chats(data)
    if not chats:
        raise ValueError("Не похоже на экспорт Telegram: нет messages/chats")
    chosen = pick_chat(chats, chat_name=chat_name)
    messages = [m for m in (chosen.get("messages") or []) if isinstance(m, dict)]
    return messages, chats


def find_local_export_dirs() -> list[Path]:
    if not DEFAULT_IMPORT_DIR.is_dir():
        return []
    dirs = []
    for p in sorted(DEFAULT_IMPORT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_dir():
            continue
        if (p / "result_ideas.json").is_file() or (p / "result.json").is_file():
            dirs.append(p)
    return dirs


def read_local_export_json(export_dir: Path) -> tuple[str, Path]:
    """Читает JSON из папки экспорта; предпочитает result_ideas.json."""
    for name in ("result_ideas.json", "result.json"):
        path = export_dir / name
        if path.is_file():
            return path.read_text(encoding="utf-8-sig"), path
    raise FileNotFoundError(f"В {export_dir} нет result.json")


def classify_messages(
    messages: list[dict],
    *,
    existing_by_minute: dict[str, tuple[int, str]] | None = None,
    processed_msg_ids: set[int] | None = None,
) -> list[ImportRow]:
    """
    existing_by_minute: 'YYYY-MM-DDTHH:MM' -> (task_id, label)
    processed_msg_ids: уже разобранные msg id — не показываем снова
    """
    existing_by_minute = existing_by_minute or {}
    processed_msg_ids = processed_msg_ids or set()

    idea_msg_ids: set[int] = set()
    provisional: list[dict] = []

    for msg in messages:
        if msg.get("type") and msg.get("type") != "message":
            continue
        text = extract_message_text(msg.get("text")).strip()
        media_paths = extract_media_paths(msg)
        if not text and not msg.get("forwarded_from") and not media_paths:
            continue
        msg_id = msg.get("id")
        try:
            msg_id_i = int(msg_id) if msg_id is not None else None
        except (TypeError, ValueError):
            msg_id_i = None

        if msg_id_i is not None and msg_id_i in processed_msg_ids:
            continue

        dt = parse_export_datetime(msg)
        dt_local = dt.strftime("%Y-%m-%dT%H:%M") if dt else ""
        author = _sender_name(msg)
        forwarded = msg.get("forwarded_from")
        forwarded_s = str(forwarded).strip() if forwarded else None
        reply_raw = msg.get("reply_to_message_id")
        try:
            reply_to = int(reply_raw) if reply_raw is not None else None
        except (TypeError, ValueError):
            reply_to = None

        is_idea = _looks_like_new_idea(text)
        # Не скрываем по дате: в одну минуту бывает несколько идей.
        # Уже разобранные сообщения отфильтрованы через processed_msg_ids.

        if is_idea and msg_id_i is not None:
            idea_msg_ids.add(msg_id_i)

        urls = extract_urls_from_message(msg, text)
        provisional.append(
            {
                "msg_id": msg_id_i,
                "text": text,
                "author": author,
                "dt_local": dt_local,
                "forwarded_from": forwarded_s,
                "reply_to": reply_to,
                "is_idea": is_idea,
                "media_paths": media_paths,
                "urls": urls,
            }
        )

    rows: list[ImportRow] = []
    idea_drafts: list[ImportRow] = []
    last_idea_draft: ImportRow | None = None

    for i, item in enumerate(provisional):
        text = item["text"]
        author = normalize_author(item["author"], default=DEFAULT_TASK_AUTHOR)
        dt_local = item["dt_local"]
        media_paths = item["media_paths"]
        notes: list[str] = []
        dup_id = None
        dup_label = None
        if dt_local and dt_local in existing_by_minute:
            dup_id, dup_label = existing_by_minute[dt_local]
            notes.append(f"Уже в базе: {dup_label}")

        urls: list[str] = list(item.get("urls") or [])
        # text_link.href часто не совпадает с видимым текстом («здесь», «eyes»)
        if urls and not item["is_idea"]:
            text = _enrich_text_with_urls(text, urls)

        if item["is_idea"]:
            parsed = parse_paste(text)
            draft_key = f"draft_{len(idea_drafts)}"
            if media_paths:
                notes.append(f"Файлов: {len(media_paths)}")
            sources = _merge_sources(parsed.get("sources"), _sources_blob(urls))
            video_url = _video_from_urls(urls) or parsed.get("video_url")
            if sources:
                notes.append(f"Ссылок: {len(sources.splitlines())}")
            # убрать URL из условия, если они ушли в источники
            condition = parsed.get("condition") or text
            if condition and urls:
                cleaned = condition
                for u in urls:
                    cleaned = cleaned.replace(u, "")
                cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
                if cleaned:
                    condition = cleaned
            row = ImportRow(
                index=i,
                msg_id=item["msg_id"],
                kind="idea",
                confidence="high",
                save=True,
                text=text,
                author=author or DEFAULT_TASK_AUTHOR,
                telegram_datetime=dt_local,
                forwarded_from=item["forwarded_from"],
                reply_to_msg_id=item["reply_to"],
                idea_number=parsed.get("idea_number"),
                title=parsed.get("title"),
                condition=condition,
                naznachenie=parsed.get("naznachenie") or DEFAULT_NAZNACHENIE,
                sources=sources,
                video_url=video_url,
                media_paths=media_paths,
                duplicate_task_id=dup_id,
                duplicate_label=dup_label,
                draft_key=draft_key,
                notes=notes,
            )
            rows.append(row)
            idea_drafts.append(row)
            last_idea_draft = row
            continue

        mentions = _mentioned_idea_numbers(text)
        reply_to_idea = item["reply_to"] is not None and item["reply_to"] in idea_msg_ids
        is_forward = bool(item["forwarded_from"])
        media_only = (not text) and bool(media_paths)
        source_followup = _is_source_followup(text, urls, media_paths)

        link_to: str | None = None
        confidence = "low"
        kind = "skip"
        save = False
        author = normalize_author(item["author"], default=DEFAULT_COMMENT_AUTHOR)
        row_sources: str | None = None

        if source_followup and not mentions:
            # ссылки → в источники идеи (ответ / предыдущая); строку не сохраняем отдельно
            target: ImportRow | None = None
            if reply_to_idea:
                for idea in idea_drafts:
                    if idea.msg_id == item["reply_to"]:
                        target = idea
                        break
            if target is None:
                target = last_idea_draft
            if target and target.draft_key:
                target.sources = _merge_sources(target.sources, _sources_blob(urls))
                video_url = _video_from_urls(urls)
                if video_url and not target.video_url:
                    target.video_url = video_url
                kind = "skip"
                confidence = "high"
                save = False
                link_to = target.draft_key
                row_sources = _sources_blob(urls)
                notes.append("Ссылки добавлены к источникам идеи")
            else:
                kind = "skip"
                confidence = "medium"
                save = False
                row_sources = _sources_blob(urls)
                notes.append("Похоже на ссылки-источники, но идея выше не найдена")
        elif media_only and not is_forward and not reply_to_idea and not mentions:
            # фото/видео без текста → к предыдущей идее как вложение задачи
            kind = "media"
            confidence = "medium"
            if last_idea_draft and last_idea_draft.draft_key:
                link_to = last_idea_draft.draft_key
                save = True
                notes.append("Медиа без текста → к предыдущей идее")
            else:
                notes.append("Медиа без текста, идея выше не найдена")
        elif reply_to_idea:
            for idea in idea_drafts:
                if idea.msg_id == item["reply_to"]:
                    link_to = idea.draft_key
                    break
            kind = "comment"
            confidence = "high"
            save = True
            notes.append("Ответ на сообщение-идею")
        elif mentions:
            num = mentions[0]
            for idea in reversed(idea_drafts):
                if idea.idea_number == num:
                    link_to = idea.draft_key
                    break
            kind = "comment"
            confidence = "medium" if link_to else "low"
            save = bool(link_to)
            notes.append(f"Упоминание идеи {num}")
        elif is_forward:
            kind = "comment"
            confidence = "medium"
            if last_idea_draft and last_idea_draft.draft_key:
                link_to = last_idea_draft.draft_key
                save = True
                notes.append("Пересланное → привязка к предыдущей идее")
            else:
                notes.append("Пересланное, но идея выше не найдена")
        else:
            kind = "skip"
            confidence = "low"
            save = False
            notes.append("Не похоже на идею/комментарий — проверьте вручную")

        if media_paths:
            notes.append(f"Файлов: {len(media_paths)}")

        row = ImportRow(
            index=i,
            msg_id=item["msg_id"],
            kind=kind,
            confidence=confidence,
            save=save,
            text=text,
            author=author or DEFAULT_COMMENT_AUTHOR,
            telegram_datetime=dt_local,
            forwarded_from=item["forwarded_from"],
            reply_to_msg_id=item["reply_to"],
            sources=row_sources,
            link_to=link_to,
            media_paths=media_paths,
            duplicate_task_id=dup_id,
            duplicate_label=dup_label,
            notes=notes,
        )
        rows.append(row)

    return rows


def parse_telegram_export(
    payload: str | bytes | dict,
    *,
    existing_by_minute: dict[str, tuple[int, str]] | None = None,
    processed_msg_ids: set[int] | None = None,
    chat_name: str | None = None,
) -> tuple[list[ImportRow], list[dict]]:
    messages, chats = load_export_messages(payload, chat_name=chat_name)
    rows = classify_messages(
        messages,
        existing_by_minute=existing_by_minute,
        processed_msg_ids=processed_msg_ids,
    )
    return rows, chats
