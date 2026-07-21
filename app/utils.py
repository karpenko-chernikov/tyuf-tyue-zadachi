import re
from datetime import datetime

IDEA_RE = re.compile(r"(?:Идея|идея)\s*(?:№|#|номер)?\s*(\d+)", re.IGNORECASE)
KAPITANY_RE = re.compile(r"(?:на\s+)?кк|конкурс\s+капитанов|задание\s+на\s+кк", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>\"']+")

# Короткая строка после «Идея N» — это название, а не условие
_TITLE_MAX_LEN = 80


def extract_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def parse_idea_number(text: str):
    match = IDEA_RE.search(text or "")
    return int(match.group(1)) if match else None


def is_kapitany(text: str) -> bool:
    return bool(KAPITANY_RE.search(text or ""))


def _looks_like_title(line: str) -> bool:
    text = (line or "").strip()
    if not text or text.startswith("http"):
        return False
    if len(text) > _TITLE_MAX_LEN:
        return False
    # Длинное предложение с точкой посредине — скорее условие
    if ". " in text:
        return False
    return True


def _split_paste_body(text: str) -> tuple[str | None, str | None]:
    """После заголовка «Идея N» отделяет название и условие."""
    if not text or not str(text).strip():
        return None, None

    lines = [ln.strip() for ln in str(text).strip().splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    if not lines:
        return None, None

    title: str | None = None
    body_lines: list[str]

    first = lines[0]
    idea_match = IDEA_RE.match(first)
    if idea_match:
        after = first[idea_match.end() :].strip(" \t.:-—–")
        rest = lines[1:]
        if after and _looks_like_title(after):
            title = after
            body_lines = rest
        elif after:
            # «Идея 12 — длинный текст условия…» в одной строке
            body_lines = [after, *rest]
        else:
            body_lines = rest
    else:
        body_lines = lines

    while body_lines and not body_lines[0]:
        body_lines.pop(0)

    if title is None and body_lines:
        candidate = body_lines[0]
        more = body_lines[1:]
        # Есть ещё текст после короткой строки → это название
        if _looks_like_title(candidate) and any(ln.strip() for ln in more):
            title = candidate
            body_lines = more
            while body_lines and not body_lines[0]:
                body_lines.pop(0)
        elif _looks_like_title(candidate) and not any(ln.strip() for ln in more):
            # Только короткая строка — считаем названием
            title = candidate
            body_lines = []

    condition = "\n".join(body_lines).strip() or None
    return title, condition


def parse_paste(text: str) -> dict:
    """Разбор вставленного текста из Telegram."""
    idea_number = parse_idea_number(text)
    kapitany = is_kapitany(text)
    urls = extract_urls(text)
    title, condition = _split_paste_body(text)

    if condition:
        for url in urls:
            condition = condition.replace(url, "").strip()
        condition = re.sub(r"\n{3,}", "\n\n", condition).strip() or None

    naznachenie = "kapitany" if kapitany else None

    return {
        "idea_number": idea_number,
        "title": title,
        "condition": condition,
        "naznachenie": naznachenie,
        "sources": "\n".join(urls) if urls else None,
        "has_video": any("youtube" in u or "instagram" in u or "youtu.be" in u for u in urls),
        "video_url": next(
            (u for u in urls if "youtube" in u or "instagram" in u or "youtu.be" in u), None
        ),
    }


def parse_datetime_local(value: str):
    if not value:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def format_igraetsya(task):
    from app.enums import TURNIR_LABELS, ETAP_LABELS

    if task.status != "igraetsya":
        return None
    if task.naznachenie == "kapitany":
        if task.turnir_year and task.etap_kk:
            return f"КК · ТЮЕ {task.turnir_year} · {ETAP_LABELS.get(task.etap_kk, task.etap_kk)}"
        return None
    if task.turnir and task.turnir_year and task.task_number:
        return (
            f"{TURNIR_LABELS.get(task.turnir, task.turnir)} {task.turnir_year} · "
            f"задача № {task.task_number}"
        )
    return None


def format_idea_label(task) -> str:
    if task.idea_number is not None:
        return f"№ {task.idea_number}"
    return "Нет номера идеи"


def format_idea_title(task) -> str:
    if task.idea_number is not None:
        return f"Идея № {task.idea_number}"
    return "Нет номера идеи"


def author_pill_class(name: str | None) -> str:
    """CSS-класс цветного овала для автора."""
    key = (name or "").strip().lower().replace("ё", "е")
    mapping = {
        "никита": "nikita",
        "артем": "artem",
        "илья": "ilya",
    }
    return mapping.get(key, "other")


def status_pill_class(status: str | None) -> str:
    known = {"tg", "formulirovka", "metodkom", "igraetsya", "otklonena"}
    return status if status in known else "other"
