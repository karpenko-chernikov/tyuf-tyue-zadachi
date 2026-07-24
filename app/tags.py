"""Теги задач (вместо назначения) и миграция со старого naznachenie."""

from __future__ import annotations

import re

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.enums import Status
from app.models import Tag, Task

DEFAULT_TAG_SLUGS = ("tyuf", "tyue")

SEED_TAGS: list[tuple[str, str, bool, int]] = [
    ("tyuf", "ТЮФ", True, 10),
    ("tyue", "ТЮЕ", True, 20),
    ("kapitanka", "Капитанка", False, 30),
    ("sf4", "SF4", True, 40),
    ("sf3", "SF3", True, 50),
    ("invent_yourself", "Invent yourself", True, 60),
]

_STATUSES_WITH_METODKOM = [
    Status.TG,
    Status.FORMULIROVKA,
    Status.METODKOM,
    Status.IGRAETSYA,
    Status.OTKLONENA,
    Status.ARCHIVED,
]

_STATUSES_WITHOUT_METODKOM = [
    Status.TG,
    Status.FORMULIROVKA,
    Status.IGRAETSYA,
    Status.OTKLONENA,
    Status.ARCHIVED,
]

_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def slugify_tag_name(name: str) -> str:
    raw = (name or "").strip().lower().replace("ё", "е")
    out = []
    for ch in raw:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
        elif ch in (" ", "-", "_", "."):
            out.append("_")
    slug = re.sub(r"_+", "_", "".join(out)).strip("_")
    return slug[:60] or "tag"


def board_statuses_for_tag(tag: Tag) -> list[Status]:
    if tag.has_metodkom:
        return list(_STATUSES_WITH_METODKOM)
    return list(_STATUSES_WITHOUT_METODKOM)


def task_allows_metodkom(task: Task) -> bool:
    return any(t.has_metodkom for t in (task.tags or []))


def task_is_kapitanka(task: Task) -> bool:
    return any(t.slug == "kapitanka" for t in (task.tags or []))


def task_tag_names(task: Task) -> str:
    tags = sorted(task.tags or [], key=lambda t: (t.sort_order, t.name))
    return ", ".join(t.name for t in tags) if tags else ""


def list_tags(db: Session) -> list[Tag]:
    return db.query(Tag).order_by(Tag.sort_order.asc(), Tag.name.asc()).all()


def tags_by_slugs(db: Session, slugs: list[str]) -> list[Tag]:
    clean = [s.strip() for s in slugs if s and s.strip()]
    if not clean:
        return []
    found = db.query(Tag).filter(Tag.slug.in_(clean)).all()
    by_slug = {t.slug: t for t in found}
    return [by_slug[s] for s in clean if s in by_slug]


def infer_extra_tag_slugs(*texts: str | None) -> set[str]:
    blob = " ".join(t for t in texts if t).lower().replace("ё", "е")
    found: set[str] = set()
    if re.search(r"\bsf4\b", blob, re.IGNORECASE) or "sf4" in blob:
        found.add("sf4")
    if re.search(r"\bsf3\b", blob, re.IGNORECASE) or "sf3" in blob:
        found.add("sf3")
    if "invent yourself" in blob or "придумай сам" in blob:
        found.add("invent_yourself")
    return found


def tag_slugs_from_naznachenie(naznachenie: str | None) -> set[str]:
    key = (naznachenie or "").strip().lower()
    if key in ("both", "tyuf"):
        return {"tyuf", "tyue"}
    if key == "tyue":
        return {"tyue"}
    if key == "kapitany":
        return {"kapitanka"}
    return set(DEFAULT_TAG_SLUGS)


def infer_tag_slugs_for_new_task(
    *,
    kapitany: bool = False,
    title: str | None = None,
    condition: str | None = None,
    extra_text: str | None = None,
) -> list[str]:
    slugs: set[str] = set()
    if kapitany:
        slugs.add("kapitanka")
    else:
        slugs.update(DEFAULT_TAG_SLUGS)
    slugs |= infer_extra_tag_slugs(title, condition, extra_text)
    order = [s for s, *_ in SEED_TAGS]
    return sorted(slugs, key=lambda s: order.index(s) if s in order else 999)


def ensure_tags(db: Session) -> None:
    created = False
    for slug, name, has_metodkom, sort_order in SEED_TAGS:
        exists = db.query(Tag).filter(Tag.slug == slug).first()
        if exists:
            continue
        db.add(
            Tag(
                slug=slug,
                name=name,
                has_metodkom=has_metodkom,
                sort_order=sort_order,
            )
        )
        created = True
    if created:
        db.commit()


def migrate_naznachenie_to_tags(db: Session, engine) -> None:
    """Однократно: naznachenie + эвристики → task_tags. Повторно не трогает задачи с тегами."""
    insp = inspect(engine)
    if "tasks" not in insp.get_table_names():
        return
    if "tags" not in insp.get_table_names() or "task_tags" not in insp.get_table_names():
        return

    task_cols = {c["name"] for c in insp.get_columns("tasks")}
    has_nazn = "naznachenie" in task_cols

    ensure_tags(db)
    by_slug = {t.slug: t for t in db.query(Tag).all()}

    linked_ids = {
        row[0]
        for row in db.execute(text("SELECT DISTINCT task_id FROM task_tags")).fetchall()
    }

    tasks = db.query(Task).all()
    changed = False
    for task in tasks:
        if task.id in linked_ids:
            continue
        slugs: set[str] = set()
        if has_nazn:
            nazn = db.execute(
                text("SELECT naznachenie FROM tasks WHERE id = :id"),
                {"id": task.id},
            ).scalar()
            slugs |= tag_slugs_from_naznachenie(nazn)
        else:
            slugs |= set(DEFAULT_TAG_SLUGS)

        comment_texts = [c.text for c in task.comments]
        slugs |= infer_extra_tag_slugs(task.title, task.condition, *comment_texts)

        if not slugs:
            slugs |= set(DEFAULT_TAG_SLUGS)

        for slug in slugs:
            tag = by_slug.get(slug)
            if tag is None:
                continue
            task.tags.append(tag)
            changed = True

    if changed:
        db.commit()

    _drop_legacy_naznachenie_columns(engine)


def _drop_legacy_naznachenie_columns(engine) -> None:
    """Убираем tasks.naznachenie и старую строковую tasks.tags — один источник правды: task_tags."""
    insp = inspect(engine)
    if "tasks" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("tasks")}
    with engine.begin() as conn:
        if "naznachenie" in cols:
            conn.execute(text("ALTER TABLE tasks DROP COLUMN naznachenie"))
        if "tags" in cols:
            # Старая VARCHAR-колонка; связь many-to-many идёт через task_tags
            conn.execute(text("ALTER TABLE tasks DROP COLUMN tags"))
