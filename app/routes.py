from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.auth import change_password, login_required
from app.database import get_db
from app.enums import (
    AUTHORS,
    BOARD_STATUSES,
    ETAP_LABELS,
    METODKOM_ONLY_FOR,
    NAZNACHENIE_LABELS,
    Naznachenie,
    PROVERENA_LABELS,
    STATUS_LABELS,
    STATUS_SHORT_LABELS,
    Status,
    TURNIR_LABELS,
)
from app.export import export_tasks_csv, export_tasks_txt
from app.models import Comment, Task
from app.utils import format_igraetsya, format_idea_label, parse_datetime_local, parse_paste

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _author_suggestions(db: Session):
    names = set(AUTHORS)
    for row in db.query(Task.author).filter(Task.author.isnot(None)).distinct():
        if row[0] and row[0].strip():
            names.add(row[0].strip())
    for row in db.query(Comment.author).filter(Comment.author.isnot(None)).distinct():
        if row[0] and row[0].strip():
            names.add(row[0].strip())
    return sorted(names, key=lambda x: x.lower())


def _filter_tasks(db: Session, q, naznachenie, status):
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
    return query.order_by(Task.idea_number.asc().nullslast(), Task.id.desc())


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


def _form_context(db: Session, **extra):
    ctx = {
        "authors": _author_suggestions(db),
        "naznachenie_labels": NAZNACHENIE_LABELS,
        "proverena_labels": PROVERENA_LABELS,
        "turnir_labels": TURNIR_LABELS,
        "etap_labels": ETAP_LABELS,
        "default_telegram_datetime": _default_telegram_datetime(db),
        "form": None,
        "error": None,
        "status_hint": None,
    }
    ctx.update(extra)
    return ctx


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if login_required(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    from app.auth import verify_user

    display = verify_user(username, password)
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
        change_password(username, old_password, new_password)
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
        .order_by(Task.idea_number.asc().nullslast(), Task.id.desc())
        .all()
    )

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

    task.status = status
    if status != Status.IGRAETSYA.value:
        task.turnir = None
        task.turnir_year = None
        task.task_number = None
        task.etap_kk = None
    db.commit()

    needs_edit = status in (Status.FORMULIROVKA.value, Status.IGRAETSYA.value)
    return {
        "ok": True,
        "status": status,
        "needs_edit": needs_edit,
        "edit_url": f"/tasks/{task_id}/edit?from_status={status}" if needs_edit else None,
    }


@router.get("/", response_class=HTMLResponse)
def task_list(
    request: Request,
    db: Session = Depends(get_db),
    q: str = Query(None),
    naznachenie: str = Query(None),
    status: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tasks = _filter_tasks(db, q, naznachenie, status).all()
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "user": user,
            "tasks": tasks,
            "q": q or "",
            "naznachenie": naznachenie or "",
            "status_filter": status or "",
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

    if not condition.strip():
        raise ValueError("Заполните условие задачи")
    if not author.strip():
        raise ValueError("Укажите автора задачи")
    if not naznachenie.strip():
        raise ValueError("Выберите назначение")

    idea_num = int(idea_number) if idea_number.strip() else None

    if idea_num is not None:
        existing = db.query(Task).filter(Task.idea_number == idea_num)
        if task_id:
            existing = existing.filter(Task.id != task_id)
        if existing.first():
            raise ValueError(f"Идея № {idea_num} уже существует")

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
        # поля турнира только для статуса «играется»
        task.turnir = None
        task.turnir_year = None
        task.task_number = None
        task.etap_kk = None

    if task_id is None:
        db.add(task)
    return task


def _add_initial_comments(db: Session, task: Task, authors, texts, default_user: str):
    if not isinstance(authors, list):
        authors = [authors] if authors else []
    if not isinstance(texts, list):
        texts = [texts] if texts else []
    # выровнять длины
    n = max(len(authors), len(texts))
    for i in range(n):
        text = (texts[i] if i < len(texts) else "").strip()
        if not text:
            continue
        author = (authors[i] if i < len(authors) else "").strip() or default_user
        db.add(Comment(task_id=task.id, text=text, author=author))


@router.post("/tasks")
def create_task(
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
        _add_initial_comments(db, task, comment_authors, comment_texts, user)
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
                "author": comment_authors[i] if i < len(comment_authors) else user,
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

    task = db.query(Task).options(joinedload(Task.comments)).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "user": user,
            "task": task,
            "just_created": created == "1",
            "naznachenie_labels": NAZNACHENIE_LABELS,
            "status_labels": STATUS_LABELS,
            "proverena_labels": PROVERENA_LABELS,
            "format_igraetsya": format_igraetsya,
            "format_idea_label": format_idea_label,
            "authors": _author_suggestions(db),
        },
    )


@router.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
def edit_task_page(
    request: Request,
    task_id: int,
    db: Session = Depends(get_db),
    from_status: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    hint = None
    if from_status == Status.FORMULIROVKA.value:
        hint = "Заполните «Формулировку перед отправлением» и сохраните."
    elif from_status == Status.IGRAETSYA.value:
        hint = "Заполните «Итоговую формулировку» и данные турнира, затем сохраните."

    return templates.TemplateResponse(
        request,
        "form.html",
        _form_context(
            db,
            user=user,
            task=task,
            parsed=None,
            status_labels=_available_statuses(task.naznachenie),
            status_hint=hint,
        ),
    )


@router.post("/tasks/{task_id}")
def update_task(
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
        db.commit()
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)
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
            ),
            status_code=400,
        )


@router.post("/tasks/{task_id}/comments")
def add_comment(
    request: Request,
    task_id: int,
    db: Session = Depends(get_db),
    text: str = Form(...),
    author: str = Form(...),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if not text.strip():
        return RedirectResponse(f"/tasks/{task_id}#comments", status_code=303)

    comment = Comment(
        task_id=task_id,
        text=text.strip(),
        author=author.strip() or user,
    )
    db.add(comment)
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}#comments", status_code=303)


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
        db.delete(comment)
        db.commit()
    return RedirectResponse(f"/tasks/{task_id}#comments", status_code=303)


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


@router.get("/export/txt")
def export_txt(
    request: Request,
    db: Session = Depends(get_db),
    naznachenie: str = Query(None),
    status: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tasks = _filter_tasks(db, None, naznachenie, status).options(joinedload(Task.comments)).all()
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
    naznachenie: str = Query(None),
    status: str = Query(None),
):
    user = login_required(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tasks = _filter_tasks(db, None, naznachenie, status).options(joinedload(Task.comments)).all()
    content = export_tasks_csv(db, tasks)
    filename = f"zadachi_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        content="\ufeff" + content,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        media_type="text/csv; charset=utf-8",
    )
