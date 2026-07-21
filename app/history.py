import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.enums import (
    ETAP_LABELS,
    NAZNACHENIE_LABELS,
    PROVERENA_LABELS,
    STATUS_LABELS,
    TURNIR_LABELS,
)
from app.models import Task, TaskHistory

FIELD_LABELS = {
    "idea_number": "Номер идеи",
    "title": "Название",
    "condition": "Условие",
    "formulirovka": "Формулировка перед отправлением",
    "itogovaya_formulirovka": "Итоговая формулировка",
    "author": "Автор",
    "naznachenie": "Назначение",
    "status": "Статус",
    "proverena": "Проверена своими руками",
    "has_video": "Есть видео",
    "video_url": "Ссылка на видео",
    "sources": "Ссылки и источники",
    "telegram_datetime": "Дата в Telegram",
    "turnir": "Турнир",
    "turnir_year": "Год турнира",
    "task_number": "Номер задачи",
    "etap_kk": "Этап КК",
}

TRACKED_FIELDS = tuple(FIELD_LABELS.keys())

ACTION_LABELS = {
    "created": "создал задачу",
    "updated": "изменил",
    "comment_added": "добавил комментарий",
    "comment_deleted": "удалил комментарий",
    "file_added": "приложил файл",
    "file_deleted": "удалил файл",
}


def _raw_value(task: Task, field: str):
    value = getattr(task, field, None)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, bool):
        return value
    return value


def snapshot_task(task: Task) -> dict:
    return {field: _raw_value(task, field) for field in TRACKED_FIELDS}


def format_value(field: str, value) -> str:
    if value is None or value == "":
        return "—"
    if field == "has_video":
        return "Да" if value else "Нет"
    if field == "naznachenie":
        return NAZNACHENIE_LABELS.get(value, str(value))
    if field == "status":
        return STATUS_LABELS.get(value, str(value))
    if field == "proverena":
        return PROVERENA_LABELS.get(value, str(value))
    if field == "turnir":
        return TURNIR_LABELS.get(value, str(value))
    if field == "etap_kk":
        return ETAP_LABELS.get(value, str(value))
    if field == "idea_number" and value is None:
        return "Нет номера идеи"
    return str(value)


def diff_snapshots(before: dict, after: dict) -> list[dict]:
    changes = []
    for field in TRACKED_FIELDS:
        old = before.get(field)
        new = after.get(field)
        if old != new:
            changes.append({
                "field": field,
                "label": FIELD_LABELS[field],
                "old": format_value(field, old),
                "new": format_value(field, new),
            })
    return changes


def _add_entry(
    db: Session,
    task_id: int,
    user: str,
    action: str,
    changes: list[dict] | None = None,
    summary: str | None = None,
) -> TaskHistory:
    entry = TaskHistory(
        task_id=task_id,
        user=user,
        action=action,
        summary=(summary or "").strip() or None,
        changes_json=json.dumps(changes, ensure_ascii=False) if changes else None,
    )
    db.add(entry)
    return entry


def record_created(db: Session, task: Task, user: str) -> None:
    snap = snapshot_task(task)
    changes = []
    for field, value in snap.items():
        if value in (None, ""):
            continue
        if field == "has_video" and not value:
            continue
        changes.append({
            "field": field,
            "label": FIELD_LABELS[field],
            "old": "—",
            "new": format_value(field, value),
        })
    _add_entry(db, task.id, user, "created", changes=changes)


def record_update(db: Session, task: Task, user: str, before: dict) -> None:
    changes = diff_snapshots(before, snapshot_task(task))
    if changes:
        _add_entry(db, task.id, user, "updated", changes=changes)


def record_comment_added(db: Session, task_id: int, user: str, author: str, text: str) -> None:
    preview = text.strip()
    if len(preview) > 300:
        preview = preview[:297] + "…"
    _add_entry(
        db,
        task_id,
        user,
        "comment_added",
        summary=f"{author}: {preview}",
    )


def record_comment_deleted(db: Session, task_id: int, user: str, author: str, text: str) -> None:
    preview = text.strip()
    if len(preview) > 300:
        preview = preview[:297] + "…"
    _add_entry(
        db,
        task_id,
        user,
        "comment_deleted",
        summary=f"{author}: {preview}",
    )


def record_file_added(
    db: Session,
    task_id: int,
    user: str,
    filename: str,
    *,
    for_comment: bool = False,
) -> None:
    where = "к комментарию" if for_comment else "к условию"
    _add_entry(db, task_id, user, "file_added", summary=f"{filename} ({where})")


def record_file_deleted(db: Session, task_id: int, user: str, filename: str) -> None:
    _add_entry(db, task_id, user, "file_deleted", summary=filename)


def parse_changes(entry: TaskHistory) -> list[dict]:
    if not entry.changes_json:
        return []
    try:
        data = json.loads(entry.changes_json)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)
