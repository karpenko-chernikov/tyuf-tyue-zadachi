import io
import csv
from datetime import datetime

from sqlalchemy.orm import Session, joinedload

from app.enums import (
    NAZNACHENIE_LABELS,
    STATUS_LABELS,
    PROVERENA_LABELS,
)
from app.models import Task
from app.utils import format_igraetsya, format_idea_label


def _dt(dt):
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


def export_tasks_txt(db: Session, tasks=None) -> str:
    if tasks is None:
        tasks = (
            db.query(Task)
            .options(joinedload(Task.comments))
            .order_by(Task.idea_number.asc().nullslast(), Task.id.asc())
            .all()
        )

    blocks = []
    for task in tasks:
        header = f"=== Идея № {task.idea_number} ===" if task.idea_number else "=== Нет номера идеи ==="

        lines = [
            header,
            f"Название: {task.title or '—'}",
            f"Назначение: {NAZNACHENIE_LABELS.get(task.naznachenie or '', '—')}",
            f"Статус: {STATUS_LABELS.get(task.status, task.status)}",
            f"Дата в Telegram: {_dt(task.telegram_datetime)}",
            f"Автор: {task.author or '—'}",
            f"Проверена своими руками: {PROVERENA_LABELS.get(task.proverena or '', '—')}",
            f"Есть видео: {'Да' if task.has_video else 'Нет'}",
        ]

        igraetsya = format_igraetsya(task)
        if igraetsya:
            lines.append(f"Играется: {igraetsya}")

        lines.append("")
        lines.append("Условие:")
        lines.append(task.condition or "—")

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
            .options(joinedload(Task.comments))
            .order_by(Task.idea_number.asc().nullslast(), Task.id.asc())
            .all()
        )

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
        "Условие",
        "Источники",
        "Комментарии",
        "Играется",
    ])

    for task in tasks:
        comments = " | ".join(
            f"[{_dt(c.created_at)}] {c.author}: {c.text}"
            for c in sorted(task.comments, key=lambda x: x.created_at)
        )
        writer.writerow([
            task.idea_number or "",
            task.id,
            task.title or "",
            NAZNACHENIE_LABELS.get(task.naznachenie or "", ""),
            STATUS_LABELS.get(task.status, task.status),
            _dt(task.telegram_datetime),
            task.author or "",
            task.condition or "",
            (task.sources or "").replace("\n", " "),
            comments,
            format_igraetsya(task) or "",
        ])

    return output.getvalue()
