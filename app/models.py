from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idea_number = Column(Integer, unique=True, nullable=True, index=True)
    title = Column(String(500), nullable=True)
    condition = Column(Text, nullable=True)
    formulirovka = Column(Text, nullable=True)
    itogovaya_formulirovka = Column(Text, nullable=True)
    author = Column(String(100), nullable=True)
    naznachenie = Column(String(50), nullable=True)
    status = Column(String(50), default="tg", index=True)
    proverena = Column(String(20), nullable=True)
    has_video = Column(Boolean, default=False)
    video_url = Column(String(1000), nullable=True)
    tags = Column(String(500), nullable=True)
    sources = Column(Text, nullable=True)
    telegram_url = Column(String(1000), nullable=True)
    telegram_datetime = Column(DateTime, nullable=False)
    answer_options = Column(Text, nullable=True)

    turnir = Column(String(20), nullable=True)
    turnir_year = Column(Integer, nullable=True)
    task_number = Column(Integer, nullable=True)
    etap_kk = Column(String(20), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    comments = relationship(
        "Comment", back_populates="task", cascade="all, delete-orphan", order_by="Comment.created_at"
    )
    history = relationship(
        "TaskHistory",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskHistory.created_at.desc()",
    )


class User(Base):
    """Учётки для входа. Пароль хранится только как хеш."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    text = Column(Text, nullable=False)
    author = Column(String(100), nullable=False)
    telegram_url = Column(String(1000), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="comments")


class TaskHistory(Base):
    """Кто и что менял в задаче: до / после."""

    __tablename__ = "task_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    user = Column(String(100), nullable=False)
    action = Column(String(50), nullable=False)  # created, updated, comment_added, comment_deleted
    summary = Column(Text, nullable=True)
    changes_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    task = relationship("Task", back_populates="history")
