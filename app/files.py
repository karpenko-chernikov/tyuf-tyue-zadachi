"""Загрузка вложений (файлы хранятся в SQLite как BLOB)."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from fastapi import HTTPException, UploadFile

from app.models import Attachment

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 МБ (обычная загрузка из браузера)
MAX_IMPORT_BYTES = 100 * 1024 * 1024  # 100 МБ (файлы из экспорта Telegram)
MAX_FILES_PER_REQUEST = 10
MAX_IMPORT_FILES_PER_ROW = 30


def safe_filename(name: str) -> str:
    raw = PurePosixPath(name or "file").name.strip() or "file"
    raw = re.sub(r"[^\w.\- ()а-яА-ЯёЁ]+", "_", raw, flags=re.UNICODE)
    return raw[:200] or "file"


def guess_content_type(filename: str) -> str | None:
    name = (filename or "").lower()
    if name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".gif"):
        return "image/gif"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".mp4"):
        return "video/mp4"
    if name.endswith(".mov"):
        return "video/quicktime"
    if name.endswith(".webm"):
        return "video/webm"
    if name.endswith(".pdf"):
        return "application/pdf"
    return None


async def read_upload(upload: UploadFile, *, max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[str, str | None, bytes]:
    filename = safe_filename(upload.filename or "file")
    content_type = upload.content_type
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"Файл «{filename}» больше {max_bytes // (1024 * 1024)} МБ",
        )
    if not data:
        raise HTTPException(status_code=400, detail=f"Файл «{filename}» пустой")
    return filename, content_type, data


def save_local_files(
    db,
    *,
    task_id: int,
    comment_id: int | None,
    paths: list[Path],
    uploaded_by: str,
    max_bytes: int = MAX_IMPORT_BYTES,
) -> list[Attachment]:
    """Сохраняет файлы с диска (экспорт Telegram) как вложения."""
    if not paths:
        return []
    if len(paths) > MAX_IMPORT_FILES_PER_ROW:
        paths = paths[:MAX_IMPORT_FILES_PER_ROW]
    saved: list[Attachment] = []
    for path in paths:
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            continue
        filename = safe_filename(path.name)
        data = path.read_bytes()
        att = Attachment(
            task_id=task_id,
            comment_id=comment_id,
            filename=filename,
            content_type=guess_content_type(filename),
            size=len(data),
            data=data,
            uploaded_by=uploaded_by,
        )
        db.add(att)
        saved.append(att)
    return saved


async def save_uploads(
    db,
    *,
    task_id: int,
    comment_id: int | None,
    uploads: list[UploadFile] | None,
    uploaded_by: str,
) -> list[Attachment]:
    if not uploads:
        return []
    files = [u for u in uploads if u and u.filename]
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"За один раз можно приложить не больше {MAX_FILES_PER_REQUEST} файлов",
        )
    saved: list[Attachment] = []
    for upload in files:
        filename, content_type, data = await read_upload(upload)
        att = Attachment(
            task_id=task_id,
            comment_id=comment_id,
            filename=filename,
            content_type=content_type,
            size=len(data),
            data=data,
            uploaded_by=uploaded_by,
        )
        db.add(att)
        saved.append(att)
    return saved


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    return f"{n / (1024 * 1024):.1f} МБ"


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}


def is_image_attachment(att) -> bool:
    ctype = (getattr(att, "content_type", None) or "").lower()
    if ctype.startswith("image/"):
        return True
    name = (getattr(att, "filename", None) or "").lower()
    return any(name.endswith(ext) for ext in _IMAGE_EXTENSIONS)
