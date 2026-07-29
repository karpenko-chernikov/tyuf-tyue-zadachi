from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import deferred, relationship

from app.database import Base

task_tags = Table(
    "task_tags",
    Base.metadata,
    Column("task_id", Integer, ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    """Метка доски/турнира: ТЮФ, ТЮЕ, Капитанка, SF4, …"""

    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(80), unique=True, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    has_metodkom = Column(Boolean, default=True, nullable=False)
    sort_order = Column(Integer, default=100, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    tasks = relationship("Task", secondary=task_tags, back_populates="tags")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idea_number = Column(Integer, nullable=True, index=True)
    title = Column(String(500), nullable=True)
    condition = Column(Text, nullable=True)
    formulirovka = Column(Text, nullable=True)
    formulirovka_title = Column(String(500), nullable=True)
    itogovaya_formulirovka = Column(Text, nullable=True)
    igraetsya_title = Column(String(500), nullable=True)
    author = Column(String(100), nullable=True)
    status = Column(String(50), default="tg", index=True)
    proverena = Column(String(20), nullable=True)
    archived = Column(Boolean, default=False, nullable=False)
    video_url = Column(String(1000), nullable=True)
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

    tags = relationship("Tag", secondary=task_tags, back_populates="tasks", lazy="selectin")
    comments = relationship(
        "Comment", back_populates="task", cascade="all, delete-orphan", order_by="Comment.created_at"
    )
    history = relationship(
        "TaskHistory",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskHistory.created_at.desc()",
    )
    attachments = relationship(
        "Attachment",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Attachment.created_at",
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
    attachments = relationship(
        "Attachment",
        back_populates="comment",
        cascade="all, delete-orphan",
        order_by="Attachment.created_at",
    )


class Attachment(Base):
    """Файл к условию задачи (comment_id пустой) или к комментарию."""

    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    comment_id = Column(
        Integer, ForeignKey("comments.id", ondelete="CASCADE"), index=True, nullable=True
    )
    filename = Column(String(500), nullable=False)
    content_type = Column(String(200), nullable=True)
    size = Column(Integer, nullable=False)
    # Не тянуть BLOB при открытии карточки / экспорте — только при /files/{id}
    data = deferred(Column(LargeBinary, nullable=False))
    uploaded_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="attachments")
    comment = relationship("Comment", back_populates="attachments")


class ImportProcessedMessage(Base):
    """Сообщения Telegram, уже разобранные при импорте — больше не показываем."""

    __tablename__ = "import_processed_messages"

    msg_id = Column(Integer, primary_key=True)
    kind = Column(String(20), nullable=True)
    processed_at = Column(DateTime, default=datetime.utcnow)


class TgInboxProcessed(Base):
    """Сообщения, уже обработанные входящим ботом (личка)."""

    __tablename__ = "tg_inbox_processed"

    chat_id = Column(String(40), primary_key=True)
    message_id = Column(Integer, primary_key=True)
    kind = Column(String(20), nullable=True)
    processed_at = Column(DateTime, default=datetime.utcnow)


class TgPending(Base):
    """Превью идеи/комментария в личке бота, ждёт подтверждения."""

    __tablename__ = "tg_pending"

    user_tg_id = Column(String(40), primary_key=True)
    kind = Column(String(20), nullable=False)  # idea | comment
    payload_json = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class TaskHistory(Base):
    """Кто и что менял в задаче: до / после."""

    __tablename__ = "task_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    user = Column(String(100), nullable=False)
    action = Column(String(50), nullable=False)
    summary = Column(Text, nullable=True)
    changes_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    task = relationship("Task", back_populates="history")
