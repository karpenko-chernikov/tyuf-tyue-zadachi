"""Разбор экспорта Telegram Desktop (result.json) для импорта идей и комментариев."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from app.enums import DEFAULT_COMMENT_AUTHOR, DEFAULT_NAZNACHENIE, DEFAULT_TASK_AUTHOR
from app.utils import IDEA_RE, parse_paste

# Упоминание идеи в комментарии без заголовка новой идеи
IDEA_MENTION_RE = re.compile(
    r"(?:^|[\s,.:;—–\-])(?:к\s+)?иде[еия]\s*(?:№|#|номер)?\s*(\d+)",
    re.IGNORECASE,
)


@dataclass
class ImportRow:
    """Одна карточка на экране разбора."""

    index: int
    msg_id: int | None
    kind: str  # idea | comment | skip
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
    has_video: bool = False
    video_url: str | None = None
    # комментарий → draft_N или task:ID
    link_to: str | None = None
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


def load_export_messages(payload: str | bytes | dict) -> list[dict]:
    """Принимает result.json (объект/строка) или список сообщений."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8-sig")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        data = json.loads(text)
    else:
        data = payload

    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list):
            return [m for m in messages if isinstance(m, dict)]
        # иногда кладут один чат в chats
        chats = data.get("chats")
        if isinstance(chats, dict):
            lst = chats.get("list") or []
            out: list[dict] = []
            for chat in lst:
                if isinstance(chat, dict):
                    out.extend(m for m in (chat.get("messages") or []) if isinstance(m, dict))
            if out:
                return out
    raise ValueError("Не похоже на экспорт Telegram: нужен result.json с полем messages")


def classify_messages(
    messages: list[dict],
    *,
    existing_by_minute: dict[str, tuple[int, str]] | None = None,
) -> list[ImportRow]:
    """
    existing_by_minute: 'YYYY-MM-DDTHH:MM' -> (task_id, label)
    """
    existing_by_minute = existing_by_minute or {}

    # Сначала собрать идеи по msg_id для reply
    idea_msg_ids: set[int] = set()
    provisional: list[dict] = []

    for msg in messages:
        if msg.get("type") and msg.get("type") != "message":
            continue
        text = extract_message_text(msg.get("text")).strip()
        if not text and not msg.get("forwarded_from"):
            # пустые / стикеры без текста пропускаем молча
            continue
        msg_id = msg.get("id")
        try:
            msg_id_i = int(msg_id) if msg_id is not None else None
        except (TypeError, ValueError):
            msg_id_i = None

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
        if is_idea and msg_id_i is not None:
            idea_msg_ids.add(msg_id_i)

        provisional.append(
            {
                "msg_id": msg_id_i,
                "text": text,
                "author": author,
                "dt_local": dt_local,
                "forwarded_from": forwarded_s,
                "reply_to": reply_to,
                "is_idea": is_idea,
            }
        )

    rows: list[ImportRow] = []
    idea_drafts: list[ImportRow] = []  # для привязки комментариев
    last_idea_draft: ImportRow | None = None

    for i, item in enumerate(provisional):
        text = item["text"]
        author = item["author"] or DEFAULT_TASK_AUTHOR
        dt_local = item["dt_local"]
        notes: list[str] = []
        dup_id = None
        dup_label = None
        if dt_local and dt_local in existing_by_minute:
            dup_id, dup_label = existing_by_minute[dt_local]
            notes.append(f"Уже в базе: {dup_label}")

        if item["is_idea"]:
            parsed = parse_paste(text)
            draft_key = f"draft_{len(idea_drafts)}"
            row = ImportRow(
                index=i,
                msg_id=item["msg_id"],
                kind="idea",
                confidence="high",
                save=dup_id is None,
                text=text,
                author=author or DEFAULT_TASK_AUTHOR,
                telegram_datetime=dt_local,
                forwarded_from=item["forwarded_from"],
                reply_to_msg_id=item["reply_to"],
                idea_number=parsed.get("idea_number"),
                title=parsed.get("title"),
                condition=parsed.get("condition") or text,
                naznachenie=parsed.get("naznachenie") or DEFAULT_NAZNACHENIE,
                sources=parsed.get("sources"),
                has_video=bool(parsed.get("has_video")),
                video_url=parsed.get("video_url"),
                duplicate_task_id=dup_id,
                duplicate_label=dup_label,
                draft_key=draft_key,
                notes=notes,
            )
            if dup_id is not None:
                row.save = False
                row.kind = "skip"
                row.notes.append("Помечено «пропустить» — совпала дата с существующей задачей")
                # чтобы комментарии могли привязаться к уже существующей задаче
                row.draft_key = f"task:{dup_id}"
            rows.append(row)
            idea_drafts.append(row)
            last_idea_draft = row
            continue

        # Комментарий?
        mentions = _mentioned_idea_numbers(text)
        reply_to_idea = item["reply_to"] is not None and item["reply_to"] in idea_msg_ids
        is_forward = bool(item["forwarded_from"])
        is_comment = is_forward or reply_to_idea or bool(mentions)

        link_to: str | None = None
        confidence = "low"
        kind = "skip"
        save = False

        if reply_to_idea:
            # найти draft по msg_id
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
            # ближайшая предыдущая идея
            if last_idea_draft and last_idea_draft.draft_key:
                link_to = last_idea_draft.draft_key
                save = True
                notes.append("Пересланное → привязка к предыдущей идее")
            else:
                save = False
                notes.append("Пересланное, но идея выше не найдена")
        else:
            kind = "skip"
            confidence = "low"
            save = False
            notes.append("Не похоже на идею/комментарий — проверьте вручную")

        if dup_id is not None and kind == "comment":
            # комментарий с той же минутой, что задача — редкость; не блокируем
            pass

        row = ImportRow(
            index=i,
            msg_id=item["msg_id"],
            kind=kind,
            confidence=confidence,
            save=save and not (kind == "idea" and dup_id),
            text=text,
            author=author or DEFAULT_COMMENT_AUTHOR,
            telegram_datetime=dt_local,
            forwarded_from=item["forwarded_from"],
            reply_to_msg_id=item["reply_to"],
            link_to=link_to,
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
) -> list[ImportRow]:
    messages = load_export_messages(payload)
    return classify_messages(messages, existing_by_minute=existing_by_minute)
