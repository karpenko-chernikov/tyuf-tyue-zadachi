import os
import re
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request, status
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Логин → имя переменной пароля в .env
PASS_ENV_KEYS = {
    "nikita": "PASS_NIKITA",
    "artem": "PASS_ARTEM",
    "ilya": "PASS_ILYA",
}


def _load_users() -> dict:
    load_dotenv(ENV_PATH, override=True)
    mapping = {
        os.getenv("USER_NIKITA", "nikita"): os.getenv("PASS_NIKITA", "change-me"),
        os.getenv("USER_ARTEM", "artem"): os.getenv("PASS_ARTEM", "change-me"),
        os.getenv("USER_ILYA", "ilya"): os.getenv("PASS_ILYA", "change-me"),
    }
    display = {
        os.getenv("USER_NIKITA", "nikita"): "Никита",
        os.getenv("USER_ARTEM", "artem"): "Артём",
        os.getenv("USER_ILYA", "ilya"): "Илья",
    }
    return {u: {"password": p, "display": display.get(u, u)} for u, p in mapping.items()}


USERS = _load_users()


def reload_users() -> None:
    global USERS
    USERS = _load_users()


def verify_user(username: str, password: str) -> Optional[str]:
    user = USERS.get(username)
    if user and user["password"] == password:
        return user["display"]
    return None


def get_current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def login_required(request: Request) -> Optional[str]:
    return request.session.get("user")


def _set_env_value(key: str, value: str) -> None:
    if ENV_PATH.exists():
        text = ENV_PATH.read_text(encoding="utf-8")
    else:
        text = ""

    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"

    ENV_PATH.write_text(text, encoding="utf-8")
    os.environ[key] = value


def change_password(username: str, old_password: str, new_password: str) -> None:
    user = USERS.get(username)
    if not user:
        raise ValueError("Пользователь не найден")
    if user["password"] != old_password:
        raise ValueError("Неверный текущий пароль")
    if len(new_password) < 4:
        raise ValueError("Новый пароль слишком короткий (минимум 4 символа)")

    env_key = PASS_ENV_KEYS.get(username)
    if not env_key:
        raise ValueError("Нельзя сменить пароль для этого пользователя")

    _set_env_value(env_key, new_password)
    reload_users()
