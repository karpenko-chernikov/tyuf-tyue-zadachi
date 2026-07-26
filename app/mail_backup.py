"""Отправка TXT + gzip-копии SQLite по SMTP."""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)


def _reload_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(override=True)


def smtp_configured() -> bool:
    _reload_env()
    return bool(
        (os.getenv("SMTP_HOST") or "").strip()
        and (os.getenv("SMTP_USER") or "").strip()
        and (os.getenv("SMTP_PASSWORD") or "").strip()
    )


def smtp_from_address() -> str:
    _reload_env()
    return (
        (os.getenv("SMTP_FROM") or "").strip()
        or (os.getenv("SMTP_USER") or "").strip()
    )


def send_backup_email(
    *,
    to_addrs: list[str],
    subject: str,
    body: str,
    attachments: list[Path],
    max_attach_bytes: int = 18 * 1024 * 1024,
) -> str:
    """
    Отправляет письмо с вложениями.
    Файлы больше max_attach_bytes пропускаются (лимит Gmail ~25 МБ).
    Возвращает 'ok' или текст ошибки / предупреждения.
    """
    if not smtp_configured():
        return "SMTP не настроен (SMTP_HOST / SMTP_USER / SMTP_PASSWORD)"
    recipients = [a.strip() for a in to_addrs if a and "@" in a]
    if not recipients:
        return "Нет адресов получателей"

    _reload_env()
    host = (os.getenv("SMTP_HOST") or "").strip()
    port = int((os.getenv("SMTP_PORT") or "587").strip() or "587")
    user = (os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    from_addr = smtp_from_address()
    use_ssl = (os.getenv("SMTP_SSL") or "").strip().lower() in ("1", "true", "yes")

    included: list[Path] = []
    skipped: list[str] = []
    for path in attachments:
        if not path or not path.is_file():
            continue
        size = path.stat().st_size
        if size > max_attach_bytes:
            skipped.append(f"{path.name} ({size / (1024 * 1024):.0f} МБ)")
            continue
        included.append(path)

    notes = body
    if skipped:
        notes += (
            "\n\nНе удалось вложить (слишком большой файл для обычной почты):\n"
            + "\n".join(f"- {s}" for s in skipped)
            + "\nСкачайте БД в Настройках → «Скачать БД (.gz)».\n"
        )
    if not included and skipped:
        # всё равно отправим письмо-уведомление без вложений
        pass
    elif not included:
        return "Нет файлов для отправки"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(notes)

    for path in included:
        data = path.read_bytes()
        maintype, subtype = "application", "octet-stream"
        name = path.name.lower()
        if name.endswith(".txt"):
            maintype, subtype = "text", "plain"
        elif name.endswith(".gz"):
            maintype, subtype = "application", "gzip"
        elif name.endswith(".db"):
            maintype, subtype = "application", "x-sqlite3"
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    try:
        if use_ssl or port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=120) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=120) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(user, password)
                smtp.send_message(msg)
        if skipped:
            return f"ok (без крупных файлов: {', '.join(skipped)})"
        return "ok"
    except Exception as e:
        logger.exception("SMTP send failed")
        return f"ошибка SMTP: {e}"
