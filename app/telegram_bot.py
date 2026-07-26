"""Уведомления и ежемесячный TXT-экспорт в Telegram (Bot API)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from app.database import BACKUP_DIR, DATA_DIR, SessionLocal
from app.tags import task_tag_names
from app.utils import format_idea_label

logger = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "tg_monthly_state.json"
MAX_TG_UPLOAD_BYTES = 49 * 1024 * 1024  # запас до лимита бота 50 МБ
TG_MESSAGE_LIMIT = 4096

_scheduler_stop: threading.Event | None = None
_scheduler_thread: threading.Thread | None = None


def tg_configured() -> bool:
    return bool(_token() and _chat_id())


def tg_token_configured() -> bool:
    return bool(_token())


def _reload_env() -> None:
    """Подхватить изменения .env без рестарта uvicorn."""
    from dotenv import load_dotenv

    load_dotenv(override=True)


def _token() -> str:
    _reload_env()
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def _chat_id() -> str:
    _reload_env()
    return (os.getenv("TELEGRAM_CHAT_ID") or "").strip()


def _base_url() -> str:
    _reload_env()
    return (os.getenv("APP_BASE_URL") or "").strip().rstrip("/")


def app_base_url() -> str:
    return _base_url()


def telegram_monthly_day() -> int:
    """День месяца для автоэкспорта (из backup_config / TELEGRAM_MONTHLY_DAY)."""
    from app.backup_config import backup_monthly_day

    return backup_monthly_day()


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{_token()}/{method}"


def _format_moscow(dt: datetime | None) -> str:
    if not dt:
        return "—"
    try:
        from zoneinfo import ZoneInfo

        if dt.tzinfo is None:
            # created_at пишем как UTC naive
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        local = dt.astimezone(ZoneInfo("Europe/Moscow"))
    except Exception:
        local = dt
    return local.strftime("%d.%m.%Y %H:%M") + " (МСК)"


def _chunk_text(text: str, limit: int = TG_MESSAGE_LIMIT) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    return chunks


def notify_new_task(task, *, saved_by: str | None = None) -> None:
    """Фоновое уведомление о новой задаче (не блокирует ответ)."""
    if not tg_configured():
        return
    threading.Thread(
        target=_send_new_task_message,
        args=(task.id, saved_by or ""),
        daemon=True,
        name="tg-notify-task",
    ).start()


def notify_import_summary(*, created_tasks: int, created_comments: int) -> None:
    if not tg_configured() or created_tasks <= 0:
        return
    text = (
        f"Импорт из Telegram\n"
        f"Создано задач: {created_tasks}\n"
        f"Комментариев: {created_comments}"
    )
    threading.Thread(
        target=send_message,
        kwargs={"text": text},
        daemon=True,
        name="tg-notify-import",
    ).start()


def _build_new_task_text(task, *, saved_by: str) -> str:
    lines = [
        "Новая идея",
        format_idea_label(task),
        f"Название: {task.title or '—'}",
        f"Теги: {task_tag_names(task) or '—'}",
        f"Автор: {task.author or '—'}",
    ]
    if saved_by:
        lines.append(f"Сохранил на сайте: {saved_by}")
    lines.append(f"Создано на сайте: {_format_moscow(task.created_at)}")

    lines.append("")
    lines.append("Условие:")
    lines.append((task.condition or "").strip() or "—")

    sources = (task.sources or "").strip()
    video = (task.video_url or "").strip()
    if sources or video:
        lines.append("")
        lines.append("Источники:")
        if sources:
            lines.append(sources)
        if video and video not in sources:
            lines.append(video)

    url = _base_url()
    lines.append("")
    if url:
        lines.append(f"Ссылка: {url}/tasks/{task.id}")
    else:
        lines.append("Ссылка: задайте APP_BASE_URL в .env (публичный адрес сайта)")

    return "\n".join(lines)


def _send_new_task_message(task_id: int, saved_by: str) -> None:
    db = SessionLocal()
    try:
        from sqlalchemy.orm import undefer

        from app.files import is_image_attachment, is_video_attachment
        from app.models import Attachment, Task

        task = db.get(Task, task_id)
        if not task:
            return

        text = _build_new_task_text(task, saved_by=saved_by)
        for part in _chunk_text(text):
            send_message(text=part)

        media = (
            db.query(Attachment)
            .options(undefer(Attachment.data))
            .filter(
                Attachment.task_id == task_id,
                Attachment.comment_id.is_(None),
            )
            .order_by(Attachment.id.asc())
            .all()
        )
        for att in media:
            data = att.data
            if not data:
                continue
            if len(data) > MAX_TG_UPLOAD_BYTES:
                send_message(
                    text=f"Медиа «{att.filename}» слишком большое для Telegram "
                    f"({(att.size or len(data)) // (1024 * 1024)} МБ)."
                )
                continue
            if is_image_attachment(att):
                ok = send_photo_bytes(att.filename, data, caption=att.filename)
            elif is_video_attachment(att):
                ok = send_video_bytes(att.filename, data, caption=att.filename)
            else:
                ok = send_bytes_as_document(att.filename, data, caption=att.filename)
            if not ok:
                send_message(text=f"Не удалось отправить файл «{att.filename}»")
    except Exception:
        logger.exception("Не удалось отправить уведомление о задаче %s", task_id)
    finally:
        db.close()


def send_message(
    *,
    text: str,
    chat_id: str | None = None,
    reply_markup: dict | None = None,
) -> bool:
    if not _token():
        return False
    target = (chat_id or _chat_id()).strip()
    if not target:
        return False
    payload: dict = {
        "chat_id": target,
        "text": text[:TG_MESSAGE_LIMIT],
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(_api_url("sendMessage"), json=payload)
        if r.status_code >= 400:
            logger.error("Telegram sendMessage %s: %s", r.status_code, r.text[:500])
            return False
        return True
    except Exception:
        logger.exception("Telegram sendMessage failed")
        return False


def answer_callback_query(callback_query_id: str, *, text: str = "") -> bool:
    if not _token() or not callback_query_id:
        return False
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                _api_url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text[:200]},
            )
        return r.status_code < 400
    except Exception:
        logger.exception("Telegram answerCallbackQuery failed")
        return False


def get_updates(*, offset: int | None = None, timeout: int = 25) -> list[dict]:
    if not _token():
        return []
    params: dict = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset is not None:
        params["offset"] = offset
    try:
        with httpx.Client(timeout=timeout + 10.0) as client:
            r = client.get(_api_url("getUpdates"), params=params)
        if r.status_code >= 400:
            logger.error("Telegram getUpdates %s: %s", r.status_code, r.text[:500])
            return []
        data = r.json()
        if not data.get("ok"):
            return []
        return list(data.get("result") or [])
    except Exception:
        logger.exception("Telegram getUpdates failed")
        return []


def get_me() -> dict | None:
    if not _token():
        return None
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(_api_url("getMe"))
        data = r.json()
        if data.get("ok"):
            return data.get("result")
    except Exception:
        logger.exception("Telegram getMe failed")
    return None


def delete_webhook() -> None:
    """Long polling не работает при активном webhook."""
    if not _token():
        return
    try:
        with httpx.Client(timeout=15.0) as client:
            client.post(_api_url("deleteWebhook"), json={"drop_pending_updates": False})
    except Exception:
        logger.exception("Telegram deleteWebhook failed")


def download_telegram_file(file_id: str) -> tuple[str, bytes] | None:
    """Возвращает (filename, bytes) или None."""
    if not _token() or not file_id:
        return None
    try:
        with httpx.Client(timeout=180.0) as client:
            r = client.get(_api_url("getFile"), params={"file_id": file_id})
            data = r.json()
            if not data.get("ok"):
                return None
            file_path = (data.get("result") or {}).get("file_path") or ""
            if not file_path:
                return None
            url = f"https://api.telegram.org/file/bot{_token()}/{file_path}"
            fr = client.get(url)
            if fr.status_code >= 400:
                return None
            name = Path(file_path).name or "file"
            return name, fr.content
    except Exception:
        logger.exception("Telegram download file failed")
        return None


def send_photo_bytes(filename: str, data: bytes, *, caption: str = "") -> bool:
    if not tg_configured():
        return False
    try:
        with httpx.Client(timeout=120.0) as client:
            r = client.post(
                _api_url("sendPhoto"),
                data={"chat_id": _chat_id(), "caption": (caption or "")[:1000]},
                files={"photo": (filename or "photo.jpg", data)},
            )
        if r.status_code >= 400:
            logger.error("Telegram sendPhoto %s: %s", r.status_code, r.text[:500])
            return send_bytes_as_document(filename, data, caption=caption)
        return True
    except Exception:
        logger.exception("Telegram sendPhoto failed")
        return False


def send_video_bytes(filename: str, data: bytes, *, caption: str = "") -> bool:
    if not tg_configured():
        return False
    try:
        with httpx.Client(timeout=180.0) as client:
            r = client.post(
                _api_url("sendVideo"),
                data={"chat_id": _chat_id(), "caption": (caption or "")[:1000]},
                files={"video": (filename or "video.mp4", data)},
            )
        if r.status_code >= 400:
            logger.error("Telegram sendVideo %s: %s", r.status_code, r.text[:500])
            return send_bytes_as_document(filename, data, caption=caption)
        return True
    except Exception:
        logger.exception("Telegram sendVideo failed")
        return False


def send_bytes_as_document(filename: str, data: bytes, *, caption: str = "") -> bool:
    if not tg_configured():
        return False
    if len(data) > MAX_TG_UPLOAD_BYTES:
        return False
    try:
        with httpx.Client(timeout=180.0) as client:
            r = client.post(
                _api_url("sendDocument"),
                data={"chat_id": _chat_id(), "caption": (caption or "")[:1000]},
                files={"document": (filename or "file", data)},
            )
        if r.status_code >= 400:
            logger.error("Telegram sendDocument(bytes) %s: %s", r.status_code, r.text[:500])
            return False
        return True
    except Exception:
        logger.exception("Telegram sendDocument(bytes) failed")
        return False


def send_document(path: Path, *, caption: str = "") -> bool:
    if not tg_configured() or not path.is_file():
        return False
    size = path.stat().st_size
    if size > MAX_TG_UPLOAD_BYTES:
        logger.error("Файл %s слишком большой для Telegram: %s байт", path.name, size)
        return False
    try:
        with path.open("rb") as f, httpx.Client(timeout=120.0) as client:
            r = client.post(
                _api_url("sendDocument"),
                data={"chat_id": _chat_id(), "caption": caption[:1000]},
                files={"document": (path.name, f)},
            )
        if r.status_code >= 400:
            logger.error("Telegram sendDocument %s: %s", r.status_code, r.text[:500])
            return False
        return True
    except Exception:
        logger.exception("Telegram sendDocument failed for %s", path)
        return False


def _read_state() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_db_gzip() -> Path:
    """Только gzip-копия SQLite (для скачивания с сайта)."""
    import gzip
    import shutil

    from app.database import DB_PATH

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    db_copy = BACKUP_DIR / f"zadachi-{stamp}.db"
    db_gz = BACKUP_DIR / f"zadachi-{stamp}.db.gz"
    shutil.copy2(DB_PATH, db_copy)
    with open(db_copy, "rb") as src, gzip.open(db_gz, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    try:
        db_copy.unlink(missing_ok=True)
    except OSError:
        pass
    return db_gz


def prepare_export_files() -> tuple[Path, Path]:
    """TXT всех задач + gzip-копия SQLite. Возвращает (txt_path, db_gz_path)."""
    from app.export import export_tasks_txt

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    txt_path = BACKUP_DIR / f"export-{stamp}.txt"

    db = SessionLocal()
    try:
        txt_path.write_text(export_tasks_txt(db), encoding="utf-8")
    finally:
        db.close()

    db_gz = prepare_db_gzip()
    return txt_path, db_gz


def run_monthly_backup(*, force: bool = False, telegram: bool = True, email: bool = True) -> str:
    """
    Плановая выгрузка:
    - Telegram: только TXT (раз в месяц в день monthly_day)
    - почта: TXT + gzip БД (по расписанию каждого адреса)
    """
    from app.backup_config import (
        emails_due_today,
        load_backup_config,
        recipient_period_key,
    )
    from app.mail_backup import send_backup_email, smtp_configured

    cfg = load_backup_config()
    do_tg = bool(telegram and cfg.get("send_telegram", True))
    do_mail = bool(email and cfg.get("send_email", True))

    now = datetime.now()
    stamp_month = now.strftime("%Y-%m")
    state = _read_state()
    email_last = dict(state.get("email_last") or {})

    want_tg = (
        do_tg
        and tg_configured()
        and (force or (now.day == int(cfg["monthly_day"]) and state.get("tg_last") != stamp_month))
    )
    due_emails = (
        emails_due_today(when=now, force=force, state_email_last=email_last)
        if do_mail and smtp_configured()
        else []
    )
    want_mail = bool(due_emails)

    if not want_tg and not want_mail:
        if not force and do_tg and tg_configured() and state.get("tg_last") == stamp_month:
            if do_mail and smtp_configured() and not due_emails:
                return f"На сегодня выгрузок нет (TG за {stamp_month} уже был)"
            return f"Экспорт TG за {stamp_month} уже отправлялся"
        if not force:
            return "На сегодня запланированных выгрузок нет"
        missing = []
        if do_tg and not tg_configured():
            missing.append("Telegram")
        if do_mail and not smtp_configured():
            missing.append("SMTP")
        if not do_tg and not do_mail:
            return "В настройках выключены и Telegram, и почта"
        if missing:
            return "Не настроено: " + ", ".join(missing)
        return "Некому отправлять"

    txt_path, db_gz = prepare_export_files()
    parts: list[str] = []
    any_ok = False

    if want_tg:
        ok_txt = send_document(
            txt_path,
            caption=f"Экспорт задач (TXT) · {stamp_month}",
        )
        parts.append("TG TXT: ok" if ok_txt else "TG TXT: fail")
        if ok_txt:
            any_ok = True
            state["tg_last"] = stamp_month
            # совместимость со старым полем
            state["last_sent"] = stamp_month
            state["last_sent_at"] = now.isoformat(timespec="seconds")

    if want_mail:
        mail_result = send_backup_email(
            to_addrs=due_emails,
            subject=f"ТЮФ/ТЮЕ · бэкап {stamp_month}",
            body=(
                f"Автоматическая выгрузка задач ТЮФ/ТЮЕ за {stamp_month}.\n"
                f"Во вложении: полный TXT и gzip-копия SQLite ({db_gz.name}).\n"
            ),
            attachments=[txt_path, db_gz],
        )
        mail_ok = mail_result == "ok" or mail_result.startswith("ok ")
        parts.append(
            f"Email ({', '.join(due_emails)}): {mail_result}"
            if mail_ok
            else f"Email: {mail_result}"
        )
        if mail_ok:
            any_ok = True
            # отметить период для каждого получателя из конфига
            by_email = {r["email"].lower(): r for r in cfg["recipients"]}
            for addr in due_emails:
                rec = by_email.get(addr.lower())
                if not rec:
                    continue
                email_last[addr.lower()] = recipient_period_key(rec, now)
            state["email_last"] = email_last
            state["last_sent_at"] = now.isoformat(timespec="seconds")

    if any_ok:
        state["last_result"] = "; ".join(parts)
        _write_state(state)
    return "; ".join(parts)


def run_email_backup(*, force: bool = True) -> str:
    """Только почта: TXT + БД (для кнопки в настройках)."""
    return run_monthly_backup(force=force, telegram=False, email=True)


def run_telegram_txt_backup(*, force: bool = True) -> str:
    """Только Telegram TXT (для кнопки в настройках)."""
    return run_monthly_backup(force=force, telegram=True, email=False)


def _scheduler_loop(stop: threading.Event) -> None:
    from app.backup_config import anything_due_today

    # Первый час после старта — лёгкая пауза, чтобы не гонять при рестартах деплоя
    if stop.wait(60):
        return
    while not stop.is_set():
        try:
            if anything_due_today():
                msg = run_monthly_backup(force=False)
                logger.info("Scheduled backup: %s", msg)
        except Exception:
            logger.exception("Scheduled backup failed")
        # Проверяем раз в час
        if stop.wait(3600):
            break


_inbox_stop: threading.Event | None = None
_inbox_thread: threading.Thread | None = None


def start_telegram_scheduler() -> None:
    global _scheduler_stop, _scheduler_thread, _inbox_stop, _inbox_thread
    if not (_scheduler_thread and _scheduler_thread.is_alive()):
        _scheduler_stop = threading.Event()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            args=(_scheduler_stop,),
            daemon=True,
            name="tg-monthly-backup",
        )
        _scheduler_thread.start()
    if tg_token_configured() and not (_inbox_thread and _inbox_thread.is_alive()):
        from app.telegram_inbox import inbox_poll_loop

        _inbox_stop = threading.Event()
        _inbox_thread = threading.Thread(
            target=inbox_poll_loop,
            args=(_inbox_stop,),
            daemon=True,
            name="tg-inbox-poll",
        )
        _inbox_thread.start()
        logger.info("Telegram inbox poller started")


def stop_telegram_scheduler() -> None:
    global _scheduler_stop, _scheduler_thread, _inbox_stop, _inbox_thread
    if _scheduler_stop:
        _scheduler_stop.set()
    if _inbox_stop:
        _inbox_stop.set()
    _scheduler_thread = None
    _scheduler_stop = None
    _inbox_thread = None
    _inbox_stop = None
