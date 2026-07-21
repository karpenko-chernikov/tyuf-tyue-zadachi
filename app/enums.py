from enum import Enum


class Naznachenie(str, Enum):
    TYUE = "tyue"
    BOTH = "both"
    KAPITANY = "kapitany"

    @property
    def label(self) -> str:
        return {
            "tyue": "ТЮЕ",
            "both": "ТЮФ и ТЮЕ",
            "kapitany": "Конкурс капитанов",
        }[self.value]


class Status(str, Enum):
    TG = "tg"
    FORMULIROVKA = "formulirovka"
    METODKOM = "metodkom"
    IGRAETSYA = "igraetsya"
    OTKLONENA = "otklonena"

    @property
    def label(self) -> str:
        return {
            "tg": "Сообщение в Telegram",
            "formulirovka": "Формулировка перед отправкой",
            "metodkom": "Отправлена в методкомиссию",
            "igraetsya": "Играется в турнире",
            "otklonena": "Отклонена",
        }[self.value]

    @property
    def short(self) -> str:
        return {
            "tg": "В Telegram",
            "formulirovka": "Формулировка",
            "metodkom": "Методкомиссия",
            "igraetsya": "Играется",
            "otklonena": "Отклонена",
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


NAZNACHENIE_LABELS = {e.value: e.label for e in Naznachenie}
STATUS_LABELS = {e.value: e.label for e in Status}
STATUS_SHORT_LABELS = {e.value: e.short for e in Status}
PROVERENA_LABELS = {e.value: e.label for e in Proverena}
TURNIR_LABELS = {e.value: e.label for e in Turnir}
ETAP_LABELS = {e.value: e.label for e in EtapKK}

# Методкомиссия только для задач «ТЮФ и ТЮЕ»
METODKOM_ONLY_FOR = {Naznachenie.BOTH.value}

# Колонки канбана по доске
BOARD_STATUSES = {
    "tyue": [Status.TG, Status.FORMULIROVKA, Status.IGRAETSYA, Status.OTKLONENA],
    "both": [
        Status.TG,
        Status.FORMULIROVKA,
        Status.METODKOM,
        Status.IGRAETSYA,
        Status.OTKLONENA,
    ],
    "kapitany": [Status.TG, Status.FORMULIROVKA, Status.IGRAETSYA, Status.OTKLONENA],
}

AUTHORS = ["Никита", "Артём", "Илья"]
