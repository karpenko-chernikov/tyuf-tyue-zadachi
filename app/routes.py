from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote
import json
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload

from app.auth import change_password, login_required
from app.database import SessionLocal, get_db
from app.enums import (
    AUTHORS,
    DEFAULT_COMMENT_AUTHOR,
    DEFAULT_TASK_AUTHOR,
    ETAP_LABELS,
    PROVERENA_LABELS,
    STATUS_LABELS,
    STATUS_SHORT_LABELS,
    Status,
    TURNIR_LABELS,
    normalize_author,
)
from app.export import export_tasks_csv, export_tasks_txt
from app.files import (
    attachment_media_type,
    format_size,
    is_image_attachment,
    is_video_attachment,
    save_local_files,
    save_uploads,
)
from app.history import (
    action_label,
    parse_changes,
    record_comment_added,
    record_comment_deleted,
    record_created,
    record_file_added,
    record_file_deleted,
    record_update,
    snapshot_task,
)
from app.models import Attachment, Comment, ImportProcessedMessage, Tag, Task
from app.tags import (
    DEFAULT_TAG_SLUGS,
    board_statuses_for_tag,
    list_tags,
    slugify_tag_name,
    tags_by_slugs,
    task_allows_metodkom,
    task_is_kapitanka,
)
from app.telegram_bot import (
    app_base_url,
    notify_import_summary,
    notify_new_task,
    prepare_db_gzip,
    run_email_backup,
    run_monthly_backup,
    run_telegram_txt_backup,
    send_message,
    telegram_monthly_day,
    tg_configured,
)
from app.backup_config import load_backup_config, save_backup_config
from app.mail_backup import smtp_configured
from app.import_jobs import (
    COMMIT_CHUNK,
    MAX_JSON_BYTES,
    create_job,
    enqueue,
    load_job,
    load_rows,
    payload_path_for,
    public_status,
    save_job,
    source_json_path,
)
from app.tg_import import (
    find_local_export_dirs,
    find_local_export_json_path,
    media_path_is_image,
    media_path_is_video,
    resolve_export_media_path,
)
from app.utils import (
    attach_idea_occurrences,
    author_pill_class,
    format_igraetsya,
    format_idea_label,
    format_idea_title,
    parse_datetime_local,
    parse_idea_number_input,
    parse_paste,
    status_pill_class,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["format_size"] = format_size
templates.env.globals["is_image_attachment"] = is_image_attachment
templates.env.globals["is_video_attachment"] = is_video_attachment
templates.env.globals["author_pill_class"] = author_pill_class
templates.env.globals["status_pill_class"] = status_pill_class
templates.env.globals["format_igraetsya"] = format_igraetsya
templates.env.globals["format_idea_label"] = format_idea_label
templates.env.globals["format_idea_title"] = format_idea_title
templates.env.globals["media_path_is_image"] = media_path_is_image
templates.env.globals["media_path_is_video"] = media_path_is_video


@router.get("/health")
def health():
    return PlainTextResponse("ok")


def _uploads_from_form_list(raw) -> list[UploadFile]:
    uploads: list[UploadFile] = []
    if not raw:
        return uploads
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        # Не isinstance(UploadFile): в multipart приходит starlette UploadFile
        filename = getattr(item, "filename", None)
        if filename and callable(getattr(item, "read", None)):
            uploads.append(item)
    return uploads


async def _uploads_from_request(request: Request, field: str) -> list[UploadFile]:
    """Достаём файлы из multipart без File(...), чтобы пустые поля не давали 422."""
    form = await request.form()
    return _uploads_from_form_list(form.getlist(field))

# Поля, которые очищаем при уходе со статуса «играется»
_IGRAETSYA_ONLY_FIELDS = (
    ("itogovaya_formulirovka", "Итоговая формулировка"),
    ("igraetsya_title", "Название в итоговом списке"),
    ("turnir", "Турнир"),
    ("turnir_year", "Год турнира"),
    ("task_number", "Номер задачи"),
    ("etap_kk", "Этап КК"),
)


def _field_has_value(task: Task, field: str) -> bool:
    value = getattr(task, field, None)
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _fields_to_clear_on_status(task: Task, new_status: str) -> list[tuple[str, str]]:
    """Какие заполненные поля нужно сбросить при переходе в new_status."""
    # В архив данные не трогаем — задачу просто откладываем
    if new_status == Status.ARCHIVED.value:
        return []
    to_clear: list[tuple[str, str]] = []
    if new_status != Status.IGRAETSYA.value:
        for field, label in _IGRAETSYA_ONLY_FIELDS:
            if _field_has_value(task, field):
                to_clear.append((field, label))
    if new_status == Status.TG.value and _field_has_value(task, "formulirovka"):
        to_clear.append(("formulirovka", "Формулировка перед отправлением"))
    if new_status == Status.TG.value and _field_has_value(task, "formulirovka_title"):
        to_clear.append(("formulirovka_title", "Название для отправки"))
    return to_clear


def _apply_status_field_clears(task: Task, new_status: str) -> None:
    if new_status == Status.ARCHIVED.value:
        return
    if new_status != Status.IGRAETSYA.value:
        task.itogovaya_formulirovka = None
        task.igraetsya_title = None
        task.turnir = None
        task.turnir_year = None
        task.task_number = None
        task.etap_kk = None
    if new_status == Status.TG.value:
        task.formulirovka = None
        task.formulirovka_title = None


def _confirm_message(new_status: str, to_clear: list[tuple[str, str]]) -> str:
    status_name = STATUS_LABELS.get(new_status, new_status)
    lines = [
        f"При переносе в «{status_name}» будут удалены данные:",
        "",
    ]
    for _, label in to_clear:
        lines.append(f"• {label}")
    lines.extend(["", "Вас всё устраивает?"])
    return "\n".join(lines)


def _author_suggestions(db: Session):
    names = set(AUTHORS)
    for row in db.query(Task.author).filter(Task.author.isnot(None)).distinct():
        if row[0] and row[0].strip():
            names.add(row[0].strip())
    for row in db.query(Comment.author).filter(Comment.author.isnot(None)).distinct():
        if row[0] and row[0].strip():
            names.add(row[0].strip())
    return sorted(names, key=lambda x: x.lower())


def _filter_tasks(db: Session, q, tag_slug, status, author=None):
    query = db.query(Task)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Task.title.ilike(like),
                Task.condition.ilike(like),
                Task.sources.ilike(like),
                Task.author.ilike(like),
            )
        )
    if tag_slug:
        query = query.filter(Task.tags.any(Tag.slug == tag_slug))
    if status:
        query = query.filter(Task.status == status)
    if author:
        query = query.filter(Task.author == author)
    return query.order_by(
        Task.idea_number.asc().nullslast(),
        Task.telegram_datetime.asc().nullslast(),
        Task.id.asc(),
    )


def _sort_tasks_by_idea_display(tasks: list) -> list:
    """№ 15 раньше № 15(2); без номера — в конце."""
    return sorted(
        tasks,
        key=lambda t: (
            t.idea_number is None,
            t.idea_number if t.idea_number is not None else 0,
            getattr(t, "idea_occurrence", None) or 1,
            t.id or 0,
        ),
    )


def _available_statuses(*, allow_metodkom: bool):
    statuses = dict(STATUS_LABELS)
    if not allow_metodkom:
        statuses.pop(Status.METODKOM.value, None)
    return statuses


def _tag_slugs_from_form(form) -> list[str]:
    raw = form.getlist("tag_slugs") if hasattr(form, "getlist") else []
    if not raw:
        single = form.get("tag_slugs") if hasattr(form, "get") else None
        if single:
            raw = [single]
    return [str(s).strip() for s in raw if str(s).strip()]


def _moscow_now_minute() -> datetime:
    """Текущее время в Москве без tzinfo (как в datetime-local и экспорте TG)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Moscow")).replace(
            tzinfo=None, second=0, microsecond=0
        )
    except Exception:
        return datetime.now().replace(second=0, microsecond=0)


def _default_telegram_datetime(db: Session) -> str | None:
    """Для новой задачи — сейчас по Москве; импорт подставляет своё время из экспорта."""
    return _moscow_now_minute().strftime("%Y-%m-%dT%H:%M")


def _telegram_dt_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _task_with_telegram_datetime(
    db: Session, tg_dt: datetime, exclude_id: int | None = None
) -> Task | None:
    """Другая задача с той же датой и минутой в Telegram."""
    start = _telegram_dt_minute(tg_dt)
    end = start + timedelta(minutes=1)
    query = db.query(Task).filter(
        Task.telegram_datetime >= start,
        Task.telegram_datetime < end,
    )
    if exclude_id is not None:
        query = query.filter(Task.id != exclude_id)
    return query.first()


def _allocate_telegram_datetime(
    db: Session, tg_dt: datetime, exclude_id: int | None = None
) -> tuple[datetime, bool]:
    """
    Если минута уже занята другой задачей — сдвигаем вперёд по минуте,
    пока не найдём свободный слот (несколько идей в одну минуту в чате — норма).
    Возвращает (datetime, был_ли_сдвиг).
    """
    candidate = _telegram_dt_minute(tg_dt)
    shifted = False
    for _ in range(120):
        other = _task_with_telegram_datetime(db, candidate, exclude_id=exclude_id)
        if other is None:
            return candidate, shifted
        candidate = candidate + timedelta(minutes=1)
        shifted = True
    raise ValueError(
        "Не удалось подобрать свободную дату Telegram — слишком много задач в соседних минутах"
    )


def _max_idea_number(db: Session) -> int | None:
    return db.query(func.max(Task.idea_number)).scalar()


def _form_context(db: Session, **extra):
    all_tags = list_tags(db)
    ctx = {
        "authors": _author_suggestions(db),
        "all_tags": all_tags,
        "proverena_labels": PROVERENA_LABELS,
        "turnir_labels": TURNIR_LABELS,
        "etap_labels": ETAP_LABELS,
        "default_telegram_datetime": _default_telegram_datetime(db),
        "default_comment_author": DEFAULT_COMMENT_AUTHOR,
        "default_task_author": DEFAULT_TASK_AUTHOR,
        "default_tag_slugs": list(DEFAULT_TAG_SLUGS),
        "max_idea_number": _max_idea_number(db),
        "form": None,
        "error": None,
        "status_hint": None,
        "pending_status": None,
        "cancel_url": None,
        "task_files": [],
    }
    ctx.update(extra)
    return ctx


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if login_required(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.auth import verify_user

    display = verify_user(db, username, password)
    if not display:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный логин или пароль"}, status_code=401
        )
    request.session["user"] = display
    request.session["username"] = username
    return RedirectResponse("/kanban", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _settings_ctx(request: Request, db: Session, **extra):
    cfg = load_backup_config()
    recipients_ui = []
    for rec in cfg["recipients"]:
        months = rec.get("months", "all")
        yearly = isinstance(months, list) and months != "all" and months
        recipients_ui.append(
            {
                "email": rec["email"],
                "day": rec.get("day", 1),
                "freq": "yearly" if yearly else "monthly",
                "month": (months[0] if yearly else 4),
            }
        )
    while len(recipients_ui) < 2:
        recipients_ui.append(
            {"email": "", "day": 1, "freq": "monthly", "month": 4}
        )
    ctx = {
        "user": login_required(request),
        "username": request.session.get("username", ""),
        "all_tags": list_tags(db),
        "tg_configured": tg_configured(),
        "tg_monthly_day": telegram_monthly_day(),
        "smtp_configured": smtp_configured(),
        "backup_monthly_day": cfg["monthly_day"],
        "backup_recipients": recipients_ui,
        "backup_month_names": [
            (1, "января"),
            (2, "февраля"),
            (3, "марта"),
            (4, "апреля"),
            (5, "мая"),
            (6, "июня"),
            (7, "июля"),
            (8, "августа"),
            (9, "сентября"),
            (10, "октября"),
            (11, "ноября"),
            (12, "декабря"),
        ],
        "app_base_url": app_base_url(),
        "error": None,
        "success": None,
    }
    ctx.update(extra)
    return ctx


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "settings.html", _settings_ctx(request, db))


@router.post("/settings/password")
def settings_password(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    new_password2: str = Form(...),
    db: Session = Depends(get_db),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    username = request.session.get("username", "")
    ctx = _settings_ctx(request, db)

    if new_password != new_password2:
        ctx["error"] = "Новые пароли не совпадают"
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)

    try:
        change_password(db, username, old_password, new_password)
        ctx["success"] = "Пароль изменён"
    except ValueError as e:
        ctx["error"] = str(e)
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)

    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings/telegram/test")
def settings_telegram_test(request: Request, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    ok = send_message(text=f"Тест от ТЮФ/ТЮЕ · {user} · {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    ctx = _settings_ctx(
        request,
        db,
        success="Тестовое сообщение отправлено" if ok else None,
        error=None if ok else "Не удалось отправить (проверьте токен, chat_id и логи)",
    )
    return templates.TemplateResponse(request, "settings.html", ctx, status_code=200 if ok else 400)


@router.post("/settings/telegram/backup")
def settings_telegram_backup(request: Request, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    result = run_telegram_txt_backup(force=True)
    any_ok = "TG TXT: ok" in result
    ctx = _settings_ctx(
        request,
        db,
        success=f"Экспорт: {result}" if any_ok else None,
        error=None if any_ok else f"Экспорт не отправлен: {result}",
    )
    return templates.TemplateResponse(request, "settings.html", ctx, status_code=200 if any_ok else 400)


@router.post("/settings/backup")
async def settings_backup_save(
    request: Request,
    db: Session = Depends(get_db),
    monthly_day: int = Form(1),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    form = await request.form()
    recipients: list[dict] = []
    for i in range(8):
        email = (form.get(f"email_{i}") or "").strip()
        if not email or "@" not in email:
            continue
        freq = (form.get(f"freq_{i}") or "monthly").strip()
        try:
            day = int(form.get(f"day_{i}") or 1)
        except (TypeError, ValueError):
            day = 1
        day = max(1, min(28, day))
        if freq == "yearly":
            try:
                month = int(form.get(f"month_{i}") or 4)
            except (TypeError, ValueError):
                month = 4
            month = max(1, min(12, month))
            recipients.append({"email": email, "day": day, "months": [month]})
        else:
            recipients.append({"email": email, "day": day, "months": "all"})

    try:
        cfg = save_backup_config(
            monthly_day=monthly_day,
            recipients=recipients,
            send_telegram=True,
            send_email=True,
        )
    except (TypeError, ValueError) as e:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_ctx(request, db, error=str(e)),
            status_code=400,
        )

    n_mail = len(cfg["recipients"])
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_ctx(
            request,
            db,
            success=f"Сохранено: чат — {cfg['monthly_day']}-го числа, почта — {n_mail} адрес(а)",
        ),
    )


@router.post("/settings/backup/email")
def settings_backup_email(request: Request, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    result = run_email_backup(force=True)
    any_ok = ": ok" in result or "): ok" in result
    ctx = _settings_ctx(
        request,
        db,
        success=f"Почта: {result}" if any_ok else None,
        error=None if any_ok else f"Не отправлено: {result}",
    )
    return templates.TemplateResponse(request, "settings.html", ctx, status_code=200 if any_ok else 400)


@router.post("/settings/backup/now")
def settings_backup_now(request: Request, db: Session = Depends(get_db)):
    """Полная плановая выгрузка: TXT в TG + БД+TXT на почту."""
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    result = run_monthly_backup(force=True)
    any_ok = ": ok" in result
    ctx = _settings_ctx(
        request,
        db,
        success=f"Выгрузка: {result}" if any_ok else None,
        error=None if any_ok else f"Не отправлено: {result}",
    )
    return templates.TemplateResponse(request, "settings.html", ctx, status_code=200 if any_ok else 400)


@router.get("/settings/backup/db")
def settings_download_db(request: Request, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    from fastapi.responses import FileResponse

    db_gz = prepare_db_gzip()
    return FileResponse(
        path=db_gz,
        filename=db_gz.name,
        media_type="application/gzip",
    )


@router.post("/settings/tags")
def settings_create_tag(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    slug: str = Form(""),
    has_metodkom: str = Form(""),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    display = (name or "").strip()
    if not display:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_ctx(request, db, error="Укажите название тега"),
            status_code=400,
        )

    slug_val = (slug or "").strip().lower() or slugify_tag_name(display)
    slug_val = slugify_tag_name(slug_val) if slug_val else slugify_tag_name(display)
    if db.query(Tag).filter(Tag.slug == slug_val).first():
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_ctx(request, db, error=f"Тег со slug «{slug_val}» уже есть"),
            status_code=400,
        )

    max_order = db.query(func.max(Tag.sort_order)).scalar() or 0
    db.add(
        Tag(
            slug=slug_val,
            name=display,
            has_metodkom=has_metodkom in ("1", "on", "true", "yes"),
            sort_order=max_order + 10,
        )
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_ctx(request, db, success=f"Тег «{display}» создан"),
    )


@router.get("/kanban")
def kanban_root(request: Request, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    tags = list_tags(db)
    if not tags:
        raise HTTPException(status_code=404, detail="Нет тегов — создайте тег в настройках")
    return RedirectResponse(f"/kanban/{tags[0].slug}", status_code=303)


@router.get("/kanban/{board}", response_class=HTMLResponse)
def kanban_board(request: Request, board: str, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tag = db.query(Tag).filter(Tag.slug == board).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Нет такой доски")

    columns = board_statuses_for_tag(tag)
    allowed = {s.value for s in columns}

    tasks = (
        db.query(Task)
        .options(selectinload(Task.tags), selectinload(Task.attachments))
        .filter(Task.tags.any(Tag.slug == board))
        .order_by(
            Task.idea_number.asc().nullslast(),
            Task.telegram_datetime.asc().nullslast(),
            Task.id.asc(),
        )
        .all()
    )
    attach_idea_occurrences(db, tasks)
    tasks = _sort_tasks_by_idea_display(tasks)

    media_counts: dict[int, dict[str, int]] = {}
    for task in tasks:
        imgs = vids = other = 0
        for att in task.attachments or []:
            if att.comment_id is not None:
                continue
            if is_image_attachment(att):
                imgs += 1
            elif is_video_attachment(att):
                vids += 1
            else:
                other += 1
        if imgs or vids or other:
            media_counts[task.id] = {"images": imgs, "videos": vids, "files": other}

    tasks_by_status = {s.value: [] for s in columns}
    for task in tasks:
        key = task.status if task.status in allowed else columns[0].value
        tasks_by_status[key].append(task)

    boards = {t.slug: t.name for t in list_tags(db)}
    return templates.TemplateResponse(
        request,
        "kanban.html",
        {
            "user": user,
            "board": board,
            "board_label": tag.name,
            "boards": boards,
            "columns": columns,
            "tasks_by_status": tasks_by_status,
            "media_counts": media_counts,
            "status_short": STATUS_SHORT_LABELS,
            "format_igraetsya": format_igraetsya,
            "format_idea_label": format_idea_label,
        },
    )


@router.post("/api/tasks/{task_id}/status")
def api_set_status(
    request: Request,
    task_id: int,
    status: str = Form(...),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    user = login_required(request)
    if not user:
        raise HTTPException(status_code=401, detail="Нужен вход")

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if status not in STATUS_LABELS:
        raise HTTPException(status_code=400, detail="Неизвестный статус")

    if status == Status.METODKOM.value and not task_allows_metodkom(task):
        raise HTTPException(
            status_code=400,
            detail="Статус «Методкомиссия» недоступен для тегов этой задачи",
        )

    # Для статусов с обязательными полями — сначала форма, статус ещё не меняем.
    needs_edit = False
    if status == Status.FORMULIROVKA.value:
        if not (task.formulirovka or "").strip() or not (task.formulirovka_title or "").strip():
            needs_edit = True
    elif status == Status.METODKOM.value:
        if not (task.formulirovka or "").strip() or not (task.formulirovka_title or "").strip():
            needs_edit = True
    elif status == Status.IGRAETSYA.value:
        if not (task.itogovaya_formulirovka or "").strip() or not (task.igraetsya_title or "").strip():
            needs_edit = True
        elif not (task.formulirovka or "").strip() or not (task.formulirovka_title or "").strip():
            # на этап «играется» тоже нужны данные формулировки
            needs_edit = True
        elif task_is_kapitanka(task):
            if not task.etap_kk or not task.turnir_year:
                needs_edit = True
        elif not task.turnir or not task.turnir_year or not task.task_number:
            needs_edit = True

    if needs_edit:
        return {
            "ok": True,
            "status_changed": False,
            "needs_edit": True,
            "needs_confirm": False,
            "edit_url": f"/tasks/{task_id}/edit?pending_status={status}",
        }

    to_clear = _fields_to_clear_on_status(task, status)
    if to_clear and confirm not in ("1", "true", "yes"):
        return {
            "ok": True,
            "status_changed": False,
            "needs_edit": False,
            "needs_confirm": True,
            "message": _confirm_message(status, to_clear),
            "clear_fields": [label for _, label in to_clear],
        }

    before = snapshot_task(task)
    task.status = status
    task.archived = status == Status.ARCHIVED.value
    _apply_status_field_clears(task, status)
    record_update(db, task, user, before)
    db.commit()

    return {
        "ok": True,
        "status": status,
        "status_changed": True,
        "needs_edit": False,
        "needs_confirm": False,
        "edit_url": None,
        "cleared": bool(to_clear),
        "archived": task.archived,
    }


@router.get("/", response_class=HTMLResponse)
def task_list(
    request: Request,
    db: Session = Depends(get_db),
    q: str = Query(None),
    tag: str = Query(None),
    status: str = Query(None),
    author: str = Query(None),
    sort: str = Query(None),
    order: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    author_filter = (author or "").strip() or None
    query = _filter_tasks(db, q, tag, status, author_filter)
    sort_key = (sort or "").strip().lower()
    order_key = (order or "").strip().lower()
    if order_key not in ("asc", "desc"):
        order_key = "desc"

    if sort_key == "tg":
        query = query.order_by(None)
        if order_key == "asc":
            query = query.order_by(Task.telegram_datetime.asc(), Task.id.asc())
        else:
            query = query.order_by(Task.telegram_datetime.desc(), Task.id.desc())
        active_order = order_key
    else:
        sort_key = ""
        active_order = ""

    tasks = query.all()
    attach_idea_occurrences(db, tasks)
    if not sort_key:
        tasks = _sort_tasks_by_idea_display(tasks)
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "user": user,
            "tasks": tasks,
            "q": q or "",
            "tag": tag or "",
            "status_filter": status or "",
            "author_filter": author_filter or "",
            "authors": _author_suggestions(db),
            "all_tags": list_tags(db),
            "sort": sort_key,
            "order": active_order,
            "status_labels": STATUS_LABELS,
            "status_short": STATUS_SHORT_LABELS,
            "format_igraetsya": format_igraetsya,
            "format_idea_label": format_idea_label,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_task_page(
    request: Request,
    db: Session = Depends(get_db),
    created: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "form.html",
        _form_context(
            db,
            user=user,
            task=None,
            parsed=None,
            status_labels=_available_statuses(allow_metodkom=True),
            just_created=created == "1",
        ),
    )


@router.post("/parse")
def parse_task_paste(request: Request, db: Session = Depends(get_db), paste: str = Form("")):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    parsed = parse_paste(paste)
    return templates.TemplateResponse(
        request,
        "form.html",
        _form_context(
            db,
            user=user,
            task=None,
            parsed=parsed,
            paste=paste,
            status_labels=_available_statuses(allow_metodkom=True),
        ),
    )


def _build_task_from_form(
    db: Session,
    task_id,
    idea_number: str,
    title: str,
    condition: str,
    author: str,
    tag_slugs: list[str],
    status: str,
    proverena: str,
    video_url: str,
    sources: str,
    telegram_datetime: str,
    formulirovka: str,
    itogovaya_formulirovka: str,
    turnir: str,
    turnir_year: str,
    task_number: str,
    etap_kk: str,
    formulirovka_title: str = "",
    igraetsya_title: str = "",
) -> Task:
    tg_dt = parse_datetime_local(telegram_datetime)
    if not tg_dt:
        if task_id is None:
            tg_dt = _moscow_now_minute()
        else:
            raise ValueError("Укажите корректную дату и время сообщения в Telegram")
    tg_dt = _telegram_dt_minute(tg_dt)

    datetime_unchanged = False
    if task_id is not None:
        current = db.get(Task, task_id)
        if current and current.telegram_datetime:
            datetime_unchanged = _telegram_dt_minute(current.telegram_datetime) == tg_dt

    if not datetime_unchanged:
        other = _task_with_telegram_datetime(db, tg_dt, exclude_id=task_id)
        if other:
            if task_id is None:
                # Новая задача: если минута занята — сдвигаем, как при импорте
                tg_dt, _shifted = _allocate_telegram_datetime(db, tg_dt)
            else:
                attach_idea_occurrences(db, [other])
                label = format_idea_label(other)
                raise ValueError(
                    f"Уже есть задача ({label}) с такой же датой и временем в Telegram — "
                    f"укажите другое время"
                )

    if not condition.strip():
        raise ValueError("Заполните условие задачи")
    author_raw = (author or "").strip()
    if not author_raw:
        raise ValueError("Укажите автора задачи")
    author = normalize_author(author_raw)

    tags = tags_by_slugs(db, tag_slugs or [])
    if not tags:
        raise ValueError("Выберите хотя бы один тег")

    try:
        idea_num = parse_idea_number_input(idea_number)
    except ValueError as e:
        raise ValueError(str(e)) from e

    allow_metodkom = any(t.has_metodkom for t in tags)
    is_kk = any(t.slug == "kapitanka" for t in tags)

    if status == Status.METODKOM.value and not allow_metodkom:
        raise ValueError("Статус «Отправлена в методкомиссию» недоступен для выбранных тегов")

    if status == Status.FORMULIROVKA.value or status == Status.METODKOM.value:
        if not formulirovka_title.strip():
            formulirovka_title = title.strip()
        if not formulirovka.strip():
            raise ValueError("Заполните «Формулировку перед отправлением»")
        if not formulirovka_title.strip():
            raise ValueError("Укажите «Название для отправки» (или заполните обычное «Название» задачи)")

    if status == Status.IGRAETSYA.value:
        if not formulirovka_title.strip():
            formulirovka_title = title.strip()
        if not igraetsya_title.strip():
            igraetsya_title = formulirovka_title.strip() or title.strip()
        if not itogovaya_formulirovka.strip():
            raise ValueError("Заполните «Итоговую формулировку»")
        if not igraetsya_title.strip():
            raise ValueError("Укажите «Название в итоговом списке»")
        if not formulirovka.strip():
            # если формулировку ещё не заводили — копируем итоговую как стартовую
            formulirovka = itogovaya_formulirovka
        if not formulirovka_title.strip():
            formulirovka_title = igraetsya_title
        if is_kk:
            if not etap_kk.strip() or not turnir_year.strip():
                raise ValueError("Для Капитанки укажите этап (полуфинал/финал) и год")
        else:
            if not turnir.strip() or not turnir_year.strip() or not task_number.strip():
                raise ValueError("Укажите турнир (ТЮФ/ТЮЕ), год и номер задачи")

    sources_clean = sources.strip() or None
    video_clean = video_url.strip() or None

    task = db.get(Task, task_id) if task_id else Task()
    task.idea_number = idea_num
    task.title = title.strip() or None
    task.condition = condition.strip() or None
    task.formulirovka = formulirovka.strip() or None
    task.formulirovka_title = formulirovka_title.strip() or None
    task.itogovaya_formulirovka = itogovaya_formulirovka.strip() or None
    task.igraetsya_title = igraetsya_title.strip() or None
    task.author = author.strip() or None
    task.tags = tags
    task.status = status or Status.TG.value
    task.proverena = proverena or None
    task.archived = task.status == Status.ARCHIVED.value
    task.video_url = video_clean
    task.sources = sources_clean
    task.telegram_datetime = tg_dt

    if status == Status.IGRAETSYA.value:
        task.turnir = turnir or None
        task.turnir_year = int(turnir_year) if turnir_year.strip() else None
        task.task_number = int(task_number) if task_number.strip() else None
        task.etap_kk = etap_kk or None
        if is_kk:
            task.turnir = None
            task.task_number = None
        else:
            task.etap_kk = None
    else:
        _apply_status_field_clears(task, status)

    if task_id is None:
        db.add(task)
    return task


async def _add_initial_comments(
    db: Session,
    task: Task,
    authors,
    texts,
    default_user: str,
    request: Request,
) -> None:
    if not isinstance(authors, list):
        authors = [authors] if authors else []
    if not isinstance(texts, list):
        texts = [texts] if texts else []

    form = await request.form()
    file_indices: list[int] = []
    for key in form.keys():
        key_s = str(key)
        if key_s.startswith("comment_files_"):
            suffix = key_s.removeprefix("comment_files_")
            if suffix.isdigit():
                file_indices.append(int(suffix))
    n = max(len(authors), len(texts), 0)
    if file_indices:
        n = max(n, max(file_indices) + 1)

    for i in range(n):
        text = (texts[i] if i < len(texts) else "").strip()
        uploads = _uploads_from_form_list(form.getlist(f"comment_files_{i}"))
        if not text and not uploads:
            continue
        author = (authors[i] if i < len(authors) else "").strip() or DEFAULT_COMMENT_AUTHOR
        comment = Comment(task_id=task.id, text=text, author=author)
        db.add(comment)
        db.flush()
        summary = text if text else "(только файл)"
        record_comment_added(db, task.id, default_user, author, summary)
        for att in await save_uploads(
            db,
            task_id=task.id,
            comment_id=comment.id,
            uploads=uploads,
            uploaded_by=default_user,
        ):
            record_file_added(db, task.id, default_user, att.filename, for_comment=True)


@router.post("/tasks")
async def create_task(
    request: Request,
    db: Session = Depends(get_db),
    idea_number: str = Form(""),
    title: str = Form(""),
    condition: str = Form(""),
    author: str = Form(""),
    tag_slugs: list[str] = Form(default=[]),
    status: str = Form(Status.TG.value),
    proverena: str = Form(""),
    video_url: str = Form(""),
    sources: str = Form(""),
    telegram_datetime: str = Form(""),
    formulirovka: str = Form(""),
    formulirovka_title: str = Form(""),
    itogovaya_formulirovka: str = Form(""),
    igraetsya_title: str = Form(""),
    turnir: str = Form(""),
    turnir_year: str = Form(""),
    turnir_year_kk: str = Form(""),
    task_number: str = Form(""),
    etap_kk: str = Form(""),
    comment_authors: list[str] = Form(default=[]),
    comment_texts: list[str] = Form(default=[]),
    after: str = Form(""),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    year_val = turnir_year_kk.strip() or turnir_year.strip()

    try:
        task = _build_task_from_form(
            db,
            None,
            idea_number,
            title,
            condition,
            author,
            tag_slugs if isinstance(tag_slugs, list) else ([tag_slugs] if tag_slugs else []),
            status,
            proverena,
            video_url,
            sources,
            telegram_datetime,
            formulirovka,
            itogovaya_formulirovka,
            turnir,
            year_val,
            task_number,
            etap_kk,
            formulirovka_title=formulirovka_title,
            igraetsya_title=igraetsya_title,
        )
        db.flush()
        record_created(db, task, user)
        await _add_initial_comments(db, task, comment_authors, comment_texts, user, request)
        for att in await save_uploads(
            db,
            task_id=task.id,
            comment_id=None,
            uploads=await _uploads_from_request(request, "task_files"),
            uploaded_by=user,
        ):
            record_file_added(db, task.id, user, att.filename, for_comment=False)
        db.commit()
        db.refresh(task)
        notify_new_task(task, saved_by=user)
        if after == "new":
            return RedirectResponse("/new?created=1", status_code=303)
        return RedirectResponse(f"/tasks/{task.id}?created=1", status_code=303)
    except ValueError as e:
        db.rollback()
        # Собираем пары комментариев, чтобы не потерять при ошибке
        if not isinstance(comment_authors, list):
            comment_authors = [comment_authors] if comment_authors else []
        if not isinstance(comment_texts, list):
            comment_texts = [comment_texts] if comment_texts else []
        comments_draft = []
        n = max(len(comment_authors), len(comment_texts), 1)
        for i in range(n):
            comments_draft.append({
                "author": comment_authors[i] if i < len(comment_authors) else DEFAULT_COMMENT_AUTHOR,
                "text": comment_texts[i] if i < len(comment_texts) else "",
            })
        form = {
            "idea_number": idea_number,
            "title": title,
            "condition": condition,
            "author": author,
            "tag_slugs": tag_slugs if isinstance(tag_slugs, list) else ([tag_slugs] if tag_slugs else []),
            "status": status,
            "proverena": proverena,
            "video_url": video_url,
            "sources": sources,
            "telegram_datetime": telegram_datetime,
            "formulirovka": formulirovka,
            "formulirovka_title": formulirovka_title,
            "itogovaya_formulirovka": itogovaya_formulirovka,
            "igraetsya_title": igraetsya_title,
            "turnir": turnir,
            "turnir_year": year_val,
            "task_number": task_number,
            "etap_kk": etap_kk,
            "comments": comments_draft,
        }
        return templates.TemplateResponse(
            request,
            "form.html",
            _form_context(
                db,
                user=user,
                task=None,
                parsed=None,
                form=form,
                status_labels=_available_statuses(allow_metodkom=True),
                error=str(e),
            ),
            status_code=400,
        )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(
    request: Request,
    task_id: int,
    db: Session = Depends(get_db),
    created: str = Query(None),
    error: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    task = db.query(Task).options(
        selectinload(Task.comments).selectinload(Comment.attachments),
        selectinload(Task.attachments),
        selectinload(Task.history),
    ).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    history = []
    for entry in sorted(task.history, key=lambda e: e.created_at or datetime.min, reverse=True):
        history.append({
            "entry": entry,
            "action_label": action_label(entry.action),
            "changes": parse_changes(entry),
        })

    task_files = [a for a in task.attachments if a.comment_id is None]
    attach_idea_occurrences(db, [task])

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "user": user,
            "task": task,
            "task_files": task_files,
            "just_created": created == "1",
            "error": error,
            "status_labels": STATUS_LABELS,
            "proverena_labels": PROVERENA_LABELS,
            "format_igraetsya": format_igraetsya,
            "format_idea_label": format_idea_label,
            "format_idea_title": format_idea_title,
            "authors": _author_suggestions(db),
            "default_comment_author": DEFAULT_COMMENT_AUTHOR,
            "history": history,
        },
    )


@router.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
def edit_task_page(
    request: Request,
    task_id: int,
    db: Session = Depends(get_db),
    pending_status: str = Query(None),
    from_status: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    task = db.query(Task).options(selectinload(Task.attachments)).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    # from_status — старый параметр; pending_status — статус ещё не сохранён
    target = pending_status or from_status
    if target and target not in STATUS_LABELS:
        target = None
    if target == Status.METODKOM.value and not task_allows_metodkom(task):
        target = None

    hint = None
    if target == Status.FORMULIROVKA.value:
        hint = (
            "Заполните блок ниже: «Название для отправки» и «Формулировку перед отправлением», "
            "затем «Сохранить». «Отмена» — статус не изменится."
        )
    elif target == Status.METODKOM.value:
        hint = (
            "Для методкомиссии нужны «Название для отправки» и «Формулировка перед отправлением». "
            "Заполните и нажмите «Сохранить»."
        )
    elif target == Status.IGRAETSYA.value:
        hint = (
            "Заполните блок «Играется в турнире»: «Название в итоговом списке», "
            "итоговую формулировку и данные турнира, затем «Сохранить»."
        )

    first_tag = sorted(task.tags, key=lambda t: (t.sort_order, t.name))[0] if task.tags else None
    cancel_url = f"/kanban/{first_tag.slug}" if first_tag else f"/tasks/{task.id}"
    task_files = [a for a in task.attachments if a.comment_id is None]
    attach_idea_occurrences(db, [task])

    return templates.TemplateResponse(
        request,
        "form.html",
        _form_context(
            db,
            user=user,
            task=task,
            parsed=None,
            pending_status=target,
            status_labels=_available_statuses(allow_metodkom=task_allows_metodkom(task)),
            status_hint=hint,
            cancel_url=cancel_url,
            task_files=task_files,
        ),
    )


@router.post("/tasks/{task_id}")
async def update_task(
    request: Request,
    task_id: int,
    db: Session = Depends(get_db),
    idea_number: str = Form(""),
    title: str = Form(""),
    condition: str = Form(""),
    author: str = Form(""),
    tag_slugs: list[str] = Form(default=[]),
    status: str = Form(Status.TG.value),
    proverena: str = Form(""),
    video_url: str = Form(""),
    sources: str = Form(""),
    telegram_datetime: str = Form(""),
    formulirovka: str = Form(""),
    formulirovka_title: str = Form(""),
    itogovaya_formulirovka: str = Form(""),
    igraetsya_title: str = Form(""),
    turnir: str = Form(""),
    turnir_year: str = Form(""),
    turnir_year_kk: str = Form(""),
    task_number: str = Form(""),
    etap_kk: str = Form(""),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    year_val = turnir_year_kk.strip() or turnir_year.strip()

    try:
        before = snapshot_task(task)
        _build_task_from_form(
            db,
            task_id,
            idea_number,
            title,
            condition,
            author,
            tag_slugs if isinstance(tag_slugs, list) else ([tag_slugs] if tag_slugs else []),
            status,
            proverena,
            video_url,
            sources,
            telegram_datetime,
            formulirovka,
            itogovaya_formulirovka,
            turnir,
            year_val,
            task_number,
            etap_kk,
            formulirovka_title=formulirovka_title,
            igraetsya_title=igraetsya_title,
        )
        record_update(db, task, user, before)
        for att in await save_uploads(
            db,
            task_id=task.id,
            comment_id=None,
            uploads=await _uploads_from_request(request, "task_files"),
            uploaded_by=user,
        ):
            record_file_added(db, task.id, user, att.filename, for_comment=False)
        db.commit()
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)
    except (IntegrityError, OperationalError) as e:
        db.rollback()
        form = {
            "idea_number": idea_number,
            "title": title,
            "condition": condition,
            "author": author,
            "tag_slugs": tag_slugs if isinstance(tag_slugs, list) else ([tag_slugs] if tag_slugs else []),
            "status": status,
            "proverena": proverena,
            "video_url": video_url,
            "sources": sources,
            "telegram_datetime": telegram_datetime,
            "formulirovka": formulirovka,
            "formulirovka_title": formulirovka_title,
            "itogovaya_formulirovka": itogovaya_formulirovka,
            "igraetsya_title": igraetsya_title,
            "turnir": turnir,
            "turnir_year": year_val,
            "task_number": task_number,
            "etap_kk": etap_kk,
        }
        task_files_existing = [a for a in task.attachments if a.comment_id is None]
        msg = str(e.orig if getattr(e, "orig", None) else e)
        if "readonly" in msg.lower():
            error = "База данных сейчас только для чтения — перезапустите приложение (./run.sh)."
        else:
            error = (
                "Не удалось сохранить (конфликт данных). "
                "Повторяющийся номер идеи разрешён — если ошибка про дату Telegram, сдвиньте время на минуту."
            )
        return templates.TemplateResponse(
            request,
            "form.html",
            _form_context(
                db,
                user=user,
                task=task,
                form=form,
                error=error,
                status_labels=_available_statuses(allow_metodkom=True),
                task_files=task_files_existing,
            ),
            status_code=400,
        )
    except ValueError as e:
        db.rollback()
        form = {
            "idea_number": idea_number,
            "title": title,
            "condition": condition,
            "author": author,
            "tag_slugs": tag_slugs if isinstance(tag_slugs, list) else ([tag_slugs] if tag_slugs else []),
            "status": status,
            "proverena": proverena,
            "video_url": video_url,
            "sources": sources,
            "telegram_datetime": telegram_datetime,
            "formulirovka": formulirovka,
            "formulirovka_title": formulirovka_title,
            "itogovaya_formulirovka": itogovaya_formulirovka,
            "igraetsya_title": igraetsya_title,
            "turnir": turnir,
            "turnir_year": year_val,
            "task_number": task_number,
            "etap_kk": etap_kk,
        }
        task_files_existing = [a for a in task.attachments if a.comment_id is None]
        return templates.TemplateResponse(
            request,
            "form.html",
            _form_context(
                db,
                user=user,
                task=task,
                parsed=None,
                form=form,
                status_labels=_available_statuses(allow_metodkom=True),
                error=str(e),
                task_files=task_files_existing,
            ),
            status_code=400,
        )


@router.post("/tasks/{task_id}/comments")
async def add_comment(
    request: Request,
    task_id: int,
    db: Session = Depends(get_db),
    text: str = Form(""),
    author: str = Form(...),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    text_clean = (text or "").strip()
    uploads = await _uploads_from_request(request, "comment_files")
    has_files = bool(uploads)

    # Можно: только текст, только файлы, или и то и другое
    if not text_clean and not has_files:
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    try:
        comment = Comment(
            task_id=task_id,
            text=text_clean,
            author=author.strip() or user,
        )
        db.add(comment)
        db.flush()
        summary = text_clean if text_clean else ("файл" if has_files else "")
        if has_files and text_clean:
            summary = text_clean
        elif has_files and not text_clean:
            summary = "(только файл)"
        record_comment_added(db, task_id, user, comment.author, summary)
        for att in await save_uploads(
            db,
            task_id=task_id,
            comment_id=comment.id,
            uploads=uploads,
            uploaded_by=user,
        ):
            record_file_added(db, task_id, user, att.filename, for_comment=True)
        db.commit()
    except HTTPException as e:
        db.rollback()
        detail = e.detail if isinstance(e.detail, str) else "Не удалось загрузить файл"
        return RedirectResponse(
            f"/tasks/{task_id}?error={quote(detail)}",
            status_code=303,
        )
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/comments/{comment_id}/delete")
def delete_comment(
    request: Request,
    task_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    comment = db.get(Comment, comment_id)
    if comment and comment.task_id == task_id:
        record_comment_deleted(db, task_id, user, comment.author, comment.text)
        db.delete(comment)
        db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.get("/files/{attachment_id}")
def download_file(
    request: Request,
    attachment_id: int,
    db: Session = Depends(get_db),
    download: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # Только метаданные — BLOB не тянем в RAM (иначе OOM на больших файлах / канбане)
    from sqlalchemy.orm import load_only

    att = (
        db.query(Attachment)
        .options(
            load_only(
                Attachment.id,
                Attachment.filename,
                Attachment.content_type,
                Attachment.size,
            )
        )
        .filter(Attachment.id == attachment_id)
        .first()
    )
    if not att:
        raise HTTPException(status_code=404, detail="Файл не найден")

    quoted = quote(att.filename)
    force_download = download in ("1", "true", "yes")
    as_image = is_image_attachment(att) and not force_download
    as_video = is_video_attachment(att) and not force_download
    inline = as_image or as_video
    disposition = "inline" if inline else "attachment"
    media = attachment_media_type(att)
    size = int(att.size or 0)
    if size <= 0:
        size = int(
            db.execute(
                text("SELECT length(data) FROM attachments WHERE id = :id"),
                {"id": attachment_id},
            ).scalar()
            or 0
        )

    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quoted}",
        "Cache-Control": "private, max-age=3600",
        "Accept-Ranges": "bytes",
    }

    # Куски по 256 КБ — и для Range, и для полной отдачи (видео, фото, прочее)
    chunk_size = 256 * 1024
    max_range = 2 * 1024 * 1024

    def read_slice(start: int, length: int) -> bytes:
        piece = db.execute(
            text(
                "SELECT substr(data, :start, :length) FROM attachments WHERE id = :id"
            ),
            {"start": start + 1, "length": length, "id": attachment_id},
        ).scalar()
        if piece is None:
            return b""
        return bytes(piece)

    def iter_blob(start: int = 0, end: int | None = None):
        """Читает BLOB кусками в отдельной сессии (не держит request-сессию)."""
        last = (end if end is not None else size - 1)
        offset = start
        with SessionLocal() as stream_db:
            while offset <= last:
                length = min(chunk_size, last - offset + 1)
                piece = stream_db.execute(
                    text(
                        "SELECT substr(data, :start, :length) FROM attachments WHERE id = :id"
                    ),
                    {
                        "start": offset + 1,
                        "length": length,
                        "id": attachment_id,
                    },
                ).scalar()
                if not piece:
                    break
                data = bytes(piece)
                offset += len(data)
                yield data
                if len(data) < length:
                    break

    range_header = request.headers.get("range") if size > 0 else None
    if range_header and range_header.startswith("bytes="):
        try:
            _, _, rng = range_header.partition("=")
            start_s, _, end_s = rng.partition("-")
            start = int(start_s) if start_s else 0
            if end_s:
                end = int(end_s)
            else:
                end = start + max_range - 1
            end = min(end, size - 1, start + max_range - 1)
            if start < 0 or start > end or start >= size:
                raise ValueError("bad range")
            length = end - start + 1
            # Маленький Range — одним ответом; большой — стримом
            if length <= chunk_size:
                chunk = read_slice(start, length)
                if not chunk and length > 0:
                    raise HTTPException(status_code=404, detail="Файл не найден")
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                headers["Content-Length"] = str(len(chunk))
                return Response(
                    content=chunk,
                    status_code=206,
                    media_type=media,
                    headers=headers,
                )
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            headers["Content-Length"] = str(length)
            return StreamingResponse(
                iter_blob(start, end),
                status_code=206,
                media_type=media,
                headers=headers,
            )
        except ValueError:
            headers["Content-Range"] = f"bytes */{size}"
            return Response(status_code=416, headers=headers)

    if size <= 0:
        raise HTTPException(status_code=404, detail="Файл пустой")

    headers["Content-Length"] = str(size)
    return StreamingResponse(iter_blob(), media_type=media, headers=headers)


@router.post("/files/{attachment_id}/delete")
def delete_file(
    request: Request,
    attachment_id: int,
    db: Session = Depends(get_db),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    att = db.get(Attachment, attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="Файл не найден")

    task_id = att.task_id
    record_file_deleted(db, task_id, user, att.filename)
    db.delete(att)
    db.commit()

    referer = request.headers.get("referer") or f"/tasks/{task_id}"
    if "/edit" in referer:
        return RedirectResponse(f"/tasks/{task_id}/edit", status_code=303)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/delete")
def delete_task(request: Request, task_id: int, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    task = db.get(Task, task_id)
    if task:
        db.delete(task)
        db.commit()
    return RedirectResponse("/", status_code=303)


def _existing_tasks_by_minute(
    db: Session,
) -> dict[str, list[tuple[int, str, str | None, int | None]]]:
    """Минута Telegram → список задач: (id, label, title, idea_number)."""
    tasks = db.query(Task).order_by(Task.telegram_datetime.asc(), Task.id.asc()).all()
    attach_idea_occurrences(db, tasks)
    out: dict[str, list[tuple[int, str, str | None, int | None]]] = {}
    for task in tasks:
        if not task.telegram_datetime:
            continue
        key = _telegram_dt_minute(task.telegram_datetime).strftime("%Y-%m-%dT%H:%M")
        out.setdefault(key, []).append(
            (task.id, format_idea_label(task), task.title, task.idea_number)
        )
    return out


def _existing_comment_keys(db: Session) -> set[tuple[str, str, str]]:
    """(минута Telegram задачи, автор, начало текста) — уже сохранённые комментарии."""
    keys: set[tuple[str, str, str]] = set()
    rows = (
        db.query(Comment.text, Comment.author, Task.telegram_datetime)
        .join(Task, Task.id == Comment.task_id)
        .all()
    )
    for text, author, tg_dt in rows:
        if not tg_dt:
            continue
        minute = _telegram_dt_minute(tg_dt).strftime("%Y-%m-%dT%H:%M")
        body = re.sub(r"\s+", " ", (text or "").strip().lower().replace("ё", "е"))[:180]
        who = (author or "").strip().lower().replace("ё", "е")
        if body:
            keys.add((minute, who, body))
    return keys


def _processed_import_msg_ids(db: Session) -> set[int]:
    rows = db.query(ImportProcessedMessage.msg_id).all()
    return {int(r[0]) for r in rows if r[0] is not None}


def _mark_import_msg_processed(db: Session, msg_id: int | None, kind: str) -> None:
    if msg_id is None:
        return
    existing = db.get(ImportProcessedMessage, msg_id)
    if existing:
        existing.kind = kind
        existing.processed_at = datetime.utcnow()
    else:
        db.add(ImportProcessedMessage(msg_id=msg_id, kind=kind))


def _import_existing_link_options(db: Session) -> list[dict]:
    tasks = db.query(Task).order_by(Task.idea_number.asc().nullslast(), Task.id.asc()).all()
    attach_idea_occurrences(db, tasks)
    options = []
    for task in tasks:
        title = (task.title or "").strip()
        label = format_idea_label(task)
        if title:
            label = f"{label} — {title[:60]}"
        dt = ""
        if task.telegram_datetime:
            dt = task.telegram_datetime.strftime("%Y-%m-%dT%H:%M")
        options.append({"value": f"task:{task.id}", "label": label, "dt": dt})
    return options


def _import_page_ctx(db: Session, user: str, **extra):
    local_dirs = find_local_export_dirs()
    ctx = {
        "user": user,
        "rows": None,
        "error": None,
        "success": None,
        "all_tags": list_tags(db),
        "default_tag_slugs": list(DEFAULT_TAG_SLUGS),
        "existing_links": _import_existing_link_options(db),
        "default_task_author": DEFAULT_TASK_AUTHOR,
        "default_comment_author": DEFAULT_COMMENT_AUTHOR,
        "local_exports": [{"path": str(p), "name": p.name} for p in local_dirs],
        "export_root": None,
        "chat_name": None,
        "chats": None,
    }
    ctx.update(extra)
    return ctx


def _safe_import_file(export_root: Path, rel: str) -> Path | None:
    rel = (rel or "").strip().lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None
    root = export_root.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


@router.get("/import/media")
def import_media_preview(
    request: Request,
    root: str = Query(...),
    path: str = Query(...),
):
    """Превью файла из локального экспорта Telegram (только data/imports)."""
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    export_root = Path(root)
    if not export_root.is_dir():
        raise HTTPException(status_code=404, detail="Нет папки экспорта")
    # только внутри data/imports
    imports_root = Path(__file__).resolve().parent.parent / "data" / "imports"
    try:
        export_root.resolve().relative_to(imports_root.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Недоступный путь")
    file_path = resolve_export_media_path(export_root, path) or _safe_import_file(export_root, path)
    if not file_path:
        raise HTTPException(status_code=404, detail="Файл не найден")
    from fastapi.responses import FileResponse

    from app.files import guess_content_type

    media = guess_content_type(file_path.name) or "application/octet-stream"
    # без filename → inline (превью картинок/видео в импорте)
    return FileResponse(
        file_path,
        media_type=media,
        content_disposition_type="inline",
    )


def _resolve_link_to_task_id(link_to: str, draft_to_task: dict[str, int], db: Session) -> int | None:
    link_to = (link_to or "").strip()
    if link_to.startswith("task:"):
        try:
            task_id = int(link_to.split(":", 1)[1])
        except ValueError:
            return None
        return task_id if db.get(Task, task_id) else None
    if link_to.startswith("draft_"):
        return draft_to_task.get(link_to)
    return None


def _media_paths_from_form(form, i: int) -> list[str]:
    raw = (form.get(f"media_paths_{i}") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split("\n") if p.strip()]


def _attach_import_media(
    db: Session,
    *,
    export_root: Path | None,
    rel_paths: list[str],
    task_id: int,
    comment_id: int | None,
    user: str,
    skip_notes: list[str] | None = None,
) -> int:
    if not rel_paths:
        return 0
    if not export_root:
        if skip_notes is not None:
            skip_notes.append(
                "нет папки экспорта на сервере (нужен data/imports/… с photos и video_files, "
                "одного result.json мало)"
            )
        return 0
    paths: list[Path] = []
    for rel in rel_paths:
        found = resolve_export_media_path(export_root, rel)
        if found:
            paths.append(found)
        elif skip_notes is not None:
            skip_notes.append(f"{rel}: файл не найден в {export_root.name}")
    if not paths:
        return 0
    skipped: list[str] = []
    saved = save_local_files(
        db,
        task_id=task_id,
        comment_id=comment_id,
        paths=paths,
        uploaded_by=user,
        skipped=skipped,
    )
    if skip_notes is not None and skipped:
        skip_notes.extend(skipped)
    for att in saved:
        record_file_added(db, task_id, user, att.filename, for_comment=comment_id is not None)
    return len(saved)


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "import.html", _import_page_ctx(db, user))


async def _stream_upload_to_path(upload, dest: Path, *, max_bytes: int = MAX_JSON_BYTES) -> int:
    """Пишет UploadFile на диск чанками. Возвращает размер. Кидает ValueError при переполнении."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    chunk_size = 1024 * 1024
    with dest.open("wb") as out:
        while True:
            chunk = await upload.read(chunk_size)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise ValueError(
                    f"Файл больше {max_bytes // (1024 * 1024)} МБ — слишком большой для импорта"
                )
            out.write(chunk)
    if written <= 0:
        dest.unlink(missing_ok=True)
        raise ValueError("Пустой файл экспорта")
    return written


@router.post("/import")
async def import_parse(
    request: Request,
    db: Session = Depends(get_db),
    paste: str = Form(""),
    local_export: str = Form(""),
    chat_name: str = Form(""),
):
    """Принимает источник и сразу уходит в фоновый разбор — HTTP не ждёт парсинг."""
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    chosen_chat = (chat_name or "").strip() or None
    form = await request.form()
    upload = form.get("export_file")
    filename = getattr(upload, "filename", None) if upload is not None else None
    local_path = (local_export or "").strip()

    job = create_job(kind="parse", user=user, chat_name=chosen_chat)
    job_id = job["id"]

    try:
        if filename and callable(getattr(upload, "read", None)):
            dest = source_json_path(job_id)
            await _stream_upload_to_path(upload, dest)
            job["source"] = {"type": "file", "path": str(dest)}
            job["export_root"] = None
        elif local_path:
            export_dir = Path(local_path)
            if not export_dir.is_dir():
                raise ValueError(f"Папка экспорта не найдена: {local_path}")
            json_path = find_local_export_json_path(export_dir)
            job["source"] = {
                "type": "local",
                "export_root": str(export_dir.resolve()),
                "json_path": str(json_path.resolve()),
            }
            job["export_root"] = str(export_dir.resolve())
        elif paste.strip():
            dest = source_json_path(job_id)
            dest.write_text(paste.strip(), encoding="utf-8")
            job["source"] = {"type": "paste", "path": str(dest)}
            job["export_root"] = None
        else:
            dirs = find_local_export_dirs()
            if not dirs:
                raise ValueError(
                    "Загрузите result.json или укажите папку экспорта в data/imports"
                )
            export_dir = dirs[0]
            json_path = find_local_export_json_path(export_dir)
            job["source"] = {
                "type": "local",
                "export_root": str(export_dir.resolve()),
                "json_path": str(json_path.resolve()),
            }
            job["export_root"] = str(export_dir.resolve())
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        save_job(job)
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_page_ctx(db, user, error=str(e)),
            status_code=400,
        )

    job["message"] = "В очереди на разбор…"
    save_job(job)
    enqueue(job_id)
    return RedirectResponse(f"/import/job/{job_id}", status_code=303)


@router.get("/import/job/{job_id}", response_class=HTMLResponse)
def import_job_page(request: Request, job_id: str, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    job = load_job(job_id)
    if not job:
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_page_ctx(db, user, error="Задача импорта не найдена"),
            status_code=404,
        )

    status = job.get("status")
    kind = job.get("kind")

    # Разбор готов — показываем таблицу проверки
    if kind == "parse" and status == "done":
        rows = load_rows(job_id)
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_page_ctx(
                db,
                user,
                rows=rows,
                export_root=job.get("export_root"),
                chat_name=job.get("chat_name"),
                chats=job.get("chats"),
                parse_job_id=job_id,
            ),
        )

    # Сохранение готово — сводка
    if kind == "commit" and status == "done":
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_page_ctx(db, user, success=job.get("success") or "Сохранено"),
        )

    # Ошибка
    if status == "error":
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_page_ctx(
                db,
                user,
                error=job.get("error") or "Ошибка импорта",
                chats=job.get("chats"),
                chat_name=job.get("chat_name"),
                export_root=job.get("export_root"),
            ),
            status_code=400,
        )

    # В процессе
    return templates.TemplateResponse(
        request,
        "import_job.html",
        {
            "user": user,
            "job": public_status(job),
            "job_id": job_id,
        },
    )


@router.get("/import/job/{job_id}/status")
def import_job_status(request: Request, job_id: str):
    user = login_required(request)
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    job = load_job(job_id)
    if not job:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(public_status(job))


def run_import_commit_payload(
    *,
    user: str,
    payload: dict,
    on_progress=None,
) -> str:
    """Сохраняет обработанные строки импорта чанками (фон). Без backup на критическом пути."""
    rows = payload.get("rows") or []
    export_root_raw = (payload.get("export_root") or "").strip()
    export_root = Path(export_root_raw) if export_root_raw else None
    if export_root and not export_root.is_dir():
        export_root = None

    reviewed = [r for r in rows if r.get("reviewed")]
    total = max(len(reviewed), 1)
    done = 0

    def _progress(message: str) -> None:
        nonlocal done
        if on_progress:
            on_progress(done=done, total=total, message=message)

    created_tasks = 0
    created_comments = 0
    attached_files = 0
    skipped = 0
    not_reviewed = sum(1 for r in rows if not r.get("reviewed"))
    draft_to_task: dict[str, int] = {}
    errors: list[str] = []

    db = SessionLocal()
    try:
        # --- идеи ---
        idea_rows = [
            (idx, r)
            for idx, r in enumerate(rows)
            if r.get("reviewed") and (r.get("kind") or "skip") == "idea"
        ]
        skip_rows = [
            (idx, r)
            for idx, r in enumerate(rows)
            if r.get("reviewed") and (r.get("kind") or "skip") == "skip"
        ]

        for idx, r in skip_rows:
            skipped += 1
            msg_id = r.get("msg_id")
            try:
                msg_id = int(msg_id) if msg_id is not None and str(msg_id).strip() != "" else None
            except (TypeError, ValueError):
                msg_id = None
            _mark_import_msg_processed(db, msg_id, "skip")
            done += 1
            if done % COMMIT_CHUNK == 0:
                db.commit()
                _progress(f"Пропуски… {done}/{total}")

        for batch_start in range(0, len(idea_rows), COMMIT_CHUNK):
            batch = idea_rows[batch_start : batch_start + COMMIT_CHUNK]
            for idx, r in batch:
                msg_id = r.get("msg_id")
                try:
                    msg_id = int(msg_id) if msg_id is not None and str(msg_id).strip() != "" else None
                except (TypeError, ValueError):
                    msg_id = None
                draft_key = (r.get("draft_key") or "").strip() or f"draft_{idx}"
                author = normalize_author(
                    (r.get("author") or "").strip(),
                    default=DEFAULT_TASK_AUTHOR,
                )
                tg_raw = (r.get("telegram_datetime") or "").strip()
                title = (r.get("title") or "").strip() or None
                condition = (r.get("condition") or "").strip()
                sources = (r.get("sources") or "").strip() or None
                row_tag_slugs = [s.strip() for s in (r.get("tag_slugs") or []) if str(s).strip()]
                if not row_tag_slugs:
                    row_tag_slugs = list(DEFAULT_TAG_SLUGS)
                idea_number_raw = r.get("idea_number")
                if idea_number_raw is None or str(idea_number_raw).strip() == "":
                    idea_number_raw = ""
                else:
                    idea_number_raw = str(idea_number_raw).strip()
                try:
                    idea_number = parse_idea_number_input(idea_number_raw)
                except ValueError as e:
                    errors.append(f"Строка {idx + 1}: {e}")
                    done += 1
                    continue
                media_paths = [p.strip() for p in (r.get("media_paths") or []) if str(p).strip()]

                if not title:
                    errors.append(f"Строка {idx + 1}: у идеи нет названия — пропущена")
                    done += 1
                    continue
                if not condition:
                    errors.append(f"Строка {idx + 1}: у идеи нет условия — пропущена")
                    done += 1
                    continue
                tg_dt = parse_datetime_local(tg_raw)
                if not tg_dt:
                    errors.append(f"Строка {idx + 1}: некорректная дата Telegram — пропущена")
                    done += 1
                    continue
                try:
                    tg_dt, shifted = _allocate_telegram_datetime(db, tg_dt)
                except ValueError as e:
                    errors.append(f"Строка {idx + 1}: {e}")
                    done += 1
                    continue
                if shifted:
                    errors.append(
                        f"Строка {idx + 1}: дата занята другой задачей — сохранено как "
                        f"{tg_dt.strftime('%Y-%m-%d %H:%M')} (+сдвиг на свободную минуту)"
                    )

                video_url = None
                if sources:
                    for u in sources.splitlines():
                        u = u.strip()
                        if any(x in u.lower() for x in ("youtube", "youtu.be", "instagram")):
                            video_url = u
                            break

                task = Task(
                    idea_number=idea_number,
                    title=title,
                    condition=condition,
                    author=author,
                    status=Status.TG.value,
                    archived=False,
                    video_url=video_url,
                    sources=sources,
                    telegram_datetime=tg_dt,
                )
                db.add(task)
                db.flush()
                task.tags = tags_by_slugs(db, row_tag_slugs)
                if not task.tags:
                    task.tags = tags_by_slugs(db, list(DEFAULT_TAG_SLUGS))
                record_created(db, task, user)
                draft_to_task[draft_key] = task.id
                created_tasks += 1
                _mark_import_msg_processed(db, msg_id, "idea")
                idea_skips: list[str] = []
                attached_files += _attach_import_media(
                    db,
                    export_root=export_root,
                    rel_paths=media_paths,
                    task_id=task.id,
                    comment_id=None,
                    user=user,
                    skip_notes=idea_skips,
                )
                for note in idea_skips:
                    errors.append(f"Строка {idx + 1}: пропуск файла — {note}")
                done += 1
            db.commit()
            _progress(f"Идеи… {done}/{total}")

        # --- комментарии и медиа ---
        other_rows = [
            (idx, r)
            for idx, r in enumerate(rows)
            if r.get("reviewed") and (r.get("kind") or "skip") in ("comment", "media")
        ]
        for batch_start in range(0, len(other_rows), COMMIT_CHUNK):
            batch = other_rows[batch_start : batch_start + COMMIT_CHUNK]
            for idx, r in batch:
                kind = (r.get("kind") or "skip").strip()
                msg_id = r.get("msg_id")
                try:
                    msg_id = int(msg_id) if msg_id is not None and str(msg_id).strip() != "" else None
                except (TypeError, ValueError):
                    msg_id = None
                text = (r.get("text") or "").strip()
                author = normalize_author(
                    (r.get("author") or "").strip(),
                    default=DEFAULT_COMMENT_AUTHOR,
                )
                link_to = (r.get("link_to") or "").strip()
                media_paths = [p.strip() for p in (r.get("media_paths") or []) if str(p).strip()]
                task_id = _resolve_link_to_task_id(link_to, draft_to_task, db)
                if not task_id:
                    errors.append(f"Строка {idx + 1}: нет привязки к идее — пропущена")
                    done += 1
                    continue

                if kind == "media":
                    media_skips: list[str] = []
                    n = _attach_import_media(
                        db,
                        export_root=export_root,
                        rel_paths=media_paths,
                        task_id=task_id,
                        comment_id=None,
                        user=user,
                        skip_notes=media_skips,
                    )
                    if n == 0:
                        detail = (
                            "; ".join(media_skips)
                            if media_skips
                            else "медиафайлы не найдены на диске"
                        )
                        errors.append(f"Строка {idx + 1}: {detail}")
                    else:
                        _mark_import_msg_processed(db, msg_id, "media")
                        for note in media_skips:
                            errors.append(f"Строка {idx + 1}: пропуск файла — {note}")
                    attached_files += n
                    done += 1
                    continue

                if not text and not media_paths:
                    errors.append(f"Строка {idx + 1}: пустой комментарий — пропущен")
                    done += 1
                    continue
                if not text:
                    text = "(файл)"
                comment = Comment(task_id=task_id, text=text, author=author)
                db.add(comment)
                db.flush()
                record_comment_added(db, task_id, user, author, text)
                created_comments += 1
                _mark_import_msg_processed(db, msg_id, "comment")
                comment_skips: list[str] = []
                attached_files += _attach_import_media(
                    db,
                    export_root=export_root,
                    rel_paths=media_paths,
                    task_id=task_id,
                    comment_id=comment.id,
                    user=user,
                    skip_notes=comment_skips,
                )
                for note in comment_skips:
                    errors.append(f"Строка {idx + 1}: пропуск файла — {note}")
                done += 1
            db.commit()
            _progress(f"Комментарии… {done}/{total}")

        db.commit()
    finally:
        db.close()

    if created_tasks:
        notify_import_summary(
            created_tasks=created_tasks,
            created_comments=created_comments,
        )

    parts = [
        f"Создано задач: {created_tasks}",
        f"комментариев: {created_comments}",
        f"файлов: {attached_files}",
        f"явно пропущено: {skipped}",
        f"не обработано (оставлено): {not_reviewed}",
    ]
    success = ". ".join(parts) + "."
    if errors:
        success += " Замечания: " + "; ".join(errors[:12])
        if len(errors) > 12:
            success += f" … ещё {len(errors) - 12}"
    return success


@router.post("/import/commit")
async def import_commit(request: Request, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    form = await request.form()
    try:
        row_count = int(form.get("row_count") or 0)
    except (TypeError, ValueError):
        row_count = 0
    export_root_raw = (form.get("export_root") or "").strip()

    if row_count <= 0:
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_page_ctx(db, user, error="Нечего сохранять — сначала загрузите экспорт"),
            status_code=400,
        )

    def _is_reviewed(idx: int) -> bool:
        raw = form.get(f"reviewed_{idx}")
        if raw is None:
            return False
        return str(raw).strip().lower() in ("1", "on", "true", "yes")

    if not any(_is_reviewed(i) for i in range(row_count)):
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_page_ctx(
                db,
                user,
                error="Нет обработанных строк — отметьте «Обработано» у тех, что уже проверили",
            ),
            status_code=400,
        )

    missing_links: list[int] = []
    for i in range(row_count):
        if not _is_reviewed(i):
            continue
        kind = (form.get(f"kind_{i}") or "skip").strip()
        if kind not in ("comment", "media"):
            continue
        link_to = (form.get(f"link_to_{i}") or "").strip()
        if not link_to:
            missing_links.append(i + 1)
    if missing_links:
        nums = ", ".join(str(n) for n in missing_links[:20])
        extra = f" … ещё {len(missing_links) - 20}" if len(missing_links) > 20 else ""
        return templates.TemplateResponse(
            request,
            "import.html",
            _import_page_ctx(
                db,
                user,
                error=(
                    "Не сохранено: у комментария/файлов к задаче не указано «К идее» "
                    f"(строки: {nums}{extra}). Выберите задачу и попробуйте снова."
                ),
            ),
            status_code=400,
        )

    rows_payload: list[dict] = []
    for i in range(row_count):
        msg_raw = (form.get(f"msg_id_{i}") or "").strip()
        try:
            msg_id = int(msg_raw) if msg_raw else None
        except ValueError:
            msg_id = None
        media_raw = (form.get(f"media_paths_{i}") or "").strip()
        media_paths = [p.strip() for p in media_raw.split("\n") if p.strip()] if media_raw else []
        idea_raw = (form.get(f"idea_number_{i}") or "").strip()
        idea_number: int | str | None = idea_raw
        if idea_raw:
            try:
                idea_number = int(idea_raw)
            except ValueError:
                idea_number = idea_raw
        rows_payload.append(
            {
                "reviewed": _is_reviewed(i),
                "kind": (form.get(f"kind_{i}") or "skip").strip(),
                "msg_id": msg_id,
                "draft_key": (form.get(f"draft_key_{i}") or "").strip() or f"draft_{i}",
                "author": (form.get(f"author_{i}") or "").strip(),
                "telegram_datetime": (form.get(f"telegram_datetime_{i}") or "").strip(),
                "title": (form.get(f"title_{i}") or "").strip(),
                "condition": (form.get(f"condition_{i}") or "").strip(),
                "sources": (form.get(f"sources_{i}") or "").strip(),
                "tag_slugs": [s.strip() for s in form.getlist(f"tag_slugs_{i}") if str(s).strip()],
                "idea_number": idea_number,
                "media_paths": media_paths,
                "text": (form.get(f"text_{i}") or "").strip(),
                "link_to": (form.get(f"link_to_{i}") or "").strip(),
            }
        )

    job = create_job(kind="commit", user=user, export_root=export_root_raw or None)
    job_id = job["id"]
    payload = {"export_root": export_root_raw, "rows": rows_payload}
    path = payload_path_for(job_id)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    job["payload_path"] = str(path)
    job["message"] = "В очереди на сохранение…"
    job["total"] = sum(1 for r in rows_payload if r.get("reviewed"))
    save_job(job)
    enqueue(job_id)
    return RedirectResponse(f"/import/job/{job_id}", status_code=303)


@router.get("/export/txt")
def export_txt(
    request: Request,
    db: Session = Depends(get_db),
    q: str = Query(None),
    tag: str = Query(None),
    status: str = Query(None),
    author: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    author_filter = (author or "").strip() or None
    tasks = _filter_tasks(db, q, tag, status, author_filter).options(
        selectinload(Task.comments).selectinload(Comment.attachments),
        selectinload(Task.attachments),
    ).all()
    attach_idea_occurrences(db, tasks)
    tasks = _sort_tasks_by_idea_display(tasks)
    content = export_tasks_txt(db, tasks)
    filename = f"zadachi_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        media_type="text/plain; charset=utf-8",
    )


@router.get("/export/csv")
def export_csv(
    request: Request,
    db: Session = Depends(get_db),
    q: str = Query(None),
    tag: str = Query(None),
    status: str = Query(None),
    author: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    author_filter = (author or "").strip() or None
    tasks = _filter_tasks(db, q, tag, status, author_filter).options(
        selectinload(Task.comments).selectinload(Comment.attachments),
        selectinload(Task.attachments),
    ).all()
    attach_idea_occurrences(db, tasks)
    tasks = _sort_tasks_by_idea_display(tasks)
    content = export_tasks_csv(db, tasks)
    filename = f"zadachi_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        content="\ufeff" + content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
