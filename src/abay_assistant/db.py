from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Field, SQLModel, create_engine, select, Session

from abay_assistant.config import get_settings


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(unique=True, index=True)
    role: str  # "owner" | "assistant" | "unknown"
    name: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True)
    role: str  # "user" | "assistant"
    text: str
    created_at: datetime = Field(default_factory=datetime.now)


class Pending(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True)
    text: str
    source: str = "telegram"  # "telegram" | "voice" | "trello"
    processed: bool = False
    created_at: datetime = Field(default_factory=datetime.now)


class Reminder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True)
    text: str
    remind_at: datetime  # naive datetime, Almaty local
    sent: bool = False
    created_at: datetime = Field(default_factory=datetime.now)


class DailyStat(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    date: str = Field(index=True)  # "2026-04-30"
    done_count: int = 0
    postponed_count: int = 0
    failed_count: int = 0
    energy: int = 0  # 1-5
    charged_by: str = ""
    drained_by: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class ToolUsage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True)
    tool_name: str = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.now)


class Idea(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True)
    text: str
    status: str = "open"  # "open" | "done" | "rejected"
    created_at: datetime = Field(default_factory=datetime.now)


class WeeklyCache(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    week_start: str  # ISO date string
    summary: str
    created_at: datetime = Field(default_factory=datetime.now)


_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, echo=False)
    return _engine


def init_db() -> None:
    SQLModel.metadata.create_all(_get_engine())


def get_session() -> Session:
    return Session(_get_engine())


def save_message(telegram_id: int, role: str, text: str) -> Message:
    """Сохранить сообщение в БД."""
    msg = Message(telegram_id=telegram_id, role=role, text=text)
    with get_session() as session:
        session.add(msg)
        session.commit()
        session.refresh(msg)
    return msg


def get_recent_messages(telegram_id: int, limit: int = 5) -> list[Message]:
    """Получить последние N сообщений пользователя."""
    with get_session() as session:
        stmt = (
            select(Message)
            .where(Message.telegram_id == telegram_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        messages = list(session.exec(stmt).all())
    messages.reverse()  # хронологический порядок
    return messages


# ─────────────────────────────────────────
# Reminders
# ─────────────────────────────────────────

def create_reminder(telegram_id: int, text: str, remind_at: datetime) -> Reminder:
    """Создать напоминание."""
    reminder = Reminder(telegram_id=telegram_id, text=text, remind_at=remind_at)
    with get_session() as session:
        session.add(reminder)
        session.commit()
        session.refresh(reminder)
    return reminder


def claim_due_reminders() -> list[Reminder]:
    """Атомарно получить и пометить как отправленные все наступившие напоминания.

    Предотвращает дублирование: SELECT + UPDATE в одной транзакции.
    """
    now = datetime.now()
    with get_session() as session:
        stmt = (
            select(Reminder)
            .where(Reminder.sent == False, Reminder.remind_at <= now)
        )
        reminders = list(session.exec(stmt).all())
        for r in reminders:
            r.sent = True
            session.add(r)
        session.commit()
        # Обновить объекты после коммита
        for r in reminders:
            session.refresh(r)
    return reminders


def get_active_reminders(telegram_id: int) -> list[Reminder]:
    """Получить все активные (не отправленные) напоминания пользователя."""
    with get_session() as session:
        stmt = (
            select(Reminder)
            .where(Reminder.telegram_id == telegram_id, Reminder.sent == False)
            .order_by(Reminder.remind_at)
        )
        return list(session.exec(stmt).all())


def delete_reminder(reminder_id: int, telegram_id: int) -> bool:
    """Удалить напоминание. Возвращает True если удалено."""
    with get_session() as session:
        stmt = select(Reminder).where(
            Reminder.id == reminder_id, Reminder.telegram_id == telegram_id
        )
        reminder = session.exec(stmt).first()
        if reminder:
            session.delete(reminder)
            session.commit()
            return True
        return False


# ─────────────────────────────────────────
# Daily Stats (вечерний свод)
# ─────────────────────────────────────────

def save_daily_stat(
    date: str,
    done_count: int,
    postponed_count: int,
    failed_count: int,
    energy: int,
    charged_by: str = "",
    drained_by: str = "",
) -> DailyStat:
    """Сохранить статистику вечернего свода. Перезаписывает если дата уже есть."""
    with get_session() as session:
        stmt = select(DailyStat).where(DailyStat.date == date)
        existing = session.exec(stmt).first()
        if existing:
            existing.done_count = done_count
            existing.postponed_count = postponed_count
            existing.failed_count = failed_count
            existing.energy = energy
            existing.charged_by = charged_by
            existing.drained_by = drained_by
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        stat = DailyStat(
            date=date,
            done_count=done_count,
            postponed_count=postponed_count,
            failed_count=failed_count,
            energy=energy,
            charged_by=charged_by,
            drained_by=drained_by,
        )
        session.add(stat)
        session.commit()
        session.refresh(stat)
        return stat


def log_tool_usage(telegram_id: int, tool_name: str) -> None:
    """Записать вызов tool."""
    with get_session() as session:
        session.add(ToolUsage(telegram_id=telegram_id, tool_name=tool_name))
        session.commit()


def get_tool_stats(days: int = 7) -> list[dict]:
    """Статистика tool calls за N дней. Возвращает [{tool_name, count}] отсортированные."""
    from sqlalchemy import func
    cutoff = datetime.now() - timedelta(days=days)
    with get_session() as session:
        stmt = (
            select(ToolUsage.tool_name, func.count().label("cnt"))
            .where(ToolUsage.created_at >= cutoff)
            .group_by(ToolUsage.tool_name)
            .order_by(func.count().desc())
        )
        rows = session.exec(stmt).all()
        return [{"tool_name": r[0], "count": r[1]} for r in rows]


def get_message_stats(days: int = 7) -> dict:
    """Статистика сообщений за N дней."""
    from sqlalchemy import func
    cutoff = datetime.now() - timedelta(days=days)
    with get_session() as session:
        stmt = (
            select(func.count())
            .where(Message.created_at >= cutoff, Message.role == "user")
        )
        user_count = session.exec(stmt).one()
        stmt2 = (
            select(func.count())
            .where(Message.created_at >= cutoff, Message.role == "assistant")
        )
        bot_count = session.exec(stmt2).one()
    return {"user_messages": user_count, "bot_messages": bot_count}


# ─────────────────────────────────────────
# Ideas
# ─────────────────────────────────────────

def save_idea(telegram_id: int, text: str) -> Idea:
    """Сохранить идею по улучшению бота."""
    idea = Idea(telegram_id=telegram_id, text=text)
    with get_session() as session:
        session.add(idea)
        session.commit()
        session.refresh(idea)
    return idea


def get_ideas(status: str = "open") -> list[Idea]:
    """Получить идеи по статусу."""
    with get_session() as session:
        stmt = (
            select(Idea)
            .where(Idea.status == status)
            .order_by(Idea.created_at.desc())
        )
        return list(session.exec(stmt).all())


def update_idea_status(idea_id: int, status: str) -> bool:
    """Обновить статус идеи. Возвращает True если обновлено."""
    with get_session() as session:
        stmt = select(Idea).where(Idea.id == idea_id)
        idea = session.exec(stmt).first()
        if idea:
            idea.status = status
            session.add(idea)
            session.commit()
            return True
        return False


def get_daily_stats(start_date: str, end_date: str) -> list[DailyStat]:
    """Получить статистику за период (включительно)."""
    with get_session() as session:
        stmt = (
            select(DailyStat)
            .where(DailyStat.date >= start_date, DailyStat.date <= end_date)
            .order_by(DailyStat.date)
        )
        return list(session.exec(stmt).all())
