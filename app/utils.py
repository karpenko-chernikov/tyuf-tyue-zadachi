import re
from datetime import datetime

IDEA_RE = re.compile(r"(?:Идея|идея)\s*(?:№|#|номер)?\s*(\d+)", re.IGNORECASE)
KAPITANY_RE = re.compile(r"(?:на\s+)?кк|конкурс\s+капитанов|задание\s+на\s+кк", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>\"']+")


def extract_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def parse_idea_number(text: str):
    match = IDEA_RE.search(text or "")
    return int(match.group(1)) if match else None


def is_kapitany(text: str) -> bool:
    return bool(KAPITANY_RE.search(text or ""))


def guess_title(text: str, idea_number) -> str | None:
    if not text:
        return None
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    for line in lines:
        if IDEA_RE.match(line):
            rest = IDEA_RE.sub("", line).strip(" .:-—")
            if rest:
                return title_from_condition(rest)
            continue
        if not line.startswith("http") and len(line) > 3:
            return title_from_condition(line)
    return None


_FILLER_RE = re.compile(
    r"^(?:"
    r"попробуйте\s+(?:придумать\s+)?"
    r"|придумайте\s+"
    r"|нужно\s+(?:придумать\s+)?"
    r"|необходимо\s+"
    r"|следует\s+"
    r"|задание[:\s]+"
    r")",
    re.IGNORECASE,
)

_ACTION_RE = re.compile(
    r"\b(распознать|определить|измерить|найти|построить|собрать|исследовать|"
    r"сравнить|оценить|проверить|создать|разработать|вычислить|обнаружить|"
    r"объяснить|придумать|смоделировать|воспроизвести)\b",
    re.IGNORECASE,
)


def _fit_title(text: str, max_len: int) -> str:
    text = re.sub(r"\s+", " ", text).strip(" «»\"'.,;")
    if len(text) <= max_len:
        return text
    cut = text[: max_len + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-—") + "…"


def _capitalize_title(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    return text[0].upper() + text[1:]


def title_from_condition(condition: str, max_len: int = 58) -> str | None:
    """Короткое смысловое название по тексту условия (без нейросети)."""
    if not condition or not str(condition).strip():
        return None

    lines: list[str] = []
    for raw in str(condition).strip().splitlines():
        line = raw.strip()
        if not line or line.startswith("http"):
            continue
        if IDEA_RE.match(line):
            rest = IDEA_RE.sub("", line).strip(" .:-—")
            if rest:
                lines.append(rest)
            continue
        lines.append(line)
    if not lines:
        return None

    blob = re.sub(r"\s+", " ", " ".join(lines)).strip(" «»\"'")
    blob = blob.rstrip(").]")

    # Суть часто после двоеточия / тире
    for sep in (":", "—", "–"):
        if sep not in blob:
            continue
        left, right = blob.split(sep, 1)
        right = right.strip(" ).]")
        right = re.sub(r"\bли\s+ли\b", "ли", right, flags=re.I)
        right = right.split(" или ")[0].strip()
        if len(right) < 8:
            continue
        action_matches = list(_ACTION_RE.finditer(left))
        preferred = [m for m in action_matches if m.group(1).lower() not in {"придумать", "создать"}]
        action_m = preferred[-1] if preferred else (action_matches[-1] if action_matches else None)
        right_title = _capitalize_title(right)
        # «случайно ли расположены точки» → «случайно ли точки»
        right_short = re.sub(
            r"^(случайно|произвольно)\s+ли\s+расположен[а-я]*\s+(?:ли\s+)?",
            r"\1 ли ",
            right,
            flags=re.I,
        ).strip()
        right_short = re.sub(r"\s+ли\s+ли\s+", " ли ", right_short)
        if "точк" in right_short.lower() and "картин" not in right_short.lower() and "картин" in left.lower():
            right_short = right_short.rstrip(".") + " на картинке"
        if len(right_short) >= 8:
            right_title = _capitalize_title(right_short)
        if action_m:
            action = action_m.group(1).lower()
            rest = right_title[0].lower() + right_title[1:] if right_title else ""
            candidate = f"{_capitalize_title(action)}, {rest}"
        else:
            candidate = right_title
        return _fit_title(candidate, max_len)

    # Без двоеточия: срезаем вводные и берём действие + хвост
    cleaned = _FILLER_RE.sub("", blob).strip()
    cleaned = re.sub(r"^(?:способ|метод)\s+", "", cleaned, flags=re.I).strip()
    action_m = _ACTION_RE.search(cleaned)
    if action_m:
        rest = cleaned[action_m.start() :].strip()
        # обрезаем длинные «с помощью… / по картинке с большим…»
        rest = re.split(r",\s*(?:если|когда|чтобы)\s+", rest, maxsplit=1)[0]
        return _fit_title(_capitalize_title(rest), max_len)

    sentence = re.split(r"(?<=[.!?…])\s+", cleaned, maxsplit=1)[0].strip()
    return _fit_title(_capitalize_title(sentence or cleaned), max_len)


def strip_idea_header(text: str) -> str:
    if not text:
        return ""
    lines = text.strip().splitlines()
    if lines and IDEA_RE.match(lines[0].strip()):
        lines = lines[1:]
    return "\n".join(lines).strip()


def parse_paste(text: str) -> dict:
    """Разбор вставленного текста из Telegram."""
    idea_number = parse_idea_number(text)
    kapitany = is_kapitany(text)
    urls = extract_urls(text)
    title = guess_title(text, idea_number)
    condition = strip_idea_header(text)
    for url in urls:
        condition = condition.replace(url, "").strip()
    condition = re.sub(r"\n{3,}", "\n\n", condition).strip()

    naznachenie = "kapitany" if kapitany else None

    return {
        "idea_number": idea_number,
        "title": title,
        "condition": condition or None,
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
