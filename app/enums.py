from enum import Enum


class Status(str, Enum):
    TG = "tg"
    FORMULIROVKA = "formulirovka"
    METODKOM = "metodkom"
    IGRAETSYA = "igraetsya"
    OTKLONENA = "otklonena"
    ARCHIVED = "archived"

    @property
    def label(self) -> str:
        return {
            "tg": "Сообщение в Telegram",
            "formulirovka": "Формулировка перед отправкой",
            "metodkom": "Отправлена в методкомиссию",
            "igraetsya": "Играется в турнире",
            "otklonena": "Отклонена",
            "archived": "Архив: больше не предлагаем",
        }[self.value]

    @property
    def short(self) -> str:
        return {
            "tg": "В Telegram",
            "formulirovka": "Формулировка",
            "metodkom": "Методкомиссия",
            "igraetsya": "Играется",
            "otklonena": "Отклонена",
            "archived": "Архив",
        }[self.value]


class Proverena(str, Enum):
    DA = "da"
    NET = "net"
    CHASTICHNO = "chastichno"

    @property
    def label(self) -> str:
        return {"da": "Да", "net": "Нет", "chastichno": "Частично"}[self.value]


class Turnir(str, Enum):
    TYUF = "tyuf"
    TYUE = "tyue"

    @property
    def label(self) -> str:
        return {"tyuf": "ТЮФ", "tyue": "ТЮЕ"}[self.value]


class EtapKK(str, Enum):
    POLUFINAL = "polufinal"
    FINAL = "final"

    @property
    def label(self) -> str:
        return {"polufinal": "Полуфинал", "final": "Финал"}[self.value]


STATUS_LABELS = {e.value: e.label for e in Status}
STATUS_SHORT_LABELS = {e.value: e.short for e in Status}
PROVERENA_LABELS = {e.value: e.label for e in Proverena}
TURNIR_LABELS = {e.value: e.label for e in Turnir}
ETAP_LABELS = {e.value: e.label for e in EtapKK}

AUTHORS = [
    "Nikita Karpenko-Chernikov",
    "Артем Голомолзин",
    "Артем Барат",
    "Александр Миркин",
    "Александр Зинкевич",
    "Ilya",
    "Сергей Булыкин",
]
DEFAULT_COMMENT_AUTHOR = "Ilya"
DEFAULT_TASK_AUTHOR = "Nikita Karpenko-Chernikov"

# Короткие имена и варианты из Telegram → каноническое имя в базе
AUTHOR_ALIASES = {
    "никита": "Nikita Karpenko-Chernikov",
    "nikita": "Nikita Karpenko-Chernikov",
    "nikita karpenko-chernikov": "Nikita Karpenko-Chernikov",
    "артём": "Артем Голомолзин",
    "артем": "Артем Голомолзин",
    "артем голомолзин": "Артем Голомолзин",
    "артём голомолзин": "Артем Голомолзин",
    "artem golomolzin": "Артем Голомолзин",
    "илья": "Ilya",
    "ilya": "Ilya",
    "сергей б": "Сергей Булыкин",
    "сергей булыкин": "Сергей Булыкин",
    "sergey bulykin": "Сергей Булыкин",
}


def normalize_author(name: str | None, *, default: str | None = None) -> str:
    raw = (name or "").strip()
    if not raw:
        return DEFAULT_TASK_AUTHOR if default is None else default
    # схлопываем пробелы; ё→е для словаря алиасов
    key = " ".join(raw.lower().replace("ё", "е").split())
    if key in AUTHOR_ALIASES:
        return AUTHOR_ALIASES[key]
    # Нечёткие совпадения только для полных имён (с фамилией).
    # Иначе «Артем Сухов» ошибочно схлопывался в «Артем Голомолзин»
    # из‑за алиаса «артем».
    for alias, canonical in AUTHOR_ALIASES.items():
        if " " not in alias:
            continue
        if key.startswith(alias) or alias.startswith(key):
            if len(key) >= 4:
                return canonical
    return raw
