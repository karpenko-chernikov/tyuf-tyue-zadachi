"""Фоновые job’ы разбора и сохранения импорта Telegram — не блокируют HTTP."""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.database import DATA_DIR, SessionLocal

logger = logging.getLogger(__name__)

JOBS_DIR = DATA_DIR / "import_jobs"
COMMIT_CHUNK = 8
MAX_JSON_BYTES = 500 * 1024 * 1024  # 500 МБ — защита от заливки

_job_queue: queue.Queue[str] = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def jobs_root() -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return JOBS_DIR


def job_dir(job_id: str) -> Path:
    return jobs_root() / job_id


def _state_path(job_id: str) -> Path:
    return job_dir(job_id) / "state.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_job(job_id: str) -> dict | None:
    path = _state_path(job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_job(job: dict) -> None:
    job_id = job["id"]
    job["updated_at"] = _now_iso()
    _atomic_write_json(_state_path(job_id), job)


def update_job(job_id: str, **fields: Any) -> dict | None:
    job = load_job(job_id)
    if not job:
        return None
    job.update(fields)
    save_job(job)
    return job


def create_job(*, kind: str, user: str, **extra: Any) -> dict:
    job_id = uuid.uuid4().hex[:16]
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id,
        "kind": kind,  # parse | commit
        "status": "queued",  # queued | running | done | error
        "user": user,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "progress": 0,
        "total": 0,
        "message": "В очереди…",
        "error": None,
        "success": None,
        "export_root": None,
        "chat_name": None,
        "chats": None,
        "rows_path": None,
        **extra,
    }
    save_job(job)
    return job


def rows_path_for(job_id: str) -> Path:
    return job_dir(job_id) / "rows.json"


def payload_path_for(job_id: str) -> Path:
    return job_dir(job_id) / "commit_payload.json"


def source_json_path(job_id: str) -> Path:
    return job_dir(job_id) / "result.json"


def load_rows(job_id: str) -> list[dict] | None:
    path = rows_path_for(job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, list) else None


def save_rows(job_id: str, rows: list[dict]) -> Path:
    path = rows_path_for(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def enqueue(job_id: str) -> None:
    _ensure_worker()
    _job_queue.put(job_id)


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, name="import-jobs", daemon=True)
        t.start()
        _worker_started = True
        logger.info("Import jobs worker started")


def cleanup_old_jobs(*, keep_days: int = 7) -> int:
    """Удаляет каталоги job’ов старше keep_days. Возвращает число удалённых."""
    root = jobs_root()
    if not root.is_dir():
        return 0
    cutoff = datetime.utcnow().timestamp() - keep_days * 86400
    removed = 0
    for path in root.iterdir():
        if not path.is_dir():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            logger.exception("Failed to remove import job dir %s", path)
    if removed:
        logger.info("Removed %s old import job dirs", removed)
    return removed


def start_import_jobs_worker() -> None:
    """Вызвать из lifespan приложения."""
    try:
        cleanup_old_jobs()
    except Exception:
        logger.exception("Import jobs cleanup failed")
    _ensure_worker()


def _worker_loop() -> None:
    while True:
        job_id = _job_queue.get()
        try:
            job = load_job(job_id)
            if not job:
                continue
            kind = job.get("kind")
            if kind == "parse":
                _run_parse(job_id)
            elif kind == "commit":
                _run_commit(job_id)
            else:
                update_job(job_id, status="error", error=f"Неизвестный тип job: {kind}")
        except Exception:
            logger.exception("Import job %s failed", job_id)
            try:
                update_job(job_id, status="error", error="Внутренняя ошибка при импорте (см. логи сервера)")
            except Exception:
                pass
        finally:
            _job_queue.task_done()


def _run_parse(job_id: str) -> None:
    from app.routes import (
        _existing_comment_keys,
        _existing_tasks_by_minute,
        _mark_import_msg_processed,
        _processed_import_msg_ids,
    )
    from app.tg_import import parse_telegram_export, pick_chat

    update_job(job_id, status="running", message="Разбираем экспорт…", progress=5)
    job = load_job(job_id) or {}
    chosen_chat = (job.get("chat_name") or "").strip() or None
    export_root = job.get("export_root")
    source = job.get("source") or {}

    payload: str | bytes | None = None
    if source.get("type") == "file":
        path = Path(source["path"])
        if not path.is_file():
            update_job(job_id, status="error", error="Файл экспорта не найден на сервере")
            return
        update_job(job_id, message="Читаем JSON…", progress=15)
        payload = path.read_bytes()
    elif source.get("type") == "local":
        path = Path(source["json_path"])
        if not path.is_file():
            update_job(job_id, status="error", error=f"Нет JSON в экспорте: {path}")
            return
        export_root = source.get("export_root") or export_root
        update_job(job_id, message="Читаем локальный экспорт…", progress=15)
        payload = path.read_bytes()
    elif source.get("type") == "paste":
        path = Path(source["path"])
        payload = path.read_text(encoding="utf-8")
    else:
        update_job(job_id, status="error", error="Не указан источник экспорта")
        return

    update_job(job_id, message="Сверяем с базой…", progress=35)
    db = SessionLocal()
    try:
        existing = _existing_tasks_by_minute(db)
        processed = _processed_import_msg_ids(db)
        comment_keys = _existing_comment_keys(db)
        update_job(job_id, message="Классифицируем сообщения…", progress=50)
        rows, chats, synced_msg_ids = parse_telegram_export(
            payload,
            existing_by_minute=existing,
            processed_msg_ids=processed,
            existing_comment_keys=comment_keys,
            chat_name=chosen_chat,
        )
        if synced_msg_ids:
            for mid in synced_msg_ids:
                _mark_import_msg_processed(db, mid, "synced")
            db.commit()
        chats_meta = [
            {"name": c.get("name"), "messages": len(c.get("messages") or [])} for c in chats
        ]
        if chosen_chat is None and chats_meta:
            chosen_chat = pick_chat(chats).get("name")
        if not rows:
            update_job(
                job_id,
                status="error",
                error=(
                    "Новых сообщений нет — всё уже в базе или отмечено обработанным "
                    "при прошлых импортах"
                ),
                chats=chats_meta,
                chat_name=chosen_chat,
                export_root=export_root,
                progress=100,
            )
            return
        row_dicts = [r.to_dict() for r in rows]
        save_rows(job_id, row_dicts)
        update_job(
            job_id,
            status="done",
            message=f"Готово: {len(row_dicts)} строк для проверки",
            progress=100,
            total=len(row_dicts),
            chats=chats_meta,
            chat_name=chosen_chat,
            export_root=export_root,
            rows_path=str(rows_path_for(job_id)),
            error=None,
        )
    except Exception as e:
        logger.exception("Parse job %s", job_id)
        update_job(job_id, status="error", error=str(e), progress=100)
    finally:
        db.close()


def _run_commit(job_id: str) -> None:
    from app.routes import run_import_commit_payload

    job = load_job(job_id) or {}
    payload_path = Path(job.get("payload_path") or payload_path_for(job_id))
    if not payload_path.is_file():
        update_job(job_id, status="error", error="Нет данных для сохранения")
        return
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        update_job(job_id, status="error", error=f"Не удалось прочитать данные: {e}")
        return

    update_job(job_id, status="running", message="Сохраняем в базу…", progress=1)

    def on_progress(*, done: int, total: int, message: str) -> None:
        pct = int(100 * done / total) if total else 100
        update_job(job_id, progress=pct, total=total, message=message)

    try:
        success = run_import_commit_payload(
            user=job.get("user") or "",
            payload=payload,
            on_progress=on_progress,
        )
        update_job(
            job_id,
            status="done",
            progress=100,
            message="Сохранено",
            success=success,
            error=None,
        )
    except Exception as e:
        logger.exception("Commit job %s", job_id)
        update_job(job_id, status="error", error=str(e), progress=100)


def public_status(job: dict) -> dict:
    """Короткий JSON для polling (без огромных полей)."""
    return {
        "id": job.get("id"),
        "kind": job.get("kind"),
        "status": job.get("status"),
        "progress": job.get("progress") or 0,
        "total": job.get("total") or 0,
        "message": job.get("message") or "",
        "error": job.get("error"),
        "success": job.get("success"),
        "chat_name": job.get("chat_name"),
        "export_root": job.get("export_root"),
    }


ProgressCb = Callable[..., None]
