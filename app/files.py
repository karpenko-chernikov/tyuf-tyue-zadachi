"""Загрузка вложений (файлы хранятся в SQLite как BLOB)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from fastapi import HTTPException, UploadFile

from app.models import Attachment

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 МБ
MAX_FILES_PER_REQUEST = 10


def safe_filename(name: str) -> str:
    raw = PurePosixPath(name or "file").name.strip() or "file"
    raw = re.sub(r"[^\w.\- ()а-яА-ЯёЁ]+", "_", raw, flags=re.UNICODE)
    return raw[:200] or "file"


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
