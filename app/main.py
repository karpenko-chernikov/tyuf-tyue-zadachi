import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import inspect, text

from app.database import Base, SessionLocal, backup_sqlite_db, engine
from app.auth import ensure_users
from app.models import Task
from app.routes import router

load_dotenv()

backup_sqlite_db()
Base.metadata.create_all(bind=engine)


def _ensure_columns():
    """Добавляем новые колонки в SQLite, если их ещё нет."""
    insp = inspect(engine)
    if "tasks" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("tasks")}
    with engine.begin() as conn:
        if "formulirovka" not in cols:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN formulirovka TEXT"))
        if "itogovaya_formulirovka" not in cols:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN itogovaya_formulirovka TEXT"))
        if "archived" not in cols:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN archived BOOLEAN DEFAULT 0 NOT NULL"))

    # Разрешаем одинаковые номера идей (раньше был UNIQUE)
    with engine.begin() as conn:
        indexes = conn.execute(text("PRAGMA index_list('tasks')")).fetchall()
        for row in indexes:
            # row: seq, name, unique, origin, partial
            name = row[1]
            is_unique = bool(row[2])
            if not is_unique:
                continue
            cols_info = conn.execute(text(f"PRAGMA index_info('{name}')")).fetchall()
            col_names = [c[2] for c in cols_info]
            if col_names == ["idea_number"]:
                conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_tasks_idea_number ON tasks (idea_number)")
        )


_ensure_columns()


def _migrate_tyuf_to_both():
    """Старое назначение «только ТЮФ» больше не используем → «ТЮФ и ТЮЕ»."""
    db = SessionLocal()
    try:
        updated = (
            db.query(Task)
            .filter(Task.naznachenie == "tyuf")
            .update({Task.naznachenie: "both"}, synchronize_session=False)
        )
        if updated:
            db.commit()
    finally:
        db.close()


_migrate_tyuf_to_both()


def _migrate_archived_status():
    """Флаг archived → статус «archived», чтобы задачи попали в колонку Архив."""
    db = SessionLocal()
    try:
        updated = (
            db.query(Task)
            .filter(Task.archived.is_(True), Task.status != "archived")
            .update({Task.status: "archived"}, synchronize_session=False)
        )
        synced = (
            db.query(Task)
            .filter(Task.status == "archived", Task.archived.is_(False))
            .update({Task.archived: True}, synchronize_session=False)
        )
        if updated or synced:
            db.commit()
    finally:
        db.close()


_migrate_archived_status()


def _migrate_author_names():
    """Короткие имена авторов → канонические (как в Telegram)."""
    from app.enums import AUTHOR_ALIASES
    from app.models import Comment, Task

    db = SessionLocal()
    try:
        mapping = {}
        for alias, canonical in AUTHOR_ALIASES.items():
            # только точные короткие ключи без пробелов или известные старые
            mapping[alias] = canonical
        # явные старые значения в БД
        explicit = {
            "Никита": "Nikita Karpenko-Chernikov",
            "Артём": "Артем Голомолзин",
            "Артем": "Артем Голомолзин",
            "Илья": "Ilya",
            "Сергей Б": "Сергей Булыкин",
        }
        changed = 0
        for old, new in explicit.items():
            changed += (
                db.query(Task)
                .filter(Task.author == old)
                .update({Task.author: new}, synchronize_session=False)
            )
            changed += (
                db.query(Comment)
                .filter(Comment.author == old)
                .update({Comment.author: new}, synchronize_session=False)
            )
        if changed:
            db.commit()
    finally:
        db.close()


_migrate_author_names()


def _seed_users():
    db = SessionLocal()
    try:
        ensure_users(db)
    finally:
        db.close()


_seed_users()

app = FastAPI(title="Задачи ТЮФ/ТЮЕ")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "change-me-to-random-string"),
    max_age=60 * 60 * 24 * 30,
)
app.include_router(router)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
