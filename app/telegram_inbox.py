"""Входящие идеи из чата идей; подтверждение в личке с ботом."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta

from app.database import DATA_DIR, SessionLocal
from app.enums import Status, normalize_author
from app.files import save_bytes_attachment
from app.history import record_comment_added, record_created, record_file_added
from app.models import Comment, Task, TgInboxProcessed, TgPending
from app.tags import infer_tag_slugs_for_new_task, tags_by_slugs
from app.telegram_bot import (
    _chat_id,
    answer_callback_query,
    app_base_url,
    delete_webhook,
    download_telegram_file,
    get_me,
    get_updates,
    notify_new_task,
    send_message,
    tg_token_configured,
)
from app.utils import IDEA_RE, parse_paste

logger = logging.getLogger(__name__)

OFFSET_PATH = DATA_DIR / "tg_inbox_offset.json"
PENDING_TTL = timedelta(minutes=30)
HELP_TEXT = (
    "Бот работает в две стороны.\n\n"
    "1) Задача на сайте → сообщение в чат идей.\n\n"
    "2) Идея в чате идей → превью сюда → «Сохранить».\n\n"
    "Формат идеи:\n"
    "Идея №12\n"
    "Название\n"
    "Условие…\n"
    "Теги: ТЮФ, ТЮЕ\n"
    "(теги по-русски, можно несколько; "
    "если строку не указать — будут ТЮФ и ТЮЕ)\n"
    "Можно так: Теги: Капитанка  или  #ТЮФ #SF4\n"
    "+ медиа при необходимости\n\n"
    "Комментарий:\n"
    "К идее №12\n"
    "Текст…\n\n"
    "Кнопки: «Сохранить» / «Отмена» (или /ok / /cancel).\n"
    "Нужен /start, иначе личка недоступна."
)

COMMENT_RE = re.compile(
    r"^(?:к\s+идее|комментарий\s+к\s+(?:идее)?)\s*(?:№|#)?\s*(\d+)\b",
    re.IGNORECASE,
)

_bot_user_id: int | None = None
_offset_lock = threading.Lock()


def inbox_poll_loop(stop: threading.Event) -> None:
    if not tg_token_configured():
        return
    # короткая пауза после старта
    if stop.wait(2):
        return
    delete_webhook()
    _ensure_bot_id()
    offset = _load_offset()
    while not stop.is_set():
        try:
            updates = get_updates(offset=offset, timeout=25)
            for upd in updates:
                uid = upd.get("update_id")
                if isinstance(uid, int):
                    offset = uid + 1
                    _save_offset(offset)
                try:
                    _handle_update(upd)
                except Exception:
                    logger.exception("Inbox update failed: %s", uid)
        except Exception:
            logger.exception("Inbox poll iteration failed")
            if stop.wait(5):
                break


def _ensure_bot_id() -> None:
    global _bot_user_id
    if _bot_user_id is not None:
        return
    me = get_me()
    if me and me.get("id") is not None:
        _bot_user_id = int(me["id"])


def _load_offset() -> int | None:
    if not OFFSET_PATH.is_file():
        return None
    try:
        data = json.loads(OFFSET_PATH.read_text(encoding="utf-8"))
        val = data.get("offset")
        return int(val) if val is not None else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _save_offset(offset: int) -> None:
    with _offset_lock:
        try:
            OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
            OFFSET_PATH.write_text(
                json.dumps({"offset": offset}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("Cannot save inbox offset")


def _handle_update(upd: dict) -> None:
    if upd.get("callback_query"):
        _handle_callback(upd["callback_query"])
        return
    msg = upd.get("message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    if frm.get("is_bot"):
        return
    if _bot_user_id is not None and frm.get("id") == _bot_user_id:
        return

    chat_type = chat.get("type") or ""
    if chat_type == "private":
        _handle_private(msg)
        return
    if chat_type in ("group", "supergroup") and _is_ideas_group(chat):
        _handle_ideas_group(msg)
        return


def _is_ideas_group(chat: dict) -> bool:
    target = (_chat_id() or "").strip()
    if not target:
        return False
    return str(chat.get("id")) == target


def _handle_private(msg: dict) -> None:
    """Личка: справка, подтверждение, доп. медиа к превью. Идеи из чата идей."""
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    chat_id = str(chat.get("id"))
    user_id = str(frm.get("id"))
    message_id = msg.get("message_id")
    if message_id is None:
        return

    db = SessionLocal()
    try:
        if _already_processed(db, chat_id, int(message_id)):
            return

        text = (msg.get("text") or msg.get("caption") or "").strip()
        media = _extract_media_refs(msg)
        lower = text.lower()

        if lower in ("/start", "/help", "start", "help") or lower.startswith("/start@"):
            send_message(text=HELP_TEXT, chat_id=chat_id)
            pending = _get_pending(db, user_id)
            if pending:
                _resend_pending_preview(db, pending, chat_id=chat_id)
            _mark_processed(db, chat_id, int(message_id), "help")
            db.commit()
            return

        if lower in ("/ok", "ok") or lower.startswith("/ok@"):
            _confirm_pending(db, user_id=user_id, chat_id=chat_id)
            _mark_processed(db, chat_id, int(message_id), "ok")
            db.commit()
            return

        if lower in ("/cancel", "cancel") or lower.startswith("/cancel@"):
            _clear_pending(db, user_id)
            send_message(text="Отменено.", chat_id=chat_id)
            _mark_processed(db, chat_id, int(message_id), "cancel")
            db.commit()
            return

        if media and not text:
            pending = _get_pending(db, user_id)
            if pending:
                payload = json.loads(pending.payload_json)
                files = list(payload.get("files") or [])
                files.extend(media)
                payload["files"] = files
                pending.payload_json = json.dumps(payload, ensure_ascii=False)
                pending.expires_at = datetime.utcnow() + PENDING_TTL
                send_message(
                    text=f"Файл добавлен к превью ({len(files)} шт.). Нажмите «Сохранить» или /ok.",
                    chat_id=chat_id,
                    reply_markup=_confirm_keyboard(),
                )
                _mark_processed(db, chat_id, int(message_id), "media")
                db.commit()
                return

        # В личке идеи/комментарии не создаём — только напоминаем про общий чат
        send_message(text=HELP_TEXT, chat_id=chat_id)
        _mark_processed(db, chat_id, int(message_id), "help")
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("handle private message failed")
        send_message(text="Не удалось обработать сообщение. Попробуйте ещё раз.", chat_id=chat_id)
    finally:
        db.close()


def _handle_ideas_group(msg: dict) -> None:
    """Сообщение в чате идей → превью автору в личку (без ответов в группу)."""
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    group_chat_id = str(chat.get("id"))
    user_id = str(frm.get("id") or "")
    message_id = msg.get("message_id")
    if message_id is None or not user_id:
        return

    db = SessionLocal()
    try:
        if _already_processed(db, group_chat_id, int(message_id)):
            return

        text = (msg.get("text") or msg.get("caption") or "").strip()
        media = _extract_media_refs(msg)

        if _is_comment(text):
            ok = _start_comment_preview(
                db,
                user_id=user_id,
                reply_chat_id=user_id,
                text=text,
                media=media,
                frm=frm,
                msg=msg,
            )
            _mark_processed(
                db,
                group_chat_id,
                int(message_id),
                "comment_preview" if ok else "comment_dm_fail",
            )
            db.commit()
            return

        if _is_idea(text):
            ok = _start_idea_preview(
                db,
                user_id=user_id,
                reply_chat_id=user_id,
                text=text,
                media=media,
                frm=frm,
                msg=msg,
            )
            _mark_processed(
                db,
                group_chat_id,
                int(message_id),
                "idea_preview" if ok else "idea_dm_fail",
            )
            db.commit()
            return

        # Обычные сообщения чата — не трогаем
    except Exception:
        db.rollback()
        logger.exception("handle ideas group message failed")
    finally:
        db.close()


def _handle_callback(cb: dict) -> None:
    data = (cb.get("data") or "").strip()
    cq_id = cb.get("id") or ""
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    frm = cb.get("from") or {}
    user_id = str(frm.get("id") or "")
    answer_callback_query(cq_id)

    db = SessionLocal()
    try:
        if data == "tg:cancel":
            _clear_pending(db, user_id)
            send_message(text="Отменено.", chat_id=chat_id)
            db.commit()
            return
        if data == "tg:ok":
            _confirm_pending(db, user_id=user_id, chat_id=chat_id)
            db.commit()
            return
        if data.startswith("tg:com:"):
            try:
                task_id = int(data.split(":")[2])
            except (IndexError, ValueError):
                send_message(text="Некорректный выбор задачи.", chat_id=chat_id)
                db.commit()
                return
            pending = _get_pending(db, user_id)
            if not pending or pending.kind != "comment":
                send_message(text="Нет активного превью комментария.", chat_id=chat_id)
                db.commit()
                return
            payload = json.loads(pending.payload_json)
            payload["task_id"] = task_id
            pending.payload_json = json.dumps(payload, ensure_ascii=False)
            pending.expires_at = datetime.utcnow() + PENDING_TTL
            task = db.get(Task, task_id)
            label = f"id={task_id}"
            if task:
                label = f"id={task_id}, №{task.idea_number}, {(task.title or '—')[:40]}"
            send_message(
                text=f"Выбрана задача ({label}). Подтвердите сохранение.",
                chat_id=chat_id,
                reply_markup=_confirm_keyboard(),
            )
            db.commit()
            return
    except Exception:
        db.rollback()
        logger.exception("callback failed")
        send_message(text="Ошибка при подтверждении.", chat_id=chat_id)
    finally:
        db.close()


def _confirm_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Сохранить", "callback_data": "tg:ok"},
                {"text": "Отмена", "callback_data": "tg:cancel"},
            ]
        ]
    }


def _tasks_keyboard(tasks: list[Task]) -> dict:
    rows = []
    for t in tasks[:8]:
        title = (t.title or "без названия")[:30]
        rows.append(
            [{"text": f"id={t.id} · №{t.idea_number} · {title}", "callback_data": f"tg:com:{t.id}"}]
        )
    rows.append([{"text": "Отмена", "callback_data": "tg:cancel"}])
    return {"inline_keyboard": rows}


def _is_idea(text: str) -> bool:
    if not text or _is_comment(text):
        return False
    # Номер может быть не в первой строке («Идея:» / пустые строки сверху)
    for line in text.splitlines():
        line = line.strip()
        if line and IDEA_RE.search(line):
            return True
    return False


def _is_comment(text: str) -> bool:
    if not text:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return bool(COMMENT_RE.match(line))
    return False


def _comment_idea_number(text: str) -> int | None:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = COMMENT_RE.match(line)
        return int(m.group(1)) if m else None
    return None


def _comment_body(text: str) -> str:
    lines = text.splitlines()
    out = []
    skipped = False
    for line in lines:
        if not skipped and COMMENT_RE.match(line.strip() or ""):
            skipped = True
            continue
        if not skipped and not line.strip():
            continue
        skipped = True
        out.append(line)
    return "\n".join(out).strip()


def _author_from_tg(frm: dict) -> str:
    parts = [frm.get("first_name") or "", frm.get("last_name") or ""]
    name = " ".join(p for p in parts if p).strip()
    if not name:
        name = frm.get("username") or "Telegram"
    return normalize_author(name)


def _msg_datetime(msg: dict) -> datetime:
    ts = msg.get("date")
    if isinstance(ts, int):
        return datetime.utcfromtimestamp(ts).replace(second=0, microsecond=0)
    return datetime.utcnow().replace(second=0, microsecond=0)


def _extract_media_refs(msg: dict) -> list[dict]:
    """Список {file_id, filename, content_type}."""
    out: list[dict] = []
    if msg.get("photo"):
        photos = msg["photo"]
        best = photos[-1] if photos else None
        if best and best.get("file_id"):
            out.append(
                {
                    "file_id": best["file_id"],
                    "filename": "photo.jpg",
                    "content_type": "image/jpeg",
                }
            )
    doc = msg.get("document")
    if doc and doc.get("file_id"):
        out.append(
            {
                "file_id": doc["file_id"],
                "filename": doc.get("file_name") or "document",
                "content_type": doc.get("mime_type"),
            }
        )
    video = msg.get("video")
    if video and video.get("file_id"):
        out.append(
            {
                "file_id": video["file_id"],
                "filename": video.get("file_name") or "video.mp4",
                "content_type": video.get("mime_type") or "video/mp4",
            }
        )
    anim = msg.get("animation")
    if anim and anim.get("file_id"):
        out.append(
            {
                "file_id": anim["file_id"],
                "filename": anim.get("file_name") or "animation.mp4",
                "content_type": anim.get("mime_type"),
            }
        )
    return out


def _already_processed(db, chat_id: str, message_id: int) -> bool:
    return (
        db.query(TgInboxProcessed)
        .filter_by(chat_id=chat_id, message_id=message_id)
        .first()
        is not None
    )


def _mark_processed(db, chat_id: str, message_id: int, kind: str) -> None:
    if _already_processed(db, chat_id, message_id):
        return
    db.add(TgInboxProcessed(chat_id=chat_id, message_id=message_id, kind=kind))


def _get_pending(db, user_id: str) -> TgPending | None:
    row = db.get(TgPending, user_id)
    if not row:
        return None
    if row.expires_at and row.expires_at < datetime.utcnow():
        db.delete(row)
        db.flush()
        return None
    return row


def _clear_pending(db, user_id: str) -> None:
    row = db.get(TgPending, user_id)
    if row:
        db.delete(row)
        db.flush()


def _set_pending(db, user_id: str, kind: str, payload: dict) -> None:
    _clear_pending(db, user_id)
    db.add(
        TgPending(
            user_tg_id=user_id,
            kind=kind,
            payload_json=json.dumps(payload, ensure_ascii=False),
            expires_at=datetime.utcnow() + PENDING_TTL,
        )
    )
    db.flush()


def _start_idea_preview(
    db,
    *,
    user_id: str,
    reply_chat_id: str,
    text: str,
    media: list[dict],
    frm: dict,
    msg: dict,
) -> bool:
    parsed = parse_paste(text)
    idea_number = parsed.get("idea_number")
    title = parsed.get("title")
    condition = parsed.get("condition")
    if idea_number is None:
        send_message(
            text="Не найден номер идеи (нужна строка «Идея №…»).",
            chat_id=reply_chat_id,
        )
        return False
    if not (title or condition or media):
        send_message(
            text="Добавьте название, условие или медиа — иначе нечего сохранять.",
            chat_id=reply_chat_id,
        )
        return False

    warnings = []
    if not condition:
        warnings.append("нет условия")
    if not title:
        warnings.append("нет названия")

    author = _author_from_tg(frm)
    tag_slugs = parsed.get("tag_slugs") or infer_tag_slugs_for_new_task(
        title=title, condition=condition, extra_text=text
    )
    payload = {
        "idea_number": idea_number,
        "title": title,
        "condition": condition,
        "sources": parsed.get("sources"),
        "video_url": parsed.get("video_url"),
        "tag_slugs": tag_slugs,
        "author": author,
        "telegram_datetime": _msg_datetime(msg).isoformat(timespec="seconds"),
        "files": media,
        "saved_by": author,
        "preview_kind": "idea",
    }
    _set_pending(db, user_id, "idea", payload)

    lines = _idea_preview_lines(payload, warnings)
    ok = send_message(
        text="\n".join(lines),
        chat_id=reply_chat_id,
        reply_markup=_confirm_keyboard(),
    )
    if not ok:
        logger.warning(
            "Не удалось написать в личку user_id=%s (нужен /start у бота)",
            reply_chat_id,
        )
    return ok


def _idea_preview_lines(payload: dict, warnings: list[str] | None = None) -> list[str]:
    from app.tags import tag_names_for_slugs

    warnings = warnings or []
    lines = [
        "Из чата идей — превью. Сохранить на сайт?",
        f"Номер: {payload.get('idea_number')}",
        f"Название: {payload.get('title') or '—'}",
        f"Теги: {tag_names_for_slugs(payload.get('tag_slugs') or [])}",
        f"Автор: {payload.get('author') or '—'}",
        f"Файлов: {len(payload.get('files') or [])}",
        "",
        "Условие:",
        (payload.get("condition") or "—")[:1500],
    ]
    if warnings:
        lines.append("")
        lines.append("Замечания: " + ", ".join(warnings))
    return lines


def _start_comment_preview(
    db,
    *,
    user_id: str,
    reply_chat_id: str,
    text: str,
    media: list[dict],
    frm: dict,
    msg: dict,
) -> bool:
    idea_number = _comment_idea_number(text)
    body = _comment_body(text)
    if idea_number is None:
        send_message(
            text="Укажите номер: «К идее №12» в первой строке.",
            chat_id=reply_chat_id,
        )
        return False
    if not body and not media:
        send_message(text="Нужен текст комментария или медиа.", chat_id=reply_chat_id)
        return False

    tasks = (
        db.query(Task)
        .filter(Task.idea_number == idea_number)
        .order_by(Task.telegram_datetime.desc().nullslast(), Task.id.desc())
        .all()
    )
    if not tasks:
        send_message(
            text=f"На сайте нет задачи с идеей №{idea_number}.",
            chat_id=reply_chat_id,
        )
        return False

    author = _author_from_tg(frm)
    payload = {
        "idea_number": idea_number,
        "text": body or "(медиа)",
        "author": author,
        "files": media,
        "saved_by": author,
        "task_id": tasks[0].id if len(tasks) == 1 else None,
        "preview_kind": "comment",
    }
    _set_pending(db, user_id, "comment", payload)

    if len(tasks) > 1:
        lines = [
            f"Из чата идей: комментарий к №{idea_number}. Найдено несколько задач — выберите:",
        ]
        for t in tasks[:8]:
            lines.append(f"• id={t.id} · {(t.title or '—')[:50]}")
        ok = send_message(
            text="\n".join(lines),
            chat_id=reply_chat_id,
            reply_markup=_tasks_keyboard(tasks),
        )
    else:
        t = tasks[0]
        ok = send_message(
            text=(
                f"Из чата идей — превью комментария к №{idea_number} (id={t.id}):\n"
                f"Автор: {author}\n"
                f"Файлов: {len(media)}\n\n"
                f"{(body or '—')[:1500]}"
            ),
            chat_id=reply_chat_id,
            reply_markup=_confirm_keyboard(),
        )
    if not ok:
        logger.warning(
            "Не удалось написать в личку user_id=%s (нужен /start у бота)",
            reply_chat_id,
        )
    return ok


def _resend_pending_preview(db, pending: TgPending, *, chat_id: str) -> None:
    payload = json.loads(pending.payload_json)
    if pending.kind == "idea":
        send_message(
            text="\n".join(_idea_preview_lines(payload)),
            chat_id=chat_id,
            reply_markup=_confirm_keyboard(),
        )
        return
    if pending.kind == "comment":
        task_id = payload.get("task_id")
        if not task_id:
            idea_number = payload.get("idea_number")
            tasks = (
                db.query(Task)
                .filter(Task.idea_number == idea_number)
                .order_by(Task.telegram_datetime.desc().nullslast(), Task.id.desc())
                .all()
            )
            if tasks:
                send_message(
                    text=f"Выберите задачу для комментария к №{idea_number}:",
                    chat_id=chat_id,
                    reply_markup=_tasks_keyboard(tasks),
                )
            return
        send_message(
            text=(
                f"Превью комментария к №{payload.get('idea_number')} (id={task_id}):\n"
                f"{(payload.get('text') or '—')[:1500]}"
            ),
            chat_id=chat_id,
            reply_markup=_confirm_keyboard(),
        )


def _confirm_pending(db, *, user_id: str, chat_id: str) -> None:
    pending = _get_pending(db, user_id)
    if not pending:
        send_message(
            text="Нет активного превью. Когда в чате идей появится «Идея №…», я пришлю его сюда.",
            chat_id=chat_id,
        )
        return
    payload = json.loads(pending.payload_json)
    kind = pending.kind
    if kind == "idea":
        task = _commit_idea(db, payload)
        _clear_pending(db, user_id)
        db.commit()
        link = ""
        base = app_base_url()
        if base:
            link = f"\n{base}/tasks/{task.id}"
        send_message(
            text=f"Сохранено: идея №{task.idea_number}, задача id={task.id}.{link}",
            chat_id=chat_id,
        )
        notify_new_task(task, saved_by=payload.get("saved_by") or "Telegram")
        return

    if kind == "comment":
        task_id = payload.get("task_id")
        if not task_id:
            send_message(
                text="Сначала выберите задачу кнопкой из списка.",
                chat_id=chat_id,
            )
            return
        comment, task = _commit_comment(db, payload)
        _clear_pending(db, user_id)
        db.commit()
        link = ""
        base = app_base_url()
        if base:
            link = f"\n{base}/tasks/{task.id}"
        send_message(
            text=f"Комментарий добавлен к идее №{task.idea_number} (id={task.id}).{link}",
            chat_id=chat_id,
        )
        return

    send_message(text="Неизвестный тип превью.", chat_id=chat_id)


def _commit_idea(db, payload: dict) -> Task:
    tag_slugs = payload.get("tag_slugs") or []
    tags = tags_by_slugs(db, tag_slugs)
    if not tags:
        tags = tags_by_slugs(db, ["tyuf", "tyue"])

    tg_raw = payload.get("telegram_datetime") or ""
    try:
        tg_dt = datetime.fromisoformat(tg_raw)
    except ValueError:
        tg_dt = datetime.utcnow().replace(second=0, microsecond=0)

    task = Task(
        idea_number=payload.get("idea_number"),
        title=(payload.get("title") or None),
        condition=(payload.get("condition") or None),
        author=payload.get("author") or "Telegram",
        status=Status.TG.value,
        archived=False,
        video_url=payload.get("video_url"),
        sources=payload.get("sources"),
        telegram_datetime=tg_dt,
    )
    db.add(task)
    db.flush()
    task.tags = tags
    saved_by = payload.get("saved_by") or task.author or "Telegram"
    record_created(db, task, saved_by)

    for ref in payload.get("files") or []:
        _attach_tg_file(db, task_id=task.id, comment_id=None, ref=ref, uploaded_by=saved_by)

    db.flush()
    return task


def _commit_comment(db, payload: dict) -> tuple[Comment, Task]:
    task = db.get(Task, int(payload["task_id"]))
    if not task:
        raise ValueError("Задача не найдена")
    author = payload.get("author") or "Telegram"
    text = (payload.get("text") or "").strip() or "(медиа)"
    comment = Comment(task_id=task.id, text=text, author=author)
    db.add(comment)
    db.flush()
    saved_by = payload.get("saved_by") or author
    record_comment_added(db, task.id, saved_by, author, text)
    for ref in payload.get("files") or []:
        _attach_tg_file(
            db,
            task_id=task.id,
            comment_id=comment.id,
            ref=ref,
            uploaded_by=saved_by,
        )
    db.flush()
    return comment, task


def _attach_tg_file(
    db,
    *,
    task_id: int,
    comment_id: int | None,
    ref: dict,
    uploaded_by: str,
) -> None:
    file_id = ref.get("file_id")
    if not file_id:
        return
    downloaded = download_telegram_file(file_id)
    if not downloaded:
        return
    name, data = downloaded
    filename = ref.get("filename") or name
    att = save_bytes_attachment(
        db,
        task_id=task_id,
        comment_id=comment_id,
        filename=filename,
        data=data,
        content_type=ref.get("content_type"),
        uploaded_by=uploaded_by,
    )
    if att:
        record_file_added(
            db,
            task_id,
            uploaded_by,
            att.filename,
            for_comment=comment_id is not None,
        )
