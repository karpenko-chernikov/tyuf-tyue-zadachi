"""Авторизация: пароли только как хеши в таблице users."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from app.models import User

# pbkdf2_sha256$iterations$salt_hex$hash_hex
_HASH_PREFIX = "pbkdf2_sha256"
_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _ITERATIONS,
    )
    return f"{_HASH_PREFIX}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, iter_s, salt_hex, hash_hex = password_hash.split("$", 3)
        if algo != _HASH_PREFIX:
            return False
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(digest, expected)


DEFAULT_USERS = (
    ("nikita", "Никита", "PASS_NIKITA"),
    ("artem", "Артём", "PASS_ARTEM"),
    ("ilya", "Илья", "PASS_ILYA"),
)


def ensure_users(db: Session) -> None:
    """Создаёт трёх пользователей, если их ещё нет.

    Начальный пароль берётся из .env (PASS_*), иначе change-me.
    После первого запуска пароли живут только в БД (хеши).
    """
    created = False
    for username, display_name, env_key in DEFAULT_USERS:
        exists = db.query(User).filter(User.username == username).first()
        if exists:
            continue
        plain = os.getenv(env_key) or "change-me"
        db.add(
            User(
                username=username,
                display_name=display_name,
                password_hash=hash_password(plain),
            )
        )
        created = True
    if created:
        db.commit()


def verify_user(db: Session, username: str, password: str) -> Optional[str]:
    user = db.query(User).filter(User.username == username.strip()).first()
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user.display_name


def get_current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def login_required(request: Request) -> Optional[str]:
    return request.session.get("user")


def change_password(
    db: Session,
    username: str,
    old_password: str,
    new_password: str,
) -> None:
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise ValueError("Пользователь не найден")
    if not verify_password(old_password, user.password_hash):
        raise ValueError("Неверный текущий пароль")
    if len(new_password) < 4:
        raise ValueError("Новый пароль слишком короткий (минимум 4 символа)")

    user.password_hash = hash_password(new_password)
    db.commit()
