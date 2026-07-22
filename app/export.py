import io
import csv
from datetime import datetime

from sqlalchemy.orm import Session, joinedload

from app.enums import (
    NAZNACHENIE_LABELS,
    STATUS_LABELS,
    PROVERENA_LABELS,
    TURNIR_LABELS,
    ETAP_LABELS,
)
from app.models import Comment, Task
from app.utils import attach_idea_occurrences, format_igraetsya, format_idea_label


def _dt(dt):
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


def export_tasks_txt(db: Session, tasks=None) -> str:
    if tasks is None:
        tasks = (
            db.query(Task)
            .options(
                joinedload(Task.comments).joinedload(Comment.attachments),
                joinedload(Task.attachments),
            )
            .order_by(Task.idea_number.asc().nullslast(), Task.id.asc())
            .all()
        )

    attach_idea_occurrences(db, tasks)
    blocks = []
    for task in tasks:
        header = f"=== {format_idea_label(task)} ==="

        lines = [
            header,
            f"Название: {task.title or '—'}",
            f"Назначение: {NAZNACHENIE_LABELS.get(task.naznachenie or '', '—')}",
            f"Статус: {STATUS_LABELS.get(task.status, task.status)}",
            f"Дата в Telegram: {_dt(task.telegram_datetime)}",
            f"Автор: {task.author or '—'}",
            f"Проверена своими руками: {PROVERENA_LABELS.get(task.proverena or '', '—')}",
            f"Есть видео: {'Да' if task.has_video else 'Нет'}",
            f"Архив: {'Да — больше не предлагаем' if task.archived else 'Нет'}",
        ]

        igraetsya = format_igraetsya(task)
        if igraetsya:
            lines.append(f"Играется: {igraetsya}")

        lines.append("")
        lines.append("Условие:")
        lines.append(task.condition or "—")

        task_files = [a for a in (task.attachments or []) if a.comment_id is None]
        if task_files:
            lines.append("")
            lines.append("Файлы к условию:")
            for a in task_files:
                lines.append(f"- {a.filename}")

        if task.formulirovka:
            lines.append("")
            lines.append("Формулировка перед отправлением:")
            lines.append(task.formulirovka)

        if task.itogovaya_formulirovka:
            lines.append("")
            lines.append("Итоговая формулировка:")
            lines.append(task.itogovaya_formulirovka)

        if task.sources:
            lines.append("")
            lines.append("Ссылки и источники:")
            for src in task.sources.splitlines():
                if src.strip():
                    lines.append(f"- {src.strip()}")

        lines.append("")
        lines.append("Комментарии:")
        if task.comments:
            for c in sorted(task.comments, key=lambda x: x.created_at):
                lines.append(f"[{_dt(c.created_at)}] {c.author}:")
                for comment_line in (c.text or "—").splitlines():
                    lines.append(f"  {comment_line}")
                for a in (c.attachments or []):
                    lines.append(f"  [файл] {a.filename}")
                lines.append("")
        else:
            lines.append("  —")

        lines.append("-" * 40)
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else "Нет задач для выгрузки."


def export_tasks_csv(db: Session, tasks=None) -> str:
    if tasks is None:
        tasks = (
            db.query(Task)
            .options(
                joinedload(Task.comments).joinedload(Comment.attachments),
                joinedload(Task.attachments),
            )
            .order_by(Task.idea_number.asc().nullslast(), Task.id.asc())
            .all()
        )

    attach_idea_occurrences(db, tasks)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Номер идеи",
        "ID",
        "Название",
        "Назначение",
        "Статус",
        "Дата в Telegram",
        "Автор",
        "Архив",
        "Условие",
        "Файлы к условию",
        "Формулировка перед отправлением",
        "Итоговая формулировка",
        "Источники",
        "Комментарии",
        "Турнир",
        "Год",
        "Номер задачи",
        "Этап КК",
        "Играется",
    ])

    for task in tasks:
        comments = " | ".join(
            f"[{_dt(c.created_at)}] {c.author}: {c.text}"
            + (
                " [" + ", ".join(a.filename for a in (c.attachments or [])) + "]"
                if c.attachments
                else ""
            )
            for c in sorted(task.comments, key=lambda x: x.created_at)
        )
        task_files = ", ".join(
            a.filename for a in (task.attachments or []) if a.comment_id is None
        )
        writer.writerow([
            format_idea_label(task) if task.idea_number is not None else "",
            task.id,
            task.title or "",
            NAZNACHENIE_LABELS.get(task.naznachenie or "", ""),
            STATUS_LABELS.get(task.status, task.status),
            _dt(task.telegram_datetime),
            task.author or "",
            "Да" if task.archived else "Нет",
            task.condition or "",
            task_files,
            task.formulirovka or "",
            task.itogovaya_formulirovka or "",
            (task.sources or "").replace("\n", " "),
            comments,
            TURNIR_LABELS.get(task.turnir or "", task.turnir or ""),
            task.turnir_year or "",
            task.task_number or "",
            ETAP_LABELS.get(task.etap_kk or "", task.etap_kk or ""),
            format_igraetsya(task) or "",
        ])

    return output.getvalue()
