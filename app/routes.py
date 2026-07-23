from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, joinedload

from app.auth import change_password, login_required
from app.database import backup_sqlite_db, get_db
from app.enums import (
    AUTHORS,
    BOARD_STATUSES,
    DEFAULT_COMMENT_AUTHOR,
    DEFAULT_NAZNACHENIE,
    DEFAULT_TASK_AUTHOR,
    ETAP_LABELS,
    METODKOM_ONLY_FOR,
    NAZNACHENIE_LABELS,
    Naznachenie,
    PROVERENA_LABELS,
    STATUS_LABELS,
    STATUS_SHORT_LABELS,
    Status,
    TURNIR_LABELS,
    normalize_author,
)
from app.export import export_tasks_csv, export_tasks_txt
from app.files import format_size, is_image_attachment, save_local_files, save_uploads
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
from app.models import Attachment, Comment, Task
from app.tg_import import (
    find_local_export_dirs,
    parse_telegram_export,
    read_local_export_json,
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
templates.env.globals["author_pill_class"] = author_pill_class
templates.env.globals["status_pill_class"] = status_pill_class
templates.env.globals["format_igraetsya"] = format_igraetsya
templates.env.globals["format_idea_label"] = format_idea_label
templates.env.globals["format_idea_title"] = format_idea_title


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
    return to_clear


def _apply_status_field_clears(task: Task, new_status: str) -> None:
    if new_status == Status.ARCHIVED.value:
        return
    if new_status != Status.IGRAETSYA.value:
        task.itogovaya_formulirovka = None
        task.turnir = None
        task.turnir_year = None
        task.task_number = None
        task.etap_kk = None
    if new_status == Status.TG.value:
        task.formulirovka = None


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


def _filter_tasks(db: Session, q, naznachenie, status, author=None):
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
    if naznachenie:
        query = query.filter(Task.naznachenie == naznachenie)
    if status:
        query = query.filter(Task.status == status)
    if author:
        query = query.filter(Task.author == author)
    # Сначала № 15, потом 15(2): по номеру, затем по дате TG / id (как у суффикса)
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


def _available_statuses(naznachenie):
    statuses = dict(STATUS_LABELS)
    if naznachenie not in METODKOM_ONLY_FOR:
        statuses.pop(Status.METODKOM.value, None)
    return statuses


def _sources_have_video(sources: str, video_url: str) -> bool:
    blob = f"{sources or ''}\n{video_url or ''}".lower()
    return any(x in blob for x in ("youtube", "youtu.be", "instagram", "tiktok", "vk.com/video"))


def _default_telegram_datetime(db: Session) -> str | None:
    last = db.query(Task).order_by(Task.id.desc()).first()
    if last and last.telegram_datetime:
        return last.telegram_datetime.strftime("%Y-%m-%dT%H:%M")
    return None


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


def _form_context(db: Session, **extra):
    ctx = {
        "authors": _author_suggestions(db),
        "naznachenie_labels": NAZNACHENIE_LABELS,
        "proverena_labels": PROVERENA_LABELS,
        "turnir_labels": TURNIR_LABELS,
        "etap_labels": ETAP_LABELS,
        "default_telegram_datetime": _default_telegram_datetime(db),
        "default_comment_author": DEFAULT_COMMENT_AUTHOR,
        "default_task_author": DEFAULT_TASK_AUTHOR,
        "default_naznachenie": DEFAULT_NAZNACHENIE,
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


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "username": request.session.get("username", ""),
            "error": None,
            "success": None,
        },
    )


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
    ctx = {
        "user": user,
        "username": username,
        "error": None,
        "success": None,
    }

    if new_password != new_password2:
        ctx["error"] = "Новые пароли не совпадают"
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)

    try:
        change_password(db, username, old_password, new_password)
        ctx["success"] = "Пароль изменён. При следующем входе используйте новый."
    except ValueError as e:
        ctx["error"] = str(e)
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)

    return templates.TemplateResponse(request, "settings.html", ctx)


@router.get("/kanban")
def kanban_root(request: Request):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/kanban/both", status_code=303)


@router.get("/kanban/{board}", response_class=HTMLResponse)
def kanban_board(request: Request, board: str, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if board not in BOARD_STATUSES:
        raise HTTPException(status_code=404, detail="Нет такой доски")

    columns = BOARD_STATUSES[board]
    allowed = {s.value for s in columns}

    tasks = (
        db.query(Task)
        .filter(Task.naznachenie == board)
        .order_by(
            Task.idea_number.asc().nullslast(),
            Task.telegram_datetime.asc().nullslast(),
            Task.id.asc(),
        )
        .all()
    )
    attach_idea_occurrences(db, tasks)
    tasks = _sort_tasks_by_idea_display(tasks)

    tasks_by_status = {s.value: [] for s in columns}
    for task in tasks:
        # если у задачи «странный» статус (например методкомиссия на ТЮЕ) — в первую колонку
        key = task.status if task.status in allowed else columns[0].value
        tasks_by_status[key].append(task)

    return templates.TemplateResponse(
        request,
        "kanban.html",
        {
            "user": user,
            "board": board,
            "board_label": NAZNACHENIE_LABELS[board],
            "boards": NAZNACHENIE_LABELS,
            "columns": columns,
            "tasks_by_status": tasks_by_status,
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

    if status == Status.METODKOM.value and task.naznachenie not in METODKOM_ONLY_FOR:
        raise HTTPException(
            status_code=400,
            detail="Статус «Методкомиссия» только для доски «ТЮФ и ТЮЕ»",
        )

    allowed = BOARD_STATUSES.get(task.naznachenie or "")
    if allowed and status not in {s.value for s in allowed}:
        raise HTTPException(status_code=400, detail="Этот статус недоступен для данной задачи")

    # Для «формулировка» / «играется» статус меняем только если поля уже заполнены.
    # Иначе открываем форму — без сохранения статус не меняется (Отмена = остаётся как было).
    needs_edit = False
    if status == Status.FORMULIROVKA.value and not (task.formulirovka or "").strip():
        needs_edit = True
    elif status == Status.IGRAETSYA.value:
        if not (task.itogovaya_formulirovka or "").strip():
            needs_edit = True
        elif task.naznachenie == Naznachenie.KAPITANY.value:
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
    naznachenie: str = Query(None),
    status: str = Query(None),
    author: str = Query(None),
    sort: str = Query(None),
    order: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    author_filter = (author or "").strip() or None
    query = _filter_tasks(db, q, naznachenie, status, author_filter)
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
            "naznachenie": naznachenie or "",
            "status_filter": status or "",
            "author_filter": author_filter or "",
            "authors": _author_suggestions(db),
            "sort": sort_key,
            "order": active_order,
            "naznachenie_labels": NAZNACHENIE_LABELS,
            "status_labels": STATUS_LABELS,
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
            status_labels=_available_statuses(None),
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
            status_labels=_available_statuses(parsed.get("naznachenie")),
        ),
    )


def _build_task_from_form(
    db: Session,
    task_id,
    idea_number: str,
    title: str,
    condition: str,
    author: str,
    naznachenie: str,
    status: str,
    proverena: str,
    has_video: bool,
    video_url: str,
    sources: str,
    telegram_datetime: str,
    formulirovka: str,
    itogovaya_formulirovka: str,
    turnir: str,
    turnir_year: str,
    task_number: str,
    etap_kk: str,
) -> Task:
    tg_dt = parse_datetime_local(telegram_datetime)
    if not tg_dt:
        raise ValueError("Укажите корректную дату и время сообщения в Telegram")
    tg_dt = _telegram_dt_minute(tg_dt)

    # При создании нельзя оставить дату/время из последней задачи без изменения
    if task_id is None:
        default_str = _default_telegram_datetime(db)
        if default_str and tg_dt.strftime("%Y-%m-%dT%H:%M") == default_str:
            raise ValueError(
                "Измените дату и время в Telegram — сейчас стоит значение из последней задачи"
            )

    # Уникальность даты: при создании всегда; при правке — только если дату поменяли
    datetime_unchanged = False
    if task_id is not None:
        current = db.get(Task, task_id)
        if current and current.telegram_datetime:
            datetime_unchanged = _telegram_dt_minute(current.telegram_datetime) == tg_dt

    if not datetime_unchanged:
        other = _task_with_telegram_datetime(db, tg_dt, exclude_id=task_id)
        if other:
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
    if not naznachenie.strip():
        raise ValueError("Выберите назначение")

    try:
        idea_num = parse_idea_number_input(idea_number)
    except ValueError as e:
        raise ValueError(str(e)) from e

    if status == Status.METODKOM.value and naznachenie not in METODKOM_ONLY_FOR:
        raise ValueError("Статус «Отправлена в методкомиссию» только для ТЮФ / ТЮФ и ТЮЕ")

    if status == Status.FORMULIROVKA.value and not formulirovka.strip():
        raise ValueError("Заполните «Формулировку перед отправлением»")

    if status == Status.IGRAETSYA.value:
        if not itogovaya_formulirovka.strip():
            raise ValueError("Заполните «Итоговую формулировку»")
        if naznachenie == Naznachenie.KAPITANY.value:
            if not etap_kk.strip() or not turnir_year.strip():
                raise ValueError("Для КК укажите этап (полуфинал/финал) и год")
        else:
            if not turnir.strip() or not turnir_year.strip() or not task_number.strip():
                raise ValueError("Укажите турнир (ТЮФ/ТЮЕ), год и номер задачи")

    sources_clean = sources.strip() or None
    video_clean = video_url.strip() or None
    video_flag = has_video or _sources_have_video(sources_clean or "", video_clean or "")

    task = db.get(Task, task_id) if task_id else Task()
    task.idea_number = idea_num
    task.title = title.strip() or None
    task.condition = condition.strip() or None
    task.formulirovka = formulirovka.strip() or None
    task.itogovaya_formulirovka = itogovaya_formulirovka.strip() or None
    task.author = author.strip() or None
    task.naznachenie = naznachenie or None
    task.status = status or Status.TG.value
    task.proverena = proverena or None
    task.has_video = video_flag
    task.archived = task.status == Status.ARCHIVED.value
    task.video_url = video_clean
    task.sources = sources_clean
    task.telegram_datetime = tg_dt

    if status == Status.IGRAETSYA.value:
        task.turnir = turnir or None
        task.turnir_year = int(turnir_year) if turnir_year.strip() else None
        task.task_number = int(task_number) if task_number.strip() else None
        task.etap_kk = etap_kk or None
        if naznachenie == Naznachenie.KAPITANY.value:
            task.turnir = None
            task.task_number = None
        else:
            task.etap_kk = None
    else:
        # при откате статуса лишние поля сбрасываем
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
    naznachenie: str = Form(""),
    status: str = Form(Status.TG.value),
    proverena: str = Form(""),
    has_video: bool = Form(False),
    video_url: str = Form(""),
    sources: str = Form(""),
    telegram_datetime: str = Form(""),
    formulirovka: str = Form(""),
    itogovaya_formulirovka: str = Form(""),
    turnir: str = Form(""),
    turnir_year: str = Form(""),
    task_number: str = Form(""),
    etap_kk: str = Form(""),
    comment_authors: list[str] = Form(default=[]),
    comment_texts: list[str] = Form(default=[]),
    after: str = Form(""),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    try:
        task = _build_task_from_form(
            db,
            None,
            idea_number,
            title,
            condition,
            author,
            naznachenie,
            status,
            proverena,
            has_video,
            video_url,
            sources,
            telegram_datetime,
            formulirovka,
            itogovaya_formulirovka,
            turnir,
            turnir_year,
            task_number,
            etap_kk,
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
            "naznachenie": naznachenie,
            "status": status,
            "proverena": proverena,
            "has_video": has_video,
            "video_url": video_url,
            "sources": sources,
            "telegram_datetime": telegram_datetime,
            "formulirovka": formulirovka,
            "itogovaya_formulirovka": itogovaya_formulirovka,
            "turnir": turnir,
            "turnir_year": turnir_year,
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
                status_labels=_available_statuses(naznachenie),
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
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    task = db.query(Task).options(
        joinedload(Task.comments).joinedload(Comment.attachments),
        joinedload(Task.attachments),
        joinedload(Task.history),
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
            "naznachenie_labels": NAZNACHENIE_LABELS,
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

    task = db.query(Task).options(joinedload(Task.attachments)).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    # from_status — старый параметр; pending_status — статус ещё не сохранён
    target = pending_status or from_status
    if target and target not in STATUS_LABELS:
        target = None
    if target and BOARD_STATUSES.get(task.naznachenie or ""):
        if target not in {s.value for s in BOARD_STATUSES[task.naznachenie]}:
            target = None

    hint = None
    if target == Status.FORMULIROVKA.value:
        hint = "Заполните «Формулировку перед отправлением» и нажмите «Сохранить». «Отмена» — статус не изменится."
    elif target == Status.IGRAETSYA.value:
        hint = "Заполните «Итоговую формулировку» и данные турнира, затем «Сохранить». «Отмена» — статус не изменится."

    cancel_url = f"/kanban/{task.naznachenie}" if task.naznachenie else f"/tasks/{task.id}"
    task_files = [a for a in task.attachments if a.comment_id is None]

    return templates.TemplateResponse(
        request,
        "form.html",
        _form_context(
            db,
            user=user,
            task=task,
            parsed=None,
            pending_status=target,
            status_labels=_available_statuses(task.naznachenie),
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
    naznachenie: str = Form(""),
    status: str = Form(Status.TG.value),
    proverena: str = Form(""),
    has_video: bool = Form(False),
    video_url: str = Form(""),
    sources: str = Form(""),
    telegram_datetime: str = Form(""),
    formulirovka: str = Form(""),
    itogovaya_formulirovka: str = Form(""),
    turnir: str = Form(""),
    turnir_year: str = Form(""),
    task_number: str = Form(""),
    etap_kk: str = Form(""),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    try:
        before = snapshot_task(task)
        _build_task_from_form(
            db,
            task_id,
            idea_number,
            title,
            condition,
            author,
            naznachenie,
            status,
            proverena,
            has_video,
            video_url,
            sources,
            telegram_datetime,
            formulirovka,
            itogovaya_formulirovka,
            turnir,
            turnir_year,
            task_number,
            etap_kk,
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
            "naznachenie": naznachenie,
            "status": status,
            "proverena": proverena,
            "has_video": has_video,
            "video_url": video_url,
            "sources": sources,
            "telegram_datetime": telegram_datetime,
            "formulirovka": formulirovka,
            "itogovaya_formulirovka": itogovaya_formulirovka,
            "turnir": turnir,
            "turnir_year": turnir_year,
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
                status_labels=_available_statuses(naznachenie),
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
            "naznachenie": naznachenie,
            "status": status,
            "proverena": proverena,
            "has_video": has_video,
            "video_url": video_url,
            "sources": sources,
            "telegram_datetime": telegram_datetime,
            "formulirovka": formulirovka,
            "itogovaya_formulirovka": itogovaya_formulirovka,
            "turnir": turnir,
            "turnir_year": turnir_year,
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
                status_labels=_available_statuses(naznachenie),
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

    att = db.get(Attachment, attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="Файл не найден")

    quoted = quote(att.filename)
    force_download = download in ("1", "true", "yes")
    as_image = is_image_attachment(att) and not force_download
    disposition = "attachment" if force_download or not as_image else "inline"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quoted}",
        "Cache-Control": "private, max-age=3600",
    }
    media = att.content_type or ("image/jpeg" if as_image else "application/octet-stream")
    return Response(
        content=bytes(att.data),
        media_type=media,
        headers=headers,
    )


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


def _existing_tasks_by_minute(db: Session) -> dict[str, tuple[int, str]]:
    tasks = db.query(Task).order_by(Task.telegram_datetime.asc(), Task.id.asc()).all()
    attach_idea_occurrences(db, tasks)
    out: dict[str, tuple[int, str]] = {}
    for task in tasks:
        if not task.telegram_datetime:
            continue
        key = _telegram_dt_minute(task.telegram_datetime).strftime("%Y-%m-%dT%H:%M")
        out[key] = (task.id, format_idea_label(task))
    return out


def _import_existing_link_options(db: Session) -> list[dict]:
    tasks = db.query(Task).order_by(Task.idea_number.asc().nullslast(), Task.id.asc()).all()
    attach_idea_occurrences(db, tasks)
    options = []
    for task in tasks:
        title = (task.title or "").strip()
        label = format_idea_label(task)
        if title:
            label = f"{label} — {title[:60]}"
        options.append({"value": f"task:{task.id}", "label": label})
    return options


def _import_page_ctx(db: Session, user: str, **extra):
    local_dirs = find_local_export_dirs()
    ctx = {
        "user": user,
        "rows": None,
        "error": None,
        "success": None,
        "naznachenie_labels": NAZNACHENIE_LABELS,
        "existing_links": _import_existing_link_options(db),
        "default_task_author": DEFAULT_TASK_AUTHOR,
        "default_comment_author": DEFAULT_COMMENT_AUTHOR,
        "default_naznachenie": DEFAULT_NAZNACHENIE,
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
    file_path = _safe_import_file(export_root, path)
    if not file_path:
        raise HTTPException(status_code=404, detail="Файл не найден")
    from fastapi.responses import FileResponse

    return FileResponse(file_path, filename=file_path.name)


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
) -> int:
    if not export_root or not rel_paths:
        return 0
    paths: list[Path] = []
    for rel in rel_paths:
        # пути в JSON вида chats/chat_001/photos/...
        candidate = (export_root / rel).resolve()
        try:
            candidate.relative_to(export_root.resolve())
        except ValueError:
            continue
        paths.append(candidate)
    saved = save_local_files(
        db,
        task_id=task_id,
        comment_id=comment_id,
        paths=paths,
        uploaded_by=user,
    )
    for att in saved:
        record_file_added(db, task_id, user, att.filename, for_comment=comment_id is not None)
    return len(saved)


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db)):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "import.html", _import_page_ctx(db, user))


@router.post("/import", response_class=HTMLResponse)
async def import_parse(
    request: Request,
    db: Session = Depends(get_db),
    paste: str = Form(""),
    local_export: str = Form(""),
    chat_name: str = Form(""),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    error = None
    rows = None
    chats_meta = None
    export_root: str | None = None
    chosen_chat = (chat_name or "").strip() or None
    try:
        form = await request.form()
        upload = form.get("export_file")
        payload: str | bytes | None = None
        filename = getattr(upload, "filename", None) if upload is not None else None
        if filename and callable(getattr(upload, "read", None)):
            payload = await upload.read()

        local_path = (local_export or "").strip()
        if not payload and local_path:
            export_dir = Path(local_path)
            if not export_dir.is_dir():
                raise ValueError(f"Папка экспорта не найдена: {local_path}")
            payload, _json_path = read_local_export_json(export_dir)
            export_root = str(export_dir.resolve())

        if not payload and paste.strip():
            payload = paste.strip()

        if not payload:
            # авто: последняя папка в data/imports
            dirs = find_local_export_dirs()
            if dirs:
                payload, _ = read_local_export_json(dirs[0])
                export_root = str(dirs[0].resolve())
            else:
                raise ValueError("Загрузите result.json или укажите папку экспорта в data/imports")

        rows, chats = parse_telegram_export(
            payload,
            existing_by_minute=_existing_tasks_by_minute(db),
            chat_name=chosen_chat,
        )
        chats_meta = [
            {"name": c.get("name"), "messages": len(c.get("messages") or [])} for c in chats
        ]
        if chosen_chat is None and chats_meta:
            # зафиксируем фактически выбранный чат
            from app.tg_import import pick_chat

            chosen_chat = pick_chat(chats).get("name")
        if not rows:
            raise ValueError("В выбранном чате не найдено сообщений для импорта")
    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        request,
        "import.html",
        _import_page_ctx(
            db,
            user,
            rows=[r.to_dict() for r in rows] if rows else None,
            error=error,
            export_root=export_root,
            chat_name=chosen_chat,
            chats=chats_meta,
        ),
        status_code=400 if error else 200,
    )


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
    export_root = Path(export_root_raw) if export_root_raw else None
    if export_root and not export_root.is_dir():
        export_root = None

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

    backup_sqlite_db()

    created_tasks = 0
    created_comments = 0
    attached_files = 0
    skipped = 0
    not_reviewed = 0
    draft_to_task: dict[str, int] = {}
    errors: list[str] = []

    for i in range(row_count):
        if not _is_reviewed(i):
            not_reviewed += 1
            continue
        kind = (form.get(f"kind_{i}") or "skip").strip()
        if kind == "skip":
            skipped += 1
            continue
        if kind != "idea":
            continue
        draft_key = (form.get(f"draft_key_{i}") or "").strip() or f"draft_{i}"
        author = normalize_author(
            (form.get(f"author_{i}") or "").strip(),
            default=DEFAULT_TASK_AUTHOR,
        )
        tg_raw = (form.get(f"telegram_datetime_{i}") or "").strip()
        title = (form.get(f"title_{i}") or "").strip() or None
        condition = (form.get(f"condition_{i}") or "").strip()
        sources = (form.get(f"sources_{i}") or "").strip() or None
        naznachenie = (form.get(f"naznachenie_{i}") or "").strip() or DEFAULT_NAZNACHENIE
        idea_number_raw = (form.get(f"idea_number_{i}") or "").strip()
        idea_number = int(idea_number_raw) if idea_number_raw.isdigit() else None
        media_paths = _media_paths_from_form(form, i)

        if not title:
            errors.append(f"Строка {i + 1}: у идеи нет названия — пропущена")
            continue
        if not condition:
            errors.append(f"Строка {i + 1}: у идеи нет условия — пропущена")
            continue
        tg_dt = parse_datetime_local(tg_raw)
        if not tg_dt:
            errors.append(f"Строка {i + 1}: некорректная дата Telegram — пропущена")
            continue
        tg_dt = _telegram_dt_minute(tg_dt)
        other = _task_with_telegram_datetime(db, tg_dt)
        if other:
            attach_idea_occurrences(db, [other])
            errors.append(
                f"Строка {i + 1}: дата {tg_raw} уже занята задачей {format_idea_label(other)} — пропущена"
            )
            continue

        video_url = None
        has_video = False
        if sources:
            for u in sources.splitlines():
                u = u.strip()
                if any(x in u.lower() for x in ("youtube", "youtu.be", "instagram")):
                    video_url = u
                    has_video = True
                    break
        if any(p.lower().endswith((".mp4", ".mov", ".webm", ".mkv")) for p in media_paths):
            has_video = True

        task = Task(
            idea_number=idea_number,
            title=title,
            condition=condition,
            author=author,
            naznachenie=naznachenie,
            status=Status.TG.value,
            has_video=has_video,
            archived=False,
            video_url=video_url,
            sources=sources,
            telegram_datetime=tg_dt,
        )
        db.add(task)
        db.flush()
        record_created(db, task, user)
        draft_to_task[draft_key] = task.id
        created_tasks += 1
        attached_files += _attach_import_media(
            db,
            export_root=export_root,
            rel_paths=media_paths,
            task_id=task.id,
            comment_id=None,
            user=user,
        )

    for i in range(row_count):
        if not _is_reviewed(i):
            continue
        kind = (form.get(f"kind_{i}") or "skip").strip()
        if kind == "skip" or kind not in ("comment", "media"):
            continue
        text = (form.get(f"text_{i}") or "").strip()
        author = normalize_author(
            (form.get(f"author_{i}") or "").strip(),
            default=DEFAULT_COMMENT_AUTHOR,
        )
        link_to = (form.get(f"link_to_{i}") or "").strip()
        media_paths = _media_paths_from_form(form, i)
        task_id = _resolve_link_to_task_id(link_to, draft_to_task, db)
        if not task_id:
            errors.append(f"Строка {i + 1}: нет привязки к идее — пропущена")
            continue

        if kind == "media":
            # только файлы к задаче
            n = _attach_import_media(
                db,
                export_root=export_root,
                rel_paths=media_paths,
                task_id=task_id,
                comment_id=None,
                user=user,
            )
            if n == 0:
                errors.append(f"Строка {i + 1}: медиафайлы не найдены на диске")
            attached_files += n
            continue

        if not text and not media_paths:
            errors.append(f"Строка {i + 1}: пустой комментарий — пропущен")
            continue
        if not text:
            text = "(файл)"
        comment = Comment(task_id=task_id, text=text, author=author)
        db.add(comment)
        db.flush()
        record_comment_added(db, task_id, user, author, text)
        created_comments += 1
        attached_files += _attach_import_media(
            db,
            export_root=export_root,
            rel_paths=media_paths,
            task_id=task_id,
            comment_id=comment.id,
            user=user,
        )

    db.commit()

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

    return templates.TemplateResponse(
        request,
        "import.html",
        _import_page_ctx(db, user, success=success),
    )


@router.get("/export/txt")
def export_txt(
    request: Request,
    db: Session = Depends(get_db),
    q: str = Query(None),
    naznachenie: str = Query(None),
    status: str = Query(None),
    author: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    author_filter = (author or "").strip() or None
    tasks = _filter_tasks(db, q, naznachenie, status, author_filter).options(
        joinedload(Task.comments).joinedload(Comment.attachments),
        joinedload(Task.attachments),
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
    naznachenie: str = Query(None),
    status: str = Query(None),
    author: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    author_filter = (author or "").strip() or None
    tasks = _filter_tasks(db, q, naznachenie, status, author_filter).options(
        joinedload(Task.comments).joinedload(Comment.attachments),
        joinedload(Task.attachments),
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
