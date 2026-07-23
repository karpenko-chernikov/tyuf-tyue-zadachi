import os
import shutil
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
BACKUP_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "zadachi.db"


def _resolve_database_url(raw: str | None) -> str:
    """Относительный sqlite:///./data/... всегда ведём к абсолютному пути проекта."""
    if not raw:
        return f"sqlite:///{DB_PATH}"
    url = raw.strip()
    if not url.startswith("sqlite:///"):
        return url
    rest = url[len("sqlite:///") :]
    if rest == ":memory:" or rest.startswith("file:"):
        return url
    path = Path(rest)
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return f"sqlite:///{path}"


DATABASE_URL = _resolve_database_url(os.getenv("DATABASE_URL"))
# Для бэкапов: фактический файл SQLite
if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.endswith(":memory:"):
    DB_PATH = Path(DATABASE_URL[len("sqlite:///") :])

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def backup_sqlite_db(keep: int = 20) -> Path | None:
    """Копия локальной SQLite перед работой — чтобы git/сбой не съели данные."""
    if not DATABASE_URL.startswith("sqlite"):
        return None
    if not DB_PATH.is_file() or DB_PATH.stat().st_size == 0:
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"zadachi-{stamp}.db"
    shutil.copy2(DB_PATH, dest)
    backups = sorted(BACKUP_DIR.glob("zadachi-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
