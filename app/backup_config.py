"""Настройки автоэкспорта (день, получатели с расписанием) — data/backup_config.json."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from app.database import DATA_DIR

CONFIG_PATH = DATA_DIR / "backup_config.json"

# months: "all" = каждый месяц; список [4] = только апрель
DEFAULT_RECIPIENTS = [
    {
        "email": "chernikov.nikita@gmail.com",
        "day": 1,
        "months": "all",
    },
    {
        "email": "ilyamartch@gmail.com",
        "day": 1,
        "months": [4],
    },
]

_MONTH_NAMES_RU = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def _default_day() -> int:
    raw = (os.getenv("TELEGRAM_MONTHLY_DAY") or "1").strip()
    try:
        day = int(raw)
    except ValueError:
        return 1
    return max(1, min(28, day))


def _clamp_day(day) -> int:
    try:
        d = int(day)
    except (TypeError, ValueError):
        d = _default_day()
    return max(1, min(28, d))


def _normalize_months(raw) -> list[int] | str:
    if raw is None or raw == "all" or raw == "*" or raw == "":
        return "all"
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("all", "*", "каждый", "ежемесячно", "monthly"):
            return "all"
        parts = re.split(r"[\s,;]+", low)
        months: list[int] = []
        for p in parts:
            if not p:
                continue
            try:
                m = int(p)
            except ValueError:
                continue
            if 1 <= m <= 12:
                months.append(m)
        return sorted(set(months)) or "all"
    if isinstance(raw, (list, tuple)):
        months = []
        for x in raw:
            try:
                m = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= m <= 12:
                months.append(m)
        return sorted(set(months)) or "all"
    return "all"


def _normalize_recipient(item, *, default_day: int) -> dict | None:
    if isinstance(item, str):
        email = item.strip()
        if "@" not in email:
            return None
        return {"email": email, "day": default_day, "months": "all"}
    if not isinstance(item, dict):
        return None
    email = (item.get("email") or "").strip()
    if not email or "@" not in email:
        return None
    return {
        "email": email,
        "day": _clamp_day(item.get("day", default_day)),
        "months": _normalize_months(item.get("months", "all")),
    }


def _normalize_recipients(raw, *, default_day: int) -> list[dict]:
    items: list = []
    if isinstance(raw, str):
        items = _parse_recipients_text(raw, default_day=default_day)
        if items:
            return items
    elif isinstance(raw, list):
        items = raw
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        rec = _normalize_recipient(item, default_day=default_day)
        if not rec:
            continue
        key = rec["email"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out or [dict(r) for r in DEFAULT_RECIPIENTS]


def _parse_recipients_text(text: str, *, default_day: int) -> list[dict]:
    """
    Формат строк (по одной):
      email
      email | каждый месяц
      email | каждый месяц | день 1
      email | 1 апреля
      email | yearly | 4-1
    """
    out: list[dict] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # "email = …" или "email — …" или "email | …"
        parts = re.split(r"\s*[|=—–-]\s*", line, maxsplit=1)
        email = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if "@" not in email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)

        day = default_day
        months: list[int] | str = "all"
        low = rest.lower().replace("ё", "е")

        if not rest or "кажд" in low or "ежемес" in low or "monthly" in low or low == "all":
            months = "all"
            m_day = re.search(r"(?:день|day)\s*(\d{1,2})", low)
            if m_day:
                day = _clamp_day(m_day.group(1))
            else:
                m_only = re.search(r"\b(\d{1,2})\b", low)
                if m_only and "апрел" not in low and "april" not in low:
                    # «день 1» уже выше; одиночная цифра в «каждый месяц, 1»
                    val = int(m_only.group(1))
                    if 1 <= val <= 28:
                        day = val
        elif "yearly" in low or re.search(r"\d{1,2}\s*[-./]\s*\d{1,2}", low):
            m = re.search(r"(\d{1,2})\s*[-./]\s*(\d{1,2})", low)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                # 4-1 → апрель, день 1; 1-4 → день 1, апрель (если первое ≤12 и второе ≤28)
                if a <= 12 and b <= 28:
                    months, day = [a], _clamp_day(b)
                elif b <= 12 and a <= 28:
                    months, day = [b], _clamp_day(a)
            else:
                months, day = [4], 1
        else:
            # «1 апреля», «только апреля», «апрель»
            month_num = None
            for num, name in _MONTH_NAMES_RU.items():
                stem = name[:4]  # апре, янва, …
                if stem in low or name in low:
                    month_num = num
                    break
            if "april" in low:
                month_num = 4
            m_day = re.search(r"\b(\d{1,2})\b", low)
            day = _clamp_day(m_day.group(1)) if m_day else 1
            months = [month_num] if month_num else "all"

        out.append({"email": email, "day": day, "months": months})
    return out


def format_recipient_schedule(rec: dict) -> str:
    day = _clamp_day(rec.get("day", 1))
    months = rec.get("months", "all")
    if months == "all" or months == "*":
        return f"каждый месяц, день {day}"
    if isinstance(months, list) and len(months) == 1:
        m = months[0]
        name = _MONTH_NAMES_RU.get(m, str(m))
        return f"{day} {name}"
    if isinstance(months, list) and months:
        names = ", ".join(_MONTH_NAMES_RU.get(m, str(m)) for m in months)
        return f"день {day}: {names}"
    return f"день {day}"


def format_recipients_text(recipients: list[dict]) -> str:
    lines = []
    for rec in recipients:
        lines.append(f"{rec['email']} | {format_recipient_schedule(rec)}")
    return "\n".join(lines)


def recipient_due_on(rec: dict, when: datetime) -> bool:
    if when.day != _clamp_day(rec.get("day", 1)):
        return False
    months = rec.get("months", "all")
    if months == "all" or months == "*":
        return True
    if isinstance(months, list):
        return when.month in months
    return False


def recipient_period_key(rec: dict, when: datetime) -> str:
    """Ключ «уже отправляли» для получателя."""
    months = rec.get("months", "all")
    if months == "all" or months == "*":
        return when.strftime("%Y-%m")
    # раз в год (или в выбранные месяцы) — год+месяц
    return when.strftime("%Y-%m")


def load_backup_config() -> dict:
    data: dict = {}
    if CONFIG_PATH.is_file():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    day = _clamp_day(data.get("monthly_day", _default_day()))
    # совместимость со старым полем emails: [...]
    raw_recipients = data.get("recipients")
    if raw_recipients is None and data.get("emails") is not None:
        raw_recipients = data.get("emails")
    recipients = _normalize_recipients(raw_recipients, default_day=day)
    return {
        "monthly_day": day,
        "recipients": recipients,
        "emails": [r["email"] for r in recipients],
        "send_telegram": bool(data.get("send_telegram", True)),
        "send_email": bool(data.get("send_email", True)),
    }


def save_backup_config(
    *,
    monthly_day: int,
    emails: list[str] | str | None = None,
    recipients: list[dict] | str | None = None,
    send_telegram: bool = True,
    send_email: bool = True,
) -> dict:
    day = _clamp_day(monthly_day)
    raw = recipients if recipients is not None else emails
    recs = _normalize_recipients(raw, default_day=day)
    cfg = {
        "monthly_day": day,
        "recipients": recs,
        "send_telegram": bool(send_telegram),
        "send_email": bool(send_email),
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return load_backup_config()


def backup_monthly_day() -> int:
    return int(load_backup_config()["monthly_day"])


def backup_emails() -> list[str]:
    return list(load_backup_config()["emails"])


def emails_due_today(
    *,
    when: datetime | None = None,
    force: bool = False,
    state_email_last: dict | None = None,
) -> list[str]:
    """Адреса, которым сегодня положена выгрузка (с учётом уже отправленных)."""
    cfg = load_backup_config()
    now = when or datetime.now()
    last = state_email_last or {}
    out: list[str] = []
    for rec in cfg["recipients"]:
        if not force and not recipient_due_on(rec, now):
            continue
        period = recipient_period_key(rec, now)
        key = rec["email"].lower()
        if not force and last.get(key) == period:
            continue
        out.append(rec["email"])
    return out


def anything_due_today(*, when: datetime | None = None) -> bool:
    """Нужно ли сегодня запускать плановую выгрузку (TG или почта)."""
    cfg = load_backup_config()
    now = when or datetime.now()
    if cfg.get("send_telegram") and now.day == cfg["monthly_day"]:
        return True
    if cfg.get("send_email"):
        for rec in cfg["recipients"]:
            if recipient_due_on(rec, now):
                return True
    return False
